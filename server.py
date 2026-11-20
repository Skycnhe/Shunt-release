#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shunt 面板引导程序（Go 版）。

Go 版面板是单个程序 /opt/mihomo-panel/shunt，不再需要 python3。这个文件只在两种情况下运行：
  1. 旧的 Python 版面板「在线更新」到 Go 版：旧版只会下载 update-files.txt 里的文件（含本文件与 bin/SHA256SUMS），
     重启后 init.d/shunt 发现还没有 shunt 程序，就用 python3 运行本文件；
  2. shunt 程序被误删。
本文件按 bin/SHA256SUMS 下载本机架构的程序（先 gz 再解压，两次 sha256 校验，可走加速镜像），装到 /opt/mihomo-panel/shunt，
然后原地 exec 成它（进程号不变，OpenRC 的 pidfile 照常有效）。下载失败时退出码非 0：
旧版在线更新留下的回滚守护 90 秒内等不到面板，会自动恢复旧版本。
命令行参数（--gen、--tpenv 等）原样交给 shunt。
"""
import gzip, hashlib, json, os, platform, sys, time, urllib.request

PANEL_VERSION = "11.7.8-preview.791+104233c"
BASE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(BASE, "shunt")
SUMS = os.path.join(BASE, "bin", "SHA256SUMS")
DATA = os.environ.get("PANEL_DATA", "/etc/mihomo-panel/data.json")
REPO = "Skycnhe/Shunt-release"
MIRRORS = ("https://ghfast.top/", "https://gh-proxy.com/")


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "[引导]", *a, flush=True)


def arch():
    m = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "armv7l": "armv7", "armv8l": "armv7",
            "i686": "386", "i386": "386"}.get(m, m)


def sums():
    out = {"files": {}}
    with open(SUMS) as f:
        for ln in f:
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
    h = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    req = urllib.request.Request(url, headers={"User-Agent": "shunt-bootstrap"})
    with urllib.request.build_opener(h).open(req, timeout=120) as r:
        data = r.read(96 << 20)
        left = getattr(r, "length", None)
        if left:
            raise IOError("下载不完整")
        return data


def install():
    s = sums()
    name = "shunt-linux-" + arch()
    want_gz, want = s["files"].get(name + ".gz"), s["files"].get(name)
    if not (s.get("commit") and want_gz and want):
        raise SystemExit(f"发布里没有 {arch()} 架构的面板程序")
    url = f"https://raw.githubusercontent.com/{REPO}/{s['commit']}/{name}.gz"
    gh = gh_proxy()
    tries = ([(gh + url, "")] if gh else []) + [(url, "http://127.0.0.1:7890"), (url, "")] + [(m + url, "") for m in MIRRORS]
    errs = []
    for u, proxy in tries:
        try:
            gz = fetch(u, proxy)
            if hashlib.sha256(gz).hexdigest() != want_gz:
                raise IOError("压缩包 sha256 不一致")
            raw = gzip.decompress(gz)
            if hashlib.sha256(raw).hexdigest() != want:
                raise IOError("程序 sha256 不一致")
            tmp = BIN + ".new"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.chmod(tmp, 0o755)
            os.replace(tmp, BIN)
            log(f"已安装 Go 版面板程序 {s.get('version', '')}（{name}，来自 {u.split('/')[2]}）")
            return
        except Exception as e:
            errs.append(f"{u.split('/')[2]}{'(代理)' if proxy else ''}: {str(e)[:100]}")
    raise SystemExit("下载 Go 版面板程序失败：" + "；".join(errs))


def main():
    if not os.access(BIN, os.X_OK):
        log("还没有 Go 版面板程序，开始下载")
        install()
    os.execv(BIN, [BIN] + sys.argv[1:])


if __name__ == "__main__":
    main()
