#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shunt 面板引导程序（Go 版）。

Go 版面板是单个程序 /opt/mihomo-panel/shunt，不再需要 python3。这个文件只在两种情况下运行：
  1. 旧的 Python 版面板「在线更新」到 Go 版：旧版只会下载 update-files.txt 里的文件（含本文件与 bin/SHA256SUMS），
     重启后 init.d/shunt 的 start_pre 发现还没有 shunt 程序，就用「python3 本文件 --version」装好程序（服务进程本身始终是 shunt：
     以 python3 启动再 exec 成 shunt 的话，OpenRC 按记下的 python3 停不掉它，rc-status 也显示 crashed）；
  2. shunt 程序被误删，或 tproxy.sh / init.d/shunt-core 在程序装好前调用（--tpenv、--gen 等一次性命令）。
本文件按 bin/SHA256SUMS 下载本机架构的程序（先 gz 再解压，两次 sha256 校验），装到 /opt/mihomo-panel/shunt，
然后原地 exec 成它、命令行参数原样传过去。

时间限制：旧版在线更新留下的回滚守护从重启后第 30 秒起探测面板，约 90 秒还打不开就恢复旧版本。
以前每条线路超时 120 秒、先直连 raw 再走代理：国内 raw.githubusercontent.com 常被 DNS 污染 / 丢包，直连一条就卡满 120 秒，
来不及试镜像就被回滚（核心没起来、7890 不通时必现）。现在：
  - 线路顺序「GitHub 加速（设置里填的）→ 本机代理 127.0.0.1:7890 → 内置加速镜像 → 直连」，按顺序每隔几秒启动下一条并行下载，
    前一条失败立刻启动下一条，谁先下载完且校验通过就用谁（镜像下载的内容按 sha256 校验，篡改无效）；
  - 单次连接 / 读取超时 10 秒（卡住即放弃），整个下载最多 60 秒（含失败后重试），之后退出码非 0，交给回滚守护。
bin/SHA256SUMS 缺失时（旧版拿不到 GitHub 校验值时只按内置文件列表更新），先从发布分支下载它（只走加速 / 代理 / 直连，不走内置镜像）。
日志写 stderr（stdout 留给 --tpenv 等命令的输出）。命令行参数（--gen、--tpenv 等）原样交给 shunt。
"""
import gzip, hashlib, json, os, platform, queue, sys, threading, time, urllib.request

PANEL_VERSION = "11.7.9-preview.826+3e7f0ad"
BASE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(BASE, "shunt")
SUMS = os.path.join(BASE, "bin", "SHA256SUMS")
DATA = os.environ.get("PANEL_DATA", "/etc/mihomo-panel/data.json")
REPO = "Skycnhe/Shunt-release"
BRANCH = "shunt-singbox-preview"
MIRRORS = ("https://ghfast.top/", "https://gh-proxy.com/")
PROXY = "http://127.0.0.1:7890"
T0 = time.time()
DEADLINE = T0 + float(os.environ.get("SHUNT_BOOT_DEADLINE", "60"))  # 整个下载的时间上限（秒）
STALL = float(os.environ.get("SHUNT_BOOT_STALL", "10"))  # 单次连接 / 读取超时
STAGGER = float(os.environ.get("SHUNT_BOOT_STAGGER", "3"))  # 前一条线路还没结果时，隔多久启动下一条
CAP = 96 << 20


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "[引导]", *a, file=sys.stderr, flush=True)


def arch():
    m = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "armv7l": "armv7", "armv8l": "armv7",
            "i686": "386", "i386": "386"}.get(m, m)


def parse_sums(text):
    out = {"files": {}}
    for ln in text.splitlines():
        p = ln.split()
        if len(p) < 2 or p[0].startswith("#"):
            continue
        if p[0] in ("version", "commit"):
            out[p[0]] = p[1]
        elif len(p[0]) == 64:
            out["files"][p[1].lstrip("*")] = p[0].lower()
    return out


def gh_proxy():
    try:
        with open(DATA) as f:
            g = (json.load(f).get("gh_proxy") or "").strip()
        return g.rstrip("/") + "/" if g else ""
    except Exception:
        return ""


def fetch(url, proxy):
    """下载一个地址：单次连接 / 读取超过 STALL 秒没有进展、或超过总时限即失败"""
    left = DEADLINE - time.time()
    if left <= 1:
        raise IOError("超时")
    h = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    req = urllib.request.Request(url, headers={"User-Agent": "shunt-bootstrap"})
    with urllib.request.build_opener(h).open(req, timeout=min(STALL, left)) as r:
        want = getattr(r, "length", None)
        buf = bytearray()
        while True:
            if time.time() > DEADLINE:
                raise IOError("超时（已下载 %d KB）" % (len(buf) >> 10))
            chunk = r.read(64 << 10)
            if not chunk:
                break
            buf += chunk
            if len(buf) > CAP:
                raise IOError("文件过大")
        if want is not None and len(buf) != want:
            raise IOError("下载不完整")
        return bytes(buf)


def routes(url, mirrors=True):
    """(名称, 地址, 代理)，按优先顺序：GitHub 加速 → 本机代理 → 内置镜像 → 直连"""
    gh = gh_proxy()
    out = [("GitHub 加速 " + gh.split("/")[2], gh + url, "")] if gh.startswith("http") else []
    out.append(("代理 127.0.0.1:7890", url, PROXY))
    if mirrors:
        out += [("镜像 " + m.split("/")[2], m + url, "") for m in MIRRORS]
    out.append(("直连 " + url.split("/")[2], url, ""))
    return out


def race(url, check, mirrors=True):
    """按顺序错开启动各线路并行下载，返回 (数据, 线路名)；check(数据) 不通过抛异常。总时限内失败会整轮重试"""
    errs, rnd = {}, 0
    while time.time() < DEADLINE - 2:
        rnd += 1
        rs = routes(url, mirrors)
        q = queue.Queue()

        def run(i):
            name, u, proxy = rs[i]
            try:
                data = fetch(u, proxy)
                check(data)
                q.put((i, data, None))
            except Exception as e:
                q.put((i, None, e))

        started, done = 0, 0
        nxt = time.time()
        while done < len(rs):
            now = time.time()
            if now >= DEADLINE:
                break
            if started < len(rs) and (now >= nxt or started == done):
                t = threading.Thread(target=run, args=(started,))
                t.daemon = True
                t.start()
                started += 1
                nxt = time.time() + STAGGER
            wait = (nxt - time.time()) if started < len(rs) else (DEADLINE - time.time())
            try:
                i, data, err = q.get(timeout=max(0.05, min(wait, DEADLINE - time.time())))
            except queue.Empty:
                continue
            done += 1
            if err is None:
                return data, rs[i][0]
            errs[rs[i][0]] = str(err)[:100]
        if done < len(rs):  # 到时限仍有线路没结果
            break
        time.sleep(min(3, max(0, DEADLINE - time.time() - 2)))
    raise IOError("；".join("%s: %s" % kv for kv in errs.items()) or "超时")


def load_sums():
    try:
        with open(SUMS) as f:
            return parse_sums(f.read())
    except OSError:
        pass
    if "@" in BRANCH or "@" in REPO:
        raise SystemExit("缺少 bin/SHA256SUMS，且引导程序不知道发布分支")
    log("缺少 bin/SHA256SUMS，从发布分支下载")
    url = "https://raw.githubusercontent.com/%s/%s/bin/SHA256SUMS" % (REPO, BRANCH)

    def ok(data):
        s = parse_sums(data.decode("utf-8", "replace"))
        if not (s.get("commit") and s["files"]):
            raise IOError("SHA256SUMS 内容不对")

    try:  # 没有 GitHub 校验值：与旧版在线更新一样不走内置镜像
        data, _ = race(url, ok, mirrors=False)
    except IOError as e:
        raise SystemExit("下载 bin/SHA256SUMS 失败：%s" % e)
    os.makedirs(os.path.dirname(SUMS), exist_ok=True)
    tmp = "%s.%d.tmp" % (SUMS, os.getpid())
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, SUMS)
    return parse_sums(data.decode("utf-8", "replace"))


def install():
    s = load_sums()
    name = "shunt-linux-" + arch()
    want_gz, want = s["files"].get(name + ".gz"), s["files"].get(name)
    if not (s.get("commit") and want_gz and want):
        raise SystemExit("发布里没有 %s 架构的面板程序" % arch())
    url = "https://raw.githubusercontent.com/%s/%s/%s.gz" % (REPO, s["commit"], name)
    box = {}

    def check(gz):
        if hashlib.sha256(gz).hexdigest() != want_gz:
            raise IOError("压缩包 sha256 不一致")
        raw = gzip.decompress(gz)
        if hashlib.sha256(raw).hexdigest() != want:
            raise IOError("程序 sha256 不一致")
        box["raw"] = raw

    try:
        _, via = race(url, check)
    except IOError as e:
        raise SystemExit("下载 Go 版面板程序失败（%.0f 秒）：%s" % (time.time() - T0, e))
    tmp = "%s.%d.new" % (BIN, os.getpid())  # 带进程号：init.d 与 tproxy.sh 同时运行引导程序时互不覆盖
    with open(tmp, "wb") as f:
        f.write(box["raw"])
    os.chmod(tmp, 0o755)
    os.replace(tmp, BIN)
    log("已安装 Go 版面板程序 %s（%s，经%s，用时 %.1f 秒）" % (s.get("version", ""), name, via, time.time() - T0))


def main():
    if not os.access(BIN, os.X_OK):
        log("还没有 Go 版面板程序，开始下载")
        install()
    os.execv(BIN, [BIN] + sys.argv[1:])


if __name__ == "__main__":
    main()
