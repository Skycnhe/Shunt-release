#!/usr/bin/env python3
"""旁路由测速 A/B：同一节点，比较 面板现网 / 面板配置去掉 TUN / 同内核最简配置 / 官方 mihomo 最简配置。
用法（设备上，root）：python3 /opt/mihomo-panel/tools/abspeed.py [节点名]
不改现网：另起的内核只开 127.0.0.1:7891-7893，结束后全部停掉、删除临时目录。节点名缺省取「🇯🇵 日本竞技」当前选中的节点。"""
import gzip, json, os, re, shutil, signal, statistics, subprocess, sys, tempfile, time, urllib.request

DATA = os.environ.get("PANEL_DATA", "/etc/mihomo-panel/data.json")
SB_CONF = os.path.join(os.environ.get("SB_DIR", "/etc/sing-box"), "config.json")
CTRL = "http://127.0.0.1:9090"
URLS = [("ovh 单连接", "https://proof.ovh.net/files/100Mb.dat", 1),
        ("ovh 4 并发", "https://proof.ovh.net/files/100Mb.dat", 4),
        ("google 单连接", "https://dl.google.com/go/go1.23.4.linux-arm64.tar.gz", 1)]
RUNS, SECS = 3, 15
MIRRORS = ["", "https://ghfast.top/", "https://gh-proxy.com/"]


def api(path, secret, method="GET", body=None):
    r = urllib.request.Request(CTRL + path, method=method, data=json.dumps(body).encode() if body else None,
                               headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=5) as f:
        t = f.read()
        return json.loads(t) if t else {}


def curl_speed(port, url, n):
    ps = [subprocess.Popen(["curl", "-s", "-o", "/dev/null", "-x", f"http://127.0.0.1:{port}", "--max-time", str(SECS),
                            "-w", "%{size_download} %{time_total}", url], stdout=subprocess.PIPE, text=True) for _ in range(n)]
    tot = 0.0
    for p in ps:
        try:
            size, t = p.communicate(timeout=SECS + 10)[0].split()
            tot += float(size) / max(float(t), 0.01)
        except Exception:
            pass
    return tot * 8 / 1e6  # Mbps


def bench(port):
    out = []
    for _, url, n in URLS:
        out.append(statistics.median(curl_speed(port, url, n) for _ in range(RUNS)))
    return out


def wait_port(port, t=20):
    end = time.time() + t
    while time.time() < end:
        if subprocess.run(["curl", "-s", "-o", "/dev/null", "-x", f"http://127.0.0.1:{port}", "--max-time", "5",
                           "http://www.gstatic.com/generate_204"]).returncode == 0:
            return True
        time.sleep(1)
    return False


def sb_bin():
    for pid in os.listdir("/proc"):
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
            if os.path.basename(exe).startswith("sing-box"):
                return exe
        except OSError:
            pass
    return shutil.which("sing-box")


def start(cmd, tmp, name):
    log = open(os.path.join(tmp, name + ".log"), "w")
    return subprocess.Popen(cmd, stdout=log, stderr=log, cwd=tmp, start_new_session=True)


def mihomo_official(tmp, gh):
    rel = None
    for m in MIRRORS:
        try:
            with urllib.request.urlopen("https://api.github.com/repos/MetaCubeX/mihomo/releases/latest", timeout=10) as f:
                rel = json.load(f)
            break
        except Exception:
            continue
    if not rel:
        return None, "?"
    asset = next(a for a in rel["assets"] if re.fullmatch(r"mihomo-linux-arm64-v[\d.]+\.gz", a["name"]))
    for m in ([gh] if gh else []) + MIRRORS:
        try:
            with urllib.request.urlopen(m + asset["browser_download_url"], timeout=60) as f:
                b = gzip.decompress(f.read())
            p = os.path.join(tmp, "mihomo")
            open(p, "wb").write(b)
            os.chmod(p, 0o755)
            return p, rel["tag_name"]
        except Exception:
            continue
    return None, rel["tag_name"]


def main():
    d = json.load(open(DATA))
    sec = d.get("secret", "")
    cfg = json.load(open(SB_CONF))
    node = sys.argv[1] if len(sys.argv) > 1 else api("/proxies/" + urllib.request.quote("🇯🇵 日本竞技"), sec).get("now")
    ob = next((o for o in cfg["outbounds"] if o.get("tag") == node), None)
    if not ob:
        sys.exit(f"配置里找不到节点 {node!r}")
    tun = next((i for i in cfg.get("inbounds", []) if i.get("type") == "tun"), {})
    print(f"节点：{node}（{ob.get('type')}）  内核：{sb_bin()}")
    print(f"现网 TUN：mtu={tun.get('mtu', '未写(默认)')} stack={tun.get('stack', '默认')} auto_redirect={tun.get('auto_redirect')}"
          f"  日志={cfg.get('log', {}).get('level')}  规则集={len(cfg.get('route', {}).get('rule_set', []))}"
          f"  GOMAXPROCS={os.environ.get('GOMAXPROCS', '-')} GOGC={os.environ.get('GOGC', '-')}")
    tmp = tempfile.mkdtemp(prefix="abspeed-")
    procs, res = [], {}
    try:
        # A 现网 7890（🚀 → 竞技组；测试前后记录竞技组选中的节点）
        res["A 面板现网 :7890"] = bench(7890)
        now2 = api("/proxies/" + urllib.request.quote("🇯🇵 日本竞技"), sec).get("now")
        if now2 != node:
            print(f"注意：测试期间竞技组从 {node} 换到了 {now2}")
        # B 面板完整配置，只留 mixed 7893 入站，出站固定为该节点（保留 DNS / 嗅探 / 规则集 / 出站参数）
        c2 = json.loads(json.dumps(cfg))
        c2["inbounds"] = [{"type": "mixed", "tag": "ab-in", "listen": "127.0.0.1", "listen_port": 7893}]
        exp = c2.setdefault("experimental", {})
        exp.pop("cache_file", None)
        exp.get("clash_api", {}).update(external_controller="127.0.0.1:19093")
        exp.get("clash_api", {}).pop("external_ui", None)
        rules = c2.setdefault("route", {}).setdefault("rules", [])
        k = 0
        while k < len(rules) and rules[k].get("action") in ("sniff", "hijack-dns", "resolve"):
            k += 1
        rules.insert(k, {"inbound": ["ab-in"], "outbound": node})
        p2 = os.path.join(tmp, "b.json")
        json.dump(c2, open(p2, "w"), ensure_ascii=False)
        # C 同内核最简配置 7892
        o3 = {k: v for k, v in ob.items() if k not in ("detour", "domain_resolver")}
        c3 = {"log": {"level": "warn"}, "dns": {"servers": [{"type": "udp", "tag": "d", "server": "223.5.5.5"}]},
              "inbounds": [{"type": "mixed", "tag": "in", "listen": "127.0.0.1", "listen_port": 7892}],
              "outbounds": [o3], "route": {"final": node, "default_domain_resolver": "d"}}
        p3 = os.path.join(tmp, "c.json")
        json.dump(c3, open(p3, "w"), ensure_ascii=False)
        sb = sb_bin()
        for name, path, port in (("B 面板配置去掉 TUN :7893", p2, 7893), ("C 同内核最简 :7892", p3, 7892)):
            p = start([sb, "run", "-c", path, "-D", tmp], tmp, path[-6])
            procs.append(p)
            if wait_port(port):
                res[name] = bench(port)
            else:
                print(f"{name} 启动失败，见 {tmp}/{path[-6]}.log")
            p.terminate()
        # D 官方 mihomo 最新正式版 + 订阅（经现网代理下载到本地文件）+ 固定该节点
        mh, tag = mihomo_official(tmp, d.get("gh_proxy", ""))
        if mh:
            provs = {}
            for i, s in enumerate(x for x in d.get("subs", []) if x.get("url")):
                f = os.path.join(tmp, f"s{i}.yaml")
                subprocess.run(["curl", "-s", "-L", "-x", "http://127.0.0.1:7890", "-A", "clash.meta", "--max-time", "30",
                                "-o", f, s["url"]])
                provs[f"s{i}"] = {"type": "file", "path": f}
            man = [n["proxy"] for n in d.get("nodes", []) if n.get("proxy", {}).get("name") == node]
            esc = re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", node)  # RE2 不接受转义空格等，只转义元字符
            mc = {"mixed-port": 7891, "bind-address": "127.0.0.1", "allow-lan": False, "log-level": "warning", "mode": "rule",
                  "external-controller": "127.0.0.1:19091", "proxies": man, "proxy-providers": provs,
                  "proxy-groups": [{"name": "T", "type": "select", "proxies": [node]} if man else
                                   {"name": "T", "type": "select", "use": list(provs), "filter": f"^{esc}$"}],
                  "rules": ["MATCH,T"]}
            pm = os.path.join(tmp, "config.yaml")
            json.dump(mc, open(pm, "w"), ensure_ascii=False)  # YAML 兼容 JSON
            p = start([mh, "-d", tmp, "-f", pm], tmp, "mihomo")
            procs.append(p)
            if wait_port(7891, 40):
                res[f"D 官方 mihomo {tag} :7891"] = bench(7891)
            else:
                print(f"官方 mihomo 启动失败或找不到节点，见 {tmp}/mihomo.log")
            p.terminate()
        else:
            print("下载官方 mihomo 失败，跳过 D")
    finally:
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except Exception:
                pass
    print(f"\n各 {RUNS} 次取中位，单位 Mbps（每次最多 {SECS} 秒）")
    print("路径".ljust(30) + "".join(n.rjust(14) for n, _, _ in URLS))
    for k, v in res.items():
        print(k.ljust(30) + "".join(f"{x:14.1f}" for x in v))
    print("\n判读：A≈B≪C → TUN/系统层；B≪C → 面板配置（DNS/嗅探/出站参数）；C≪D → sing-box 内核与 mihomo 的差异；都差不多 → 节点 / 线路本身")
    shutil.rmtree(tmp, ignore_errors=True) if all(res.values()) else print(f"日志保留在 {tmp}")


if __name__ == "__main__":
    main()
