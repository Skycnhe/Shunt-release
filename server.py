#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shunt 分流: 零依赖的旁路由透明代理管理后端 (Alpine Linux, mihomo / sing-box)

本文件由 tools/build.py 生成，内嵌了 shunt/ 目录下全部模块的源码。请修改 shunt/ 下的文件后重新生成，不要直接改这里。
"""
PANEL_VERSION = "8.10.24"
import importlib.abc, importlib.util, linecache, marshal, os, sys, types, zlib

_SRC = {}
_SRC['shunt'] = '''"""Shunt 分流面板后端。

设备上运行的是仓库根目录的 server.py：它由 tools/build.py 生成，内嵌了本目录全部模块的源码，所以部署时仍然只有一个文件。
模块之间直接 from-import，依赖无环（tools/build.py 生成前可用文末的 SCC 脚本验证）。
唯一的例外是 core.py 末尾的 _link()：core.reload_core() 需要 latency.retest_soon()，
而 latency 的定时器又需要 core.refresh_regions()，真互递归，只能延迟绑定这一个名字。
"""
import importlib
import os

# 加载顺序：模块顶层代码只用到排在前面的模块
MODULES = [
    "base",
    "errlog",
    "data",
    "links",
    "ruleutil",
    "groups",
    "mihomo",
    "validate",
    "core",
    "corehttp",
    "nodeprobe",
    "sb_common",
    "sb_yaml",
    "sb_subs",
    "sb_nodes",
    "sb_rules",
    "sb_dns",
    "sb_config",
    "sb_switch",
    "panel_update",
    "backup",
    "health",
    "core_update",
    "core_cache",
    "sysopt",
    "notify",
    "latency",
    "stats",
    "unlock",
    "speedtest",
    "ruletest",
    "logstream",
    "adblock",
    "live",
    "logclean",
    "schedule",
    "devices",
    "diag",
    "dnsleak",
    "ipinfo",
    "migrate",
    "https",
    "access",
    "auth",
    "update_guard",
    "svcmigrate",
    "static",
    "webapi_base",
    "webapi_nodes",
    "webapi_subs",
    "webapi_rules",
    "webapi_sys",
    "webapi",
    "cli",
]
SERVER_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server.py")
_loaded = []


def load(server_file=None):
    """按顺序加载全部模块并绑定模块间的引用；返回模块列表（重复调用直接返回）"""
    global SERVER_FILE
    if _loaded:
        return _loaded
    if server_file:
        SERVER_FILE = server_file
    mods = [importlib.import_module(__name__ + "." + n) for n in MODULES]
    for m in mods:
        if hasattr(m, "_link"):
            m._link()
    _loaded.extend(mods)
    return _loaded


def setvar(name, value):
    """测试 / 调试用：把所有模块里同名的全局变量一起改掉（from-import 让每个模块都有自己的一份引用）"""
    for m in _loaded:
        if name in vars(m):
            setattr(m, name, value)
'''
_SRC['shunt.base'] = '''"""基础常量与通用工具"""
import shunt
import ipaddress
import os, threading, re
import secrets
import socket
import time
import urllib.error
import urllib.request
from collections import deque
from urllib.parse import urlsplit

BASE = os.path.dirname(os.path.abspath(shunt.SERVER_FILE))  # 面板文件所在目录（index.html、tproxy.sh 等）
CONF_DIR = os.environ.get("MIHOMO_DIR", "/etc/mihomo")
DATA_FILE = os.environ.get("PANEL_DATA", "/etc/mihomo-panel/data.json")
LGBM_URL = os.environ.get("SMART_REPO", "https://github.com/vernesong/mihomo/releases") + "/download/LightGBM-Model/Model.bin"
UPD_STATE = {"busy": False}  # 面板在线更新忙标志：panel_update 与 core_update 共用，避免循环导入
WD_HOLD = {"until": 0.0, "why": ""}  # 面板自己在重启 / 重载核心：这段时间内看门狗不判定故障（sysopt.svc_restart、sb_hot_reload 置位）


def wd_hold(sec, why="", reset=False):
    """reset=True：直接改成「从现在起 sec 秒」，用于重启命令返回后缩短暂停期"""
    import time as _t
    t = _t.time() + sec
    WD_HOLD["until"] = t if reset else max(WD_HOLD["until"], t)
    WD_HOLD["why"] = why


CORE_UPD = {"busy": False, "stage": "", "pct": 0, "ok": None, "message": "", "log": deque(maxlen=30), "t": 0}  # 内核更新状态：core_update 与 notify 共用
_UPD_LOCK = threading.Lock()


def upd_begin(kind):
    """面板更新 / 内核更新（含切换、回滚）互斥：在锁内「检查 + 置忙」一步完成，双击或多标签页同时点也只有一个能进。
    kind="panel" 置 UPD_STATE，其余置 CORE_UPD；两者任一在忙都返回 False。结束时由调用方把对应的 busy 置回 False"""
    with _UPD_LOCK:
        if UPD_STATE["busy"] or CORE_UPD["busy"]:
            return False
        (UPD_STATE if kind == "panel" else CORE_UPD)["busy"] = True
        return True


def upd_busy_msg():
    return "内核正在更新或切换，请稍后再试" if CORE_UPD["busy"] else "面板正在更新，请稍后再试"


MASS_TEST = {"n": 0, "active": {}}  # 进行中的全节点测速（/group/*/delay）：corehttp 登记，notify 看门狗据此暂停故障判定，避免高负载误判
_MASS_LOCK = threading.Lock()
MASS_MAX_AGE = 300  # 单次测速登记超过 5 分钟视为失效（异常路径漏了注销也不会让看门狗永久暂停）


def mass_begin():
    """登记一次全节点测速，返回凭据交给 mass_end。锁内增删，计数不会因并发丢失"""
    import time as _t
    tok = object()
    with _MASS_LOCK:
        MASS_TEST["active"][tok] = _t.time()
        MASS_TEST["n"] = len(MASS_TEST["active"])
    return tok


def mass_end(tok):
    with _MASS_LOCK:
        MASS_TEST["active"].pop(tok, None)
        MASS_TEST["n"] = len(MASS_TEST["active"])


def mass_testing(max_age=MASS_MAX_AGE):
    """是否有全节点测速在进行；顺带清掉超时的登记（自愈）"""
    import time as _t
    now = _t.time()
    with _MASS_LOCK:
        act = MASS_TEST["active"]
        for tok in [k for k, t0 in act.items() if now - t0 > max_age]:
            del act[tok]
        MASS_TEST["n"] = len(act)
        return bool(act)


def local_file(f):
    """部署文件 f 的本地路径（init.d/ 下的优先用 /etc/init.d/ 的系统版本）"""
    if f.startswith("init.d/"):
        sysf = "/etc/init.d/" + f[7:]
        return sysf if BASE == "/opt/mihomo-panel" and os.path.isfile(sysf) else os.path.join(BASE, f)
    return os.path.join(BASE, f)


def udp_dns_query(name, port=1053, host="127.0.0.1", timeout=8, qtype=1):
    """向本机 DNS 端口发一个查询（默认 A 记录，qtype 可改）；收到同 ID 的应答（含 NXDOMAIN）即视为成功"""
    qid = secrets.randbits(16)
    pkt = qid.to_bytes(2, "big") + b"\\x01\\x00\\x00\\x01\\x00\\x00\\x00\\x00\\x00\\x00"
    for lab in name.strip(".").split("."):
        b = lab.encode("idna") if lab else b""
        pkt += bytes([len(b)]) + b
    pkt += b"\\x00" + int(qtype).to_bytes(2, "big") + b"\\x00\\x01"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(pkt, (host, port))
        end = time.time() + timeout
        while time.time() < end:
            data = s.recv(1500)
            if len(data) >= 12 and int.from_bytes(data[:2], "big") == qid and data[2] & 0x80:
                return True
    except Exception:
        return False
    finally:
        s.close()
    return False
PANEL_DIR = os.path.dirname(DATA_FILE)
STATS_FILE = os.path.join(PANEL_DIR, "stats.json")
NOTIFY_FILE = os.path.join(PANEL_DIR, "notify.json")
CERT, KEY = os.path.join(PANEL_DIR, "cert.pem"), os.path.join(PANEL_DIR, "key.pem")
def _env_port(k, default):
    """端口环境变量：不是 1-65535 的整数时用默认值（以前 int() 直接抛异常，面板起不来）"""
    v = (os.environ.get(k) or "").strip()
    return int(v) if v.isdigit() and 0 < int(v) < 65536 else default


PORT = _env_port("PANEL_PORT", 8080)  # 面板端口：/etc/conf.d/shunt 里写 PANEL_PORT=8081，Docker 用 -e PANEL_PORT=8081
MIHOMO_BIN = os.environ.get("MIHOMO_BIN", "/usr/local/bin/mihomo")
SMART_BIN = os.environ.get("SMART_BIN", "/usr/local/bin/mihomo-smart")  # vernesong/mihomo：带 Smart 策略组的 mihomo
TPROXY_SH = os.environ.get("PANEL_TPROXY", os.path.join(BASE, "tproxy.sh"))
CTRL_HOST, CTRL_PORT = "127.0.0.1", 9090
MAX_BODY = 32 * 2**20  # 请求体上限 32MB（备份恢复可能较大）
CTRL = f"http://{CTRL_HOST}:{CTRL_PORT}"
MIXED = 7890
WD_INTERVAL = int(os.environ.get("PANEL_WD_INTERVAL", "30"))  # 看门狗检查间隔（秒）
LOCK = threading.RLock()  # 可重入：load() 内迁移写盘时可能已在 update() 的锁内

# v7.7.6 起服务名为 shunt（面板）/ shunt-core（核心）；还没迁移的旧设备仍是 mihomo-panel / mihomo（svcmigrate.py 负责迁移）
SVC_NEW = os.path.isfile("/etc/init.d/shunt-core")
CORE_SVC = "shunt-core" if SVC_NEW else "mihomo"
PANEL_SVC = "shunt" if os.path.isfile("/etc/init.d/shunt") else "mihomo-panel"
SVC = os.environ.get("PANEL_SVC", "rc-service " + CORE_SVC)  # 核心服务控制命令（测试环境可覆盖）
# 启动 / 重启核心前面板（或 --gen）写下的透明代理参数（tp_env 输出）。init.d 的 start_pre 见到 2 分钟内的这个文件就跳过 --gen、
# start_post 交给 tproxy.sh 直接读，一次启动少起两次 Python。只在正式部署目录启用（路径与 init.d 里写死的一致）
CORE_PREP = os.environ.get("PANEL_CORE_PREP") or ("/run/shunt-core.prep" if BASE == "/opt/mihomo-panel" and os.path.isdir("/run") else "")
LOG_FILES = [x for x in os.environ.get("PANEL_LOGS", "/var/log/mihomo.log,/var/log/mihomo-panel.log").split(",") if x]
MIHOMO_LOG = LOG_FILES[0] if LOG_FILES else "/var/log/mihomo.log"
AB_DIR = os.path.join(CONF_DIR, "adblock")
AB_STATS_FILE = os.path.join(PANEL_DIR, "adblock_stats.json")
LEASE_FILES = ["/var/lib/misc/dnsmasq.leases", "/tmp/dhcp.leases", "/var/lib/dnsmasq/dnsmasq.leases"]

DEFAULT_EXCLUDE = "剩余|到期|官网|流量|过期|Expire|Traffic"
PROXY_MODES = ("tproxy", "tun", "off")
TUN_STACKS = ("auto", "system", "gvisor", "mixed")  # auto：不写 stack，sing-box 1.15+ 用 sing-tun 自有协议栈
DEFAULT_FAKE_FILTER = ["*.lan", "+.local", "+.msftconnecttest.com", "+.msftncsi.com", "time.*.com", "ntp.*.com", "+.ntp.org",
                       "+.stun.*.*", "+.market.xiaomi.com", "localhost.ptlogin2.qq.com"]
DNS_DEFAULT = {"direct": ["https://doh.pub/dns-query", "https://dns.alidns.com/dns-query"],
               "proxy": ["https://1.1.1.1/dns-query#🚀 节点选择", "https://dns.google/dns-query#🚀 节点选择"],
               "default": ["223.5.5.5", "119.29.29.29"], "pserver": [], "mode": "fake-ip", "fake_filter": DEFAULT_FAKE_FILTER,
               "cache": "arc", "policy": True, "respect_rules": True, "block_bypass": False, "hosts": [], "policies": []}
DNS_LISTS = ("direct", "proxy", "default", "pserver", "fake_filter")
# 客户端绕过 mihomo DNS 的常见公共 DoH / DoT 服务器（阻止客户端绕过 DNS 时拒绝 LAN 设备直连它们的 443 / 853）
DOH_IPS = ["1.1.1.1/32", "1.0.0.1/32", "8.8.8.8/32", "8.8.4.4/32", "9.9.9.9/32", "149.112.112.112/32", "208.67.222.222/32",
           "208.67.220.220/32", "94.140.14.14/32", "94.140.15.15/32", "101.101.101.101/32", "185.222.222.222/32", "45.11.45.11/32",
           "223.5.5.5/32", "223.6.6.6/32", "119.29.29.29/32", "1.12.12.12/32", "120.53.53.53/32", "180.76.76.76/32",
           "180.184.1.1/32", "180.184.2.2/32", "2606:4700:4700::1111/128", "2606:4700:4700::1001/128", "2001:4860:4860::8888/128",
           "2001:4860:4860::8844/128", "2620:fe::fe/128", "2400:3200::1/128", "2400:3200:baba::1/128", "2402:4e00::/128"]
DOH_DOMAINS = ["+.dns.google", "+.cloudflare-dns.com", "+.one.one.one.one", "+.dns.quad9.net", "+.doh.opendns.com", "+.dns.adguard-dns.com",
               "+.dns.adguard.com", "+.dns.nextdns.io", "+.doh.pub", "+.dot.pub", "+.dns.alidns.com", "+.doh.360.cn", "+.dns.twnic.tw",
               "+.mozilla.cloudflare-dns.com", "+.chrome.cloudflare-dns.com", "+.dns.sb", "+.doh.dns.sb"]
# 国内常用 App / 网站直连名单（内置 cn-apps 规则集）：geosite:cn 之外再兜一层。
# 漏网的典型是字节系 snssdk.com：不在 geosite-cn 里，没命中域名规则就会按 IP 判断，经代理 DNS 解析
# 拿到海外 IP，最后落进「漏网之鱼」走代理。名单排在 AI / 流媒体 / geolocation-!cn 之前，DNS 也走直连。
CN_APPS = {
    "社交": ["qq.com", "qq.com.cn", "weixin.qq.com", "wechat.com", "weixinbridge.com", "wechatpay.com", "qpic.cn", "gtimg.cn",
             "gtimg.com", "idqqimg.com", "qlogo.cn", "tencent.com", "tencent-cloud.net", "myqcloud.com", "qcloud.com", "tencentcs.com",
             "weibo.com", "weibo.cn", "sinaimg.cn", "sina.com.cn", "sina.cn", "xiaohongshu.com", "xhscdn.com", "xhslink.com",
             "zhihu.com", "zhimg.com", "douban.com", "doubanio.com", "immomo.com", "momocdn.com", "soulapp.cn", "soulapp.me",
             "tieba.baidu.com", "hupu.com", "hoopchina.com.cn", "dingtalk.com", "dingtalkapps.com", "feishu.cn", "feishucdn.com",
             "larksuite.cn", "work.weixin.qq.com"],
    "视频": ["bilibili.com", "bilibili.cn", "biliapi.com", "biliapi.net", "bilivideo.com", "bilivideo.cn", "hdslb.com", "biligame.com",
             "iqiyi.com", "iqiyipic.com", "qiyi.com", "qiyipic.com", "71.am", "youku.com", "ykimg.com", "tudou.com", "mgtv.com",
             "hitv.com", "v.qq.com", "video.qq.com", "douyu.com", "douyucdn.cn", "huya.com", "msstatic.com", "cctv.com", "cntv.cn",
             "yangshipin.cn", "miguvideo.com", "migu.cn", "music.163.com", "126.net", "kugou.com", "kuwo.cn", "y.qq.com", "ximalaya.com",
             "xmcdn.com"],
    "短视频": ["douyin.com", "douyincdn.com", "douyinpic.com", "douyinvod.com", "douyinstatic.com", "amemv.com", "snssdk.com",
               "bytedance.com", "byteimg.com", "bytecdn.cn", "bytedns.net", "byted-static.com", "bytegoofy.com", "ibytedtos.com",
               "ixigua.com", "pstatp.com", "toutiao.com", "toutiaoimg.com", "toutiaocdn.com", "zijieapi.com", "zijieimg.com",
               "volces.com", "volccdn.com", "kuaishou.com", "kuaishouzt.com", "gifshow.com", "ksapisrv.com", "kwaicdn.com",
               "kwimgs.com", "yximgs.com", "kspkg.com", "weishi.qq.com", "channels.weixin.qq.com"],
    "头部网站": ["baidu.com", "bdstatic.com", "bdimg.com", "baidupcs.com", "bcebos.com", "taobao.com", "tmall.com", "alicdn.com",
                 "aliyun.com", "aliyuncs.com", "alibaba.com", "tbcdn.cn", "mmstat.com", "jd.com", "jd.hk", "360buyimg.com", "jdcloud.com",
                 "pinduoduo.com", "yangkeduo.com", "pddpic.com", "pddugc.com", "meituan.com", "meituan.net", "dianping.com",
                 "ele.me", "elemecdn.com", "163.com", "netease.com", "ctrip.com", "trip.com", "12306.cn", "amap.com", "autonavi.com",
                 "didi.cn", "didiglobal.com", "didistatic.com", "xiaomi.com", "mi.com", "miui.com", "huawei.com", "hicloud.com",
                 "dbankcloud.com", "hihonorcloud.com", "honor.com", "vivo.com.cn", "oppo.com", "heytapmobi.com", "coloros.com",
                 "csdn.net", "sohu.com", "ifeng.com", "360.cn", "qhimg.com", "smzdm.com", "58.com", "zhipin.com"],
    "支付银行": ["alipay.com", "alipay.cn", "alipayobjects.com", "alipay-eco.com", "antgroup.com", "mybank.cn", "tenpay.com",
                 "wechatpay.com", "unionpay.com", "95516.com", "chinaums.com", "jdpay.com", "icbc.com.cn", "ccb.com", "abchina.com",
                 "boc.cn", "bankcomm.com", "cmbchina.com", "psbc.com", "spdb.com.cn", "citicbank.com", "cebbank.com", "cmbc.com.cn",
                 "pingan.com", "webank.com", "yunshanfu.cn"],
}
CN_APP_DOMAINS = list(dict.fromkeys("+." + x for v in CN_APPS.values() for x in v))
# 规则匹配前解析域名时带上国内网段（ECS）：未知的国内域名经代理 DNS 解析也能拿到国内 CDN 的 IP，GEOIP,CN 才能命中直连；
# 只用于分流判断，不暴露用户真实 IP（223.5.5.0/24 是阿里公共 DNS 所在网段）
CN_ECS = "223.5.5.0/24"
LAN_IN = "IN-TYPE,TPROXY/TUN/REDIR"  # 只针对透明代理进来的局域网流量；mihomo 自己的 DNS 查询（Inner）与 7890 端口不受影响
# 国内（会看到你查询国外域名就算泄露）的 DNS 服务器特征
CN_DNS = re.compile(r"(?i)(?:^|[/@\\[])(?:223\\.5\\.5\\.5|223\\.6\\.6\\.6|2400:3200|119\\.29\\.29\\.29|119\\.28\\.28\\.28|182\\.254\\.11[68]\\.116|"
                    r"1\\.12\\.12\\.12|120\\.53\\.53\\.53|2402:4e00|114\\.114\\.11[45]\\.11[45]|180\\.76\\.76\\.76|180\\.184\\.[12]\\.[12]|"
                    r"101\\.226\\.4\\.6|218\\.30\\.118\\.6|117\\.50\\.\\d+\\.\\d+|52\\.80\\.\\d+\\.\\d+|"
                    r"[\\w.-]*(?:alidns\\.com|doh\\.pub|dot\\.pub|dnspod\\.(?:cn|com)|360\\.cn|114dns\\.com|onedns\\.net|baidu\\.com|volces\\.com))(?:[:/#\\]]|$)")
AB_DEFAULT = {"enabled": False, "lists": [], "black": [], "black_cn": [], "black_proxy": [], "white": [], "interval": 86400, "dns": False}
# 内置预设：按「国内 / 国外 × 视频 / 网页 / 隐私」分组，前端按 cat 分行显示
BM = "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Clash/"
AB_PRESETS = [
    {"cat": "国内 · 视频 / App", "name": "秋风广告规则", "url": "https://raw.githubusercontent.com/TG-Twilight/AWAvenue-Ads-Rule/main/AWAvenue-Ads-Rule.txt",
     "dsc": "国内 App 开屏、视频贴片、广告 SDK"},
    {"cat": "国内 · 视频 / App", "name": "小米电视广告", "url": BM + "AdvertisingMiTV/AdvertisingMiTV.list", "dsc": "小米电视 / 盒子开机与界面广告"},
    {"cat": "国内 · 网页", "name": "anti-AD", "url": "https://anti-ad.net/easylist.txt", "dsc": "国内网页、App 广告综合"},
    {"cat": "国内 · 网页", "name": "EasyList China", "url": "https://easylist-downloads.adblockplus.org/easylistchina.txt",
     "dsc": "Adblock Plus 官方中文列表（只取整域名规则）"},
    {"cat": "国内 · 网页", "name": "AdGuard 中文", "url": "https://filters.adtidy.org/extension/ublock/filters/224.txt", "dsc": "中文网站广告"},
    {"cat": "国内 · 隐私", "name": "隐私追踪 (blackmatrix7)", "url": BM + "Privacy/Privacy.list", "dsc": "统计、埋点、行为追踪"},
    {"cat": "国外 · 视频 / 电视", "name": "Smart-TV", "url": "https://raw.githubusercontent.com/Perflyst/PiHoleBlocklist/master/SmartTV.txt",
     "dsc": "三星 / LG / 索尼等电视界面广告与回传"},
    {"cat": "国外 · 网页", "name": "AdGuard DNS filter", "url": "https://adguardteam.github.io/AdGuardSDNSFilter/Filters/filter.txt",
     "dsc": "国外网站、App 广告综合"},
    {"cat": "国外 · 网页", "name": "EasyList", "url": "https://easylist.to/easylist/easylist.txt",
     "dsc": "Adblock Plus 默认列表（只取整域名规则）"},
    {"cat": "国外 · 网页", "name": "HaGeZi Multi", "url": "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/multi.txt",
     "dsc": "广告 + 追踪 + 恶意域名，覆盖更广"},
    {"cat": "国外 · 隐私", "name": "EasyPrivacy", "url": "https://easylist.to/easylist/easyprivacy.txt", "dsc": "国外统计与追踪"},
    {"cat": "国外 · 隐私", "name": "Frogeye 一方追踪", "url": "https://hostfiles.frogeye.fr/firstparty-trackers.txt",
     "dsc": "伪装成网站子域名的追踪器"}]
TUN_DEFAULT = {"stack": "auto", "device": "Meta", "auto_redirect": True, "strict_route": False}
GROUPS_DEFAULT = {"type": "url-test", "lb": True, "auto": True, "strategy": "consistent-hashing", "interval": 60,
                  "tolerance": 0, "url": "https://www.gstatic.com/generate_204", "extra": True, "other": True, "lazy": True,
                  "lgbm": False}
SCHED_DEFAULT = {"sub_update": "", "core_restart": "", "geo_update": "", "latency": 0}
DEFAULT = {"secret": "", "mode": "rule", "tproxy": True, "subs": [], "rules": [],
           "rulesets": [], "bypass": [], "tests": None, "sub_interval": 86400, "region_groups": True,
           "nodes": [], "ipv6": False, "https": False, "allow_public": False, "allow_hosts": [], "watchdog": True, "tg_token": "", "tg_chat": "",
           "proxy_mode": "", "tun": TUN_DEFAULT, "log_limit": 5, "adblock": AB_DEFAULT, "dns": DNS_DEFAULT,
           "devices": {}, "schedule": SCHED_DEFAULT, "groups_cfg": GROUPS_DEFAULT, "custom_groups": [], "sniffer": True,
           "gh_proxy": "", "gh_token": "", "core": "mihomo", "schema": 0, "sysopt": [], "sysopt_orig": {}, "sysopt_mods": []}
TESTS = [
    {"name": "Google", "url": "https://www.google.com/generate_204"},
    {"name": "YouTube", "url": "https://www.youtube.com/generate_204"},
    {"name": "Telegram", "url": "https://api.telegram.org"},
    {"name": "GitHub", "url": "https://github.com"},
    {"name": "百度", "url": "https://www.baidu.com"},
    {"name": "淘宝", "url": "https://www.taobao.com"},
]
G_SEL, G_YT, G_GG, G_TG, G_FINAL = "🚀 节点选择", "📹 YouTube", "🔍 Google", "📲 Telegram", "🐟 漏网之鱼"
G_AI, G_NF = "🤖 AI 服务", "🎬 Netflix"
G_MANUAL, G_AUTO, G_DIRECT = "\\U0001F590\\uFE0F 手动选择", "⚡ 全局自动选择", "🏠 直连"
LB_PREFIX, AUTO_TAIL, LB_TAIL = "\\u2696\\uFE0F ", "竞技", "负载均衡"
AUTO_TAIL_OLD = "自动优选"  # v6–v6.7.2 的地区组后缀，v6.7.3 改为「竞技」，仅用于迁移
LEGACY_AUTO = "♻️ 自动选择"  # v5 及以前的名称，读取旧数据时自动迁移
AUTO_ORDER = ["日本", "新加坡", "香港", "美国"]  # 「自动优选」组在节点选择中的顺序，其余地区按识别顺序排在后面
LB_ORDER = ["香港", "日本", "新加坡", "美国"]    # 「负载均衡」组的顺序
SCHEMA = 9
PANEL_VERSION = "8.10.24"
L, R = "(?<![A-Za-z])", "(?![A-Za-z])"  # 英文缩写两侧不能紧挨字母，避免 (?i)US 误匹配 Russia / Plus / Australia
REGIONS = [  # (分组名, 正则)；正则同时在 Python 与 mihomo(regexp2) 中使用，只用两者都支持的语法
    ("🇭🇰 香港", f"🇭🇰|(?i:香港|港|Hong ?Kong)|{L}HKG?{R}"),
    ("🇹🇼 台湾", f"🇹🇼|(?i:台|Taiwan|Taipei)|{L}TWN?{R}"),
    ("🇯🇵 日本", f"🇯🇵|(?i:日本|东京|東京|大阪|埼玉|Japan|Tokyo|Osaka)|{L}JPN?{R}"),
    ("🇸🇬 新加坡", f"🇸🇬|(?i:新加坡|狮城|獅城|Singapore)|{L}SGP?{R}"),
    ("🇺🇸 美国", f"🇺🇸|(?i:美国|美國|洛杉矶|圣何塞|硅谷|纽约|西雅图|芝加哥|达拉斯|凤凰城|United ?States|America|Los ?Angeles|San ?Jose|Seattle|New ?York|Chicago|Dallas)|{L}USA?{R}"),
    # 以下为扩展地区（设置 → 策略组 可关闭），只有确实存在节点时才生成
    ("🇰🇷 韩国", f"🇰🇷|(?i:韩国|韓國|首尔|首爾|春川|Korea|Seoul)|{L}KOR?{R}"),
    ("🇬🇧 英国", f"🇬🇧|(?i:英国|英國|伦敦|倫敦|United ?Kingdom|Britain|London)|{L}(?:UK|GB|GBR){R}"),
    ("🇩🇪 德国", f"🇩🇪|(?i:德国|德國|法兰克福|Germany|Frankfurt)|{L}DEU?{R}"),
    ("🇫🇷 法国", f"🇫🇷|(?i:法国|法國|巴黎|France|Paris)|{L}FRA?{R}"),
    ("🇳🇱 荷兰", f"🇳🇱|(?i:荷兰|荷蘭|阿姆斯特丹|Netherlands|Amsterdam)|{L}NLD?{R}"),
    ("🇨🇦 加拿大", f"🇨🇦|(?i:加拿大|多伦多|温哥华|Canada|Toronto|Vancouver)|{L}CAN?{R}"),
    ("🇦🇺 澳大利亚", f"🇦🇺|(?i:澳大利亚|澳洲|悉尼|墨尔本|Australia|Sydney|Melbourne)|{L}AUS?{R}"),
    ("🇷🇺 俄罗斯", f"🇷🇺|(?i:俄罗斯|俄羅斯|莫斯科|Russia|Moscow)|{L}RUS?{R}"),
    ("🇮🇳 印度", f"🇮🇳|(?i:印度|孟买|India|Mumbai)|{L}IND?{R}"),
    ("🇹🇷 土耳其", f"🇹🇷|(?i:土耳其|伊斯坦布尔|Turkey|Türkiye|Istanbul)|{L}TUR?{R}"),
    ("🇲🇾 马来西亚", f"🇲🇾|(?i:马来|馬來|吉隆坡|Malaysia)|{L}MYS?{R}"),
    ("🇹🇭 泰国", f"🇹🇭|(?i:泰国|泰國|曼谷|Thailand|Bangkok)|{L}THA?{R}"),
    ("🇻🇳 越南", f"🇻🇳|(?i:越南|胡志明|Vietnam)|{L}VNM?{R}"),
    ("🇵🇭 菲律宾", f"🇵🇭|(?i:菲律宾|菲律賓|马尼拉|Philippines)|{L}PHL?{R}"),
    ("🇦🇷 阿根廷", f"🇦🇷|(?i:阿根廷|Argentina)|{L}ARG?{R}"),
    ("🇧🇷 巴西", f"🇧🇷|(?i:巴西|圣保罗|Brazil)|{L}BRA?{R}"),
]
PRIMARY_REGIONS = 5
G_OTHER = "🌐 其他"
LB_SUFFIX, AUTO_SUFFIX = "均衡", "自动"  # 旧版（v5）地区组后缀，仅用于迁移
GROUP_TYPES = ("select", "url-test", "fallback", "load-balance")
LB_STRATEGIES = ("consistent-hashing", "round-robin")  # 自定义负载均衡组：一致性哈希（同一域名固定节点）/ 轮询（每条新连接换下一个节点）
SIDE_GROUPS = [G_YT, G_GG, G_TG, G_AI, G_NF, G_FINAL]
BUILTIN_POLICIES = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"}
HC = "https://www.gstatic.com/generate_204"
# 直连测速用国内地址（DIRECT 是国内直连，测谷歌没有意义）；第一个不可用时试下一个
# 直连测速地址：与概览「百度」卡片同一个地址、同一种测法，两处数字才可比
DIRECT_HC = "https://www.baidu.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


# Fake-IP 网段：路由器自己用本机 DNS 时，公网域名会解析成这些地址，连接由 mihomo / sing-box 接管并按真实域名转发，
# 不是内网目标，SSRF 校验放行（否则开着 Fake-IP 时在线更新、订阅下载直连全被误拦）
FAKEIP_NETS = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("fc00::/18"), ipaddress.ip_network("fdfe:dcba:9876::/64"))


def url_host_forbidden(url, via_proxy=False):
    """SSRF 防护：下载类 URL 的目标主机必须解析到公网地址。

    命中以下任一情况返回 True（禁止）：无主机名 / localhost / 字面内网 IP /
    （直连时）DNS 解析失败或解析出的任一 IP 不是公网地址（内网、回环、链路本地、保留、组播等；Fake-IP 网段除外）。
    via_proxy=True（经 mihomo 代理下载）时跳过 DNS 校验：DNS 由代理自己解析，
    本地校验无意义，且在 DNS 受干扰的环境（如国内）会误伤合法下载；字面 IP 仍拦截。
    注意：检查与实际建连之间存在 DNS TOCTOU 窗口，这里只防住直接的内网目标。"""
    try:
        host = (urlsplit(url).hostname or "").strip().lower().rstrip(".")
    except Exception:
        return True
    if not host or host == "localhost":
        return True
    try:
        return not ipaddress.ip_address(host).is_global  # 字面 IP：无需 DNS，直接判定
    except ValueError:
        pass
    if via_proxy:
        return False
    try:
        addrs = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return True
    for _, _, _, _, sa in addrs:
        try:
            ip = ipaddress.ip_address(sa[0])
        except ValueError:
            return True
        if not ip.is_global and not any(ip in n for n in FAKEIP_NETS):
            return True
    return False


def dl_check(url, via_proxy=False):
    """下载入口统一校验：目标非公网地址时抛 IOError（调用方已有通用异常处理）"""
    if url_host_forbidden(url, via_proxy=via_proxy):
        raise IOError(f"目标地址非公网，已拦截（SSRF 防护）：{urlsplit(url).hostname}")


def read_full(r, limit):
    """读 urllib 响应体，最多 limit 字节；连接提前断开（Content-Length 没收满）时抛 IOError。
    http.client 的 read(n) 在对方中途断开时不报错，只返回已收到的部分：以前背景图只存下半张（浏览器只画出上半截），
    订阅 / 规则集 / 广告列表只解析出前面一部分，节点或规则悄悄变少"""
    data = r.read(limit)
    left = getattr(r, "length", None)
    if left and len(data) < limit:
        raise IOError(f"下载不完整：连接中途断开（收到 {len(data)} 字节，还差 {left} 字节）")
    return data


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """跟随重定向前校验目标主机，防止 302 跳到内网（urllib 默认无条件跟随）"""

    def __init__(self, via_proxy=False):
        self._via_proxy = via_proxy

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if url_host_forbidden(newurl, via_proxy=self._via_proxy):
            raise urllib.error.HTTPError(newurl, 403, "重定向目标非公网地址，已拦截（SSRF 防护）", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)
'''
_SRC['shunt.errlog'] = '''"""错误记录：原本被吞掉的异常记到面板日志和「诊断」页"""
import time
from collections import deque

ERRS = deque(maxlen=30)
_ERR_SEEN = {}


def log_err(where, e, tb=False):
    """把原本被吞掉的异常记到面板日志和「诊断」里；同一处同一错误 10 分钟只记一次"""
    msg = f"{type(e).__name__}: {e}"[:300] if isinstance(e, BaseException) else str(e)[:300]
    key = (where, msg)
    now = time.time()
    if now - _ERR_SEEN.get(key, 0) < 600:
        return
    _ERR_SEEN[key] = now
    if len(_ERR_SEEN) > 500:
        _ERR_SEEN.clear()
    ERRS.appendleft({"t": int(now), "where": where, "msg": msg})
    extra = ""
    if tb and isinstance(e, BaseException):
        import traceback
        extra = "\\n" + "".join(traceback.format_exception(type(e), e, e.__traceback__))[-1500:]
    print(f"[error] {where}: {msg}{extra}", flush=True)
'''
_SRC['shunt.data'] = '''"""数据"""
import copy, hashlib, json, os, re, secrets, threading, time

from .base import (
    AUTO_SUFFIX, AUTO_TAIL, AUTO_TAIL_OLD, DATA_FILE, DEFAULT, G_AUTO, G_OTHER, LB_PREFIX, LB_SUFFIX, LB_TAIL, LEGACY_AUTO, LOCK,
    PANEL_DIR, PROXY_MODES, REGIONS, SCHEMA, TESTS
)
from .ruleutil import dns_policy_of, rule_policy_index
OLD_PW_KEYS = ("password", "pw_hash", "pw_default")


def load():
    """返回 data.json 的一份独立副本（调用方可随意修改）。

    缓存的是规范化后的 JSON 文本，命中时 json.loads 一次（比 deepcopy 整份配置快约 4 倍）。
    缓存项 (键, 代数, 文本) 用一个元组一次写入，读者不会拿到「新键 + 旧数据」；
    键在读文件之前取（内容只会比键新，最多多一次未命中），save() 自增代数，同一 mtime 内的进程内写入也会失效"""
    _fix_legacy_perms()
    key = _data_key()
    gen = _DATA_GEN[0]
    v = _DATA_CACHE.get("v")
    if key is not None and v is not None and v[0] == key and v[1] == gen:
        return json.loads(v[2])
    d = _load_uncached()
    if key is not None:
        _DATA_CACHE["v"] = (key, gen, json.dumps(d, ensure_ascii=False))
    return d


def _data_key():
    """data.json 的缓存键：路径 + mtime + 大小。save() 必改 mtime，写后缓存自动失效"""
    try:
        st = os.stat(DATA_FILE)
        return (DATA_FILE, st.st_mtime_ns, st.st_size)
    except OSError:
        return None


_DATA_CACHE = {}  # {"v": (键, 代数, 规范化 JSON 文本)}
_DATA_GEN = [0]  # save() 自增：让进程内的写入一定使缓存失效（不依赖 mtime 精度）


def _fix_legacy_perms():
    """一次性收紧旧版留下的 644 敏感文件（含订阅/节点密码）：只跑一次"""
    global _PERMS_FIXED
    if _PERMS_FIXED:
        return
    _PERMS_FIXED = True
    paths = [HISTORY_FILE, os.path.join(PANEL_DIR, "sub-debug.txt")]
    try:
        paths += [os.path.join(PANEL_DIR, "sb_subs", n) for n in os.listdir(os.path.join(PANEL_DIR, "sb_subs"))
                  if n.endswith(".json")]
    except OSError:
        pass
    for p in paths:
        try:
            if os.path.isfile(p):
                os.chmod(p, 0o600)
        except OSError:
            pass


_PERMS_FIXED = False


def _load_uncached():
    bad = False
    try:
        with open(DATA_FILE) as f:
            d = json.load(f)
    except FileNotFoundError:
        d = {}
    except Exception:
        d, bad = {}, True
    if not isinstance(d, dict):  # 手改 / 损坏成数组或字符串：按空数据处理，免得所有接口 500
        d, bad = {}, True
    if bad:
        _keep_corrupt()
    if normalize(d):
        with LOCK:
            save(d)
    return d


def _keep_corrupt():
    """data.json 解析不了（断电写坏、手改出错）时，下面 normalize + save 会用默认值覆盖它，订阅 / 节点 / 规则全部丢失。
    覆盖前先原样备份到 data.json.corrupt（已有同样内容的备份时不重复），可手动找回"""
    bak = DATA_FILE + ".corrupt"
    try:
        with open(DATA_FILE, "rb") as f:
            raw = f.read()
        if not raw.strip():
            return
        try:
            with open(bak, "rb") as f:
                if f.read() == raw:
                    return
        except OSError:
            pass
        _atomic_write(bak, raw)
        print(time.strftime("%F %T"), f"[data] {DATA_FILE} 无法解析，已备份到 {bak}", flush=True)
    except OSError as e:
        print("backup corrupt data.json", e, flush=True)


def _atomic_write(path, raw, mode=0o600, sync=False):
    """先写同目录下的唯一临时文件（0600 创建，不会有一瞬间可读），再 os.replace 换上去。
    以前固定用 path + ".tmp"：两个线程同时写同一个文件（看门狗事件、通知状态、定时任务记录）会互相截断临时文件，
    或一方 os.replace 后另一方 FileNotFoundError；写失败也不会留下半截临时文件"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}-{threading.get_ident()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            if sync:  # 断电保护：data.json 是全部配置，先落盘再改名（路由器常被直接拔电）
                f.flush()
                os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def normalize(d):
    """补默认值 + 旧版迁移（原地修改 d）。返回是否有需要写回 data.json 的变化。版本备份恢复时也用它处理备份里的 data.json"""
    for k, v in DEFAULT.items():
        d.setdefault(k, copy.deepcopy(v))
        if isinstance(v, dict) and isinstance(d[k], dict) and k != "devices":
            for kk, vv in v.items():
                d[k].setdefault(kk, copy.deepcopy(vv))
    if d["proxy_mode"] not in PROXY_MODES:  # 旧版只有 tproxy 布尔值
        d["proxy_mode"] = "tproxy" if d.get("tproxy", True) else "off"
    d["tproxy"] = d["proxy_mode"] == "tproxy"
    dirty = False
    if not d["tests"]:
        d["tests"] = [dict(t) for t in TESTS]
        d["tests_rev"] = 2
    elif int(d.get("tests_rev") or 0) < 2:  # 默认站点第六个：v7.6.6 是哔哩哔哩，v7.6.7 改为淘宝；仍是默认列表的补上 / 换掉，改过的不动
        cur = [(t.get("name"), t.get("url")) for t in d["tests"]]
        base5 = [(t["name"], t["url"]) for t in TESTS[:5]]
        if cur == base5:
            d["tests"].append(dict(TESTS[5]))
        elif cur == base5 + [("哔哩哔哩", "https://www.bilibili.com")]:
            d["tests"][5] = dict(TESTS[5])
        d["tests_rev"] = 2
        dirty = True
    if any(k in d for k in OLD_PW_KEYS):  # v6.8.2 起面板不再有密码：清掉旧版留下的密码字段
        for k in OLD_PW_KEYS:
            d.pop(k, None)
        dirty = True
    for s in d.get("subs") or []:  # v8.4.0 订阅的 removed：格式不对的条目去掉（旧版没有这个字段，不补）
        if isinstance(s, dict) and "removed" in s:
            v = s["removed"]
            good = list(dict.fromkeys(x for x in v if isinstance(x, str) and x)) if isinstance(v, list) else []
            if good != v:
                s["removed"] = good
                dirty = True
    for g in d.get("custom_groups") or []:  # v8.4.0 自定义组勾选的订阅节点 sub_nodes：同上
        if isinstance(g, dict) and "sub_nodes" in g:
            v = g["sub_nodes"]
            good = [{"sub": x["sub"], "name": x["name"]} for x in (v if isinstance(v, list) else [])
                    if isinstance(x, dict) and isinstance(x.get("sub"), str) and isinstance(x.get("name"), str) and x["sub"] and x["name"]]
            if good != v:
                g["sub_nodes"] = good
                dirty = True
    if not d["secret"]:
        d["secret"] = secrets.token_hex(16)
        dirty = True
    if int(d.get("schema") or 0) < SCHEMA:  # v5 → v6：地区组改名（如 🇺🇸 美国 → 🇺🇸 美国自动优选），同步所有引用
        # 旧版地区负载均衡与未写策略的自定义负载均衡组用的是 groups_cfg.strategy（没保存过 = 当时默认的一致性哈希），保留下来，行为不变
        d["groups_cfg"]["strategy"] = "consistent-hashing"
        gc0 = d["groups_cfg"]  # v6.7.3 竞技：仍是旧默认值（300 秒 / 50 ms）的改为 60 秒 / 0 ms，自己改过的保留
        if int(d.get("schema") or 0) < 9:
            if gc0.get("interval") in (None, 300):
                gc0["interval"] = 60
            if gc0.get("tolerance") in (None, 50):
                gc0["tolerance"] = 0
        migrate_names(d)  # 旧「⚖️ X负载均衡」与 v6.6.9 的「⚖️ X轮询 / 粘性」都改指「⚖️ X哈希」
        for g in d.get("custom_groups") or []:  # 自定义负载均衡组的轮询 / 粘性会话也改为一致性哈希
            if isinstance(g, dict) and g.get("type") == "load-balance":
                g["strategy"] = "consistent-hashing"
        d["schema"] = SCHEMA
        dirty = True
    return dirty


def region_label(rname):
    """「🇺🇸 美国」→ (「🇺🇸」, 「美国」)"""
    flag, _, label = rname.partition(" ")
    return flag, label


def auto_name(rname, tail=AUTO_TAIL):
    """「🇯🇵 日本竞技」：url-test，始终选延迟最低的节点"""
    flag, label = region_label(rname)
    return f"{flag} {label}{tail}"


LB_KINDS = (("consistent-hashing", "哈希"),)  # 每个地区一个负载均衡组「⚖️ 日本哈希」
LB_DROPPED = (("round-robin", "轮询"), ("sticky-sessions", "粘性"))  # v6.6.9–v6.7.0 的轮询 / 粘性组，v6.7.1 移除，仅用于迁移
LB_ALL = dict(LB_KINDS + LB_DROPPED)


def lb_name(rname, strategy="consistent-hashing"):
    """「⚖️ 日本哈希」（旧版还有「⚖️ 日本轮询」「⚖️ 日本粘性」）"""
    return LB_PREFIX + region_label(rname)[1] + LB_ALL[strategy]


def lb_names(rname):
    return [lb_name(rname, st) for st, _ in LB_KINDS]


def lb_dropped_names(rname):
    return [lb_name(rname, st) for st, _ in LB_DROPPED]


def lb_old_name(rname):
    """v6.6.8 及以前的单个「⚖️ 日本负载均衡」组"""
    return LB_PREFIX + region_label(rname)[1] + LB_TAIL


def region_lb_set():
    return {n for r, _ in REGIONS + [(G_OTHER, None)] for n in lb_names(r)}


def legacy_map(st=None):
    """旧版策略组名 → 新名称。st：旧版「⚖️ X负载均衡」实际用的策略（存在 groups_cfg.strategy，没保存过就是当时的默认一致性哈希），
    迁移到同策略的组，行为与升级前一致"""
    m = {LEGACY_AUTO: G_AUTO}
    for rname in [r[0] for r in REGIONS] + [G_OTHER]:
        m[rname] = m[rname + AUTO_SUFFIX] = m[auto_name(rname, AUTO_TAIL_OLD)] = auto_name(rname)
        m[rname + LB_SUFFIX] = m[lb_old_name(rname)] = lb_name(rname)  # 只剩一致性哈希
        for n in lb_dropped_names(rname):
            m[n] = lb_name(rname)
    return m


def map_dns_server(x, mp):
    pol = dns_policy_of(x)
    if pol and pol in mp:
        head, _, tail = x.partition("#")
        return head + "#" + mp[pol] + tail[len(pol):]
    return x


def map_refs(d, mp, drop=()):
    """按 mp {旧名: 新名} 改写自定义规则、规则集、自定义组成员、各 DNS 列表里的策略组引用；drop 中的名字从自定义组成员里删除。返回改动数"""
    n = 0
    rules = []
    for r in d.get("rules") or []:
        parts = r.split(",")
        i = rule_policy_index(parts)
        if not r.startswith("#") and i > 0 and parts[i].strip() in mp:
            parts[i] = mp[parts[i].strip()]
            n += 1
        rules.append(",".join(parts))
    d["rules"] = rules
    for r in d.get("rulesets") or []:
        if r.get("target") in mp:
            r["target"] = mp[r["target"]]
            n += 1
    for g in d.get("custom_groups") or []:
        old = list(g.get("proxies") or [])
        new = []
        for m in old:
            if m in drop:
                continue
            m = mp.get(m, m)
            if m not in new:
                new.append(m)
        if new != old:
            g["proxies"] = new
            n += 1
    dc = d.get("dns") or {}
    for k in ("direct", "proxy", "pserver"):
        out = []
        for x in dc.get(k) or []:
            y = map_dns_server(x, mp)
            n += y != x
            out.append(y)
        if k in dc:
            dc[k] = out
    for pe in dc.get("policies") or []:
        out = [map_dns_server(x, mp) for x in pe.get("servers") or []]
        n += out != pe.get("servers")
        pe["servers"] = out
    return n


def migrate_names(d, st=None):
    mp = legacy_map(st if st else (d.get("groups_cfg") or {}).get("strategy"))
    return map_refs(d, {k: v for k, v in mp.items() if k != v})


_CONFIG_VERSION = [0]  # data.json 每次 save() 自增：/api/ping 用它判断配置是否变化


def config_version():
    return _CONFIG_VERSION[0]


def save(d):
    _atomic_write(DATA_FILE, json.dumps(d, ensure_ascii=False, indent=2).encode(), sync=True)
    with LOCK:  # RLock 可重入：调用方可能已持有
        _DATA_GEN[0] += 1
        _DATA_CACHE.pop("v", None)
        _CONFIG_VERSION[0] += 1


def update(fn):
    """在锁内读-改-写 data.json，返回修改前的副本（用于配置校验失败时回滚）；有实际变化时把旧版本记入配置快照。
    history.json 的全量读+写移到 LOCK 外（另用 _HISTORY_LOCK 保证原子），避免大快照拖慢写配置并阻塞其他 API。"""
    with LOCK:
        d = load()
        prev = load()  # load() 每次返回独立副本，比 deepcopy 快
        fn(d)
        save(d)
    try:
        with _HISTORY_LOCK:
            history_push(prev, d)
    except Exception as e:
        print("history error", e, flush=True)
    return prev


_HISTORY_LOCK = threading.Lock()  # history.json 读-改-写原子性（update 的全局 LOCK 之外）


HISTORY_FILE = os.path.join(PANEL_DIR, "history.json")
HISTORY_MAX = 10
HISTORY_SKIP = {"tests_rev", "core", "secret", "devices", "https", "schema", "sysopt", "sysopt_orig", "sysopt_mods", "health", "health_cfg", "gh_token"}
KEY_LABEL = {"subs": "订阅", "nodes": "节点", "rules": "自定义规则", "rulesets": "规则集", "dns": "DNS", "adblock": "广告拦截",
             "custom_groups": "自定义策略组", "groups_cfg": "地区分组", "region_groups": "地区分组", "proxy_mode": "代理方式",
             "tun": "TUN", "ipv6": "IPv6", "bypass": "绕过设备", "mode": "代理模式", "sniffer": "域名嗅探", "schedule": "定时任务",
             "tests": "延迟站点", "sub_interval": "订阅间隔", "tg_token": "Telegram", "tg_chat": "Telegram", "watchdog": "看门狗",
             "log_limit": "日志上限", "gh_proxy": "GitHub 加速", "gh_token": "GitHub Token", "tproxy": "代理方式"}


def history_push(prev, cur):
    keys = sorted(k for k in set(prev) | set(cur) if k not in HISTORY_SKIP and prev.get(k) != cur.get(k))
    if not keys:
        return
    h = read_json(HISTORY_FILE, [])
    snap = {k: v for k, v in prev.items() if k not in HISTORY_SKIP}
    if h and h[0].get("data") == snap:
        return
    labels = []
    for k in keys:
        lb = KEY_LABEL.get(k, k)
        if lb not in labels:
            labels.append(lb)
    h.insert(0, {"id": secrets.token_hex(4), "t": int(time.time()), "changed": labels, "data": snap})
    write_json(HISTORY_FILE, h[:HISTORY_MAX])


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj, mode=0o600):
    """原子写 JSON。默认 600：经此函数落盘的文件多含订阅/节点等敏感信息，路由器本地非 root 用户不应可读"""
    _atomic_write(path, json.dumps(obj, ensure_ascii=False).encode(), mode)


def safe(name):
    return re.sub(r"[^\\w\\-]", "_", name) or "sub"


def rs_file(name):
    """规则集缓存文件名主体：safe(名) + 名字的短哈希。以前只用 safe(名)，名字只差符号的两个规则集
    （「国外.规则」与「国外_规则」）共用一个缓存文件，后下载的覆盖先下载的，两条规则按同一份列表分流"""
    return f"{safe(name)}_{hashlib.sha1(name.encode('utf-8')).hexdigest()[:8]}"

'''
_SRC['shunt.links'] = '''"""单节点链接解析"""
import base64, json, re
from urllib.parse import parse_qs, unquote, urlsplit

from .base import (
    AUTO_SUFFIX, AUTO_TAIL_OLD, BUILTIN_POLICIES, G_AUTO, G_DIRECT, G_MANUAL, G_OTHER, G_SEL, LB_SUFFIX, LEGACY_AUTO, REGIONS, SIDE_GROUPS
)
from .data import auto_name, lb_dropped_names, lb_names, lb_old_name
def b64d(s):
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s).decode("utf-8", "ignore")


def q1(qs, *keys, default=""):
    for k in keys:
        if qs.get(k) and qs[k][0] != "":
            return qs[k][0]
    return default


def truthy(v):
    return str(v).lower() in ("1", "true", "yes")


def host_port(netloc, default_port=None):
    """返回 (host, port_spec)，兼容 [IPv6]:port 与 hy2 的多端口 443,2000-3000。
    default_port：协议规定端口可省略时（hysteria2 默认 443）没写端口用它"""
    hp = netloc.rpartition("@")[2]
    if default_port:
        m = re.fullmatch(r"\\[([0-9A-Fa-f:.]+)\\]", hp) or re.fullmatch(r"([^:\\[\\]]+)", hp)
        if m:
            return m.group(1), str(default_port)
    m = re.fullmatch(r"\\[([0-9A-Fa-f:.]+)\\]:([\\d,\\-]+)", hp) or re.fullmatch(r"([^:\\[\\]]+):([\\d,\\-]+)", hp)
    if not m:
        raise ValueError("服务器地址或端口无效")
    return m.group(1), m.group(2)


def common_tls(p, qs, sni_key="servername"):
    sni = q1(qs, "sni", "peer", "serverName")
    if sni:
        p[sni_key] = sni
    alpn = q1(qs, "alpn")
    if alpn:
        p["alpn"] = [a for a in alpn.split(",") if a]
    fp = q1(qs, "fp")
    if fp:
        p["client-fingerprint"] = fp
    if truthy(q1(qs, "allowInsecure", "insecure", "allow_insecure", "skip-cert-verify")):
        p["skip-cert-verify"] = True


def transport(p, net, qs):
    net = (net or "tcp").lower()
    host, path = q1(qs, "host"), q1(qs, "path")
    if net in ("ws", "httpupgrade"):
        p["network"] = "ws"
        o = {"path": path or "/"}
        if host:
            o["headers"] = {"Host": host}
        if net == "httpupgrade":
            o["v2ray-http-upgrade"] = True
        p["ws-opts"] = o
    elif net == "grpc":
        p["network"] = "grpc"
        p["grpc-opts"] = {"grpc-service-name": q1(qs, "serviceName", "path")}
    elif net in ("h2", "http"):
        p["network"] = "h2"
        o = {"path": path or "/"}
        if host:
            o["host"] = host.split(",")
        p["h2-opts"] = o
    elif net == "tcp":
        if q1(qs, "headerType") == "http":
            p["network"] = "http"
            o = {"path": [path or "/"]}
            if host:
                o["headers"] = {"Host": host.split(",")}
            p["http-opts"] = o
    else:
        raise ValueError(f"不支持的传输方式 {net}")


def link_parts(line, first_port=False, default_port=None):
    """URL 形式的链接 → (查询参数, 主机, 端口, 名称, 用户名, 密码)；first_port：端口范围只取第一个"""
    u = urlsplit(line)
    host, port = host_port(u.netloc, default_port)
    if first_port:
        port = re.split(r"[,\\-]", port)[0]
    pw = unquote(u.password) if u.password is not None else None
    return parse_qs(u.query), host, port, unquote(u.fragment), unquote(u.username or ""), pw


def _secret(user, pw):
    """userinfo 整体作为密码：「pa:ss@host」里没转义的冒号属于密码本身"""
    return user + (":" + pw if pw is not None else "")


def parse_vmess(line):
    j = json.loads(b64d(line[8:].split("#")[0]))
    p = {"name": str(j.get("ps") or ""), "type": "vmess", "server": j["add"], "port": int(j["port"]),
         "uuid": j["id"], "alterId": int(j.get("aid") or 0), "cipher": j.get("scy") or "auto", "udp": True}
    if j.get("tls") in ("tls", True):
        p["tls"] = True
        sni = j.get("sni") or j.get("host")
        if sni:
            p["servername"] = sni
        if j.get("alpn"):
            p["alpn"] = str(j["alpn"]).split(",")
        if j.get("fp"):
            p["client-fingerprint"] = j["fp"]
        if truthy(j.get("allowInsecure") or j.get("skip-cert-verify") or ""):
            p["skip-cert-verify"] = True
    qs = {k: [str(j.get(f) or "")] for k, f in (("path", "path"), ("host", "host"), ("serviceName", "path"), ("headerType", "type"))}
    transport(p, j.get("net") or "tcp", qs)
    return p


def parse_ss(line):
    body, _, frag = line[5:].partition("#")
    if "@" in body:
        userinfo, _, rest = body.rpartition("@")
        userinfo = unquote(userinfo)
        if ":" not in userinfo:
            userinfo = b64d(userinfo)
    else:  # 旧格式 ss://base64(method:pass@host:port)
        b, sep, qpart = body.partition("?")
        dec = b64d(b.rstrip("/"))
        userinfo, _, hp = dec.rpartition("@")
        rest = hp + (sep + qpart if qpart else "")
    method, _, password = userinfo.partition(":")
    u = urlsplit("ss://x@" + rest)
    host, port = host_port(u.netloc)
    p = {"name": unquote(frag), "type": "ss", "server": host, "port": int(port), "cipher": method,
         "password": password, "udp": True}
    plugin = q1(parse_qs(u.query), "plugin")
    if plugin:
        parts = plugin.split(";")
        opts = dict((x.split("=", 1) + [""])[:2] for x in parts[1:])
        if parts[0] in ("obfs-local", "simple-obfs", "obfs"):
            p["plugin"] = "obfs"
            p["plugin-opts"] = {"mode": opts.get("obfs", "http"), "host": opts.get("obfs-host", "bing.com")}
        elif parts[0] == "v2ray-plugin":
            p["plugin"] = "v2ray-plugin"
            p["plugin-opts"] = {"mode": opts.get("mode", "websocket"), "host": opts.get("host", ""),
                                "path": opts.get("path", "/"), "tls": "tls" in opts}
        else:
            raise ValueError(f"不支持的 ss 插件 {parts[0]}")
    return p


def parse_vless(line):
    qs, host, port, name, user, pw = link_parts(line)
    p = {"name": name, "type": "vless", "server": host, "port": int(port), "uuid": user, "udp": True}
    sec = q1(qs, "security")
    if sec in ("tls", "reality", "xtls"):
        p["tls"] = True
        common_tls(p, qs)
    if sec == "reality":
        p["reality-opts"] = {"public-key": q1(qs, "pbk"), "short-id": q1(qs, "sid")}
        p.setdefault("client-fingerprint", "chrome")
    flow = q1(qs, "flow")
    if flow:
        p["flow"] = flow
    enc = q1(qs, "encryption")
    if enc and enc != "none":
        p["encryption"] = enc
    transport(p, q1(qs, "type"), qs)
    return p


def parse_trojan(line):
    qs, host, port, name, user, pw = link_parts(line)
    # 密码里的「:」没转义时 urlsplit 会把它拆成用户名 + 密码：以前只取冒号前半段，节点密码错误连不上
    p = {"name": name, "type": "trojan", "server": host, "port": int(port), "password": _secret(user, pw), "udp": True}
    common_tls(p, qs, "sni")
    transport(p, q1(qs, "type"), qs)
    return p


def parse_hysteria2(line):
    # hysteria2 URI 规范：端口可省略，默认 443（以前报「服务器地址或端口无效」）
    qs, host, port, name, user, pw = link_parts(line, default_port=443)
    auth = _secret(user, pw)
    first = re.split(r"[,\\-]", port)[0]
    p = {"name": name, "type": "hysteria2", "server": host, "port": int(first), "password": auth, "udp": True}
    if not port.isdigit():
        p["ports"] = port
    # 端口跳跃：hy2://…@host:443?mport=20000-30000（v2rayN / NekoBox 写法），主端口照旧用 443
    mport = re.sub(r"\\s+", "", q1(qs, "mport", "ports") or "")
    if mport and re.fullmatch(r"\\d+(?:[-:]\\d+)?(?:,\\d+(?:[-:]\\d+)?)*", mport):
        p["ports"] = mport.replace(":", "-")  # mihomo 认「a-b」，sing-box 那边再转成「a:b」
    hop = q1(qs, "hop_interval", "hopInterval", "hop-interval")
    if hop and re.match(r"\\s*\\d+", hop):
        p["hop-interval"] = int(re.match(r"\\s*(\\d+)", hop).group(1))
    common_tls(p, qs, "sni")
    p.pop("client-fingerprint", None)
    obfs = q1(qs, "obfs")
    if obfs and obfs != "none":
        p["obfs"] = obfs
        p["obfs-password"] = q1(qs, "obfs-password")
    return p


def parse_tuic(line):
    qs, host, port, name, user, pw = link_parts(line)
    p = {"name": name, "type": "tuic", "server": host, "port": int(port), "uuid": user, "password": pw or "",
         "udp": True, "alpn": ["h3"]}
    common_tls(p, qs, "sni")
    p.pop("client-fingerprint", None)
    p["congestion-controller"] = q1(qs, "congestion_control", "congestion-control", default="bbr")
    p["udp-relay-mode"] = q1(qs, "udp_relay_mode", "udp-relay-mode", default="native")
    if truthy(q1(qs, "disable_sni")):
        p["disable-sni"] = True
    return p


def parse_anytls(line):
    qs, host, port, name, user, pw = link_parts(line, first_port=True)
    p = {"name": name, "type": "anytls", "server": host, "port": int(port), "password": _secret(user, pw), "udp": True}
    common_tls(p, qs, "sni")
    return p


def parse_hysteria(line):
    qs, host, port, name, user, pw = link_parts(line, first_port=True)
    p = {"name": name, "type": "hysteria", "server": host, "port": int(port), "udp": True,
         "up": q1(qs, "upmbps", "up", default="10"), "down": q1(qs, "downmbps", "down", default="50")}
    auth = q1(qs, "auth", "auth_str") or user
    if auth:
        p["auth-str"] = auth
    if q1(qs, "obfsParam", "obfs-password", "obfs"):
        p["obfs"] = q1(qs, "obfsParam", "obfs-password", "obfs")
    common_tls(p, qs, "sni")
    p.pop("client-fingerprint", None)
    return p


def parse_socks(line):
    qs, host, port, name, user, pw = link_parts(line, first_port=True)
    if pw is None and user and ":" not in user:  # socks://base64(user:pass)@host:port
        try:
            user, _, pw = b64d(user).partition(":")
        except ValueError:
            pass
    p = {"name": name, "type": "socks5", "server": host, "port": int(port), "udp": True}
    if user:
        p["username"], p["password"] = user, pw or ""
    return p


LINK_PARSERS = {"vmess": parse_vmess, "ss": parse_ss, "vless": parse_vless, "trojan": parse_trojan, "hysteria2": parse_hysteria2,
                "hy2": parse_hysteria2, "tuic": parse_tuic, "anytls": parse_anytls, "hysteria": parse_hysteria, "hy": parse_hysteria,
                "socks": parse_socks, "socks5": parse_socks, "socks5h": parse_socks}


def parse_link(line):
    line = line.strip()
    scheme = line.split("://", 1)[0].lower()
    if scheme not in LINK_PARSERS:
        raise ValueError(f"不支持的链接类型 {scheme}://")
    return LINK_PARSERS[scheme](line)


def add_links(text, existing, d=None):
    """解析多行链接，返回 (新增节点列表, 错误列表)；节点名自动去重"""
    taken = {n["proxy"]["name"] for n in existing} | reserved_names(d)
    out, errs = [], []
    for i, line in enumerate([x.strip() for x in text.splitlines() if x.strip()], 1):
        try:
            p = parse_link(line)
            if not p.get("server") or not p.get("port"):
                raise ValueError("缺少服务器或端口")
            base = (p.get("name") or f"{p['type']}-{p['server']}:{p['port']}").strip()
            name, k = base, 2
            while name in taken:
                name, k = f"{base} ({k})", k + 1
            p["name"] = name
            taken.add(name)
            out.append({"link": line, "proxy": p})
        except Exception as e:
            errs.append(f"第 {i} 行：{e}")
    return out, errs


def reserved_names(d=None):
    regs = [r[0] for r in REGIONS] + [G_OTHER]
    out = {G_SEL, G_AUTO, G_MANUAL, G_DIRECT, LEGACY_AUTO, "GLOBAL"} | set(SIDE_GROUPS) | BUILTIN_POLICIES
    for r in regs:
        out |= {r, r + LB_SUFFIX, r + AUTO_SUFFIX, auto_name(r), auto_name(r, AUTO_TAIL_OLD), lb_old_name(r), *lb_names(r), *lb_dropped_names(r)}
    if d:
        out |= {g["name"] for g in d.get("custom_groups") or []}
    return out
'''
_SRC['shunt.ruleutil'] = '''"""规则工具（叶子模块）：从 mihomo.py / adblock.py 抽出的纯函数与常量。

只依赖 base、os、re；data.py、mihomo.py、adblock.py 都从这里导入，
打破 data→mihomo、mihomo→adblock 的循环。
"""
import ipaddress
import os
import re

from .base import AB_DIR

DOMAIN_RX = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9_](?:[a-z0-9_\\-]{0,61}[a-z0-9_])?\\.)+(?:[a-z]{2,63}|xn--[a-z0-9\\-]{1,59})$")


_DOTS = str.maketrans({"\\u3002": ".", "\\uff0e": ".", "\\uff61": "."})  # 中文句号「。」、全角「．」、半角「｡」都当成点


def idna_domain(x):
    """中文 / 国际化域名 → punycode（xn--…）：「中国.cn」→「xn--fiqs8s.cn」。
    两个内核和 DNS 收到的查询都是 punycode 形式，以前原样写进规则 / 广告拦截列表，永远命中不了。
    保留前缀「+.」「*.」「.」和通配标签（*、?）；纯 ASCII 原样返回（只转小写由调用方决定）；转换失败返回原值。
    先去掉首尾空白：以前「 中国.cn」把空格当成第一个标签的一部分编码，得到「xn-- -….cn」这种永远命中不了的值"""
    if not isinstance(x, str):
        return x
    x = x.strip()
    if x.isascii():
        return x
    s = x.translate(_DOTS)
    if s.isascii():
        return s
    out = []
    for lab in s.split("."):
        if lab.isascii():
            out.append(lab)
            continue
        try:
            out.append(lab.lower().encode("idna").decode("ascii"))
        except (UnicodeError, ValueError):
            return x
    return ".".join(out)


# 行内注释：规则后面跟「空白 + # 或 !」的部分（「DOMAIN,a.com,DIRECT # 说明」「||a.com^ ! 说明」）。
# 要求前面有空白：DOMAIN-REGEX 里的「(?!…)」、AdGuard 的「##」元素隐藏规则不受影响
INLINE_COMMENT_RX = re.compile(r"\\s+[#!].*$")
# 值本身是正则 / 路径 / 任意文本的规则类型：值里的「 #」「 !」是值的一部分（「DOMAIN-REGEX,^a #b$」
# 「PROCESS-PATH,C:\\\\My App !\\\\x.exe」），只去掉值后面（策略 / 参数部分）的注释
VERBATIM_TYPES = frozenset(("DOMAIN-REGEX", "PROCESS-PATH", "PROCESS-PATH-REGEX", "PROCESS-NAME", "PROCESS-NAME-REGEX",
                            "URL-REGEX", "USER-AGENT", "IN-NAME", "IN-USER"))


def _comment_floor(s):
    """注释最早可以从哪个位置开始：值原样保留的规则类型返回值结束处（第二个逗号；没有就是行尾，不去注释），
    逻辑规则返回匹配部分的「)」之后（子规则里可能有 DOMAIN-REGEX），其他返回 0"""
    t = s[1:].lstrip() if s.startswith("-") else s  # YAML 列表项「- DOMAIN-REGEX,…」
    off = len(s) - len(t)
    if LOGICAL_RX.match(t):
        e = _logical_end(t)
        return off + e + 1 if e >= 0 else 0
    i = t.find(",")
    if i < 0 or t[:i].strip().strip("'\\"").upper() not in VERBATIM_TYPES:
        return 0
    j = t.find(",", i + 1)
    return off + j if j >= 0 else len(s)


def strip_comment(line):
    """去掉行内注释，保留规则本身。整行注释（# / ! 开头）原样返回，由调用方跳过。
    DOMAIN-REGEX / PROCESS-PATH 等规则的值里的「 #」「 !」不当注释（以前正则 / 路径被截断，规则改了意思或配置加载失败）"""
    s = line.strip()
    if not s or s[0] in "#!":
        return s
    m = INLINE_COMMENT_RX.search(s, _comment_floor(s))
    return s[:m.start()] if m else s


def rule_policy_index(parts):
    i = len(parts) - 1
    while i > 0 and parts[i].strip().lower() in ("no-resolve", "src"):
        i -= 1
    return i


LOGICAL_RX = re.compile(r"^\\s*(?:AND|OR|NOT)\\s*,\\s*\\(", re.I)
RULE_FLAGS = ("no-resolve", "src")


def _logical_end(s):
    """逻辑规则「AND,(...)」里第一个「(」对应的「)」的位置；括号不配对返回 -1"""
    depth = 0
    for j in range(s.find("("), len(s)):
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return -1


def split_rule(rule):
    """规则 → (匹配部分, 策略, 末尾参数列表)。与 mihomo 一致：策略是去掉末尾 no-resolve / src 后的最后一项。
    逻辑规则同样按匹配部分的括号收尾来拆：以前按最后一个「)」拆，策略名本身带括号（如节点「HK(IPLC)」）时
    sing-box 把整条规则当成不支持跳过、mihomo 的失效策略检查也被绕过。逻辑规则没写策略时策略为空"""
    s = rule.strip()
    if LOGICAL_RX.match(s):
        e = _logical_end(s)
        if e < 0:
            return s, "", []
        tail = [x.strip() for x in s[e + 1:].split(",")[1:]] if s[e + 1:].lstrip().startswith(",") else []
        i = len(tail)
        while i > 0 and tail[i - 1].lower() in RULE_FLAGS:
            i -= 1
        return s[:e + 1], (tail[i - 1] if i else ""), tail[i:]
    parts = s.split(",")
    i = rule_policy_index(parts)
    return ",".join(parts[:i]), parts[i].strip() if i > 0 else "", [x.strip() for x in parts[i + 1:]]


# 「类型,值,策略」中值后面必须跟策略的规则类型（MATCH / 逻辑规则除外）
RULE_TYPES = frozenset("""DOMAIN DOMAIN-SUFFIX DOMAIN-KEYWORD DOMAIN-REGEX DOMAIN-WILDCARD GEOSITE GEOIP IP-CIDR IP-CIDR6 IP-SUFFIX
IP-ASN SRC-GEOIP SRC-IP-ASN SRC-IP-CIDR SRC-IP-SUFFIX DST-PORT SRC-PORT IN-PORT IN-TYPE IN-USER IN-NAME PROCESS-NAME
PROCESS-PATH PROCESS-NAME-REGEX PROCESS-PATH-REGEX UID NETWORK DSCP RULE-SET USER-AGENT URL-REGEX""".split())


def complete_rule(rule, default):
    """「DOMAIN-SUFFIX,a.com」「IP-CIDR,1.2.3.4/32,no-resolve」这类没写目标策略的规则补上 default。
    以前会把值（a.com）当成策略名：保存时报「策略不存在」，备份恢复 / 手改 data.json 的则被换成节点选择、值丢失"""
    s = rule.strip()
    if not s or s.startswith("#"):
        return rule
    parts = s.split(",")
    if parts[0].strip().upper() in RULE_TYPES and rule_policy_index(parts) <= 1 and len(parts) >= 2 and parts[1].strip():
        parts.insert(2, default)
        return ",".join(parts)
    return rule


# 规则（含逻辑规则的子规则）里域名类条件的值：「DOMAIN-SUFFIX,中国.cn」「(DOMAIN,例子.测试)」
_IDN_SUB_RX = re.compile(r"((?:^|\\()\\s*DOMAIN(?:-SUFFIX|-WILDCARD)?\\s*,\\s*)([^,()]+)", re.I)


def norm_rule(rule, policies=None):
    """规范化自定义规则里两种内核都会「静默不生效」的写法（v8.4.15）：
    - DOMAIN-SUFFIX 的值带前导「.」「+.」「*.」：mihomo 按字面匹配，永远命中不了（sing-box 只去掉了「.」，「+.」「*.」同样失效）
      → 去掉前缀（后缀规则本来就含域名自身及所有子域）
    - DOMAIN 的值以「+.」「.」开头：同样永不命中 → 改成 DOMAIN-SUFFIX
    - IP-CIDR / IP-CIDR6 / SRC-IP-CIDR 只写了单个 IP（没有 /前缀）：mihomo 整份配置校验失败（payloadRule error）
      → 补 /32 或 /128
    - 逻辑规则（AND / OR / NOT）末尾的「,no-resolve」「,src」：mihomo 把最后一项当策略，整份配置加载失败（策略 no-resolve 不存在）
      → 去掉（逻辑规则的 no-resolve 要写在子规则里，如 (IP-CIDR,1.2.3.0/24,no-resolve)）
    - 行内注释（「DOMAIN,a.com,DIRECT # 说明」）：去掉注释保留规则。以前注释被当成策略名的一部分，规则被换成节点选择
    - DOMAIN / DOMAIN-SUFFIX / DOMAIN-WILDCARD 的中文域名转成 punycode（两个内核按 xn-- 形式匹配）
    其余逻辑规则、注释、空行原样返回。
    policies：现有策略名集合。策略名本身带「 #」「 !」（节点「香港 #1」、组「Netflix !」）时整条按原样保留，
    不当成行内注释截掉（以前截成「香港」，策略不存在：保存报错 / 已保存的规则被换成节点选择）"""
    s = rule.strip()
    if not s or s.startswith("#"):
        return rule
    c = strip_comment(s)
    if c != s and not (policies and split_rule(s)[1] in policies):
        s = rule = c
    if not s.isascii():
        s = rule = _IDN_SUB_RX.sub(lambda m: m.group(1) + idna_domain(m.group(2)), s)
    if LOGICAL_RX.match(s):
        body, pol, flags = split_rule(s)
        return f"{body},{pol}" if flags and pol else rule
    parts = s.split(",")
    if len(parts) < 2:
        return rule
    typ, val = parts[0].strip().upper(), parts[1].strip()
    if typ == "DOMAIN" and val.startswith(("+.", ".")) and val.lstrip("+.").strip():
        parts[0], typ = "DOMAIN-SUFFIX", "DOMAIN-SUFFIX"
    if typ == "DOMAIN-SUFFIX":
        v = val
        while v.startswith(("+.", "*.", ".")):
            v = v[2:] if v[:2] in ("+.", "*.") else v[1:]
        if v and v != val:
            parts[1] = v
    elif typ in ("IP-CIDR", "IP-CIDR6", "SRC-IP-CIDR") and "/" not in val:
        try:
            ip = ipaddress.ip_address(val.strip("[]"))
            parts[1] = f"{ip}/{32 if ip.version == 4 else 128}"
        except ValueError:
            pass
    return ",".join(parts)


def dns_policy_of(server):
    _, sep, tail = server.partition("#")
    if not sep:
        return ""
    # "#节点" 或 "#节点&h3=true" 之类的参数
    return tail.split("&")[0] if "=" not in tail.split("&")[0] else ""


def _mh_domain(x):
    """mihomo 域名通配（+.a.com / *.a.com / a.*.com / img?.a.com）→ (sing-box 字段, 值)；中文域名转成 punycode"""
    x = idna_domain(x.strip()).lower()
    pre = ""
    if x.startswith("+."):  # 自身及所有子域
        x, pre = x[2:], r"(?:[^.]+\\.)*"
    elif x.startswith("."):  # 只匹配子域（mihomo 语义，不含自身）
        x, pre = x[1:], r"(?:[^.]+\\.)+"
    if "*" in x or "?" in x:  # 含通配：只能用正则，否则 domain_suffix 会把「stun.*.*」当成字面量，永远匹配不到
        # ? 匹配一个字符（mihomo DOMAIN-WILDCARD 语义，如 img?.a.com），以前被转义成字面量「?」，永远匹配不到
        return "domain_regex", "^" + pre + re.escape(x).replace(r"\\*", r"[^.]+").replace(r"\\?", r"[^.]") + "$"
    if pre:
        return "domain_suffix", x if pre.endswith("*") else "." + x
    return "domain", x


def _wildcard_regex(x):
    """DOMAIN-WILDCARD 的值 → sing-box domain_regex。mihomo component/wildcard 里「*」匹配任意个字符（含「.」），
    「?」匹配一个字符：*.google.com 也命中 a.b.google.com。以前借用 _mh_domain（「*」只算一级），sing-box 下多级子域不命中"""
    x = x.strip().lower()
    return "^" + re.escape(x).replace(r"\\*", ".*").replace(r"\\?", ".") + "$"


def _rs_tag(kind, name):
    return f"{kind}-{name}".lower() if kind in ("geosite", "geoip") else name


AB_ALLOW_SETS = ("ad-allow", "ad-allowimp")  # 放行名单：只出现在拦截规则的 NOT 里
# 只在规则层拦截、不进 DNS 层的广告拦截规则集：$important 要压过 @@ 例外，而 DNS 层（mihomo nameserver-policy /
# sing-box DNS 规则）先按 ad-allow 放行，表达不了「important 且不在 allowimp」。普通列表里也有这些域名，没被放行时照样在 DNS 层拦
AB_RULE_ONLY = ("ad-important",) + AB_ALLOW_SETS


def adblock_providers(d):
    ab = d.get("adblock") or {}
    if not ab.get("enabled"):
        return {}, []
    prov = {}

    def add(name, fn):
        path = os.path.join(AB_DIR, fn)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            prov[name] = {"type": "file", "behavior": "domain", "format": "text", "path": "./adblock/" + fn}
    add("ad-allow", "allow.txt")
    add("ad-important", "important.txt")  # 列表里的 $important 拦截：普通 @@ 例外压不住
    if "ad-important" in prov:
        add("ad-allowimp", "allow-important.txt")  # 能压过 $important 的放行：用户白名单 + @@…$important
    add("ad-custom", "custom.txt")
    add("ad-customcn", "custom-cn.txt")  # 自定义黑名单（只拦国内直连）
    add("ad-customproxy", "custom-proxy.txt")  # 自定义黑名单（只拦国外代理）
    for l in ab.get("lists", []):
        if l.get("enabled", True) and re.fullmatch(r"[0-9a-f]{8}", l.get("id", "")):
            add("ad-" + l["id"], l["id"] + ".txt")
    scopes = adblock_scopes(d)
    rules = []
    for k in prov:
        if k in AB_ALLOW_SETS:
            continue
        conds = [f"(RULE-SET,{k})"]
        allow = "ad-allowimp" if k == "ad-important" else "ad-allow"
        if allow in prov:
            conds.append(f"(NOT,((RULE-SET,{allow})))")
        if k in scopes:  # 限定范围的列表：只拦国内直连（geosite:cn）或只拦国外代理（非 geosite:cn）的域名
            conds.append(AB_SCOPE_COND[scopes[k]])
        rules.append(f"AND,({','.join(conds)}),REJECT" if len(conds) > 1 else f"RULE-SET,{k},REJECT")
    return prov, rules


# 广告拦截列表的生效范围：all 全部流量；cn 只拦国内直连的域名（geosite:cn）；proxy 只拦国外代理的域名（非 geosite:cn）。
# 限定范围的列表只在规则层拦截：mihomo 的 nameserver-policy 表达不了「同时属于 A 和 B」，两个核心统一不进 DNS 层
AB_SCOPES = ("all", "cn", "proxy")
AB_SCOPE_COND = {"cn": "(GEOSITE,cn)", "proxy": "(NOT,((GEOSITE,cn)))"}


def adblock_scopes(d):
    """{"ad-<id>": "cn" | "proxy"}：限定了范围的已启用列表；不在其中的列表对全部流量生效"""
    out = {"ad-customcn": "cn", "ad-customproxy": "proxy"}  # 分范围的自定义黑名单
    for l in (d.get("adblock") or {}).get("lists", []):
        if isinstance(l, dict) and l.get("scope") in AB_SCOPE_COND and re.fullmatch(r"[0-9a-f]{8}", str(l.get("id", ""))):
            out["ad-" + l["id"]] = l["scope"]
    return out
'''
_SRC['shunt.groups'] = '''"""策略组"""
import json, os, re, time

from .errlog import log_err
from .base import GROUPS_DEFAULT, GROUP_TYPES, G_OTHER, LB_STRATEGIES, PANEL_DIR, PRIMARY_REGIONS, REGIONS
from .corehttp import core_json
from .data import load, read_json, safe, write_json
from .links import reserved_names
from .sb_common import SB_SUB_DIR, active_core
PROV_FILE = os.path.join(PANEL_DIR, "provider_nodes.json")
PROV = {"t": 0, "data": None, "rev": 0, "pending": set()}  # pending：mihomo 里还没加载完的订阅名  # rev：订阅节点名单每变化一次加一（/api/state 的策略组缓存键、/api/ping 版本号用）
RX_CACHE = {}


def rx(pattern):
    """编译正则（缓存）；无效时返回 None"""
    if pattern not in RX_CACHE:
        try:
            RX_CACHE[pattern] = re.compile(pattern)
        except re.error:
            RX_CACHE[pattern] = None
    return RX_CACHE[pattern]


def sub_cache_path(name):
    return os.path.join(SB_SUB_DIR, safe(name) + ".json")


def sb_sub_cache(name):
    return read_json(sub_cache_path(name), {}) or {}


def _file_sig(path):
    try:
        st = os.stat(path)
        return st.st_mtime_ns, st.st_size, st.st_ino
    except OSError:
        return None


_SUB_META = {}   # {订阅名: (文件签名, {"updated", "info"})}
_PN_CACHE = {}   # {"v": (键, {订阅名: [节点名]})}


def sb_sub_meta(name):
    """订阅缓存里的更新时间与流量信息（按文件修改时间缓存）：sing-box 下每次 /api/px 都要，不必每次解析整份节点缓存"""
    path = sub_cache_path(name)
    sig = _file_sig(path)
    c = _SUB_META.get(name)
    if c and sig is not None and c[0] == sig:
        return c[1]
    j = sb_sub_cache(name)
    meta = {"updated": j.get("updated"), "info": j.get("info") or {}}
    if sig is not None:
        _SUB_META[name] = (sig, meta)
    return meta


def sb_provider_nodes(d):
    """{订阅名: [节点名]}，名称已按全局去重（与生成的 sing-box 配置一致）。
    结果按「各订阅缓存文件签名 + 订阅名 / 已删节点 + 本地节点名 + 保留名」缓存：sing-box 下每次 /api/px、/api/subs/nodes
    都要算一遍，以前每次都把全部订阅缓存（常有几百 KB～数 MB）重新读取解析"""
    try:
        key = json.dumps([[s["name"], sorted(sub_removed(s)), _file_sig(sub_cache_path(s["name"]))] for s in d["subs"]]
                         + [sorted(n["proxy"]["name"] for n in d.get("nodes", [])), sorted(reserved_names(d))], ensure_ascii=False, default=str)
    except Exception:
        key = None
    c = _PN_CACHE.get("v")
    if key is not None and c and c[0] == key:
        return {k: list(v) for k, v in c[1].items()}
    out = {k: [n for n, _ in v] for k, v in sb_sub_outbounds(d).items()}
    if key is not None:
        _PN_CACHE["v"] = (key, out)
    return {k: list(v) for k, v in out.items()}


def sb_sub_outbounds(d):
    """{订阅名: [(去重后的名称, 节点)]}；用户从订阅里删除的节点（removed，按原名精确匹配）在这里跳过，不用重新下载就生效"""
    taken = {n["proxy"]["name"] for n in d.get("nodes", [])} | reserved_names(d)
    out = {}
    for s in d["subs"]:
        lst = []
        gone = set(sub_removed(s))
        for p in sb_sub_cache(s["name"]).get("nodes") or []:
            if str(p.get("name") or "") in gone:
                continue
            base = str(p.get("name") or "").strip() or "node"
            name, k = base, 2
            while name in taken:
                name, k = f"{base} ({k})", k + 1
            taken.add(name)
            lst.append((name, p))
        out[s["name"]] = lst
    return out


def provider_nodes(fresh=False):
    """订阅（proxy-provider）里的节点名 {订阅名: [节点名]}：优先读运行中的核心，缓存到文件供核心未运行时生成配置"""
    now = time.time()
    if not fresh and PROV["data"] is not None and now - PROV["t"] < 60:
        return PROV["data"]
    data = None
    if active_core() == "singbox":  # sing-box 的订阅由面板下载解析，节点名直接取缓存
        data = sb_provider_nodes(load())
        prov_set(now, data)
        return data
    j = None if os.environ.get("PANEL_OFFLINE") else core_json("/providers/proxies", timeout=3)
    if j is not None:
        data = {}
        pending = set()  # 还没加载完的订阅
        cache = read_json(PROV_FILE, None)
        old = cache if isinstance(cache, dict) else {}
        provs = j.get("providers") if isinstance(j, dict) else None
        for name, p in (provs if isinstance(provs, dict) else {}).items():
            if not isinstance(p, dict) or p.get("vehicleType") not in ("HTTP", "File", "Inline") or p.get("proxies") is None:
                continue
            if not prov_loaded(p):
                pending.add(name)
                # mihomo 刚重启、订阅还没从缓存文件 / 网络加载完：节点是空列表，不是「订阅没节点」。以前照样当成空订阅，
                # 刷新地区分组时把各地区组全删了（组里的选择也丢了），等加载完才又加回来。沿用上次已知的名单，没有就当作未知
                if isinstance(old.get(name), list):
                    data[name] = old[name]
                continue
            data[name] = [x.get("name") for x in p["proxies"] if isinstance(x, dict) and x.get("name")]
        try:
            subs = [s["name"] for s in load().get("subs") or []]
        except Exception:
            subs = []
        for name in subs:  # 核心还没应用配置（只有 default provider）：订阅不在结果里，同样沿用上次的名单
            if name not in data and isinstance(old.get(name), list):
                data[name] = old[name]
            if name not in (provs if isinstance(provs, dict) else {}):
                pending.add(name)
        PROV["pending"] = pending
        if data != cache:
            try:
                write_json(PROV_FILE, data)
            except OSError as e:
                log_err("保存订阅节点列表", e)
    if data is None:
        data = read_json(PROV_FILE, {})
    prov_set(now, data)
    return data


def prov_loaded(p):
    """mihomo /providers/proxies 里的一个 provider 是否已加载：有节点，或 updatedAt 不是零值（Go 零时间 0001-01-01）。
    刚启动、还没从缓存文件 / 网络加载完（或首次下载失败、没有缓存）时节点为空且 updatedAt 是零值"""
    if p.get("vehicleType") == "Inline" or p.get("proxies"):
        return True
    ua = str(p.get("updatedAt") or "")
    return bool(ua) and not ua.startswith("0001-")


def prov_set(now, data):
    """更新订阅节点名单缓存；名单有变化时 rev 加一。直接 PROV.update 不会推进 rev（以前 sing-box 更新订阅后策略组名单一直是旧的）"""
    if PROV["data"] is not None and data != PROV["data"]:
        PROV["rev"] += 1
    PROV.update(t=now, data=data)


def provider_rev():
    """订阅节点名单的版本：节点增删 / 改名后变化"""
    return PROV["rev"]


def gcfg(d):
    gc = dict(GROUPS_DEFAULT)
    gc.update({k: v for k, v in (d.get("groups_cfg") or {}).items() if k in GROUPS_DEFAULT})
    gc["type"] = "url-test"  # v6：地区「自动优选」组固定为 url-test（旧的地区主组类型设置不再使用）
    gc["strategy"] = "consistent-hashing"  # 只支持一致性哈希
    return gc


def group_opts(t, gc, extra=None):
    """各类型策略组的公共参数"""
    e = extra or {}
    g = {"type": t}
    if t != "select":
        g.update({"url": e.get("url") or gc["url"], "interval": int(e.get("interval") or gc["interval"]), "lazy": bool(gc["lazy"])})
        if t in ("url-test", "smart"):
            g["tolerance"] = int(e.get("tolerance") if e.get("tolerance") not in (None, "") else gc["tolerance"])
        if t == "load-balance":
            st = e.get("strategy")
            g["strategy"] = st if st in LB_STRATEGIES else "consistent-hashing"
        if t == "smart":  # Smart 内核：按延迟 / 丢包 / 速度综合打分，可用 LightGBM 模型预测权重
            g.update({"uselightgbm": bool(gc.get("lgbm")), "collectdata": False})
    return g


def region_plan(d, names, use):
    """返回 [(地区名, 正则或 None, exclude 正则或 None, 手动节点, 节点总数, 是否未知)]，只保留确实有节点的地区"""
    if not (use or names) or not d.get("region_groups", True):
        return []
    gc = gcfg(d)
    pn = provider_nodes() if use else {}
    subs = [s["name"] for s in d["subs"]]
    unknown = any(s not in pn for s in subs)
    pnames = [n for s in subs for n in pn.get(s, [])]
    regs = REGIONS if gc["extra"] else REGIONS[:PRIMARY_REGIONS]
    out = []
    for i, (rname, flt) in enumerate(regs):
        r = rx(flt)
        matched = [n for n in names if r.search(n)]
        total = len(matched) + sum(1 for n in pnames if r.search(n))
        if total or (unknown and i < PRIMARY_REGIONS):  # 订阅还没加载过：沿用旧行为，先生成 5 个常用地区
            out.append((rname, flt, None, matched, total, unknown))
    if gc["other"]:
        alls = [rx(f) for _, f in regs]
        matched = [n for n in names if not any(r.search(n) for r in alls)]
        total = len(matched) + sum(1 for n in pnames if not any(r.search(n) for r in alls))
        if total:
            out.append((G_OTHER, None, "|".join(f for _, f in regs), matched, total, unknown))
    return out


def all_node_names(d):
    """手动节点 + 已知订阅节点"""
    pn = provider_nodes() if d["subs"] else {}
    return [n["proxy"]["name"] for n in d.get("nodes", [])] + [n for s in d["subs"] for n in pn.get(s["name"], [])]


NAME_BAD = re.compile(r"[,#\\n\\r\\t\\"]")
NODE_NAME_MAX = 300  # 订阅节点名长度上限（removed / sub_nodes 校验）
NODE_LIST_MAX = 5000  # 一个订阅的已删节点 / 一个策略组勾选的订阅节点最多这么多条


def sub_removed(s):
    """订阅里被用户删除的节点名（原名，精确匹配）；字段缺失或格式不对时为空（兼容旧 data.json）"""
    v = s.get("removed") if isinstance(s, dict) else None
    return [x for x in v if isinstance(x, str) and x] if isinstance(v, list) else []


def node_name_ok(n):
    return isinstance(n, str) and 0 < len(n) <= NODE_NAME_MAX and "\\n" not in n and "\\r" not in n


def clean_removed(v, label="已删除节点"):
    """校验并规范化订阅的 removed：文本列表，去重，保持顺序"""
    if v is None:
        return []
    if not isinstance(v, list):
        raise ValueError(f"{label}应为列表")
    out = []
    for x in v:
        if not node_name_ok(x):
            raise ValueError(f"{label}里有无效的节点名（应为 1–{NODE_NAME_MAX} 字的文本）")
        if x not in out:
            out.append(x)
    if len(out) > NODE_LIST_MAX:
        raise ValueError(f"{label}过多（最多 {NODE_LIST_MAX} 个）")
    return out


def clean_sub_nodes(v):
    """自定义组勾选的订阅节点 [{sub, name}]：校验、去重、保持顺序"""
    if v is None:
        return []
    if not isinstance(v, list):
        raise ValueError("勾选的订阅节点应为列表")
    out, seen = [], set()
    for x in v:
        if not isinstance(x, dict) or not isinstance(x.get("sub"), str) or not x["sub"].strip() or not node_name_ok(x.get("name")):
            raise ValueError("勾选的订阅节点格式不正确（需要订阅名和节点名）")
        k = (x["sub"].strip(), x["name"])
        if k not in seen:
            seen.add(k)
            out.append({"sub": k[0], "name": k[1]})
    if len(out) > NODE_LIST_MAX:
        raise ValueError(f"勾选的订阅节点过多（最多 {NODE_LIST_MAX} 个）")
    return out


def names_rx(names):
    """精确匹配这些节点名的正则 ^(?:a|b)$：名字里的 [ ] ( ) | . + 空格等全部转义；
    反引号写成 \\\\x60（mihomo 把 filter / exclude-filter 里的反引号当作多条正则的分隔符）"""
    return "^(?:" + "|".join(re.escape(n).replace("`", r"\\x60") for n in names) + ")$"


def rx_or(a, b):
    """两条正则取并集（命中任一条即可）。含反引号的是 mihomo 的多条正则写法，用反引号连接，语义同样是并集"""
    if not a:
        return b or ""
    if not b:
        return a
    if "`" in a or "`" in b:
        return f"{a}`{b}"
    return f"(?:{a})|{b}"


def sub_orig_nodes(d):
    """{订阅名: 节点原名集合}，只含当前确实可用的节点（已删除的不算）；某订阅还不知道节点（没下载过 / 核心没加载）时不在结果里。
    sing-box：面板缓存里的原名；mihomo：核心 / 文件缓存里的 provider 节点名（provider 不改名，就是原名）"""
    out = {}
    if active_core(d) == "singbox":
        for s in d["subs"]:
            c = sb_sub_cache(s["name"])
            if c.get("updated") or c.get("nodes"):
                gone = set(sub_removed(s))
                out[s["name"]] = {str(p.get("name") or "") for p in c.get("nodes") or []} - gone
        return out
    pn = provider_nodes()
    for s in d["subs"]:
        if s["name"] in pn:
            out[s["name"]] = set(pn[s["name"]] or []) - set(sub_removed(s))
    return out


def pick_plan(d, cg, orig=None):
    """自定义组勾选的订阅节点里还有效的部分 → {订阅名: [节点原名]}（按订阅顺序）。
    订阅已删除、节点已从订阅删除、或订阅里已经没有这个节点（已知节点名单时）都跳过，不让整份配置加载失败"""
    sn = cg.get("sub_nodes") or []
    if not sn:
        return {}
    if orig is None:
        orig = sub_orig_nodes(d)
    subs = {s["name"]: s for s in d["subs"]}
    want = {}
    for x in sn:
        s = subs.get(x["sub"])
        if not s or x["name"] in sub_removed(s):
            continue
        if x["sub"] in orig and x["name"] not in orig[x["sub"]]:
            continue
        lst = want.setdefault(x["sub"], [])
        if x["name"] not in lst:
            lst.append(x["name"])
    return {s["name"]: want[s["name"]] for s in d["subs"] if s["name"] in want}


def clean_custom_group(b):
    """校验并规范化一个自定义策略组（不含引用检查）"""
    name = str(b.get("name") or "").strip()
    if not name or len(name) > 40 or NAME_BAD.search(name):
        raise ValueError("名称不能为空、不超过 40 字，且不能包含逗号、# 或引号")
    t = b.get("type") or "select"
    if t not in GROUP_TYPES:
        raise ValueError("类型只能是 select / url-test / fallback / load-balance")
    members = []
    for m in b.get("proxies") or []:
        m = str(m).strip()
        if m and m not in members:
            members.append(m)
    flt = str(b.get("filter") or "").strip()
    if flt and rx(flt) is None:
        raise ValueError(f"筛选正则无效：{flt}")
    sub_nodes = clean_sub_nodes(b.get("sub_nodes"))
    if not members and not flt and not sub_nodes:
        raise ValueError("请至少选择一个成员（策略组 / 手动节点 / 订阅节点），或填写筛选正则")
    icon = str(b.get("icon") or "").strip()
    if icon and not re.match(r"^https?://\\S{4,500}$", icon):
        raise ValueError("图标必须是 http(s) 图片地址")
    g = {"name": name, "type": t, "proxies": members, "filter": flt, "subs": bool(b.get("subs", True)),
         "expose": bool(b.get("expose", True)), "icon": icon, "sub_nodes": sub_nodes}
    if t != "select":
        try:
            iv = int(b.get("interval") or 0)
            # 容差留空 = 跟随全局（策略组设置里的容差）；以前留空存成 0，全局改成 50 后自定义组仍按 0 频繁切换节点
            tol = None if b.get("tolerance") in (None, "") else int(b.get("tolerance"))
        except (TypeError, ValueError):
            raise ValueError("测速间隔 / 容差必须是数字")
        if iv and not 30 <= iv <= 86400:
            raise ValueError("测速间隔应在 30–86400 秒之间")
        if tol is not None and not 0 <= tol <= 1000:
            raise ValueError("容差应在 0–1000 ms 之间")
        g.update(interval=iv, tolerance=tol)
        if t == "load-balance":
            st = b.get("strategy") or "consistent-hashing"
            if st not in LB_STRATEGIES:
                raise ValueError("负载均衡策略只支持一致性哈希 / 轮询")
            g["strategy"] = st
    return g
'''
_SRC['shunt.mihomo'] = '''"""生成配置"""
import ipaddress, json, os, re, threading

from .base import (
    AUTO_ORDER, BUILTIN_POLICIES, CN_APP_DOMAINS, CONF_DIR, CTRL_HOST, CTRL_PORT, DEFAULT_EXCLUDE, DNS_DEFAULT, DNS_LISTS, DOH_DOMAINS, DOH_IPS, G_AI,
    G_AUTO, G_DIRECT, G_FINAL, G_GG, G_MANUAL, G_NF, G_SEL, G_TG, G_YT, HC, LAN_IN, LB_ORDER, LGBM_URL, MIHOMO_BIN, MIXED, SIDE_GROUPS, SMART_BIN,
    TUN_DEFAULT, TUN_STACKS
)
from .data import LB_KINDS, auto_name, lb_name, region_label, rs_file, safe
from .groups import clean_custom_group, gcfg, group_opts, names_rx, pick_plan, region_plan, rx, rx_or, sub_orig_nodes, sub_removed
from .ruleutil import AB_RULE_ONLY, DOMAIN_RX, LOGICAL_RX, adblock_providers, adblock_scopes, complete_rule, dns_policy_of, norm_rule, \\
    rule_policy_index, split_rule
from .sb_common import active_core
from .sysopt import sh


def drop_provider_cache(name):
    """删掉订阅在 mihomo 里的本地缓存 ./providers/<名>.yaml。
    mihomo 加载 provider 时只要本地文件存在就先用它，按文件修改时间等满一个更新间隔才去下载，不看地址变没变：
    改了订阅链接（换机场 / 重置 token）或删掉后用同名重新添加，以前要等到间隔（默认 24 小时）才换成新链接的节点"""
    try:
        os.remove(os.path.join(CONF_DIR, "providers", safe(name) + ".yaml"))
        return True
    except OSError:
        return False


def drop_ruleset_cache(name):
    """删掉规则集在 mihomo 里的本地缓存 ./rules/<名>.<yaml|mrs|list>。与 drop_provider_cache 同理：mihomo 加载 rule-provider 时
    只要本地文件存在就先用它，满一个更新间隔（24 小时）才去下载，不看地址变没变。同名替换规则集（换了地址 / 类型 / 格式）后
    以前最长 24 小时仍按旧地址的列表分流。旧版文件名（不带哈希）一并删掉"""
    n = 0
    for stem in (rs_file(name), safe(name)):
        for ext in RS_MH_EXTS:
            try:
                os.remove(os.path.join(CONF_DIR, "rules", f"{stem}.{ext}"))
                n += 1
            except OSError:
                pass
    return n > 0


RS_MH_EXTS = ("yaml", "mrs", "list")  # mihomo 规则集缓存 ./rules/<名>.<扩展名>
RS_SB_EXTS = ("srs", "json", "json.skip")  # sing-box 规则集文件 rs-<名>.<扩展名>（.skip 是转换时跳过条目的记录）


_RS_MIGRATE_LOCK = threading.Lock()  # 迁移不能与自己并发（保存、热重载、sing-box 下载、启动时都会调用）
_RS_NEW_RX = re.compile(r"_[0-9a-f]{8}$")  # rs_file 的文件名主体以「_ + 8 位哈希」结尾


def _move_noclobber(src, dst):
    """把 src 改名成 dst，但 dst 已存在时绝不覆盖（返回 False，src 留给调用方删）。
    os.replace 会无条件覆盖：迁移的同时 sing-box / mihomo 刚按新文件名下载好新内容，旧缓存会把它盖掉。
    硬链接是原子的「不存在才创建」；文件系统不支持硬链接时退回 O_EXCL 新建 + 复制"""
    try:
        os.link(src, dst)
    except FileExistsError:
        return False
    except OSError:
        try:
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        try:
            with os.fdopen(fd, "wb") as out, open(src, "rb") as f:
                out.write(f.read())
        except OSError:
            try:
                os.remove(dst)
            except OSError:
                pass
            raise
    os.remove(src)
    return True


def _rs_stem(fn, pre, exts):
    """缓存文件名 → 文件名主体；不是规则集缓存文件（前缀 / 扩展名不对、临时文件）返回 None"""
    if not fn.startswith(pre):
        return None
    stem, dot, ext = fn[len(pre):].partition(".")
    return stem if dot and stem and ext in exts else None


def migrate_ruleset_cache(d):
    """v8.10.22 起规则集缓存文件名带哈希（rs_file）。旧文件名 safe(名) 的缓存：
    - 只有一个规则集用这个旧名：改名成新文件名接着用（不用重新下载，与升级前内容一致）
    - 几个规则集只差符号、共用过这个旧名：分不清是谁的，删掉，各自按自己的地址重新下载
    - 新文件已存在（可能是核心刚下载的新内容）：绝不覆盖，旧文件直接删掉
    - 没有任何当前规则集认领的旧文件名缓存（规则集已删除等）：删掉。否则以后新建一个名字恰好等于这个旧文件名的
      规则集（「A.B」的旧文件名是「A_B」，再新建「A_B」）会把别人的旧列表当成自己的缓存沿用
    迁移本身加锁，不会两个线程同时改名同一批文件。返回处理的文件数"""
    names = [r["name"] for r in d.get("rulesets") or [] if isinstance(r, dict) and isinstance(r.get("name"), str)]
    cnt = {}
    for n in names:
        cnt[safe(n)] = cnt.get(safe(n), 0) + 1
    current = {rs_file(n) for n in names}
    from .sb_common import SB_RULE_DIR
    done = 0
    with _RS_MIGRATE_LOCK:
        for folder, pre, exts in ((os.path.join(CONF_DIR, "rules"), "", RS_MH_EXTS), (SB_RULE_DIR, "rs-", RS_SB_EXTS)):
            for n in names:
                old, new = safe(n), rs_file(n)
                for ext in exts:
                    src = os.path.join(folder, f"{pre}{old}.{ext}")
                    if not os.path.isfile(src):
                        continue
                    try:
                        if not (cnt[old] == 1 and _move_noclobber(src, os.path.join(folder, f"{pre}{new}.{ext}"))):
                            os.remove(src)
                        done += 1
                    except OSError:
                        pass
            try:
                files = os.listdir(folder)
            except OSError:
                continue
            for fn in files:
                stem = _rs_stem(fn, pre, exts)
                # 旧文件名：不是「_8 位哈希」结尾（旧名本身恰好这样结尾的分不出来，保留），也不是当前规则集的新文件名
                if stem is None or stem in current or _RS_NEW_RX.search(stem):
                    continue
                try:
                    os.remove(os.path.join(folder, fn))
                    done += 1
                except OSError:
                    pass
    return done


def build_config(d, strict=False):
    providers = {}
    for s in d["subs"]:
        pv = {
            "type": "http", "url": s["url"], "path": f"./providers/{safe(s['name'])}.yaml",
            "interval": int(d.get("sub_interval") or 86400),
            "health-check": {"enable": True, "url": HC, "interval": 300},
        }
        if s.get("filter"):
            pv["filter"] = s["filter"]
        exc = s.get("exclude", DEFAULT_EXCLUDE)
        gone = sub_removed(s)
        if gone:  # 用户从订阅里删除的节点：按原名精确排除，与订阅自己的排除正则取并集
            exc = rx_or(exc, names_rx(gone))
        if exc:
            pv["exclude-filter"] = exc
        providers[s["name"]] = pv
    use = list(providers)
    proxies = [n["proxy"] for n in d.get("nodes", [])]
    names = [p["name"] for p in proxies]
    gc = gcfg(d)
    race = "smart" if active_core(d) == "smart" else "url-test"  # 竞技组：mihomo 用 url-test，Smart 内核用 smart

    def with_src(g, members, src=True):
        if members:
            g["proxies"] = members
        if use and src:
            g["use"] = list(use)
        if not g.get("proxies") and not g.get("use"):
            g["proxies"] = ["COMPATIBLE"]
        return g

    # 地区分组：「<旗> <地区>竞技」(url-test，选延迟最低) + 「⚖️ <地区>哈希」(load-balance，一致性哈希)
    autos, lbs = [], []
    for rname, flt, exc, matched, total, unknown in region_plan(d, names, use):
        label = region_label(rname)[1]
        variants = [(auto_name(rname), race, autos, None)]
        if gc["lb"] and (total >= 2 or unknown):  # 地区有 ≥2 个节点时生成负载均衡组（一致性哈希）
            variants += [(lb_name(rname, st), "load-balance", lbs, {"strategy": st}) for st, _ in LB_KINDS]
        for gname, t, bucket, ex in variants:
            g = with_src(dict({"name": gname}, **group_opts(t, gc, ex)), matched)
            if use:
                if flt:
                    g["filter"] = flt
                if exc:
                    g["exclude-filter"] = exc
            bucket.append((label, g))

    def ordered(items, order):
        rank = {n: i for i, n in enumerate(order)}
        return [g for i, (label, g) in sorted(enumerate(items), key=lambda x: (rank.get(x[1][0], len(order)), x[0]))]
    region_groups = ordered(autos, AUTO_ORDER) + ordered(lbs, LB_ORDER)
    rnames = [g["name"] for g in region_groups]

    # 自定义策略组
    custom_groups, cnames, exposed = [], [], []
    region_set = set(rnames)
    orig = None
    for cg in d.get("custom_groups") or []:
        try:
            cg = clean_custom_group(cg)
        except ValueError:
            continue
        members = list(cg["proxies"])
        if cg["filter"]:
            r = rx(cg["filter"])
            members += [n for n in names if r.search(n) and n not in members]
        g = dict({"name": cg["name"]}, **group_opts(cg["type"], gc, cg))
        g["_members"] = members
        if cg["icon"]:
            g["icon"] = cg["icon"]
        uflt = cg["filter"] if cg["filter"] and cg["subs"] and use else ""
        picks = {}
        if cg["sub_nodes"] and use:
            if orig is None:
                orig = sub_orig_nodes(d)
            picks = pick_plan(d, cg, orig)
        if uflt or picks:
            # mihomo 的 proxies 里不能写订阅节点名：勾选的订阅节点用 use（只引用涉及的订阅）+ 精确匹配的 filter 表达；
            # filter 只作用于 use 引用的订阅，手动节点照常写在 proxies 里。同时有用户正则时两者取并集，用户正则原有的语义不变
            g["use"] = list(use) if uflt else [s for s in use if s in picks]
            g["filter"] = rx_or(uflt, names_rx([n for v in picks.values() for n in v]) if picks else "")
        custom_groups.append(g)
        cnames.append(cg["name"])
        if cg["expose"]:
            exposed.append(cg["name"])

    # 🚀 节点选择 只包含：各地区竞技 → 各地区负载均衡 → 🖐️ 手动选择 → ⚡ 全局自动选择 → 🏠 直连
    have = bool(use or names)
    direct_g = {"name": G_DIRECT, "type": "select", "proxies": ["DIRECT"]}
    if have:
        groups = [{"name": G_SEL, "type": "select", "proxies": rnames + [G_MANUAL, G_AUTO, G_DIRECT]}] + region_groups + [
            with_src({"name": G_MANUAL, "type": "select"}, names),
            with_src(dict({"name": G_AUTO}, **group_opts(race, gc)), names), direct_g]
        side = [G_SEL] + rnames + [G_MANUAL, G_AUTO] + exposed + ["DIRECT"] + names
    else:
        groups = [{"name": G_SEL, "type": "select", "proxies": [G_DIRECT]}, direct_g]
        side = [G_SEL] + exposed + ["DIRECT"]
    for g in SIDE_GROUPS:
        groups.append(with_src({"name": g, "type": "select"}, list(side)))
    known = {g["name"] for g in groups} | region_set | set(cnames) | BUILTIN_POLICIES | set(names)
    for g in custom_groups:
        members = g.pop("_members")
        if not strict:  # 成员已不存在（地区组消失、节点被删）时跳过，避免整份配置无法加载
            members = [m for m in members if m in known]
        if members:
            g["proxies"] = members
        if not g.get("proxies") and not g.get("use"):
            g["proxies"] = ["COMPATIBLE"]
        groups.append(g)
    policies = {g["name"] for g in groups} | BUILTIN_POLICIES | set(names)

    dc = dns_cfg(d)
    rule_providers, rules, ab_prov = mihomo_rules(d, policies, dc)
    v6 = bool(d.get("ipv6"))
    dns = mihomo_dns(d, dc, policies, ab_prov)
    hosts = mihomo_hosts(dc)
    tun = mihomo_tun(d)
    extra = {}
    if active_core(d) == "smart" and gcfg(d).get("lgbm"):  # 模型放在 -d 目录（/etc/mihomo/Model.bin），内核每 72 小时自动更新
        gh = (d.get("gh_proxy") or "").rstrip("/")
        extra = {"lgbm-auto-update": True, "lgbm-update-interval": 72, "lgbm-url": (gh + "/" if gh else "") + LGBM_URL}
    return dict(extra, **{
        "mixed-port": MIXED, "tproxy-port": 7893, "allow-lan": True, "bind-address": "*",
        "mode": d.get("mode", "rule"), "log-level": "info", "ipv6": v6,
        "external-controller": f"{CTRL_HOST}:{CTRL_PORT}", "secret": d["secret"],
        "unified-delay": True, "tcp-concurrent": True, "global-ua": "clash.meta",
        "geodata-mode": True, "geo-auto-update": True, "geo-update-interval": 24,
        "geox-url": {
            "geoip": "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geoip.dat",
            "geosite": "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geosite.dat",
            "mmdb": "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geoip.metadb",
        },
        "profile": {"store-selected": True, "store-fake-ip": True},
        # skip-domain：米家设备连云端的 TLS 握手里 SNI 是「Mijia Cloud」，嗅探后按这个假域名改写目标会连不上，设备频繁离线；
        # Apple 推送长连接被嗅探改写后会断开重连，消息推送延迟。两者都是 mihomo 官方示例里的跳过项
        "sniffer": {"enable": bool(d.get("sniffer", True)), "skip-domain": ["Mijia Cloud", "+.push.apple.com"], "sniff": {
            "HTTP": {"ports": [80, "8080-8880"], "override-destination": True},
            "TLS": {"ports": [443, 8443]}, "QUIC": {"ports": [443, 8443]}}},
        "dns": dns, "tun": tun, "hosts": hosts,
        "proxies": proxies,
        "proxy-providers": providers, "rule-providers": rule_providers, "proxy-groups": groups, "rules": rules,
    })

def build_rule_providers(d, dc):
    """rule-providers 段：规则集 + 广告拦截 + DNS 防绕过 → (rule-providers, 广告拦截 providers, 广告拦截 rules)。
    mihomo_rules 与 sing-box 轻量路径（sb_rule_need）共用，后者不再为取规则集需求表而跑完整 build_config。"""
    rule_providers = {}
    for r in d.get("rulesets", []):
        ext = {"mrs": "mrs", "text": "list"}.get(r.get("format"), "yaml")
        rule_providers[r["name"]] = {"type": "http", "behavior": r.get("behavior", "domain"), "format": r.get("format", "yaml"),
                                     "url": r["url"], "path": f"./rules/{rs_file(r['name'])}.{ext}", "interval": 86400}
    ab_prov, ab_rules = adblock_providers(d)
    rule_providers.update(ab_prov)
    rule_providers["cn-apps"] = {"type": "inline", "behavior": "domain", "payload": list(CN_APP_DOMAINS)}
    if dc["block_bypass"]:  # 阻止局域网设备绕过 mihomo DNS：拒绝 DoT/DoQ(853) 与已知公共 DoH 服务器
        rule_providers["dns-bypass-ip"] = {"type": "inline", "behavior": "ipcidr", "payload": list(DOH_IPS)}
        rule_providers["dns-bypass-domain"] = {"type": "inline", "behavior": "domain", "payload": list(DOH_DOMAINS)}
    return rule_providers, ab_prov, ab_rules


# 内置规则里的 GEOSITE/GEOIP（kind, 值）：sing-box 需为它们下载 .srs。
# 与下面 mihomo_rules 末尾硬编码列表保持一致；SBRules.use_geo 在翻译时把它们加入 [2]，sb_geo_rule_sets 显式计算供轻量路径使用。
# 注意 GEOIP,private 不会产生条目（翻译成 ip_is_private），这里保留仅作对照。
BUILTIN_GEO = (
    ("geosite", "private"), ("geoip", "private"),
    ("geosite", "category-ai-!cn"),
    ("geosite", "netflix"), ("geoip", "netflix"),
    ("geosite", "youtube"), ("geosite", "google"), ("geoip", "google"),
    ("geosite", "telegram"), ("geoip", "telegram"),
    ("geosite", "geolocation-!cn"), ("geosite", "cn"), ("geoip", "CN"),
)


NTP_RULE = "AND,((NETWORK,UDP),(DST-PORT,123)),DIRECT"


def mihomo_rules(d, policies, dc):
    """规则集、TUN 绕过、广告拦截、防绕过 DNS、自定义规则与内置分流规则 → (rule-providers, rules, 广告拦截 providers)"""
    rule_providers, ab_prov, ab_rules = build_rule_providers(d, dc)
    rs_rules = []
    for r in d.get("rulesets", []):
        tgt = r.get("target", G_SEL) if r.get("target", G_SEL) in policies else G_SEL
        rs_rules.append(f"RULE-SET,{r['name']},{tgt}" + (",no-resolve" if r.get("behavior") == "ipcidr" else ""))
    custom = [fix_rule_policy(x, policies) for x in d["rules"]]
    pre = []
    if d.get("proxy_mode") == "tun":  # TUN 模式下无法在 nftables 里按来源绕过，改用规则让这些设备直连
        for x in d.get("bypass", []):
            try:
                if "%" in x:  # 带网卡的地址生成的 SRC-IP-CIDR 内核不认（以前整份配置加载失败）
                    raise ValueError
                n = ipaddress.ip_network(x.strip(), strict=False)
                pre.append(f"SRC-IP-CIDR,{n},DIRECT")
            except ValueError:
                pass
    guard = []
    if dc["block_bypass"]:
        guard = [f"AND,(({LAN_IN}),(DST-PORT,853)),REJECT",
                 f"AND,(({LAN_IN}),(DST-PORT,443),(RULE-SET,dns-bypass-ip,no-resolve)),REJECT",
                 f"AND,(({LAN_IN}),(RULE-SET,dns-bypass-domain)),REJECT"]
    # NTP（UDP 123）始终直连：time.android.com 等是 Google 的 IP，以前会分到 Google 组走节点，
    # 节点不支持 UDP 时电视 / 盒子永远对不上时间，证书校验和 Google Play 安装前验证跟着失败（下载到 99% 卡住）
    rules = pre + guard + [NTP_RULE] + ab_rules + [x for x in custom if x] + rs_rules + [
        "GEOSITE,private,DIRECT", "GEOIP,private,DIRECT,no-resolve", "RULE-SET,cn-apps,DIRECT",
        f"GEOSITE,category-ai-!cn,{G_AI}", f"GEOSITE,netflix,{G_NF}", f"GEOIP,netflix,{G_NF},no-resolve",
        # 国行机的 Play 下载走 services.googleapis.cn，直连时被指到 Google 国内 CDN（*.xn--ngstr-lra8j.com），下载卡在 99%：两者走 Google 组
        f"DOMAIN-SUFFIX,googleapis.cn,{G_GG}", f"DOMAIN-SUFFIX,xn--ngstr-lra8j.com,{G_GG}",
        f"GEOSITE,youtube,{G_YT}", f"GEOSITE,google,{G_GG}", f"GEOIP,google,{G_GG},no-resolve",
        f"GEOSITE,telegram,{G_TG}", f"GEOIP,telegram,{G_TG},no-resolve",
        # Cloudflare 1.1.1.1 / 1.0.0.1 国内直连常被运营商干扰：固定走节点选择，不随漏网之鱼直连
        f"IP-CIDR,1.1.1.1/32,{G_SEL},no-resolve", f"IP-CIDR,1.0.0.1/32,{G_SEL},no-resolve",
        f"GEOSITE,geolocation-!cn,{G_SEL}", "DOMAIN-SUFFIX,dangbei.com,DIRECT",
        "GEOSITE,cn,DIRECT", "GEOIP,CN,DIRECT", f"MATCH,{G_FINAL}",
    ]
    return rule_providers, rules, ab_prov


def mihomo_dns(d, dc, policies, ab_prov):
    """dns 段：防泄露布局 + nameserver-policy"""
    v6 = bool(d.get("ipv6"))
    fx = lambda xs: [fix_dns_policy(x, policies) for x in xs]
    direct = fx(dc["direct"] or DNS_DEFAULT["direct"])
    proxy_dns = fx(dc["proxy"])
    plain_direct = [x.split("#", 1)[0] for x in direct]
    mode = dc["mode"]
    # 防泄露布局：默认 nameserver = 代理 DNS（经节点发出），只有 geosite:cn / private 用直连 DNS；
    # 节点域名由 proxy-server-nameserver 解析，DIRECT 连接由 direct-nameserver 解析；不使用 fallback
    dns = {
        "enable": True, "listen": "[::]:1053" if v6 else "0.0.0.0:1053", "ipv6": v6, "enhanced-mode": mode,
        "cache-algorithm": dc["cache"], "prefer-h3": False, "use-hosts": True, "use-system-hosts": False,
        "respect-rules": bool(dc["respect_rules"]),
        "default-nameserver": list(dc["default"] or DNS_DEFAULT["default"]),
        "nameserver": proxy_dns or direct,
        "proxy-server-nameserver": [x.split("#", 1)[0] for x in dc["pserver"]] or plain_direct,
        "direct-nameserver": plain_direct, "direct-nameserver-follow-policy": False,
    }
    if mode == "fake-ip":
        dns["fake-ip-range"] = "198.18.0.1/16"
        dns["fake-ip-filter"] = [x for x in dc["fake_filter"] if x.strip()]
        if v6:
            dns["fake-ip-range6"] = "fdfe:dcba:9876::1/64"
    pol = {}
    ab = d.get("adblock") or {}
    if ab.get("enabled") and ab.get("dns"):  # DNS 层拦截：白名单先走直连 DNS，其余命中拦截列表直接返回 NXDOMAIN
        if "ad-allow" in ab_prov:
            pol["rule-set:ad-allow"] = direct
        scoped = adblock_scopes(d)  # 限定国内直连 / 国外代理的列表只在规则层拦（nameserver-policy 表达不了交集）
        for k in ab_prov:
            if k not in AB_RULE_ONLY and k not in scoped:
                pol["rule-set:" + k] = "rcode://name_error"
    for pe in dc["policies"]:  # 用户自定义的 nameserver-policy（排在内置分流之前，JSON 保序，mihomo 按顺序匹配）
        if pe["match"] not in pol:
            pol[pe["match"]] = fx(pe["servers"])
    if dc["policy"] and proxy_dns:  # 国内域名走直连 DNS，其余（含未知域名）走代理 DNS
        pol.setdefault("rule-set:cn-apps", direct)
        pol.setdefault("geosite:cn,private", direct)
    if pol:
        dns["nameserver-policy"] = pol
    return dns


def mihomo_hosts(dc):
    hosts = {}
    for h in dc["hosts"]:
        vals = hosts.setdefault(h["domain"], [])
        vals += [v for v in h["value"] if v not in vals]
    hosts = {k: (v[0] if len(v) == 1 else v) for k, v in hosts.items()}
    return hosts


def tun_params(d):
    """TUN 参数归一化：mihomo_tun 与 sing-box sb_inbounds 共用同一决策（是否启用、协议栈回退、设备名、开关量）。"""
    tc = d.get("tun") or TUN_DEFAULT
    return {
        "enable": d.get("proxy_mode") == "tun",
        "stack": tc.get("stack") if tc.get("stack") in TUN_STACKS else TUN_DEFAULT["stack"],
        "device": safe_dev(tc.get("device")),
        "auto_redirect": bool(tc.get("auto_redirect", True)),
        "strict_route": bool(tc.get("strict_route", False)),
    }


def mihomo_tun(d):
    p = tun_params(d)
    if not p["enable"]:
        return {"enable": False}
    return {"enable": True, "stack": "mixed" if p["stack"] == "auto" else p["stack"], "device": p["device"], "auto-route": True,
            "auto-redirect": p["auto_redirect"], "auto-detect-interface": True, "dns-hijack": ["any:53", "tcp://any:53"],
            "strict-route": p["strict_route"]}



def fix_rule_policy(rule, policies):
    """自定义规则引用了不存在的策略组时改用节点选择，避免整份配置无法加载"""
    rule = complete_rule(norm_rule(rule.strip(), policies), G_SEL)
    if not rule or rule.startswith("#"):
        return None
    body, pol, flags = split_rule(rule)
    if LOGICAL_RX.match(rule):
        # 逻辑规则：策略同样是最后一项（可以带括号，如「HK(IPLC)」）。以前策略以「)」结尾的一律放行，
        # 删掉的节点 / 组「Gone(X)」仍被引用时 mihomo 整份配置加载失败；没写策略的补上节点选择；
        # 逻辑规则末尾的 no-resolve / src 由 norm_rule 去掉，这里不再拼回去（mihomo 会把它当成策略）
        return f"{body},{pol if pol in policies else G_SEL}"
    parts = rule.split(",")
    i = rule_policy_index(parts)
    if i > 0 and parts[i].strip() not in policies:
        parts[i] = G_SEL
    return ",".join(parts)


def safe_dev(name):
    name = re.sub(r"[^A-Za-z0-9_\\-]", "", str(name or ""))[:15]
    return name or "Meta"


DNS_RX = re.compile(r"^(?:(?:https|tls|quic|udp|tcp|h3)://[^\\s#]+|dhcp://[\\w.\\-]+|system(?:://)?|rcode://\\w+|"
                    r"\\[?[0-9a-fA-F:.]+\\]?(?::\\d+)?)(?:#\\S.*)?$")
HOST_KEY_RX = re.compile(r"^(?:\\+\\.|\\*\\.)?(?:[a-z0-9_*](?:[a-z0-9_\\-*]{0,61}[a-z0-9_*])?\\.)*[a-z0-9_\\-]{1,63}$")
POLICY_DOMAIN_RX = re.compile(r"^(?:\\+\\.|\\*\\.|\\.)?(?:[a-z0-9_*\\-]+\\.)*[a-z0-9_*\\-]+$", re.I)
POLICY_NAME_RX = re.compile(r"^[\\w@!\\-.]+$")


def norm_policy_match(m):
    """nameserver-policy 的键：geosite:a,b / rule-set:x,y / 域名1,域名2（mihomo 不支持在一个键里混用）"""
    parts = [x.strip() for x in str(m).split(",") if x.strip()]
    for pre in ("geosite:", "rule-set:"):
        if parts and parts[0].lower().startswith(pre):
            return pre + ",".join(x[len(pre):] if x.lower().startswith(pre) else x for x in parts)
    return ",".join(parts)


def policy_parts(m):
    """返回 (类型, [名称])：类型为 geosite / rule-set / domain"""
    for pre in ("geosite:", "rule-set:"):
        if m.lower().startswith(pre):
            return pre[:-1], m[len(pre):].split(",")
    return "domain", m.split(",")


def is_ip_addr(v):
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return False


_BARE_V6_RX = re.compile(r"^((?:udp|tcp|tls|https|quic|h3)://)?([0-9a-fA-F:]*:[0-9a-fA-F:]*:[0-9a-fA-F:.]*)(/[^#]*)?((?:#.*)?)$")


def norm_dns_addr(s):
    """不带方括号的 IPv6 DNS 地址（2400:3200::1、udp://2400:3200::1）补上方括号：
    以前原样写进配置，mihomo 解析不出端口拒绝启动，sing-box 配置生成直接抛异常（面板保存 DNS 后两个内核都起不来）"""
    m = _BARE_V6_RX.match(s)
    if m and is_ip_addr(m.group(2)):
        return f"{m.group(1) or ''}[{m.group(2)}]{m.group(3) or ''}{m.group(4)}"
    return s


DNS_SERVER_LISTS = ("direct", "proxy", "default", "pserver")


def dns_cfg(d):
    """data.json 里的 dns 设置补齐默认值并规范化（旧版数据没有的键用默认值）"""
    dc = dict(DNS_DEFAULT)
    dc.update(d.get("dns") or {})
    for k in DNS_LISTS:
        dc[k] = [str(x).strip() for x in (dc.get(k) if isinstance(dc.get(k), list) else DNS_DEFAULT[k]) if str(x).strip()]
        if k in DNS_SERVER_LISTS:
            dc[k] = [norm_dns_addr(x) for x in dc[k]]
    if dc.get("mode") not in ("fake-ip", "redir-host"):
        dc["mode"] = "fake-ip"
    if dc.get("cache") not in ("arc", "lru"):
        dc["cache"] = "arc"
    for k in ("policy", "respect_rules", "block_bypass"):
        dc[k] = bool(dc.get(k, DNS_DEFAULT[k]))
    hosts = []
    for h in dc.get("hosts") or []:
        if isinstance(h, dict) and h.get("domain") and h.get("value"):
            v = h["value"] if isinstance(h["value"], list) else str(h["value"]).replace(",", " ").split()
            hosts.append({"domain": str(h["domain"]).strip().lower(), "value": [str(x).strip() for x in v if str(x).strip()]})
    dc["hosts"] = hosts
    pols = []
    for pe in dc.get("policies") or []:
        if isinstance(pe, dict) and pe.get("match") and pe.get("servers"):
            sv = pe["servers"] if isinstance(pe["servers"], list) else str(pe["servers"]).split(",")
            pols.append({"match": norm_policy_match(pe["match"]),
                         "servers": [norm_dns_addr(str(x).strip()) for x in sv if str(x).strip()]})
    dc["policies"] = pols
    return dc


def fix_dns_policy(server, policies):
    """DNS 服务器后缀 #策略组 引用了不存在的组时改用节点选择"""
    server = server.strip()
    pol = dns_policy_of(server)
    if pol and pol not in policies:
        server = server.split("#", 1)[0] + "#" + G_SEL
    return server


def check_host_entry(h):
    dom, vals = h.get("domain", ""), h.get("value") or []
    if not HOST_KEY_RX.match(dom):
        return f"hosts 域名格式无效：{dom or '(空)'}（可用 example.com、+.example.com、*.example.com）"
    if not vals:
        return f"hosts「{dom}」没有填写 IP"
    for v in vals:
        if not is_ip_addr(v) and not (DOMAIN_RX.match(v.lower()) and not is_ip_addr(dom)):
            return f"hosts「{dom}」的值无效：{v}（应为 IP，或单个域名作为别名）"
    if len(vals) > 1 and not all(is_ip_addr(v) for v in vals):
        return f"hosts「{dom}」：别名域名只能填一个"
    return ""


def dns_port_ok(s):
    """DNS 地址里的端口是 1–65535 的数字、IPv6 方括号成对。DNS_RX 对带协议的地址只要求「非空白」：
    以前 1.1.1.1:99999、udp://1.1.1.1:abc、tls://[::1 都能保存，生成 sing-box 配置时 urlsplit 抛 ValueError（Go 生成坏端口），
    核心起不来、修改被回滚却只提示「生成配置失败」"""
    base = s.split("#", 1)[0].strip()
    if base.startswith(("system", "dhcp://", "rcode://")):
        return True
    host = re.sub(r"^[a-z0-9]+://", "", base).split("/", 1)[0]
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            return False
        tail = host[end + 1:]
        if not tail:
            return True
        if not tail.startswith(":"):
            return False
        port = tail[1:]
    elif ":" in host:
        if is_ip_addr(host):  # 裸 IPv6，不带端口
            return True
        h, _, port = host.rpartition(":")
        if ":" in h:
            return False
    else:
        return True
    return bool(re.fullmatch(r"[0-9]{1,5}", port)) and 0 < int(port) < 65536


def validate_dns(dc, d):
    pol = {g["name"] for g in build_config(dict(d, rules=[]))["proxy-groups"]} | BUILTIN_POLICIES | {n["proxy"]["name"] for n in d["nodes"]}
    errs = []
    for key, label in (("direct", "直连 DNS"), ("proxy", "代理 DNS"), ("default", "默认 DNS"), ("pserver", "节点域名解析 DNS")):
        for s in dc.get(key) or []:
            if not DNS_RX.match(s) or s.startswith("rcode://"):
                errs.append(f"{label} 格式无效：{s}")
            elif not dns_port_ok(s):
                errs.append(f"{label} 端口无效（应为 1–65535）：{s}")
            elif key == "default" and not re.match(r"^(?:(?:udp|tcp|tls|https)://)?\\[?[0-9a-fA-F:.]+\\]?(?::\\d+)?(?:/|$)", s):
                errs.append(f"默认 DNS 必须是 IP（用于解析 DoH 域名）：{s}")
            elif key in ("default", "pserver") and dns_policy_of(s):
                errs.append(f"{label} 不能指定 #策略组（解析节点域名本身不能经过节点）：{s}")
            elif dns_policy_of(s) and dns_policy_of(s) not in pol:
                errs.append(f"{label} 引用的策略组不存在：{s}")
    for s in dc.get("fake_filter") or []:
        if not re.match(r"^(?:geosite:|rule-set:)?[\\w*+.\\-!@:]+$", s):
            errs.append(f"Fake-IP 过滤格式无效：{s}")
    for h in dc.get("hosts") or []:
        e = check_host_entry(h)
        if e:
            errs.append(e)
    seen = set()
    for pe in dc.get("policies") or []:
        m = pe.get("match", "")
        kind, parts = policy_parts(m)
        for part in parts:
            if not (POLICY_NAME_RX if kind != "domain" else POLICY_DOMAIN_RX).match(part) or ":" in part:
                errs.append(f"nameserver-policy 匹配项无效：{part}（一条只能是 geosite:a,b、rule-set:x 或 域名列表 +.example.com,example.com，不能混用）")
        if m in seen:
            errs.append(f"nameserver-policy 重复：{m}")
        seen.add(m)
        if not pe.get("servers"):
            errs.append(f"nameserver-policy「{m}」没有填写 DNS 服务器")
        for s in pe.get("servers") or []:
            if not DNS_RX.match(s):
                errs.append(f"nameserver-policy「{m}」的 DNS 格式无效：{s}")
            elif not dns_port_ok(s):
                errs.append(f"nameserver-policy「{m}」的 DNS 端口无效（应为 1–65535）：{s}")
            elif dns_policy_of(s) and dns_policy_of(s) not in pol:
                errs.append(f"nameserver-policy「{m}」引用的策略组不存在：{s}")
    if not dc.get("direct"):
        errs.append("直连 DNS 不能为空")
    if not dc.get("default"):
        errs.append("默认 DNS 不能为空")
    return errs


def rule_policy_names(d):
    """自定义规则可以引用的策略名：策略组 + 内置策略 + 节点名"""
    return {g["name"] for g in build_config(dict(d, rules=[]))["proxy-groups"]} | BUILTIN_POLICIES | {n["proxy"]["name"] for n in d["nodes"]}


def bad_rule_policies(rules, d, pol=None):
    if pol is None:
        pol = rule_policy_names(d)
    bad = []
    for r in rules:
        _, p, _ = split_rule(r)  # 逻辑规则按匹配部分的括号收尾取策略（策略名可带括号）
        if not p or p not in pol:
            bad.append(r)
    return bad


def write_config(d, path=None):
    os.makedirs(os.path.join(CONF_DIR, "providers"), exist_ok=True)
    if path is None:
        migrate_ruleset_cache(d)
    path = path or os.path.join(CONF_DIR, "config.yaml")
    write_atomic(path, build_config(d))  # JSON 是 YAML 的子集，mihomo 可直接读取
    return path


def write_atomic(path, obj):
    """先生成内容、写临时文件再改名：生成配置抛异常或写到一半断电时，原来的配置文件保持完整。
    以前先 open(path, "w") 截断再生成，build 一出错（坏数据 / 回滚时旧数据也生成不了）正式配置就变成空文件，核心起不来"""
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    tmp = f"{path}.tmp{os.getpid()}-{threading.get_ident()}"
    try:
        with open(tmp, "w") as f:
            f.write(text)
        try:  # 沿用原文件权限（以前是原地覆盖，权限不变）
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def mh_bin(kind=None):
    """mihomo 系内核的程序路径：smart → mihomo-smart，其余 → mihomo"""
    return SMART_BIN if (kind or active_core()) == "smart" else MIHOMO_BIN


def check_config(path, kind=None):
    """用 mihomo -t 校验配置；没有核心程序时跳过"""
    b = mh_bin(kind)
    if not os.path.isfile(b):
        return True, ""
    code, out = sh(f"'{b}' -t -d '{CONF_DIR}' -f '{path}'", timeout=90)
    if code == 0:
        return True, ""
    lines = [l for l in out.splitlines() if "level=error" in l or "level=fatal" in l or "test failed" in l.lower() or l.startswith("panic:")]
    return False, "\\n".join(lines[-3:]) or out[-400:]

'''
_SRC['shunt.validate'] = '''"""/api/settings、/api/restore 的数据形状校验：残缺或类型不对的条目返回 400，不写进 data.json。

data.json 里一条坏数据（规则集缺 url、节点缺 proxy、绕过列表混入数字……）会让生成配置抛异常，
之后每次 /api/state 都 500，面板等于「变砖」。这里只查形状与取值范围，策略组引用等语义检查仍在各接口里做。"""
import ipaddress, re

from .base import DNS_LISTS, G_SEL, PROXY_MODES
from .data import safe
from .groups import NAME_BAD, clean_custom_group, clean_removed

RS_BEHAVIORS = ("domain", "ipcidr", "classical")
RS_FORMATS = ("yaml", "text", "mrs")
MAC_RX = re.compile(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")
URL_RX = re.compile(r"^https?://\\S+$")
LIST_MAX = 20000


def _list(v, label):
    if not isinstance(v, list):
        raise ValueError(f"{label}应为列表")
    if len(v) > LIST_MAX:
        raise ValueError(f"{label}条目过多")
    return v


def _str(v, label, empty=False):
    if not isinstance(v, str):
        raise ValueError(f"{label}应为文本")
    v = v.strip()
    if not v and not empty:
        raise ValueError(f"{label}不能为空")
    return v


def _url(v, label):
    v = _str(v, label)
    if not URL_RX.match(v):
        raise ValueError(f"{label}应为 http(s) 地址：{v[:80]}")
    return v


RS_RESERVED_RX = re.compile(r"^(?:cn-apps|dns-bypass-ip|dns-bypass-domain|ad-allow|ad-allowimp|ad-important|ad-custom|ad-customcn|ad-customproxy|ad-[0-9a-f]{8}|geosite-.*|geoip-.*)$", re.I)


def rs_name_reserved(name):
    """规则集名与内置 rule-provider（国内 App 名单、防绕过 DNS、广告拦截列表）或 sing-box 的 GEO 规则集 tag 同名：
    mihomo 里内置的那份覆盖用户的（规则集静默失效），sing-box 里 geosite-xx 会顶替真正的 GEO 规则集"""
    return bool(RS_RESERVED_RX.match(name))


def clean_rulesets(v):
    out, seen = [], set()
    for i, r in enumerate(_list(v, "规则集")):
        if not isinstance(r, dict):
            raise ValueError(f"规则集第 {i + 1} 项格式不正确")
        name = _str(r.get("name"), f"规则集第 {i + 1} 项的名称")
        if NAME_BAD.search(name) or len(name) > 60:
            raise ValueError(f"规则集名称不能含逗号、# 或引号，且不超过 60 字：{name}")
        if name in seen:
            raise ValueError(f"规则集重名：{name}")
        if rs_name_reserved(name):
            raise ValueError(f"规则集名称「{name}」与内置规则集冲突（cn-apps、dns-bypass-*、ad-allow、ad-allowimp、ad-important、ad-custom、ad-<8位id>、geosite-*、geoip-* 为保留名）")
        seen.add(name)
        rs = {"name": name, "url": _url(r.get("url"), f"规则集「{name}」的地址")}
        beh, fmt = r.get("behavior", "domain"), r.get("format", "yaml")
        if beh not in RS_BEHAVIORS:
            raise ValueError(f"规则集「{name}」的类型只能是 " + " / ".join(RS_BEHAVIORS))
        if fmt not in RS_FORMATS:
            raise ValueError(f"规则集「{name}」的格式只能是 " + " / ".join(RS_FORMATS))
        rs.update(behavior=beh, format=fmt, target=_str(r.get("target") or G_SEL, f"规则集「{name}」的目标策略"))
        out.append(rs)
    return out


def bypass_ok(x):
    if MAC_RX.fullmatch(x):
        return True
    if "%" in x:  # 以前 fe80::1%eth0 这种带网卡的地址也算合法：写进 nftables 集合语法错误、整份透明代理规则加载失败，
        return False  # sing-box 的 source_ip_cidr 也解析不了（Go 后端本来就不收）
    try:
        ipaddress.ip_network(x, strict=False)
        return True
    except ValueError:
        return False


def clean_bypass(v):
    out = []
    for x in _list(v, "绕过设备"):
        x = _str(x, "绕过设备的条目", empty=True)
        if not x:
            continue
        if not bypass_ok(x):
            raise ValueError(f"绕过设备只能填 IP、网段或 MAC 地址：{x[:60]}")
        out.append(x)
    return out


def clean_tests(v):
    out = []
    for i, t in enumerate(_list(v, "延迟站点")):
        if not isinstance(t, dict):
            raise ValueError(f"延迟站点第 {i + 1} 项格式不正确")
        name = _str(t.get("name"), f"延迟站点第 {i + 1} 项的名称")
        out.append({"name": name[:40], "url": _url(t.get("url"), f"延迟站点「{name}」的网址")})
    if not out:
        raise ValueError("延迟站点不能为空")
    return out


def clean_rules(v):
    out = []
    for x in _list(v, "自定义规则"):
        x = _str(x, "自定义规则", empty=True)
        if "\\n" in x or "\\r" in x:
            raise ValueError("自定义规则不能含换行")
        if x:
            out.append(x)
    return out


def sub_name_clash(name, others):
    """订阅名只差符号（「机场 A」和「机场_A」）时会共用同一个节点缓存文件，互相覆盖：返回冲突的那个名字"""
    k = safe(name)
    return next((o for o in others if o != name and safe(o) == k), None)


def rename_sub_clashes(subs):
    """恢复备份时订阅名只差符号（会共用同一个节点缓存文件）：后出现的自动加 _2、_3… 改名。返回 (新列表, [(原名, 新名)])"""
    out, renames, keys, names = [], [], set(), {s["name"] for s in subs}
    for s in subs:
        name = s["name"]
        if safe(name) in keys:
            k = 2
            while f"{name}_{k}" in names or safe(f"{name}_{k}") in keys:
                k += 1
            new = f"{name}_{k}"
            renames.append((name, new))
            names.add(new)
            s = dict(s, name=new)
        keys.add(safe(s["name"]))
        out.append(s)
    return out, renames


def rename_sub_refs(groups, renames):
    """恢复时订阅被自动改名：自定义组里勾选的订阅节点 {sub, name} 跟着改指新名字（订阅自己的 removed 名单在订阅对象里，自然随它走）"""
    m = dict(renames or [])
    if not m:
        return groups
    out = []
    for g in groups or []:
        if isinstance(g, dict) and isinstance(g.get("sub_nodes"), list):
            g = dict(g, sub_nodes=[dict(x, sub=m.get(x.get("sub"), x.get("sub"))) if isinstance(x, dict) else x for x in g["sub_nodes"]])
        out.append(g)
    return out


def clean_subs(v):
    seen = set()
    out = []
    for i, s in enumerate(_list(v, "订阅")):
        if not isinstance(s, dict):
            raise ValueError(f"订阅第 {i + 1} 项格式不正确")
        name = _str(s.get("name"), f"订阅第 {i + 1} 项的名称")
        if name in seen:
            raise ValueError(f"订阅重名：{name}")
        seen.add(name)
        _url(s.get("url"), f"订阅「{name}」的地址")
        for k in ("filter", "exclude"):
            if s.get(k) is not None and not isinstance(s[k], str):
                raise ValueError(f"订阅「{name}」的 {k} 应为文本")
        if "removed" in s:  # 从订阅里删除的节点（v8.4.0）；旧备份没有这个字段
            s = dict(s, removed=clean_removed(s["removed"], f"订阅「{name}」的已删除节点"))
        out.append(s)
    return out


def clean_nodes(v):
    for i, n in enumerate(_list(v, "节点")):
        p = n.get("proxy") if isinstance(n, dict) else None
        if not isinstance(p, dict):
            raise ValueError(f"节点第 {i + 1} 项缺少 proxy")
        name = _str(p.get("name"), f"节点第 {i + 1} 项的名称")
        _str(p.get("type"), f"节点「{name}」的类型")
        _str(p.get("server"), f"节点「{name}」的服务器")
        port = p.get("port")
        try:
            ok = not isinstance(port, bool) and 0 < int(port) < 65536
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise ValueError(f"节点「{name}」的端口无效")
    return v


def clean_custom_groups(v):
    out, seen = [], set()
    for i, g in enumerate(_list(v, "自定义策略组")):
        if not isinstance(g, dict):
            raise ValueError(f"自定义策略组第 {i + 1} 项格式不正确")
        g = clean_custom_group(g)
        if g["name"] in seen:
            raise ValueError(f"策略组重名：{g['name']}")
        seen.add(g["name"])
        out.append(g)
    return out


def clean_adblock(v):
    if not isinstance(v, dict):
        raise ValueError("广告拦截设置应为对象")
    for i, l in enumerate(_list(v.get("lists", []), "广告拦截列表")):
        if not isinstance(l, dict) or not isinstance(l.get("id"), str) or not l["id"].strip():
            raise ValueError(f"广告拦截列表第 {i + 1} 项格式不正确")
        _url(l.get("url"), f"广告拦截列表「{l['id']}」的地址")
        if l.get("scope", "all") not in ("all", "cn", "proxy"):
            raise ValueError(f"广告拦截列表「{l['id']}」的生效范围无效（all / cn / proxy）")
    for k, label in (("black", "黑名单"), ("black_cn", "国内直连黑名单"), ("black_proxy", "国外代理黑名单"), ("white", "白名单")):
        for x in _list(v.get(k, []), label):
            _str(x, label, empty=True)
    return v


def clean_devices(v):
    if not isinstance(v, dict) or not all(isinstance(k, str) and isinstance(x, str) for k, x in v.items()):
        raise ValueError("设备备注格式不正确")
    return v


def clean_schedule(v):
    if not isinstance(v, dict):
        raise ValueError("定时任务应为对象")
    for k in ("sub_update", "core_restart", "geo_update"):
        x = v.get(k) or ""
        if not isinstance(x, str) or (x and not re.fullmatch(r"([01]\\d|2[0-3]):[0-5]\\d", x)):
            raise ValueError(f"定时任务时间格式应为 HH:MM：{x}")
    try:
        int(v.get("latency") or 0)
    except (TypeError, ValueError):
        raise ValueError("自动测速间隔应为数字")
    return v


def _dict(label):
    def f(v):
        if not isinstance(v, dict):
            raise ValueError(f"{label}应为对象")
        return v
    return f


def _int(label, lo=0, hi=10**9):
    def f(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"{label}应为 {lo}–{hi} 的数字")
        return int(v)
    return f


def _bool(v):
    return bool(v)


def _text(label):
    def f(v):
        if v is None:
            return ""
        if not isinstance(v, (str, int)) or isinstance(v, bool):
            raise ValueError(f"{label}应为文本")
        return str(v).strip()
    return f


def _choice(label, opts):
    def f(v):
        if v not in opts:
            raise ValueError(f"{label}只能是 " + " / ".join(opts))
        return v
    return f


def _dns(v):
    if not isinstance(v, dict):
        raise ValueError("DNS 设置应为对象")
    for k in DNS_LISTS + ("hosts", "policies"):
        if k in v and not isinstance(v[k], list):
            raise ValueError(f"DNS 的 {k} 应为列表")
    for pe in v.get("policies") or []:
        if not isinstance(pe, dict) or not isinstance(pe.get("servers", []), list):
            raise ValueError("DNS 分流策略格式不正确")
    return v


CLEANERS = {
    "rulesets": clean_rulesets, "bypass": clean_bypass, "tests": clean_tests, "rules": clean_rules, "subs": clean_subs,
    "nodes": clean_nodes, "custom_groups": clean_custom_groups, "adblock": clean_adblock, "devices": clean_devices,
    "schedule": clean_schedule, "dns": _dns, "tun": _dict("TUN 设置"), "groups_cfg": _dict("地区分组设置"),
    "sub_interval": _int("订阅更新间隔", 0, 10**8), "log_limit": _int("日志上限", 1, 100),
    "region_groups": _bool, "ipv6": _bool, "watchdog": _bool, "sniffer": _bool, "tproxy": _bool,
    "tg_token": _text("Telegram Token"), "tg_chat": _text("Telegram Chat ID"), "gh_proxy": _text("GitHub 加速地址"), "gh_token": _text("GitHub Token"),
    "mode": _choice("代理模式", ("rule", "global", "direct")), "proxy_mode": _choice("代理方式", tuple(PROXY_MODES) + ("",)),
}


def clean_fields(b, keys):
    """按 CLEANERS 校验并规范化 b 中出现在 keys 里的字段，返回新 dict；有问题抛 ValueError（提示可直接给用户看）"""
    if not isinstance(b, dict):
        raise ValueError("请求内容应为 JSON 对象")
    out = {}
    for k in keys:
        if k in b:
            fn = CLEANERS.get(k)
            out[k] = fn(b[k]) if fn else b[k]
    return out
'''
_SRC['shunt.core'] = '''"""核心交互"""
import itertools, json, os, threading, time

from .base import CONF_DIR, LOCK, wd_hold
from .errlog import log_err
from .corehttp import core, core_json
from .data import config_version, load, read_json, save
from .groups import PROV, provider_nodes
from .mihomo import build_config, check_config, migrate_ruleset_cache, write_config
from .sb_common import SB_CONF, active_core
from .sb_config import build_singbox, check_sb_config, sb_fetch_missing, sb_hot_reload, sb_log_error, sb_rule_need, sb_running_current, sb_sync_assets, write_sb_config
from .sysopt import svc_restart
from .notify import WD, core_alive


_CONN_SNAP = {}
_CONN_LOCK = threading.Lock()


def conns_snapshot(timeout=5, ttl=2):
    """1~2 秒 TTL 的 /connections 进程内共享快照。

    stats（5 秒）、连接页（1 秒）、live（2 秒）三处原来各自全量抓取解析
    （300 连接时 100~300KB JSON），现在复用同一份快照。返回的 dict 三处只读不改。
    抓取失败返回 None（与原来语义一致），不缓存失败结果。"""
    now = time.time()
    v = _CONN_SNAP.get("v")
    if v is not None and now - v[0] < ttl:
        return v[1]
    c = core_json("/connections", timeout=timeout)
    if c is None:
        return None
    with _CONN_LOCK:
        _CONN_SNAP["v"] = (time.time(), c)
    return c


def reload_core(prev=None, force=False):
    """写配置 → mihomo -t / sing-box check 校验 → 热重载。

    prev（修改前的 data.json）给出时，任何一步失败都回滚到它：校验失败、生成配置抛异常（如残缺的规则集）、
    mihomo 热重载被拒、sing-box 重载后起不来。坏数据不会留在 data.json 里让 /api/state 一直 500"""
    try:  # sing-box：新引用的 GEO / 规则集文件先下载（锁外；已有文件时零开销）
        d = load()
        if active_core(d) == "singbox":
            bad = sb_fetch_missing(d)
            if bad:
                log_err("下载新引用的规则集", "；".join(bad)[:300])
    except Exception as e:
        log_err("下载新引用的规则集", e)
    with RELOAD_LOCK:  # 重载整体串行：替换配置、热重载、被核心拒绝后的回滚不能与另一次重载交错
        try:
            return _reload_core(prev, force)
        except Exception as e:
            log_err("重载核心配置", e, tb=True)  # 临时配置文件已由 _reload_core 清理
            if prev is None:
                return False, "生成配置失败：" + str(e)[:300]
            _rollback(prev)
            return False, "生成配置失败，已撤销本次修改：" + str(e)[:300]


# 两次保存同时触发重载时，以前替换正式配置之后的热重载 / 回滚在锁外：A 被 mihomo 拒绝后按 A 的旧数据重写 config.yaml
# 并重载，可能夹在 B 替换配置和 B 热重载之间，B 生效的其实是 A 回滚出的旧配置；sing-box 起不来时的回滚同理。
# 现在整个 reload_core 串行（可重入：重载里再触发重载不会自锁）。加锁顺序固定为 RELOAD_LOCK → LOCK
RELOAD_LOCK = threading.RLock()


def _revert_data(prev, applied=None):
    """撤销一次修改：data.json 恢复为 prev。applied 是这次重载生成配置时用的数据：
    之后又有别的保存（data.json 已不等于 applied）时只把这次改过的顶层项退回 prev，别的保存改的其他项保留，
    以前整份写回 prev，并发的另一次保存（设备备注、另一个页面的设置）被一起抹掉。须在 LOCK 内调用"""
    if applied is None:
        save(prev)
        return
    cur = load()
    if cur == applied:
        save(prev)
        return
    for k in set(prev) | set(applied):
        if prev.get(k, _MISSING) != applied.get(k, _MISSING):
            if k in prev:
                cur[k] = prev[k]
            else:
                cur.pop(k, None)
    save(cur)


_MISSING = object()


def _rollback(prev, sb=None, applied=None):
    """data.json 恢复为 prev（见 _revert_data），并按它重写当前内核的配置文件；返回 (内核是否 sing-box, 配置路径)"""
    with LOCK:
        _revert_data(prev, applied)
        d = load()
        if sb is None:
            sb = active_core(d) == "singbox"
        final = SB_CONF if sb else os.path.join(CONF_DIR, "config.yaml")
        try:
            (write_sb_config if sb else write_config)(d, final)
        except Exception as e:  # 旧数据本身也生成不了：保留磁盘上原来的配置文件
            log_err("回滚后重写配置", e)
    return sb, final


_TMP_SEQ = itertools.count(1)
RELOAD_TRIES = 3  # 校验期间配置又被改了：重新生成再校验，最后一次在锁内校验（退回旧做法，保证能结束）


def _gen_config(d, sb, tmp):
    """生成待校验的配置到临时文件 tmp"""
    # 旧版文件名的规则集缓存先改名（write_config / write_sb_config 只在写正式配置时做，这里写的是临时文件）：
    # 面板升级后核心没重启、直接热重载时，以前 mihomo 按新文件名找不到缓存，规则集全部重新下载，下载不到（GitHub 被墙）的
    # 规则集为空、对应分流规则失效
    try:
        migrate_ruleset_cache(d)
    except Exception as e:
        log_err("规则集缓存改名", e)
    if sb:
        try:  # 广告 / 白名单列表改动后，先把对应的 sing-box 规则集文件转换好，否则这些规则会被跳过
            need = {k: v for k, v in sb_rule_need(d).items() if v.get("kind") == "adblock"}
            if need:
                sb_sync_assets(d, need=need)
        except Exception as e:
            print("sb adblock sync", e, flush=True)
        return write_sb_config(d, tmp)
    return write_config(d, tmp)


def _reload_core(prev, force=False):
    """mihomo -t / sing-box check 最长 90 秒：以前整段持有全局 LOCK，期间所有 update()（设备备注、定时任务设置等）都被卡住。
    现在锁内只生成配置并记下 config_version，校验在锁外做；校验完再拿锁，版本没变才替换正式配置，变了就按最新数据重来。

    断流：sing-box 的 SIGHUP 重载会关掉旧实例，所有连接（视频、下载、游戏、SSH）一起断开。定时更新广告规则、
    GEO / 规则集时配置文件本身往往一个字节都没变，变的只是 rules/ 下的规则集文件，而 sing-box 1.10+ 会自动
    重新加载被修改的本地规则集文件（type: local 的 path），根本不需要 SIGHUP。所以新配置与磁盘上的完全相同、
    且正在运行的进程确实加载的就是这份文件时，跳过重载，连接不断"""
    tmp = None
    unchanged = False
    try:
        for i in range(RELOAD_TRIES):
            last = i == RELOAD_TRIES - 1
            with LOCK:
                d = load()
                applied = load()  # 生成配置可能改动 d：另留一份原样的，回滚时判断之后有没有别的保存
                ver = config_version()
                sb = active_core(d) == "singbox"
                final = SB_CONF if sb else os.path.join(CONF_DIR, "config.yaml")
                tmp = f"{final}.new{next(_TMP_SEQ)}"  # 唯一临时文件：并发重载各用各的，不互相覆盖
                _gen_config(d, sb, tmp)
                check = check_sb_config if sb else check_config
                if last:
                    ok, err = check(tmp)
            if not last:
                ok, err = check(tmp)  # 锁外校验
            with LOCK:
                if not ok:
                    if prev is not None:
                        _revert_data(prev, applied)
                    return False, "配置校验失败，已撤销本次修改：" + err
                if config_version() != ver and not last:
                    os.remove(tmp)  # 校验期间 data.json 被改过：这份配置已过时，丢掉重来
                    tmp = None
                    continue
                if sb and not force and _same_file(tmp, final) and sb_running_current():
                    os.remove(tmp)  # 不替换：保留原文件的修改时间，sb_running_current 下次还能判断
                    tmp = None
                    unchanged = True
                    break
                os.replace(tmp, final)
                tmp = None
                break
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    if unchanged:
        return True, "配置无变化，规则集文件已由 sing-box 自动重新加载（连接不中断）"
    if WD["manual_stop"]:  # 用户手动停止了核心：只写配置，不替用户把核心拉起来（与 refresh_regions 一致）
        return True, "配置已保存；核心已手动停止，下次启动时生效"
    retest_soon()
    if sb:
        ok, msg = sb_hot_reload()
        if not ok and prev is not None:  # 配置能通过 sing-box check 但运行时启动失败（如内核缺少模块）：撤销并恢复
            tail = sb_log_error()
            _rollback(prev, sb=True, applied=applied)
            svc_restart(timeout=90)
            return False, "sing-box 启动失败，已撤销本次修改" + ("：" + tail if tail else "")
        return ok, msg
    wd_hold(45, "mihomo 重载配置")
    code, body = core("PUT", "/configs?force=true", {"path": final}, timeout=30)
    if code == 502:  # 核心没在跑，尝试启动
        svc_restart()
        return True, "已写入配置并重启 mihomo"
    text = body.decode(errors="ignore") if isinstance(body, (bytes, bytearray)) else str(body or "")
    if code >= 300:
        if prev is None:
            return False, text or f"mihomo 重载失败（{code}）"
        # 核心拒绝了新配置、还在跑旧配置：data.json 和 config.yaml 也退回去，界面与实际生效的配置保持一致
        _, final = _rollback(prev, sb=False, applied=applied)
        core("PUT", "/configs?force=true", {"path": final}, timeout=30)
        return False, "mihomo 重载失败，已撤销本次修改：" + (text[:300] or str(code))
    return True, text or "配置已重载"


def _same_file(a, b):
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def group_sig(cfg):
    # use / exclude-filter 也要比：订阅节点勾选（v8.4.0）按 use 引用订阅，只比 proxies 会漏掉引用的订阅变化
    return [(g["name"], g["type"], g.get("filter", ""), g.get("exclude-filter", ""), tuple(g.get("use") or []),
             tuple(g.get("proxies") or [])) for g in cfg.get("proxy-groups") or []]


def refresh_regions():
    """订阅节点变化后，地区分组（存在哪些地区、是否生成均衡组）可能变化：与当前配置不同就重新生成并重载"""
    if not core_alive():  # 核心没在运行（可能是手动停止）时不重载，免得把它拉起来
        return False, "核心未运行"
    provider_nodes(fresh=True)
    if active_core() == "singbox":
        cur = read_json(SB_CONF, None)
        new = build_singbox(load())[0]
        # 比较全部出站（节点参数 + 策略组成员），按 JSON 往返后的形式比：sing-box 的订阅由面板下载后写进配置，
        # 机场换了节点的密码 / 服务器 / 端口但名字不变时，以前只比策略组成员，判定「无变化」不重载，
        # 核心一直用旧参数连接（手动「更新」订阅也一样），节点全部不通
        sig = lambda c: json.dumps((c or {}).get("outbounds") or [], ensure_ascii=False, sort_keys=True)
        if cur and sig(cur) == sig(new):
            return True, "节点与地区分组无变化"
        ok, msg = reload_core()
        return ok, "已按最新订阅节点更新配置" if ok else msg
    cur = read_json(os.path.join(CONF_DIR, "config.yaml"), None)
    if not cur:
        return False, "当前配置不存在"
    if group_sig(cur) == group_sig(build_config(load())):
        return True, "地区分组无变化"
    ok, msg = reload_core()
    print(time.strftime("%F %T"), "[groups] 订阅节点变化，已重新生成地区分组：", ok, msg[:120], flush=True)
    return ok, "地区分组已按最新订阅节点更新" if ok else msg


def refresh_later(delay=8):
    """过一会儿按核心里最新的订阅节点名单再核对一次策略组（有变化才重载；核心没在运行时什么都不做）"""
    def run():
        try:
            refresh_regions()
        except Exception as e:
            print("refresh regions error", e, flush=True)
    t = threading.Timer(delay, run)
    t.daemon = True
    t.start()


def refresh_after_sub(name, wait=90):
    """新订阅加入后等核心下载完节点，再更新地区分组"""
    def run():
        end = time.time() + wait
        while time.time() < end:
            time.sleep(10)
            # 要等核心真正加载完这个订阅：名单里有它、且不是沿用的旧缓存（换了链接时缓存里还是旧链接的节点）
            if name in provider_nodes(fresh=True) and name not in PROV.get("pending", ()):
                break
        try:
            refresh_regions()
        except Exception as e:
            print("refresh regions error", e, flush=True)
    threading.Thread(target=run, daemon=True).start()


def _link():
    # 唯一的延迟绑定：core.reload_core() 在重载后调用 latency.retest_soon() 触发补测，
    # 而 latency 的定时调度器又需要 core.refresh_regions() —— 真互递归，无法通过移动代码破环。
    # latency 侧已改为顶层导入 core，这里只延迟绑定 retest_soon 这一个名字。
    global retest_soon
    from .latency import retest_soon
'''
_SRC['shunt.corehttp'] = '''"""核心控制器 HTTP（叶子模块）：与 mihomo/sing-box 控制器 API 对话。

从 core.py 拆出：只依赖 base、data，无环；core.py 及其他模块从这里导入。
"""
import json
import os
import urllib.request
import urllib.error

from .base import CTRL, DATA_FILE, mass_begin, mass_end
from .data import load

DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 访问本机控制器绝不走系统代理


_SECRET=[None, ""]


def ctrl_secret():
    """控制器密钥按 data.json 修改时间缓存：每秒的实时请求不再反复读取解析整份配置"""
    try:
        st = os.stat(DATA_FILE)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is None or _SECRET[0] != key:
        _SECRET[:] = [key, load()["secret"]]
    return _SECRET[1]


def core(method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CTRL + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + ctrl_secret(), "Content-Type": "application/json"})
    mass = method == "GET" and "/group/" in path and "/delay" in path  # 全节点测速：核心高负载，看门狗暂停判定
    tok = mass_begin() if mass else None
    try:
        with DIRECT_OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 502, json.dumps({"message": "核心未运行或无法连接: %s" % e}).encode()
    finally:
        if tok is not None:
            mass_end(tok)


def core_json(path, timeout=10):
    code, raw = core("GET", path, timeout=timeout)
    if code != 200:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None
'''
_SRC['shunt.nodeprobe'] = '''"""逐个节点测速（/proxies/{节点}/delay），给整组测速结果不可靠的场合补测。

sing-box（实测 1.14.3）的 /group/{组}/delay 同时只测 10 个节点、整组共用一个 timeout，应答里只列测通的节点：
排在前面的失效节点占满并发等到超时，后面的可用节点根本没轮到测，也不会出现在结果里（全部没测到时照样 200 {}）。
所以 sing-box 下「组结果里缺席」不等于「测速失败」，要逐个节点再测一次才能下结论。mihomo 的整组测速会测完全部节点，
缺席就是失败（全部失败回 504），不需要补测。

notify.check_nodes（全部超时告警前复核）、health.run_check（节点健康检查）、schedule 的定时测速、retest_soon（重载后补测）、
前端经 /api/core/group/…/delay 发起的整组测速（「全部测速」、组测速、概览测速）、订阅 healthcheck 共用这里的逻辑。

并发控制：所有调用方共用一个信号量（同时最多 PROBE_WORKERS 个单节点测速），同一节点同时只测一次（后到的等先到的结果）。
以前每个调用方各开 32 个线程：前端「全部测速」同时测 GLOBAL 与手动选择两个组、再赶上重载后补测，路由器上会叠到上百个并发测速。
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from urllib.parse import quote

from .base import BUILTIN_POLICIES, HC, mass_begin, mass_end
from .corehttp import core, core_json

PROBE_WORKERS = 16   # 逐个节点测速的全局并发上限（所有调用方合计）
PROBE_BUDGET = 90    # 默认最多用多少秒（期间按「全节点测速中」登记，看门狗暂停判定）；必须小于 base.MASS_MAX_AGE
UI_BUDGET = 60       # 前端发起的整组测速 / 订阅 healthcheck 的补测上限（加上整组测速本身最多约 2 分钟）
_SEM = threading.BoundedSemaphore(PROBE_WORKERS)
_INFLIGHT = {}       # {节点: [Event, 结果]}：正在测的节点，后到的调用方等同一个结果
_INFLIGHT_LOCK = threading.Lock()
NON_NODE_TYPES = {"Direct", "Reject", "RejectDrop", "Pass", "Compatible", "Dns", "Block"}


def node_delay(name):
    """单节点测速：延迟毫秒（>0 可用）/ 0 超时或失败（两个内核都是 504 超时、503 测速出错）/
    None 没有结论（核心没应答 502、节点已不在 404、应答解析不了等）"""
    code, raw = core("GET", f"/proxies/{quote(name, safe='')}/delay?url={quote(HC)}&timeout=5000", timeout=20)
    if code in (503, 504):
        return 0
    if code != 200:
        return None
    try:
        v = json.loads(raw).get("delay")
    except Exception:
        return None
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def node_alive(name):
    """True 可用 / False 超时或失败 / None 没有结论"""
    v = node_delay(name)
    return None if v is None else v > 0


def _probe_shared(name, deadline):
    """受全局信号量限制的单节点测速；同一节点正在被别的调用方测时直接等它的结果。到 deadline 还没轮到 / 没测完返回 None"""
    with _INFLIGHT_LOCK:
        slot = _INFLIGHT.get(name)
        owner = slot is None
        if owner:
            slot = _INFLIGHT[name] = [threading.Event(), None]
    if not owner:
        slot[0].wait(max(0.0, deadline - time.monotonic()))
        return slot[1]
    try:
        left = deadline - time.monotonic()
        if left <= 0 or not _SEM.acquire(timeout=left):
            return None
        try:
            slot[1] = node_delay(name)
        finally:
            _SEM.release()
        return slot[1]
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(name, None)
        slot[0].set()


def probe_nodes(names, budget=PROBE_BUDGET, stop=None, progress=None):
    """并发逐个测速，返回 {节点: 延迟 | 0 | None}；预算内没测完的节点记 None（没有结论）。
    stop(节点, 结果) 返回真时提前结束（其余节点记 None）；progress(已完成数) 每测完一个调用一次"""
    names = list(dict.fromkeys(n for n in names if isinstance(n, str) and n))
    out = dict.fromkeys(names)
    if not names:
        return out
    tok = mass_begin()  # 测速期间核心负载高，看门狗暂停判定
    ex = ThreadPoolExecutor(min(PROBE_WORKERS, len(names)))
    deadline = time.monotonic() + budget
    try:
        futs = {ex.submit(_probe_shared, n, deadline): n for n in names}
        done = 0
        try:
            for f in as_completed(futs, timeout=budget):
                n = futs[f]
                try:
                    out[n] = f.result()
                except Exception:
                    out[n] = None
                done += 1
                if progress:
                    progress(done)
                if stop and stop(n, out[n]):
                    break
        except FuturesTimeout:
            pass  # 节点太多，预算内没测完：剩下的保持 None
        return out
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
        mass_end(tok)


def confirm_all_down(members, budget=PROBE_BUDGET):
    """「全部超时」告警前逐个节点复核：True 全部失败 / False 有节点可用 / None 没有结论（核心重载中、预算内没测完）"""
    names = [m for m in members if isinstance(m, str) and m]
    if not names:
        return True
    res = probe_nodes(names, budget, stop=lambda n, v: bool(v))
    vals = list(res.values())
    if any(vals):
        return False
    return True if all(v == 0 for v in vals) else None


def fill_absent(results, budget=PROBE_BUDGET, progress=None):
    """sing-box 整组测速的结果 {节点: 延迟(0=缺席/失败)} → 缺席的逐个补测。
    返回新的 {节点: 延迟}：测通的写延迟、确认失败的写 0，没有结论的节点不出现（本轮不记失败也不记成功）"""
    miss = [n for n, v in results.items() if not v]
    out = dict(results)
    if not miss:
        return out
    for n, v in probe_nodes(miss, budget, progress=progress).items():
        if v is None:
            out.pop(n, None)
        else:
            out[n] = v
    return out


def group_nodes(group):
    """组的直接成员里的节点（不含策略组、DIRECT / REJECT 等内置出站）；核心没应答或组不存在返回 None"""
    px = (core_json("/proxies", timeout=8) or {}).get("proxies")
    if not isinstance(px, dict) or not isinstance(px.get(group), dict):
        return None
    out = []
    for m in px[group].get("all") or []:
        p = px.get(m)
        if not isinstance(m, str) or m in BUILTIN_POLICIES or not isinstance(p, dict):
            continue
        if p.get("all") is not None or p.get("type") in NON_NODE_TYPES:
            continue
        out.append(m)
    return out


def fill_group(group, code, raw, budget=UI_BUDGET):
    """sing-box 整组测速（/group/{组}/delay）的应答 → 缺席的节点逐个补测后，按同样格式（只列测通的）返回 (状态码, 应答)。
    整组测速本身失败（非 200）、组成员取不到时原样返回；补测后仍全部不通时与原来一样回 200 {}"""
    if code != 200:
        return code, raw
    try:
        data = json.loads(raw or b"{}")
    except Exception:
        return code, raw
    if not isinstance(data, dict):
        return code, raw
    nodes = group_nodes(group)
    if not nodes:
        return code, raw
    res = fill_absent({n: (data.get(n) if isinstance(data.get(n), int) and not isinstance(data.get(n), bool) else 0) for n in nodes}, budget)
    out = {k: v for k, v in data.items() if k not in res}  # 组成员里的策略组等：保留核心原来的结果
    out.update({k: v for k, v in res.items() if v and v > 0})
    return 200, json.dumps(out, ensure_ascii=False).encode()
'''
_SRC['shunt.sb_common'] = '''"""sing-box 内核"""
import os

from .errlog import log_err
from .base import CONF_DIR, PANEL_DIR
from .data import load
from .sysopt import wfile
# 面板数据（订阅、节点、规则、DNS、设备）两个内核共用；切换内核时按各自格式生成独立的配置文件：
#   mihomo   → /etc/mihomo/config.yaml
#   sing-box → /etc/sing-box/config.json
# 魔改版内核（sb_is_fork）有 loadbalance 出站组：负载均衡组原样生成（多节点聚合），故障转移仍按 urltest。
# 官方 sing-box 没有负载均衡 / 故障转移组：自定义的这两类组降级为 urltest（自动测速），地区「⚖️ 负载均衡」组与
# 「自动优选」完全相同，不再重复生成，引用它的地方自动改指向同地区的「自动优选」。
SB_BIN = os.environ.get("SB_BIN", "/usr/local/bin/sing-box")
SB_DIR = os.environ.get("SB_DIR", "/etc/sing-box")
SB_CONF = os.path.join(SB_DIR, "config.json")
SB_RULE_DIR = os.environ.get("SB_RULES", os.path.join(SB_DIR, "rules"))
SB_SUB_DIR = os.path.join(PANEL_DIR, "sb_subs")
CORE_FILE = os.path.join(PANEL_DIR, "core")  # init.d/shunt-core 读取它决定启动哪个内核
CORE_PID = os.environ.get("PANEL_CORE_PID", "/run/shunt-core.pid" if os.path.isfile("/etc/init.d/shunt-core") else "/run/mihomo.pid")
CORES = {"mihomo": "mihomo", "smart": "mihomo Smart", "singbox": "sing-box"}
SB_GEO = "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/sing/geo"
SB_REPO = os.environ.get("SB_REPO", "https://github.com/SagerNet/sing-box/releases")
SB_API = os.environ.get("SB_API", "https://api.github.com/repos/SagerNet/sing-box/releases")
# 魔改版 sing-box：多了 loadbalance 出站组，能做真正的多节点聚合（负载均衡）。
# 默认装 reF1nd 的最新正式版（reF1nd/sing-box-releases：有稳定版、编译了 gVisor、有 musl 包，但只发 amd64 / arm64）；
# 其他架构（armv7、mips 等）退回 MiChongs/sing-box（基于 reF1nd，只发预发布版、没编译 gVisor）
FORK_REPO = os.environ.get("SB_FORK_REPO", "https://github.com/reF1nd/sing-box-releases/releases")
FORK_API = os.environ.get("SB_FORK_API", "https://api.github.com/repos/reF1nd/sing-box-releases/releases")
FORK_ARCHES = ("amd64", "arm64")
FORK2_REPO = os.environ.get("SB_FORK2_REPO", "https://github.com/MiChongs/sing-box/releases")
FORK2_API = os.environ.get("SB_FORK2_API", "https://api.github.com/repos/MiChongs/sing-box/releases")
FORK_NAME = "魔改版（reF1nd）"
SB_UA = ("clash.meta", "v2rayN/6.45")
SB_STATE = {"warn": [], "subs": {}}


def active_core(d=None):
    c = (d or load()).get("core") or "mihomo"
    return c if c in CORES else "mihomo"


def core_conf_path(kind=None):
    return SB_CONF if (kind or active_core()) == "singbox" else os.path.join(CONF_DIR, "config.yaml")


def write_core_file(kind):
    try:
        wfile(CORE_FILE, kind + "\\n")
    except OSError as e:
        log_err("记录当前内核", e)

'''
_SRC['shunt.sb_yaml'] = '''"""极简 YAML 读取（只为解析 Clash 订阅里的 proxies 列表；面板保持零依赖）"""
import copy, json, re
def _y_strip_comment(s):
    q = None
    for i, ch in enumerate(s):
        if q:
            if ch == q and (q == "'" or s[i - 1] != "\\\\"):
                q = None
        elif ch in "'\\"":
            q = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \\t"):
            return s[:i].rstrip()
    return s.rstrip()


# 凭据类字段：不加引号的值按原文保留（password: 12.50 / 0123 / true / 1:20 都是文本，不能变成数字、布尔）
RAW_KEYS = frozenset(("password", "uuid", "auth", "auth-str", "auth_str", "psk", "private-key", "private_key",
                      "public-key", "public_key", "short-id", "short_id", "obfs-password", "obfs_password",
                      "token", "username"))


def raw_key(k):
    return isinstance(k, str) and k.strip().lower() in RAW_KEYS


def _y_raw(s):
    """凭据字段的值：引号照常解析，不加引号的按原文保留；空、~、null 仍是空"""
    s = s.strip()
    if not s or s[0] in "'\\"" or s in ("~", "null", "Null", "NULL"):
        return _y_scalar(s)
    return s


def _y_scalar(s):
    s = s.strip()
    if not s:
        return None
    if s[0] == '"' and s.endswith('"') and len(s) > 1:
        try:
            return json.loads(s)
        except Exception:
            return s[1:-1]
    if s[0] == "'" and s.endswith("'") and len(s) > 1:
        return s[1:-1].replace("''", "'")
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"[-+]?\\d+", s) and not (len(s.lstrip("+-")) > 1 and s.lstrip("+-")[0] == "0"):
        return int(s)
    if re.fullmatch(r"[-+]?\\d+\\.\\d+", s):
        return float(s)
    return s


def _y_flow(s, i=0):
    """解析 {..} / [..] 流式集合，返回 (值, 结束位置)"""
    def ws(i):
        while i < len(s) and s[i] in " \\t\\r\\n":
            i += 1
        return i

    def scalar(i, stops, raw=False):
        i = ws(i)
        if i < len(s) and s[i] in "'\\"":
            q = s[i]
            j = i + 1
            while j < len(s):
                if s[j] == q:
                    if q == "'" and j + 1 < len(s) and s[j + 1] == "'":
                        j += 2
                        continue
                    if q == '"' and s[j - 1] == "\\\\":
                        j += 1
                        continue
                    break
                j += 1
            return _y_scalar(s[i:j + 1]), j + 1
        j = i
        while j < len(s) and s[j] not in stops:
            if s[j] == ":" and "}" in stops and (j + 1 >= len(s) or s[j + 1] in " ,}"):
                break
            j += 1
        return (_y_raw if raw else _y_scalar)(s[i:j]), j

    def value(i, stops, raw=False):
        i = ws(i)
        if i < len(s) and s[i] in "{[":
            return _y_flow(s, i)
        return scalar(i, stops, raw)

    i = ws(i)
    if s[i] == "{":
        out, i = {}, i + 1
        while True:
            i = ws(i)
            if i >= len(s):
                raise ValueError("YAML 花括号未闭合")
            if s[i] == "}":
                return out, i + 1
            k, i = scalar(i, ",}")
            i = ws(i)
            v = None
            if i < len(s) and s[i] == ":":
                v, i = value(i + 1, ",}", raw_key(k))
            out[str(k)] = v
            i = ws(i)
            if i < len(s) and s[i] == ",":
                i += 1
    if s[i] == "[":
        out, i = [], i + 1
        while True:
            i = ws(i)
            if i >= len(s):
                raise ValueError("YAML 方括号未闭合")
            if s[i] == "]":
                return out, i + 1
            v, i = value(i, ",]")
            out.append(v)
            i = ws(i)
            if i < len(s) and s[i] == ",":
                i += 1
    return scalar(i, "")


FLOW_MAX = 200  # 一个流式集合最多跨这么多行，超过就当它没闭合，不再往后吞


def _y_depth(s):
    """括号深度变化：引号里的不算（引号只在值的开头才算引号，Bob's 这种普通文字里的撇号不算）"""
    q, n, prev = None, 0, ","
    for i, ch in enumerate(s):
        if q:
            if ch == q and (q == "'" or s[i - 1] != "\\\\"):
                q, prev = None, ch
            continue
        if ch in "'\\"" and prev in "{[,:":
            q = ch
        elif ch in "{[":
            n += 1
        elif ch in "}]":
            n -= 1
        if ch not in " \\t":
            prev = ch
    return n


_FLOW_KEY = re.compile(r"""^\\s*(?:-\\s+)*(?:(?:"(?:[^"\\\\]|\\\\.)*"|'[^']*'|[^\\s'"{}\\[\\],:#&*-][^:#]*?|-[^\\s:#][^:#]*?)\\s*:\\s+)?(?:&[^\\s{}\\[\\],]+\\s+)?""")
_ANCHOR = re.compile(r"^&([^\\s{}\\[\\],]+)(?:\\s+|$)")


def _y_flow_start(c):
    """行内流式集合（{..} / [..]）开始的位置：「- {a: 1}」「key: [a, b]」「[a]」；没有返回 -1"""
    m = _FLOW_KEY.match(c)
    i = m.end() if m else 0
    return i if c[i:i + 1] in ("{", "[") else -1


def yaml_lite(text):
    """块状 / 流式映射与列表、引号字符串、注释、锚点 &a / 别名 *a / 合并键 <<: *a（机场常用来复用节点参数）。
    以前不认锚点：「- &a {name: …}」整条被当成一个字符串、「- &a」后面的块状节点整段丢掉，没有 PyYAML 的设备上这些节点静默消失"""
    raw = []
    for line in text.replace("\\t", "  ").splitlines():
        if line.strip() in ("---", "...") or not line.strip() or line.lstrip().startswith("#"):
            continue
        raw.append(line)
    lines, buf, depth, nbuf = [], "", 0, 0
    for line in raw:  # 跨行的流式集合合并成一行
        c = _y_strip_comment(line)
        if buf:
            buf += " " + c.strip()
            depth += _y_depth(c)
            nbuf += 1
        else:
            st = _y_flow_start(c)
            if st < 0:  # 这一行没有流式集合（块状写法里的 HK[01 之类不算括号）
                lines.append((len(c) - len(c.lstrip(" ")), c.strip()))
                continue
            buf, depth, nbuf = c, _y_depth(c[st:]), 1
        # 以前每行都对整段已合并的内容重新去注释、数括号，且引号里的 [ 也计数：
        # 一个节点名带「[」就让后面所有行并成一行，2000 个节点要十几秒（平方级）。现在逐行累计，最多合并 FLOW_MAX 行
        if depth <= 0 or nbuf >= FLOW_MAX:
            lines.append((len(buf) - len(buf.lstrip(" ")), buf.strip()))
            buf = ""
    if buf:
        lines.append((len(buf) - len(buf.lstrip(" ")), buf.strip()))

    def split_kv(t):
        if t[:1] in "'\\"":
            q = t[0]
            j = t.find(q, 1)
            while q == '"' and j > 0 and t[j - 1] == "\\\\":
                j = t.find(q, j + 1)
            if j > 0 and t[j + 1:j + 2] == ":":
                return _y_scalar(t[:j + 1]), t[j + 2:].strip()
            return None
        m = re.match(r"^([^:{}\\[\\],]+?)\\s*:(?:\\s+(.*)|$)", t)
        return (m.group(1).strip(), (m.group(2) or "").strip()) if m else None

    anchors = {}

    def anchor(v):
        """去掉值开头的锚点 &a，返回 (锚点名或 None, 剩下的值)"""
        m = _ANCHOR.match(v)
        return (m.group(1), v[m.end():].strip()) if m else (None, v)

    def alias(v):
        if isinstance(v, str) and re.fullmatch(r"\\*[^\\s{}\\[\\],]+", v):
            return copy.deepcopy(anchors[v[1:]]) if v[1:] in anchors else v
        if isinstance(v, dict):
            return merge({k: alias(x) for k, x in v.items()})
        if isinstance(v, list):
            return [alias(x) for x in v]
        return v

    def merge(m):
        """<<: *a / <<: [*a, *b]：合并进来，自己写的键优先"""
        if "<<" not in m:
            return m
        src = m.pop("<<")
        out = {}
        for x in (src if isinstance(src, list) else [src]):
            if isinstance(x, dict):
                out.update({k: v for k, v in x.items() if k not in out})
        out.update(m)
        return out

    def inline(v, raw=False):
        name, v = anchor(v)
        if v.startswith(("{", "[")):
            r = alias(_y_flow(v)[0])
        elif v in ("|", ">", "|-", ">-"):
            r = ""
        elif raw and v and not v.startswith("*"):
            r = _y_raw(v)
        else:
            r = alias(_y_scalar(v)) if v else None
        if name:
            anchors[name] = r
        return r

    def block(i, ind):
        if i >= len(lines):
            return None, i
        if lines[i][1].startswith("- ") or lines[i][1] == "-":
            out = []
            while i < len(lines) and lines[i][0] == ind and (lines[i][1].startswith("- ") or lines[i][1] == "-"):
                rest = lines[i][1][1:].strip()
                name, rest = anchor(rest)
                if name and not rest:  # 「- &a」后面跟块状映射
                    if i + 1 < len(lines) and lines[i + 1][0] > ind:
                        v, i = block(i + 1, lines[i + 1][0])
                    else:
                        v, i = None, i + 1
                    anchors[name] = v
                    out.append(v)
                    continue
                if name:
                    rest = "&" + name + " " + rest  # 行内值：交给 inline 记录锚点
                    if not rest[len(name) + 2:].startswith(("{", "[")) and split_kv(rest[len(name) + 2:]) is not None:
                        rest = rest[len(name) + 2:]  # 「- &a key: v」锚点给整个映射：少见，按普通映射解析（锚点不记录）
                if not rest:
                    if i + 1 < len(lines) and lines[i + 1][0] > ind:
                        v, i = block(i + 1, lines[i + 1][0])
                    else:
                        v, i = None, i + 1
                    out.append(v)
                    continue
                kv = None if rest.startswith(("{", "[", "&")) else split_kv(rest)
                if kv is None:
                    out.append(inline(rest))
                    i += 1
                    continue
                sub = ind + len(lines[i][1]) - len(rest)
                lines[i] = (sub, rest)  # 「- key: v」等价于下一级映射的第一行
                v, i = block(i, sub)
                out.append(v)
            return out, i
        out = {}
        while i < len(lines) and lines[i][0] == ind and not lines[i][1].startswith("- "):
            kv = split_kv(lines[i][1])
            if kv is None:
                i += 1
                continue
            k, v = kv
            i += 1
            name, rest = anchor(v) if v else (None, v)
            if name and not rest:  # 「key: &a」后面跟块状值
                if i < len(lines) and (lines[i][0] > ind or (lines[i][0] == ind and lines[i][1].startswith("- "))):
                    out[str(k)], i = block(i, lines[i][0])
                else:
                    out[str(k)] = None
                anchors[name] = out[str(k)]
            elif v and v not in ("|", ">", "|-", ">-"):
                out[str(k)] = inline(v, raw_key(k))
            elif i < len(lines) and (lines[i][0] > ind or (lines[i][0] == ind and lines[i][1].startswith("- "))):
                if v in ("|", ">", "|-", ">-"):
                    parts = []
                    while i < len(lines) and lines[i][0] > ind:
                        parts.append(lines[i][1])
                        i += 1
                    out[str(k)] = ("\\n" if v.startswith("|") else " ").join(parts)
                else:
                    out[str(k)], i = block(i, lines[i][0])
            else:
                out[str(k)] = None
        return merge(out), i

    if not lines:
        return None
    return block(0, lines[0][0])[0]
'''
_SRC['shunt.sb_subs'] = '''"""订阅：sing-box 不能自己拉取订阅，由面板下载、解析成节点后写进配置"""
import json, os, re, threading, time, urllib.request, urllib.error

from .errlog import log_err
from .base import CONF_DIR, DEFAULT_EXCLUDE, PANEL_DIR, dl_check, read_full
from .data import load, safe, write_json
from .links import b64d, parse_link
from .groups import PROV, rx, sub_cache_path
from .sb_common import SB_UA
from .sb_yaml import RAW_KEYS, yaml_lite
from .sysopt import proxy_opener
from .notify import core_alive

_DEBUG_LOCK = threading.Lock()  # sub-debug.txt 并发写保护
SUBS_WAIT = 180  # 并发更新订阅整体最多等这么多秒


_LOADER = None


def _yaml_loader():
    """PyYAML 的 SafeLoader，但凭据字段（password / uuid / short-id …）不加引号的值按原文当文本：
    safe_load 会把 password: 12.50 变成 12.5、0123 变成 83、true 变成 True、1:20 变成 80（YAML 1.1 六十进制）"""
    global _LOADER
    if _LOADER is None:
        import yaml

        class Loader(yaml.SafeLoader):
            def construct_mapping(self, node, deep=False):
                if isinstance(node, yaml.MappingNode):
                    for kn, vn in node.value:
                        if (isinstance(kn, yaml.ScalarNode) and isinstance(vn, yaml.ScalarNode) and vn.style is None
                                and str(kn.value).strip().lower() in RAW_KEYS
                                and vn.value not in ("", "~", "null", "Null", "NULL")):
                            vn.tag = "tag:yaml.org,2002:str"
                return super().construct_mapping(node, deep=deep)

        _LOADER = Loader
    return _LOADER


def _yaml_full(text):
    try:
        import yaml  # 设备装了 py3-yaml 就用完整解析器
        y = yaml.load(text, Loader=_yaml_loader())  # noqa: S506 - SafeLoader 子类
        return y if isinstance(y, dict) else None
    except ImportError:
        return None
    except Exception:
        return None


def _yaml_section(text, key):
    """取出顶层 key: 段落原文（到下一个顶层键为止）"""
    lines = text.replace("\\t", "  ").splitlines()
    out, on = [], False
    for ln in lines:
        top = ln[:1] not in ("", " ", "-", "#")
        if top:
            m = re.match(r"^[\\"']?([^:\\"']+)[\\"']?\\s*:", ln)
            on = bool(m and m.group(1).strip() == key)
            if on:
                out.append(f"{key}:" + ln.split(":", 1)[1])
            continue
        if on:
            out.append(ln)
    return "\\n".join(out) if out else ""


def _yaml_items(sec):
    """整段解析失败时，按列表项逐条解析，坏的跳过"""
    lines = sec.splitlines()[1:]
    ind = next((len(x) - len(x.lstrip(" ")) for x in lines if x.strip().startswith("-")), None)
    if ind is None:
        return []
    items, cur = [], []
    for ln in lines:
        if ln.strip().startswith("-") and len(ln) - len(ln.lstrip(" ")) == ind:
            if cur:
                items.append(cur)
            cur = [ln]
        elif cur:
            cur.append(ln)
    if cur:
        items.append(cur)
    out = []
    for it in items:
        try:
            v = yaml_lite("x:\\n" + "\\n".join(it))
            v = (v or {}).get("x")
            if isinstance(v, list) and v and isinstance(v[0], dict):
                out.append(v[0])
        except Exception as e:
            log_err("订阅节点解析（跳过一条）", e)
    return out


_ALIAS_RX = re.compile(r"(?:^|[\\s\\[{,:])\\*[^\\s{}\\[\\],]+")  # YAML 别名 *a


def sub_yaml(text):
    """Clash/mihomo 订阅 → {"proxies": [...], "proxy-providers": {...}}，尽量容错"""
    y = _yaml_full(text)
    if y is not None:
        return {"proxies": y.get("proxies") or [], "proxy-providers": y.get("proxy-providers") or {}}
    res = {"proxies": [], "proxy-providers": {}}
    for key in ("proxies", "proxy-providers"):
        sec = _yaml_section(text, key)
        if not sec:
            continue
        v = None
        try:
            v = (yaml_lite(sec) or {}).get(key)
        except Exception:
            v = None
        if key == "proxies" and _ALIAS_RX.search(sec):
            # 节点引用了别名 *a：锚点常定义在 proxies 之外的顶层键里（x-common: &c …），只解析 proxies 段落时
            # 别名解析不出来，节点缺 type / server 被整条丢掉。这时按整份文本再解析一次，节点不少于段落解析的就用它
            try:
                vf = (yaml_lite(text) or {}).get(key)
            except Exception:
                vf = None
            if isinstance(vf, list) and len(vf) >= (len(v) if isinstance(v, list) else 0):
                v = vf
        if key == "proxies":
            n_items = sum(1 for x in sec.splitlines() if re.match(r"^\\s*-\\s", x))
            if not isinstance(v, list) or len(v) < n_items:
                alt = _yaml_items(sec)
                if len(alt) > len(v if isinstance(v, list) else []):
                    v = alt
            res[key] = v if isinstance(v, list) else []
        else:
            res[key] = v if isinstance(v, dict) else {}
    return res


def parse_sub_text(text, fetch=None, depth=0):
    """订阅内容 → mihomo 风格节点列表。支持 Clash YAML、sing-box JSON、Base64 / 明文分享链接"""
    t = text.strip().lstrip("\\ufeff")
    if t.startswith("{"):
        try:
            j = json.loads(t)
            if isinstance(j, dict) and isinstance(j.get("outbounds"), list):
                # outbounds 里混了非对象元素时以前 o.get 抛 AttributeError（只捕获了 ValueError），整个订阅更新失败；tag 统一成文本
                return [{"_sb": o, "name": str(o["tag"]), "type": o.get("type")} for o in j["outbounds"]
                        if isinstance(o, dict) and o.get("type") not in ("selector", "urltest", "direct", "block", "dns")
                        and o.get("tag") and o.get("server")], []
        except ValueError:
            pass
    if re.search(r"(?m)^[\\"']?(proxies|proxy-providers)[\\"']?\\s*:", t):
        y = sub_yaml(t)
        ps = y.get("proxies") if isinstance(y.get("proxies"), list) else []
        # 名称统一成文本：YAML 里 name: 2024 会解析成数字，后面的筛选正则 / 去重遇到数字会抛 TypeError，整个订阅更新失败
        out = [dict(p, name=str(p["name"]).strip()) for p in ps
               if isinstance(p, dict) and p.get("name") not in (None, "") and p.get("type") and str(p["name"]).strip()]
        # 读不出来的条目计入「无法识别已跳过」：以前静默丢弃，节点少了也没有任何提示
        errs = [f"第 {i} 个节点无法识别" for i, p in enumerate(ps, 1)
                if not (isinstance(p, dict) and p.get("name") not in (None, "") and p.get("type") and str(p["name"]).strip())]
        pv = y.get("proxy-providers") if isinstance(y.get("proxy-providers"), dict) else {}
        # 聚合配置：mihomo 自己会去拉 proxy-providers，sing-box 不会，这里由面板逐个下载展开
        for pname, prov in pv.items():
            if not isinstance(prov, dict) or not prov.get("url") or depth > 1 or fetch is None:
                continue
            try:
                sub, e2 = parse_sub_text(fetch(str(prov["url"])), fetch, depth + 1)
                names = {x["name"] for x in out}
                for x in sub:
                    if x["name"] in names:
                        x["name"] = f"{x['name']} [{pname}]"
                    out.append(x)
                errs += e2
            except Exception as e:
                errs.append(f"{pname}：{str(e)[:80]}")
        if out or ps or pv:
            return out, errs
        return [], errs or ["订阅是 mihomo 配置，但没读到 proxies 节点"]
    body = t
    if "://" not in t:
        try:
            body = b64d(re.sub(r"\\s+", "", t))
        except Exception:
            body = ""
    out, errs = [], []
    for i, line in enumerate([x.strip() for x in body.splitlines() if re.match(r"^\\s*[A-Za-z][\\w+.\\-]*://", x)], 1):
        try:
            p = parse_link(line)
            p["name"] = (p.get("name") or f"{p['type']}-{p['server']}:{p['port']}").strip()
            out.append(p)
        except Exception as e:
            errs.append(f"第 {i} 条：{e}")
    return out, errs


def _sub_get(url, ua):
    """下载订阅里嵌套的 proxy-providers 地址：先代理后直连"""
    last = None
    for use_proxy in ((True, False) if core_alive() else (False,)):
        try:
            dl_check(url, via_proxy=use_proxy)  # SSRF 防护：直连时校验 DNS，走代理时由代理解析（字面内网 IP 永远拦截）
        except IOError as e:
            last = e
            continue
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        try:
            with proxy_opener(use_proxy).open(req, timeout=30) as r:
                return read_full(r, 20 << 20).decode("utf-8", "ignore")
        except Exception as e:
            last = e
    raise IOError(str(last)[:120])


def _sub_nodes_save(s, nodes, errs, info, ua, note=""):
    """按订阅的筛选 / 排除过滤后写入面板缓存；返回 (节点数, 消息)"""
    flt, exc = s.get("filter") or "", s.get("exclude", DEFAULT_EXCLUDE) or ""
    rf, re_ = rx(flt) if flt else None, rx(exc) if exc else None
    nodes = [p for p in nodes if (not rf or rf.search(p["name"])) and not (re_ and re_.search(p["name"]))]
    ui = {}
    for part in info.split(";"):
        k, _, v = part.strip().partition("=")
        if k in ("upload", "download", "total", "expire") and v.strip().isdigit():
            ui[k.capitalize()] = int(v)
    write_json(sub_cache_path(s["name"]), {"name": s["name"], "updated": int(time.time()), "nodes": nodes,
                                           "info": ui, "skipped": len(errs), "ua": ua})
    return len(nodes), note + f"{len(nodes)} 个节点" + (f"（{len(errs)} 条无法识别已跳过）" if errs else "")


def _mihomo_cache_fallback(s, ua):
    """所有 UA 都没解析出节点时，用 mihomo 以前下载的 ./providers/<名>.yaml 兜底（刚从 mihomo 切到 sing-box 时用得上）。
    只在面板还没有这个订阅的缓存、或 mihomo 的缓存比面板缓存新时才用：以前只要第一个 UA 没解析出节点就直接读它，
    不再试下一个 UA，并照常报「N 个节点」——mihomo 缓存往往是切换内核前的旧文件，订阅明明换了节点，
    「更新」一直显示成功、节点却一直是旧的。返回 (节点, 错误) 或 None"""
    cache = os.path.join(CONF_DIR, "providers", safe(s["name"]) + ".yaml")
    try:
        mt = os.path.getmtime(cache)
    except OSError:
        return None
    try:
        if os.path.getmtime(sub_cache_path(s["name"])) >= mt:
            return None
    except OSError:
        pass
    try:
        with open(cache, encoding="utf-8", errors="ignore") as f:
            nodes, errs = parse_sub_text(f.read(), lambda u, _ua=ua: _sub_get(u, _ua))
    except (OSError, ValueError):
        return None
    return (nodes, errs) if nodes else None


def sb_fetch_sub(s, gh=""):
    """下载并解析一个订阅，写入缓存；返回 (节点数, 消息)。
    依次试 SB_UA 里的每个 UA（每个 UA 先代理后直连），第一个解析出节点的结果写入缓存；
    全部失败时抛 IOError，面板缓存里上次的节点保持不变（不会把旧节点当成这次更新的结果报成功）"""
    last = ""
    for ua in SB_UA:
        try:
            data = None
            for use_proxy in ((True, False) if core_alive() else (False,)):
                try:
                    dl_check(s["url"], via_proxy=use_proxy)  # SSRF 防护：直连时校验 DNS，走代理时由代理解析（字面内网 IP 永远拦截）
                except IOError as e:
                    last = str(e)[:150]
                    continue
                # 每次新建 Request：走代理失败后 urllib 已把代理地址写进 Request，复用会让「直连重试」仍然连到代理
                req = urllib.request.Request(s["url"], headers={"User-Agent": ua})
                try:
                    with proxy_opener(use_proxy).open(req, timeout=30) as r:
                        data = read_full(r, 20 << 20)
                        info = r.headers.get("subscription-userinfo") or ""
                    break
                except Exception as e:
                    last = str(e)[:150]
            if data is None:
                continue
            nodes, errs = parse_sub_text(data.decode("utf-8", "ignore"), lambda u, _ua=ua: _sub_get(u, _ua))
            if not nodes:
                last = "没有解析出节点" + ("：" + errs[0] if errs else "")
                try:  # 留一份原文方便排查（只保留最近一次）；原文含节点密码，600 权限
                    with _DEBUG_LOCK:
                        dbg = os.path.join(PANEL_DIR, "sub-debug.txt")
                        with open(dbg, "wb") as f:
                            f.write(data[:2 << 20])
                        os.chmod(dbg, 0o600)
                except OSError:
                    pass
                continue  # 换下一个 UA 再试（机场常按 UA 返回不同格式）
            return _sub_nodes_save(s, nodes, errs, info, ua)
        except Exception as e:
            last = str(e)[:150]
    fb = _mihomo_cache_fallback(s, SB_UA[0])
    if fb is not None:
        return _sub_nodes_save(s, fb[0], fb[1], "", "", f"订阅这次没有解析出节点（{(last or '下载失败')[:80]}），暂用 mihomo 之前下载的缓存：")
    raise IOError((last or "下载失败") + ("（已保留上次的节点）" if os.path.isfile(sub_cache_path(s["name"])) else ""))


def _cached_names(name):
    """面板缓存里某订阅的节点名列表（没有缓存时 None）：更新前后对比，节点没变时在结果里注明"""
    try:
        with open(sub_cache_path(name)) as f:
            return [str(p.get("name") or "") for p in json.load(f).get("nodes") or [] if isinstance(p, dict)]
    except (OSError, ValueError, AttributeError):
        return None


def sb_update_subs(names=None, d=None):
    """并发更新订阅：每个订阅一个线程（单个订阅仍串行走 UA/代理/直连回退），整体耗时取决于最慢的一个"""
    d = d or load()
    gh = d.get("gh_proxy") or ""
    subs = [s for s in d["subs"] if not names or s["name"] in names]
    ok, bad = [], []
    res_lock = threading.Lock()

    def one(s):
        try:
            before = _cached_names(s["name"])
            n, msg = sb_fetch_sub(s, gh)
            if before and before == _cached_names(s["name"]):
                msg += "（节点与上次相同）"
            with res_lock:
                ok.append(f"{s['name']}：{msg}")
        except Exception as e:
            with res_lock:
                bad.append(f"{s['name']}：{e}")

    ths = [threading.Thread(target=one, args=(s,)) for s in subs]
    for t in ths:
        t.start()
    end = time.time() + SUBS_WAIT  # 整体最多等 SUBS_WAIT 秒（以前逐个 join(180)，几个订阅都卡住时要等 N×180 秒）
    for t in ths:
        t.join(max(0.1, end - time.time()))
    with res_lock:  # 超时还没结束的线程之后再写结果也不影响这里返回的快照
        ok_out = list(ok)
        bad_out = list(bad) + [f"{s['name']}：下载超时" for s, t in zip(subs, ths) if t.is_alive()]
    # 只让缓存过期、保留旧名单：下次 provider_nodes 拿新名单与它比较，有变化才能推进 provider_rev()。
    # 以前把 data 清成 None，比较时没有旧名单，rev 永远不变：sing-box 下订阅更新出新地区后，/api/state 的策略组名单
    # （策略下拉框、按设备分流目标）一直按旧缓存返回，直到下次改设置
    PROV["t"] = 0
    return ok_out, bad_out


'''
_SRC['shunt.sb_nodes'] = '''"""节点：mihomo 字段 → sing-box outbound"""
import re
def _sb_tls(p, sni_key="servername", force=False):
    if not (p.get("tls") or force):
        return None
    t = {"enabled": True}
    sni = p.get(sni_key) or p.get("sni") or p.get("servername")
    if sni:
        t["server_name"] = str(sni)
    if p.get("skip-cert-verify"):
        t["insecure"] = True
    if p.get("alpn"):
        t["alpn"] = [str(a) for a in (p["alpn"] if isinstance(p["alpn"], list) else str(p["alpn"]).split(","))]
    if p.get("disable-sni"):
        t["disable_sni"] = True
    fp = p.get("client-fingerprint")
    ro = p.get("reality-opts")
    if ro:
        t["reality"] = {"enabled": True, "public_key": str(ro.get("public-key") or ""), "short_id": str(ro.get("short-id") or "")}
        fp = fp or "chrome"
    if fp and fp not in ("none",):
        t["utls"] = {"enabled": True, "fingerprint": "chrome" if fp == "random" else str(fp)}
    return t


def _sb_transport(p):
    net = (p.get("network") or "tcp").lower()
    if net == "ws":
        o = p.get("ws-opts") or {}
        path = str(o.get("path") or "/")
        hdr = {k: (v[0] if isinstance(v, list) else str(v)) for k, v in (o.get("headers") or {}).items()}
        if o.get("v2ray-http-upgrade"):
            t = {"type": "httpupgrade", "path": path}
            if hdr.get("Host"):
                t["host"] = hdr.pop("Host")
            if hdr:
                t["headers"] = hdr
            return t
        t = {"type": "ws", "path": path}
        m = re.search(r"[?&]ed=(\\d+)", path)
        if m:
            t["path"] = re.sub(r"[?&]ed=\\d+", "", path) or "/"
            t["max_early_data"], t["early_data_header_name"] = int(m.group(1)), "Sec-WebSocket-Protocol"
        elif o.get("max-early-data"):
            t["max_early_data"] = int(o["max-early-data"])
            t["early_data_header_name"] = o.get("early-data-header-name") or "Sec-WebSocket-Protocol"
        if hdr:
            t["headers"] = hdr
        return t
    if net == "grpc":
        return {"type": "grpc", "service_name": str((p.get("grpc-opts") or {}).get("grpc-service-name") or "")}
    if net == "h2":
        o = p.get("h2-opts") or {}
        t = {"type": "http", "path": str(o.get("path") or "/")}
        if o.get("host"):
            t["host"] = [str(h) for h in (o["host"] if isinstance(o["host"], list) else [o["host"]])]
        return t
    if net == "http":
        o = p.get("http-opts") or {}
        path = o.get("path") or ["/"]
        t = {"type": "http", "path": str(path[0] if isinstance(path, list) else path), "method": str(o.get("method") or "GET")}
        hosts = (o.get("headers") or {}).get("Host")
        if hosts:
            t["host"] = [str(h) for h in (hosts if isinstance(hosts, list) else [hosts])]
        return t
    if net in ("tcp", ""):
        return None
    raise ValueError(f"sing-box 不支持传输方式 {net}")


def _mbps(v):
    m = re.match(r"\\s*(\\d+)", str(v or ""))
    return int(m.group(1)) if m else 0


def _sb_ports(v):
    """端口跳跃范围 → sing-box server_ports：「20000-30000」「20000:30000」「443,8443」「[20000-30000, 443]」都转成 ["20000:30000", …]，
    单个端口写成 "443:443"。以前已是「a:b」写法的会被拼成 "a:b:a:b"，sing-box 启动报错"""
    items = v if isinstance(v, (list, tuple)) else str(v).replace("/", ",").split(",")
    out = []
    for x in items:
        m = re.fullmatch(r"(\\d+)\\s*(?:[-:]\\s*(\\d+))?", str(x).strip())
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if 0 < min(a, b) and max(a, b) <= 65535:
            out.append(f"{min(a, b)}:{max(a, b)}")
    return out


def sb_outbound(p, tag):
    """返回 sing-box outbound；不支持的协议抛 ValueError"""
    if p.get("_sb"):
        o = dict(p["_sb"])
        o["tag"] = tag
        return o
    t = str(p.get("type") or "").lower()
    o = {"tag": tag, "server": str(p.get("server") or ""), "server_port": int(p.get("port") or 0)}
    if not o["server"] or not o["server_port"]:
        raise ValueError("缺少服务器或端口")
    if t == "ss":
        o.update(type="shadowsocks", method=str(p.get("cipher") or ""), password=str(p.get("password") or ""))
        pl, po = p.get("plugin"), p.get("plugin-opts") or {}
        if pl == "obfs":
            o["plugin"], o["plugin_opts"] = "obfs-local", f"obfs={po.get('mode', 'http')};obfs-host={po.get('host', 'bing.com')}"
        elif pl == "v2ray-plugin":
            opts = [f"mode={po.get('mode', 'websocket')}"] + ([f"host={po['host']}"] if po.get("host") else []) + \\
                   ([f"path={po['path']}"] if po.get("path") else []) + (["tls"] if po.get("tls") else [])
            o["plugin"], o["plugin_opts"] = "v2ray-plugin", ";".join(opts)
        elif pl:
            raise ValueError(f"sing-box 不支持 ss 插件 {pl}")
        if p.get("udp-over-tcp"):
            o["udp_over_tcp"] = True
    elif t == "vmess":
        o.update(type="vmess", uuid=str(p.get("uuid") or ""), security=str(p.get("cipher") or "auto"), alter_id=int(p.get("alterId") or 0))
    elif t == "vless":
        o.update(type="vless", uuid=str(p.get("uuid") or ""), packet_encoding="xudp")
        if p.get("flow"):
            o["flow"] = str(p["flow"])
    elif t == "trojan":
        o.update(type="trojan", password=str(p.get("password") or ""))
        o["tls"] = _sb_tls(p, "sni", force=True)
    elif t == "hysteria2":
        o.update(type="hysteria2", password=str(p.get("password") or ""))
        if p.get("ports"):
            o["server_ports"] = _sb_ports(p["ports"])
        if p.get("obfs"):
            o["obfs"] = {"type": str(p["obfs"]), "password": str(p.get("obfs-password") or "")}
        hi = _mbps(p.get("hop-interval") or p.get("hop_interval") or p.get("hopInterval"))  # 端口跳跃间隔（秒），mihomo 写 hop-interval: 30
        if o.get("server_ports") and hi:
            o["hop_interval"] = f"{hi}s"
        for k, sk in (("up", "up_mbps"), ("down", "down_mbps")):
            if _mbps(p.get(k)):
                o[sk] = _mbps(p.get(k))
        o["tls"] = _sb_tls(p, "sni", force=True)
    elif t == "tuic":
        o.update(type="tuic", uuid=str(p.get("uuid") or ""), password=str(p.get("password") or ""),
                 congestion_control=str(p.get("congestion-controller") or "bbr"),
                 udp_relay_mode=str(p.get("udp-relay-mode") or "native"))
        if p.get("reduce-rtt"):
            o["zero_rtt_handshake"] = True
        o["tls"] = _sb_tls(dict(p, alpn=p.get("alpn") or ["h3"]), "sni", force=True)
    elif t == "hysteria":
        o.update(type="hysteria", up_mbps=_mbps(p.get("up")) or 10, down_mbps=_mbps(p.get("down")) or 50)
        if p.get("auth-str") or p.get("auth_str"):
            o["auth_str"] = str(p.get("auth-str") or p.get("auth_str"))
        if p.get("obfs"):
            o["obfs"] = str(p["obfs"])
        o["tls"] = _sb_tls(p, "sni", force=True)
    elif t == "anytls":
        o.update(type="anytls", password=str(p.get("password") or ""))
        o["tls"] = _sb_tls(p, "sni", force=True)
    elif t in ("http", "socks5"):
        o["type"] = "http" if t == "http" else "socks"
        if t == "socks5":
            o["version"] = "5"
            if p.get("tls"):  # sing-box 的 socks 出站没有 tls 字段：以前照样写进去，整份配置校验失败（unknown field）
                raise ValueError("sing-box 的 SOCKS5 出站不支持 TLS")
        if p.get("username"):
            o["username"], o["password"] = str(p["username"]), str(p.get("password") or "")
    elif t == "ssh":
        o.update(type="ssh", user=str(p.get("username") or "root"))
        if p.get("password"):
            o["password"] = str(p["password"])
        if p.get("private-key"):
            o["private_key"] = str(p["private-key"])
    else:
        raise ValueError(f"sing-box 不支持 {t or '未知'} 协议")
    if t in ("vmess", "vless", "http"):
        tls = _sb_tls(p, "servername")
        if tls:
            o["tls"] = tls
    if t in ("vmess", "vless", "trojan"):
        tr = _sb_transport(p)
        if tr:
            o["transport"] = tr
    if o.get("tls") is None:
        o.pop("tls", None)
    elif t in ("hysteria2", "tuic", "hysteria"):
        # QUIC 协议不能用 uTLS：sing-box 每次连接都报 unsupported usage for uTLS（延迟测试显示超时）。
        # 机场的 Clash 订阅常给所有节点都带 client-fingerprint，mihomo 对 hy2 / tuic 直接忽略，sing-box 这里也去掉
        o["tls"].pop("utls", None)
    if p.get("udp") is False and t in ("ss",):
        o["network"] = "tcp"
    return o
'''
_SRC['shunt.sb_rules'] = '''"""规则：mihomo 规则字符串 → sing-box 路由规则"""
import ipaddress
import re

from .ruleutil import RULE_FLAGS, _rs_tag, _wildcard_regex, idna_domain, split_rule
from .sb_common import SB_GEO

# 值里可能带逗号的规则类型：mihomo 把类型与策略之间的所有项拼回去当值（如 DOMAIN-REGEX,^a{1,3}\\.com$）
_REGEX_TYPES = {"DOMAIN-REGEX", "PROCESS-NAME-REGEX", "PROCESS-PATH-REGEX", "URL-REGEX"}


def geo_srs_entry(kind, name):
    """GEOIP/GEOSITE → (tag, 规则集条目)。SBRules.use_geo 与 sb_geo_rule_sets 共用，避免两处硬编码同一 URL 格式。"""
    tag = _rs_tag(kind, name)
    return tag, {"kind": "geo", "url": f"{SB_GEO}/{kind}/{name.lower()}.srs", "path": f"{kind}-{name.lower()}.srs"}


def _split_top(s):
    """「(A,b),(C,d)」按最外层逗号拆分"""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def _cidr(val):
    """单个 IP（规则集文件里常见、没经过 norm_rule）补成 /32、/128；「[2001:db8::1]」去掉方括号"""
    v = val.strip()
    if "/" in v:
        return v
    try:
        ip = ipaddress.ip_address(v.strip("[]"))
        return f"{ip}/{32 if ip.version == 4 else 128}"
    except ValueError:
        return v


class SBRules:
    """收集翻译过程中用到的规则集、入站，以及无法翻译的条目"""

    def __init__(self, cfg, inbounds, alias, rule_sets):
        self.cfg, self.inbounds, self.alias, self.rule_sets = cfg, inbounds, alias, rule_sets
        self.warn = []
        # 匹配目标 IP 的规则集（ipcidr / classical）：与 mihomo 一致，引用它们且没写 no-resolve / src 时先解析域名再匹配。
        # build_singbox 会按已转换好的 classical 文件里有没有 IP 条目再收窄
        self.ip_sets = {k for k, v in ((cfg or {}).get("rule-providers") or {}).items()
                        if isinstance(v, dict) and v.get("behavior") in ("ipcidr", "classical")}

    def use_geo(self, kind, name):
        tag, entry = geo_srs_entry(kind, name)
        if tag not in self.rule_sets:
            self.rule_sets[tag] = entry
        return tag

    def cond(self, typ, val, src=False):
        """单个匹配条件 → sing-box 规则字段（dict）；不支持时返回 None。
        src：规则带「,src」参数（mihomo 按来源地址匹配 IP-CIDR / GEOIP / RULE-SET），以前被忽略、当成目标地址匹配"""
        typ, val = typ.strip().upper(), val.strip()
        if typ in ("DOMAIN", "DOMAIN-SUFFIX", "DOMAIN-WILDCARD"):  # 规则集文件里的中文域名同样转成 punycode
            val = idna_domain(val)
        if typ == "DOMAIN":
            return {"domain": [val.lower()]}
        if typ == "DOMAIN-SUFFIX":  # 规则集文件里的值没经过 norm_rule：「+.a.com」「*.a.com」同样去掉前缀，否则按字面永远不命中
            return {"domain_suffix": [re.sub(r"^(?:\\+\\.|\\*\\.|\\.)+", "", val.lower())]}
        if typ == "DOMAIN-KEYWORD":
            return {"domain_keyword": [val.lower()]}
        if typ == "DOMAIN-REGEX":
            return {"domain_regex": [val]}
        if typ == "DOMAIN-WILDCARD":  # 「*」可跨多级（与 mihomo 一致），不能用 _mh_domain（那是域名集合里「*」只算一级的写法）
            v = val.lower()
            return {"domain_regex": [_wildcard_regex(v)]} if ("*" in v or "?" in v) else {"domain": [v]}
        if typ in ("IP-CIDR", "IP-CIDR6"):
            return {"source_ip_cidr" if src else "ip_cidr": [_cidr(val)]}
        if typ in ("SRC-IP-CIDR",):
            return {"source_ip_cidr": [_cidr(val)]}
        if typ in ("DST-PORT", "SRC-PORT", "IN-PORT"):
            key = {"DST-PORT": "port", "SRC-PORT": "source_port", "IN-PORT": "port"}[typ]
            ports, ranges = [], []
            for x in val.replace("/", ",").split(","):
                x = x.strip()
                if re.fullmatch(r"\\d+", x):
                    ports.append(int(x))
                elif re.fullmatch(r"\\d+-\\d+", x):
                    ranges.append(x.replace("-", ":"))
            c = {}
            if ports:
                c[key] = ports
            if ranges:
                c[key + "_range"] = ranges
            if typ == "IN-PORT":
                return None
            return c or None
        if typ == "NETWORK":
            return {"network": [val.lower()]}
        if typ == "GEOSITE":
            return {"rule_set": [self.use_geo("geosite", val)]}
        if typ == "GEOIP":
            if val.lower() in ("private", "lan"):
                return {"source_ip_is_private" if src else "ip_is_private": True}
            r = {"rule_set": [self.use_geo("geoip", val)]}
            return dict(r, rule_set_ip_cidr_match_source=True) if src else r
        if typ == "RULE-SET":
            if val not in self.rule_sets:
                return None
            return {"rule_set": [val], "rule_set_ip_cidr_match_source": True} if src else {"rule_set": [val]}
        if typ == "IN-TYPE":
            want = {"TPROXY": "tproxy-in", "TUN": "tun-in", "MIXED": "mixed-in", "HTTP": "mixed-in", "SOCKS5": "mixed-in"}
            tags = sorted({want[x] for x in val.upper().split("/") if want.get(x) in self.inbounds})
            return {"inbound": tags} if tags else False
        if typ in ("PROCESS-NAME", "PROCESS-PATH"):
            return {"process_name" if typ == "PROCESS-NAME" else "process_path": [val]}
        if typ == "PROCESS-PATH-REGEX":
            return {"process_path_regex": [val]}
        return None

    def logical(self, typ, inner):
        """AND / OR / NOT → 规则 dict / None（不支持）/ False（永不命中）/ True（永远命中）。
        以前任何一个子条件「永不命中」（如 TProxy 模式下的 IN-TYPE,TUN）就把整条规则当成永不命中：
        OR 里的其余条件、NOT 取反后本该永远成立的条件都被一起丢掉"""
        subs = []
        for part in _split_top(inner):
            part = part.strip()
            if part.startswith("(") and part.endswith(")"):
                part = part[1:-1]
            r = self.match(part)
            if r is None:
                return None
            subs.append(r)
        if not subs:
            return None
        if typ == "NOT":
            if len(subs) == 1 and isinstance(subs[0], bool):
                return not subs[0]
            if any(x is False for x in subs):  # NOT(A 且 永不命中) = 永远命中
                return True
            subs = [x for x in subs if x is not True]
            if not subs:
                return False
            r = dict(subs[0]) if len(subs) == 1 else {"type": "logical", "mode": "and", "rules": subs}
            r["invert"] = not r.get("invert", False)
            return r
        if typ == "AND":
            if any(x is False for x in subs):
                return False
            subs = [x for x in subs if x is not True]
            if not subs:
                return True
        else:  # OR
            if any(x is True for x in subs):
                return True
            subs = [x for x in subs if x is not False]
            if not subs:
                return False
        if len(subs) == 1:
            return subs[0]
        return {"type": "logical", "mode": typ.lower(), "rules": subs}

    def match(self, body, flags=()):
        """不含策略的匹配部分 → 规则 dict / None（不支持）/ False（永不命中）。
        flags：规则末尾的参数（no-resolve / src）；逻辑规则的子规则自带的参数（如 (IP-CIDR,a/24,src)）在这里拆出"""
        m = re.match(r"^(AND|OR|NOT)\\s*,\\s*\\((.*)\\)$", body.strip(), re.I)
        if m:
            return self.logical(m.group(1).upper(), m.group(2))
        typ, _, val = body.partition(",")
        if not val:
            return None
        items = val.split(",")
        k = len(items)
        while k > 1 and items[k - 1].strip().lower() in RULE_FLAGS:
            k -= 1
        flags = {x.strip().lower() for x in items[k:]} | {x.strip().lower() for x in flags}
        # DOMAIN-REGEX 等正则的值本身可能带逗号（^a{1,3}\\.com$）：以前只取第一项，被截成「^a{1」永远不命中
        v = ",".join(items[:k]) if typ.strip().upper() in _REGEX_TYPES else items[0]
        return self.cond(typ, v, src="src" in flags)

    def action(self, pol):
        pol = self.alias.get(pol.strip(), pol.strip())
        if pol in ("REJECT",):
            return {"action": "reject"}
        if pol == "REJECT-DROP":
            return {"action": "reject", "method": "drop"}
        if pol in ("DIRECT", "COMPATIBLE"):
            return {"action": "route", "outbound": "DIRECT"}
        if pol == "PASS":
            return None
        return {"action": "route", "outbound": pol}

    def rule(self, line):
        """完整规则「类型,值,策略[,no-resolve][,src]」→ (规则 dict 或 None, 是否需要先解析域名)"""
        body, pol, flags = split_rule(line)
        flags = [x.lower() for x in flags]
        typ = body.split(",")[0].strip().upper()
        if typ == "MATCH":
            return None, False
        act = self.action(pol)
        r = self.match(body, flags)
        if act is None or r is False:
            return None, False
        if r is None:
            self.warn.append(f"规则「{line[:80]}」sing-box 不支持，已跳过")
            return None, False
        if r is True:  # 逻辑规则化简后永远命中：sing-box 规则至少要一个条件，用「TCP 或 UDP」表示全部流量
            r = {"network": ["tcp", "udp"]}
        r = dict(r, **act)
        if typ in ("AND", "OR", "NOT"):
            needs = _logical_needs_resolve(body, self.ip_sets)
        elif "no-resolve" in flags or "src" in flags or r.get("ip_is_private"):  # 按来源匹配不需要解析目标域名
            needs = False
        elif typ == "RULE-SET":  # 以前 RULE-SET 一律不解析：ipcidr / classical 规则集里的 IP 条目对域名连接永远不命中
            needs = body.split(",", 1)[1].strip() in self.ip_sets
        else:
            needs = typ in ("GEOIP", "IP-CIDR", "IP-CIDR6")
        return r, needs


def _logical_needs_resolve(body, ip_sets=()):
    """逻辑规则里有不带 no-resolve / src 的 IP 类子条件（含 ipcidr / classical 规则集）时，mihomo 同样会先解析域名再匹配。
    以前只看行首类型：AND,((NETWORK,UDP),(IP-CIDR,1.2.3.0/24)) 在 sing-box 下域名连接永远不解析，规则对域名访问不生效"""
    for m in re.finditer(r"\\(\\s*(GEOIP|IP-CIDR6?|RULE-SET)\\s*,([^()]*)\\)", body, re.I):
        typ = m.group(1).upper()
        args = [x.strip().lower() for x in m.group(2).split(",")]
        if "no-resolve" in args[1:] or "src" in args[1:] or (typ == "GEOIP" and args[0] in ("private", "lan")):
            continue
        if typ == "RULE-SET" and m.group(2).split(",")[0].strip() not in ip_sets:
            continue
        return True
    return False
'''
_SRC['shunt.sb_dns'] = '''"""DNS"""
import ipaddress
import re
from urllib.parse import urlsplit

from .base import CN_DNS, DNS_DEFAULT, G_SEL
from .mihomo import dns_cfg, fix_dns_policy, is_ip_addr, policy_parts
from .ruleutil import AB_RULE_ONLY, _mh_domain, adblock_scopes, dns_policy_of
def _sb_dns_server(addr, tag, boot, warn, via=None):
    """mihomo DNS 地址（含 #策略组）→ sing-box DNS server。
    via：没写 #策略组 时经哪个出站发出（respect-rules 开启时的代理 DNS）；不传就是直连"""
    addr = addr.strip()
    pol = dns_policy_of(addr)
    base = addr.split("#", 1)[0]
    s = {"tag": tag}
    if base.startswith("system"):
        s["type"] = "local"
    elif base.startswith("dhcp://"):
        s["type"] = "dhcp"
        ifc = base[7:]
        if ifc and ifc != "system":
            s["interface"] = ifc
    else:
        m = re.match(r"^(https|tls|quic|h3|udp|tcp)://(.+)$", base)
        scheme, rest = (m.group(1), m.group(2)) if m else ("udp", base)
        s["type"] = scheme
        if is_ip_addr(rest.strip("[]")):
            # 以前裸 IPv6（如 2001:4860:4860::8888，校验允许）交给 urlsplit 取端口会抛 ValueError，整份配置生成失败（500）；
            # 现在整体是 IP 就直接用（不带端口）
            u, host = urlsplit("x://"), rest.strip("[]")
        else:
            u = urlsplit("x://" + rest)
            host = u.hostname or rest
        s["server"] = host
        if u.port:
            s["server_port"] = u.port
        if scheme in ("https", "h3") and u.path and u.path != "/dns-query":
            s["path"] = u.path
        if not is_ip_addr(host) and boot:
            s["domain_resolver"] = boot
    if pol in ("DIRECT", "COMPATIBLE", "PASS"):
        # 「#DIRECT」：sing-box 的 DIRECT 是空的 direct 出站，detour 指向它启动即报错
        # （detour to an empty direct outbound makes no sense），不写 detour 就是直连
        pass
    elif pol in ("REJECT", "REJECT-DROP"):  # sing-box 没有 REJECT 出站，detour 指向不存在的出站整份配置加载失败
        warn.append(f"DNS「{addr}」指定的策略 {pol} sing-box 不支持，已按直连处理")
    elif pol:
        s["detour"] = pol
    elif via:
        s["detour"] = via
    return s


def _domestic(addr):
    """国内 / 本机 / 内网 DNS：respect-rules 下 mihomo 按规则也是直连发出"""
    base = addr.split("#", 1)[0]
    if CN_DNS.search(base) or base.startswith(("system", "dhcp://", "rcode://")):
        return True
    try:
        host = urlsplit("x://" + re.sub(r"^[a-z0-9]+://", "", base)).hostname or ""
    except ValueError:  # 「[::1」这类写错的地址 urlsplit 会抛异常，以前整份配置生成 500
        host = ""
    try:  # 裸 IPv6（如 fe80::1，不带方括号）urlsplit 取不出主机，先整体按 IP 解析
        ip = ipaddress.ip_address(base.strip().strip("[]"))
    except ValueError:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
    # 以前用 ip.is_private：100.64.0.0/10（CGNAT，如 Tailscale MagicDNS 100.100.100.100）不算内网，
    # respect-rules 下被改成经节点发出，节点那头根本到不了 → 这些域名全部解析失败。现在按明确的内网段判断（与 Go 一致）
    return any(ip in n for n in _LOCAL_NETS)


_LOCAL_NETS = [ipaddress.ip_network(c) for c in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.0/8",
                                                 "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10")]


def sb_dns(d, cfg, policies, rs, alias, v6):
    dc = dns_cfg(d)
    warn = rs.warn
    fixd = lambda x: (x.split("#", 1)[0] + "#" + alias[dns_policy_of(x)]) if dns_policy_of(x) in alias else x
    fx = lambda xs: [fix_dns_policy(fixd(x), policies) for x in xs]
    servers, tags = [], {}

    # respect-rules：mihomo 的 nameserver / nameserver-policy 查询连接遵循分流规则（国外 DNS 经节点发出）。
    # sing-box 以前不读这个开关：没写 #策略组 的代理 DNS 直接从 WAN 明文发出（泄露 / 被干扰），防泄露检测却显示正常。
    # 现在这类服务器经节点选择发出（国内 / 内网 DNS 照旧直连，与 mihomo 按规则的结果一致）
    via = G_SEL if dc["respect_rules"] and G_SEL in policies else None

    def server(addr, boot="dns-boot", routed=False):
        if addr.startswith("rcode://"):
            return addr
        det = via if routed and not _domestic(addr) else None
        key = (addr, det)  # 同一地址作节点域名解析（直连）和代理 DNS（经节点）时分成两个服务器，避免节点域名解析绕回节点
        if key in tags:
            return tags[key]
        tag = f"dns-{len(tags) + 1}"
        servers.append(_sb_dns_server(addr, tag, boot, warn, det))
        tags[key] = tag
        return tag

    boot_list = list(dc["default"] or DNS_DEFAULT["default"])
    servers.append(_sb_dns_server(boot_list[0], "dns-boot", None, warn))
    direct = fx(dc["direct"] or DNS_DEFAULT["direct"])
    proxy = fx(dc["proxy"])
    t_direct = server(direct[0])
    t_proxy = server(proxy[0], routed=True) if proxy else None
    t_node = server(dc["pserver"][0].split("#", 1)[0]) if dc["pserver"] else server(direct[0].split("#", 1)[0])
    final = t_proxy or t_direct
    rules = []
    fake = dc["mode"] == "fake-ip"
    fc = _fake_filter_cond(dc, rs) if fake else None

    def add_route(c, tgt):
        """按域名指定上游的 DNS 规则（广告白名单 / 自定义 DNS 策略）。fake-ip 模式下 mihomo 的 nameserver-policy 只决定
        用哪个上游，A / AAAA 照样返回 fake-ip（只有 fake-ip-filter 和 hosts 拿真实 IP）。以前这些规则排在 fakeip 规则之前，
        sing-box 直接返回真实 IP：不能嗅探的连接丢了域名、域名规则失效，白名单里的国外域名还拿到国内 DNS（可能被污染）的结果"""
        if not fake or tgt.get("action") != "route":
            rules.append(dict(c, **tgt))
            return
        if fc:  # 同时在 Fake-IP 过滤名单里的：照旧用指定的上游解析真实 IP
            rules.append({"type": "logical", "mode": "and", "rules": [c, fc], **tgt})
        rules.append(dict(c, query_type=["A", "AAAA"], action="route", server="dns-fake", rewrite_ttl=1))
        rules.append(dict(c, **tgt))  # 其余查询类型（HTTPS / TXT / MX 等）走指定的上游

    ab = d.get("adblock") or {}
    # 按类型认广告拦截规则集：以前按名字前缀「ad-」认，用户自己的规则集叫 ad-xxx（哪怕目标是直连）也会被 DNS 层拦成 NXDOMAIN
    ab_sets = [k for k, v in rs.rule_sets.items() if k.startswith("ad-") and v.get("kind") == "adblock"]
    if ab.get("enabled") and ab.get("dns") and ab_sets:
        if "ad-allow" in ab_sets:
            add_route({"rule_set": ["ad-allow"]}, {"action": "route", "server": t_direct})
        # 与 mihomo 一致：限定国内直连 / 国外代理的列表只在路由层拦。以前 sing-box 在 DNS 层把它们一律拦成 NXDOMAIN，
        # 「只拦国内直连」的列表连走代理的国外域名也拦了（反之亦然），切换内核后拦截范围不同
        scoped = adblock_scopes(d)
        bl = [k for k in ab_sets if k not in AB_RULE_ONLY and k not in scoped]
        if bl:
            rules.append({"rule_set": bl, "action": "predefined", "rcode": "NXDOMAIN"})

    def policy_cond(match):
        kind, names = policy_parts(match)
        if kind == "geosite":
            return {"rule_set": [rs.use_geo("geosite", n) for n in names]}
        if kind == "rule-set":
            have = [n for n in names if n in rs.rule_sets]
            return {"rule_set": have} if have else None
        c = {}
        for n in names:
            k, v = _mh_domain(n)
            c.setdefault(k, []).append(v)
        return c

    def target(servers_):
        s0 = servers_[0]
        if s0.startswith("rcode://"):
            rc = {"success": "NOERROR", "format_error": "FORMERR", "server_failure": "SERVFAIL", "name_error": "NXDOMAIN",
                  "not_implemented": "NOTIMP", "refused": "REFUSED"}.get(s0[8:], "NXDOMAIN")
            return {"action": "predefined", "rcode": rc}
        return {"action": "route", "server": server(s0, routed=True)}

    for pe in dc["policies"]:
        c = policy_cond(pe["match"])
        if c:
            add_route(c, target(fx(pe["servers"])))
        else:
            warn.append(f"DNS 策略「{pe['match']}」引用的规则集 sing-box 中不存在，已跳过")
    exact = {}
    for h in dc["hosts"]:
        ips = [v for v in h["value"] if is_ip_addr(v)]
        if h["domain"].startswith(("+.", "*.")) or not ips:
            warn.append(f"hosts「{h['domain']}」sing-box 只支持精确域名 → IP，已跳过")
            continue
        # 同一域名写了多条（如一条 IPv4、一条 IPv6）：与 mihomo_hosts 一样合并去重。以前后一条覆盖前一条，sing-box 下丢记录
        vals = exact.setdefault(h["domain"], [])
        vals += [v for v in ips if v not in vals]
    if exact:
        servers.append({"type": "hosts", "tag": "dns-hosts", "predefined": exact})
        # 放在最前：mihomo 的 hosts 先于 nameserver-policy / 广告拦截生效；以前排在 DNS 策略之后，
        # 同时被 DNS 策略（或拦截列表）覆盖的域名在 sing-box 下 hosts 不生效，两个内核结果不同
        rules.insert(0, {"domain": sorted(exact), "action": "route", "server": "dns-hosts"})
    cn_cond = {"rule_set": [rs.use_geo("geosite", "cn"), rs.use_geo("geosite", "private")]}
    if "cn-apps" in rs.rule_sets:  # 内置国内 App 名单与 geosite:cn 一样走直连 DNS
        cn_cond["rule_set"].insert(0, "cn-apps")
    if fake:
        # v7.5.9 曾把范围从 198.18.0.0/15 缩小到 /16，导致老版本分配的 198.19.x.x 映射变孤儿
        # （sing-box 不认识 → missing fakeip record）。/15 是 /16 的超集，含 198.18.x 和 198.19.x
        servers.append({"type": "fakeip", "tag": "dns-fake", "inet4_range": "198.18.0.0/15",
                        **({"inet6_range": "fc00::/18"} if v6 else {})})
        if fc:  # Fake-IP 过滤名单：按国内 / 国外分流到真实 DNS
            if dc["policy"] and t_proxy:
                rules.append({"type": "logical", "mode": "and", "rules": [fc, cn_cond], "action": "route", "server": t_direct})
            rules.append(dict(fc, action="route", server=final))
        # TTL 改成 1（与 mihomo 的 fake-ip 一致）：sing-box 默认 600 秒，内核被强制结束或切换内核后映射丢失时，
        # 客户端还会拿缓存的 198.18.x 连 10 分钟，日志刷「missing fakeip record」，期间这些网站都打不开
        rules.append({"query_type": ["A", "AAAA"], "action": "route", "server": "dns-fake", "rewrite_ttl": 1})
    if dc["policy"] and t_proxy:
        rules.append(dict(cn_cond, action="route", server=t_direct))
    out = {"servers": servers, "rules": rules, "final": final, "strategy": "prefer_ipv4" if v6 else "ipv4_only",
           "cache_capacity": 4096}
    return out, t_node, t_direct, t_proxy



def _fake_filter_cond(dc, rs):
    """Fake-IP 过滤名单 → sing-box DNS 规则条件（没有条目返回 None）"""
    conds = {}
    for x in dc["fake_filter"]:
        if x.lower().startswith("geosite:"):
            conds.setdefault("rule_set", []).append(rs.use_geo("geosite", x[8:]))
        elif x.lower().startswith("rule-set:"):
            if x[9:] in rs.rule_sets:
                conds.setdefault("rule_set", []).append(x[9:])
        elif x.strip():
            k, v = _mh_domain(x)
            conds.setdefault(k, []).append(v)
    if not conds:
        return None
    parts = [{k: v} for k, v in conds.items() if k == "rule_set"]
    dom = {k: v for k, v in conds.items() if k != "rule_set"}
    if dom:
        parts.insert(0, dom)
    return parts[0] if len(parts) == 1 else {"type": "logical", "mode": "or", "rules": parts}
'''
_SRC['shunt.sb_config'] = '''"""生成 sing-box 配置"""
import ipaddress, json, os, re, shlex, signal, threading, time, urllib.request, urllib.error

from .base import (
    AB_DIR, BUILTIN_POLICIES, CN_ECS, CTRL_HOST, CTRL_PORT, G_OTHER, G_SEL, MIHOMO_LOG, MIXED, REGIONS, UA, dl_check, read_full, wd_hold)
from .errlog import log_err
from .data import _atomic_write, auto_name, lb_dropped_names, lb_names, lb_old_name, load, rs_file, safe, write_json
from .groups import clean_custom_group, gcfg, pick_plan, prov_set, rx, sub_orig_nodes
from .mihomo import BUILTIN_GEO, RS_SB_EXTS, build_config, build_rule_providers, dns_cfg, migrate_ruleset_cache, policy_parts, tun_params, write_atomic, write_config
from .corehttp import core_json
from .sb_common import CORE_PID, SB_BIN, SB_CONF, SB_DIR, SB_RULE_DIR, SB_STATE, active_core
from .sb_yaml import yaml_lite
from .groups import sb_provider_nodes, sb_sub_cache, sb_sub_meta, sb_sub_outbounds
from .sb_nodes import sb_outbound
from .sb_rules import SBRules, geo_srs_entry
from .ruleutil import LOGICAL_RX, _mh_domain, split_rule, strip_comment
from .sb_dns import sb_dns
from .sysopt import nft_ok, proxy_opener, sh, svc_restart
from .notify import core_alive
def build_singbox(d):
    """把 build_config 生成的 mihomo 结构翻译成 sing-box 配置；返回 (配置, 警告列表, 需要的规则集文件)"""
    sub_out = sb_sub_outbounds(d)
    # 经 prov_set 写入：名单变了要推进 provider_rev()，/api/state 的策略组缓存和 /api/ping 版本号才会更新
    prov_set(time.time(), {k: [n for n, _ in v] for k, v in sub_out.items()})
    cfg = build_config(d)
    v6 = bool(d.get("ipv6"))
    gc = gcfg(d)
    warn = []
    outbounds, node_names, prov_names = sb_node_outbounds(d, cfg, sub_out, warn)
    picks = sb_custom_picks(d, sub_out, prov_names)
    alias, group_outs = sb_group_outbounds(cfg, gc, node_names, prov_names, warn, picks, sb_is_fork())
    outbounds += group_outs
    gt = ("selector", "urltest", "loadbalance")
    group_out = [o for o in outbounds if o["type"] in gt]
    outbounds = group_out + [o for o in outbounds if o["type"] not in gt] + [{"type": "direct", "tag": "DIRECT"}]
    policies = {o["tag"] for o in outbounds} | BUILTIN_POLICIES
    inbounds = sb_inbounds(d, v6, warn)
    in_tags = {i["tag"] for i in inbounds}
    rule_sets = sb_rule_sets(cfg, warn)
    tr = SBRules(cfg, in_tags, alias, rule_sets)
    tr.warn = warn
    tr.ip_sets = sb_ip_sets(tr.ip_sets, rule_sets)
    # 路由规则
    route_rules = []
    if d.get("sniffer", True) or dns_cfg(d)["mode"] == "fake-ip":
        route_rules.append({"action": "sniff"})
    route_rules += [{"inbound": ["dns-in"], "action": "hijack-dns"}, {"protocol": ["dns"], "action": "hijack-dns"},
                    {"clash_mode": "Direct", "action": "route", "outbound": "DIRECT"}]
    route_rules += sb_global_direct(d)
    route_rules.append({"clash_mode": "Global", "action": "route", "outbound": G_SEL if G_SEL in policies else "DIRECT"})
    resolved = False
    final = "DIRECT"
    for line in cfg["rules"]:
        if line.upper().startswith("MATCH,"):
            # 与 mihomo 一致：第一条 MATCH 之后的规则都不再生效。以前只记下 final 后继续翻译，自定义规则里写的
            # 「MATCH,DIRECT」被后面内置的分流规则和最后的 MATCH 覆盖、等于没写；MATCH,REJECT 也变成了直连
            act = tr.action(line.split(",", 1)[1])
            if act is None:  # PASS：跳过这条
                continue
            if act.get("action") == "reject":
                route_rules.append(dict({"network": ["tcp", "udp"]}, **act))
            else:
                final = act.get("outbound", "DIRECT")
            break
        r, needs = tr.rule(line)
        if r is None:
            continue
        if r.get("action") == "route" and r.get("outbound") not in policies:
            r["outbound"] = G_SEL if G_SEL in policies else "DIRECT"
        if needs and not resolved:  # 与 mihomo 一致：没有 no-resolve 的 IP 规则先解析域名再匹配
            route_rules.append({"action": "resolve"})
            resolved = True
        route_rules.append(r)
    dns, t_node, t_direct, t_proxy = sb_dns(d, cfg, policies, tr, alias, v6)
    if resolved and t_proxy:
        for r in route_rules:
            if r.get("action") == "resolve":
                r["server"] = t_proxy if dns_cfg(d)["mode"] == "fake-ip" else dns["final"]
                r["client_subnet"] = CN_ECS  # 经代理 DNS 解析时按国内网段返回，国内 CDN 域名才能命中 GEOIP,CN 直连
    elif resolved:
        for r in route_rules:
            if r.get("action") == "resolve":
                r["server"] = t_direct
    defs, missing = sb_rule_set_defs(rule_sets, warn)
    route_rules = sb_prune_rules(route_rules, missing)
    dns["rules"] = sb_prune_rules(dns["rules"], missing)
    mode = {"global": "Global", "direct": "Direct"}.get(d.get("mode"), "Rule")
    conf = {
        "log": {"level": "info", "timestamp": True},
        "dns": dns,
        "inbounds": inbounds,
        "outbounds": outbounds,
        "route": {"rules": route_rules, "rule_set": defs, "final": final if final in policies else "DIRECT",
                  "auto_detect_interface": True, "default_domain_resolver": t_node},
        "experimental": {"clash_api": {"external_controller": f"{CTRL_HOST}:{CTRL_PORT}", "secret": d["secret"],
                                       "default_mode": mode},
                         "cache_file": {"enabled": True, "path": os.path.join(SB_DIR, "cache.db"), "store_fakeip": True}},
    }
    need = {tag: rsd for tag, rsd in rule_sets.items() if rsd["kind"] != "inline"}
    return conf, list(dict.fromkeys(warn)), need


def sb_global_direct(d):
    """「全局代理」模式下仍直连的流量（排在 clash_mode Global 之前，只在全局模式生效）。
    以前全局模式一条 Global 规则把所有流量都送去节点：TUN 模式下「绕过设备」（nftables 管不到，靠 SRC-IP-CIDR 规则直连）、
    经路由器转发的局域网 / 保留地址、NTP 对时全部走节点，绕过设备照样被代理、跨网段访问局域网断开；TProxy 模式下这些在
    nftables 里本来就不进核心，两种代理方式表现不一致。现在这三类在全局模式下也直连"""
    rules = []
    if d.get("proxy_mode") == "tun":
        src = []
        for x in d.get("bypass") or []:
            try:
                if "%" in x:  # 带网卡的地址 sing-box 不认
                    raise ValueError
                n = str(ipaddress.ip_network(x.strip(), strict=False))
            except (AttributeError, TypeError, ValueError):
                continue
            if n not in src:
                src.append(n)
        if src:
            rules.append({"clash_mode": "Global", "source_ip_cidr": src, "action": "route", "outbound": "DIRECT"})
    rules.append({"clash_mode": "Global", "ip_is_private": True, "action": "route", "outbound": "DIRECT"})
    rules.append({"clash_mode": "Global", "network": ["udp"], "port": [123], "action": "route", "outbound": "DIRECT"})
    return rules


def sb_node_outbounds(d, cfg, sub_out, warn):
    """手动节点与订阅节点 → (outbounds, 手动节点名, {订阅名: 节点名列表})"""
    outbounds, node_names, prov_names = [], [], {}
    for p in cfg["proxies"]:
        try:
            outbounds.append(sb_outbound(p, p["name"]))
            node_names.append(p["name"])
        except ValueError as e:
            warn.append(f"节点「{p['name']}」：{e}，已跳过")
    skipped = 0
    convs = {}
    for sname, lst in sub_out.items():
        conv = []
        for name, p in lst:
            try:
                conv.append((name, p, sb_outbound(p, name)))
            except (ValueError, TypeError, AttributeError, KeyError):
                # 订阅内容不可信：字段类型不对（如 ws-opts.headers / grpc-opts / plugin-opts 是文本或列表）时 .get / .items
                # 抛 AttributeError，以前只捕获 ValueError / TypeError，一个坏节点让整份 sing-box 配置生成失败（订阅更新、
                # 改任何设置都报错，开机 --gen 也生成不了）。跳过这个节点，与协议不支持的节点同样计数提示
                skipped += 1
        convs[sname] = conv
    # detour 可以指向本订阅以外、但在最终配置里存在的出站（手动节点、其他订阅的节点、策略组、DIRECT / direct）：保持不变
    region_lb = {n for r, _ in REGIONS + [(G_OTHER, None)] for n in lb_names(r) + lb_dropped_names(r) + [lb_old_name(r)]}
    base_known = set(node_names) | {g["name"] for g in cfg["proxy-groups"] if g["name"] not in region_lb} | {"DIRECT", "direct"}
    all_sub = {name for conv in convs.values() for name, _, _ in conv}
    dropped = {}
    for sname, conv in convs.items():
        mine = {name for name, _, _ in conv}
        convs[sname], dropped[sname] = sb_fix_detour(conv, base_known | (all_sub - mine))
    # 跨订阅成环（订阅 A 的节点 detour 到订阅 B 的节点，B 的又 detour 回 A）：sb_fix_detour 只看本订阅，把对方当成
    # 「存在的外部出站」放行，以前整环原样写进配置，sing-box 启动失败；现在整环跳过（依赖它们的在下面的循环里一并跳过）
    owner = {name: sname for sname, conv in convs.items() for name, _, _ in conv}
    det = {name: o.get("detour") for conv in convs.values() for name, _, o in conv}
    for start in list(det):
        seen, cur = [], start
        while cur in owner and cur not in seen:
            seen.append(cur)
            cur = det.get(cur)
        if cur in seen:  # 回到了走过的订阅节点：从 cur 开始的这一段是环
            for n in seen[seen.index(cur):]:
                sname = owner[n]
                if n not in dropped[sname]:
                    dropped[sname].append(n)
                    convs[sname] = [x for x in convs[sname] if x[0] != n]
    while True:  # 跨订阅：前置节点在别的订阅里被跳过的，依赖它的也跳过
        gone = {n for v in dropped.values() for n in v}
        more = False
        for sname, conv in convs.items():
            keep = [x for x in conv if not (x[2].get("detour") in gone and x[2]["detour"] not in base_known)]
            if len(keep) != len(conv):
                dropped[sname] += [x[0] for x in conv if x not in keep]
                convs[sname] = keep
                more = True
        if not more:
            break
    for sname, conv in convs.items():
        lost = dropped[sname]
        if lost:
            msg = f"订阅「{sname}」中 {len(lost)} 个节点的前置代理（detour）不存在，已跳过：" + "、".join(lost[:5]) + \\
                  ("…" if len(lost) > 5 else "")
            warn.append(msg)
            log_err("sing-box 订阅 detour", msg)
        prov_names[sname] = [name for name, _, _ in conv]
        outbounds += [o for _, _, o in conv]
    if any(o.get("detour") == "direct" for o in outbounds) and not any(o.get("tag") == "direct" for o in outbounds):  # 订阅里常见的小写 direct：补一个同名直连出站，原样保留 detour
        outbounds.append({"type": "direct", "tag": "direct"})
    if skipped:
        warn.append(f"订阅中有 {skipped} 个节点的协议 sing-box 不支持（如 ssr、snell、wireguard）或参数格式不正确，已跳过")
    for s in d["subs"]:
        if not sb_sub_cache(s["name"]).get("updated"):
            warn.append(f"订阅「{s['name']}」还没有下载过节点")
    return outbounds, node_names, prov_names


def sb_fix_detour(conv, known=()):
    """sing-box JSON 订阅的节点可带 detour（前置代理，指向订阅里另一个节点的原 tag）。节点全局去重改名后原 tag 可能失效：
    按「原 tag → 现名」重映射；指向本订阅以外、但在最终配置里存在的出站（known：DIRECT / direct、手动节点、其他订阅节点、策略组）
    原样保留；哪里都不存在（被筛选 / 协议不支持 / 本来就没有）或成环时跳过该节点，保证整份配置能通过 check。
    conv: [(现名, 原节点, outbound)]，返回 (保留的 conv, 跳过的节点名)"""
    if not any(o.get("detour") for _, _, o in conv):
        return conv, []
    known = set(known)
    tag_map = {}
    for name, p, _ in conv:
        orig = str(((p.get("_sb") or {}).get("tag")) or p.get("name") or "")
        tag_map.setdefault(orig, name)
    names = {name for name, _, _ in conv}
    dep = {}
    for name, p, o in conv:
        det = o.get("detour")
        if det:
            det = str(det)
            if det in tag_map:
                tgt = tag_map[det]
            elif det in names or det in known:
                tgt = det
            else:
                tgt = None
            if tgt is not None:
                o["detour"] = tgt
            dep[name] = tgt
    bad = set()
    changed = True
    while changed:  # 前置节点被跳过的，依赖它的也跳过；成环的整环跳过
        changed = False
        for name, tgt in dep.items():
            if name in bad:
                continue
            seen, cur = {name}, tgt
            ok = tgt is not None
            while ok and cur is not None:
                if cur not in names:  # 本订阅以外的出站：上面已确认存在
                    break
                if cur in bad or cur in seen:
                    ok = False
                    break
                seen.add(cur)
                cur = dep.get(cur)
            if not ok:
                bad.add(name)
                changed = True
    return [x for x in conv if x[0] not in bad], [x[0] for x in conv if x[0] in bad]


def sb_custom_picks(d, sub_out, prov_names):
    """自定义组勾选的订阅节点 → {组名: (用户正则或 "", [出站 tag])}。按「订阅 + 节点原名」映射到本次生成配置里去重改名后的 tag
    （sb_sub_outbounds 的命名），被 detour / 协议不支持跳过的、已失效的都不列出"""
    out = {}
    orig = None
    for cg in d.get("custom_groups") or []:
        try:
            cg = clean_custom_group(cg)
        except ValueError:
            continue
        if not cg["sub_nodes"]:
            continue
        if orig is None:
            orig = sub_orig_nodes(d)
        plan = pick_plan(d, cg, orig)
        tags = []
        for sname, names in plan.items():
            alive = set(prov_names.get(sname) or [])
            want = set(names)
            tags += [tag for tag, p in sub_out.get(sname) or [] if str(p.get("name") or "") in want and tag in alive and tag not in tags]
        out[cg["name"]] = (cg["filter"] if cg["filter"] and cg["subs"] else "", tags)
    return out


def sb_group_outbounds(cfg, gc, node_names, prov_names, warn, picks=None, fork=False):
    """策略组：官方内核下地区负载均衡与自动优选相同 → 去掉并重定向引用；其余 load-balance / fallback 改为 urltest。
    魔改版内核（fork）：负载均衡组生成 loadbalance（多节点聚合），只有故障转移改为 urltest。返回 (别名表, 组 outbounds)"""
    outbounds = []
    alias = {}
    region_lb = set()
    for r, _ in REGIONS + [(G_OTHER, None)]:
        for n in ([] if fork else lb_names(r)) + lb_dropped_names(r) + [lb_old_name(r)]:
            alias[n] = auto_name(r)
            region_lb.add(n)
    groups = [g for g in cfg["proxy-groups"] if g["name"] not in region_lb]
    gnames = {g["name"] for g in groups}
    alias = {k: v for k, v in alias.items() if v in gnames}
    lb_custom = [g["name"] for g in groups if g["type"] == "fallback" or (g["type"] == "load-balance" and not fork)]
    if lb_custom:
        what = "故障转移组" if fork else "负载均衡 / 故障转移组（换成魔改版内核可用负载均衡）"
        warn.append(f"sing-box 没有{what}，已按自动测速运行：" + "、".join(lb_custom[:6]) + ("…" if len(lb_custom) > 6 else ""))
    all_sub = [n for v in prov_names.values() for n in v]
    known = set(node_names) | set(all_sub) | gnames
    for g in groups:
        members = []
        for m in g.get("proxies") or []:
            m = alias.get(m, m)
            if m in ("REJECT", "REJECT-DROP", "PASS"):
                continue
            if m == "COMPATIBLE":
                m = "DIRECT"
            if (m in known or m == "DIRECT") and m != g["name"] and m not in members:
                members.append(m)
        pk = (picks or {}).get(g["name"])
        if pk is not None:  # 勾选了订阅节点的自定义组：mihomo 用的合并 filter 不适用于改名后的 tag，这里按用户正则 + 勾选的 tag 展开
            uf, tags = pk
            if uf and g.get("use"):
                r = rx(uf)
                members += [n for u in g["use"] for n in prov_names.get(u, []) if r and r.search(n) and n not in members]
            members += [t for t in tags if t not in members and t != g["name"]]
        elif g.get("use"):
            pool = [n for u in g["use"] for n in prov_names.get(u, [])]
            f, ex = g.get("filter"), g.get("exclude-filter")
            rf, rex = (rx(f) if f else None), (rx(ex) if ex else None)
            members += [n for n in pool if (not rf or rf.search(n)) and not (rex and rex.search(n)) and n not in members]
        if not members:
            members = ["DIRECT"]
        if g["type"] == "select":
            o = {"type": "selector", "tag": g["name"], "outbounds": members, "interrupt_exist_connections": False}
        elif g["type"] == "load-balance" and fork:  # 魔改版：多节点聚合，按策略把连接分摊到组内各节点，自动跳过不健康的节点
            o = {"type": "loadbalance", "tag": g["name"], "outbounds": members,
                 "strategy": g.get("strategy") or gc.get("strategy") or "consistent-hashing",
                 "url": g.get("url") or gc["url"], "interval": f"{max(int(g.get('interval') or gc['interval']), 30)}s"}
            if not g.get("lazy", True):
                o["idle_timeout"] = "720h"
        else:
            o = {"type": "urltest", "tag": g["name"], "outbounds": members, "url": g.get("url") or gc["url"],
                 "interval": f"{max(int(g.get('interval') or gc['interval']), 30)}s",
                 "tolerance": int(g.get("tolerance") if g.get("tolerance") is not None else gc["tolerance"]),
                 "interrupt_exist_connections": False}
            if g["type"] == "url-test" and not g.get("lazy", True):
                o["idle_timeout"] = "720h"
        outbounds.append(o)
    return alias, outbounds


def sb_inbounds(d, v6, warn=None):
    lis = "::" if v6 else "0.0.0.0"
    inbounds = [{"type": "mixed", "tag": "mixed-in", "listen": lis, "listen_port": MIXED},
                {"type": "direct", "tag": "dns-in", "listen": lis, "listen_port": 1053}]
    if d.get("proxy_mode") == "tproxy":  # 透明代理（tproxy.sh 把流量转到 7893）
        inbounds.insert(1, {"type": "tproxy", "tag": "tproxy-in", "listen": lis, "listen_port": 7893})
    tp = tun_params(d)  # TUN 决策与 mihomo_tun 共用；auto_redirect 另需 nftables 可用
    if tp["enable"]:
        tun = {"type": "tun", "tag": "tun-in", "interface_name": tp["device"], "address": ["172.19.0.1/30"] + (["fdfe:dcba:9876::1/126"] if v6 else []),
               "auto_route": True, "auto_redirect": tp["auto_redirect"] and nft_ok(), "strict_route": tp["strict_route"],
               "stack": sb_tun_stack(tp["stack"])}
        if tun["stack"] == "auto":
            del tun["stack"]  # 不写 stack：1.15+ 用 sing-tun 自有协议栈（官方推荐，stack 已废弃），旧版本由内核自选
        if tp["stack"] in ("gvisor", "mixed") and tun.get("stack") != tp["stack"] and warn is not None:
            warn.append(f"当前 sing-box 内核没有编译 gVisor，TUN 协议栈 {tp['stack']} 已自动改用"
                        + ("默认（sing-tun 自有协议栈）" if "stack" not in tun else " system"))
        inbounds.append(tun)
    return inbounds


def sb_geo_rule_sets(d, dc=None):
    """显式计算 sing-box 需要的 GEO 规则集条目（{tag: {kind:url:path}}）。

    之前这些条目靠完整 build_singbox 的副作用产生：SBRules.use_geo 在翻译 GEOSITE/GEOIP 规则、
    sb_dns 在生成 DNS 策略时把它们塞进共用的 rule_sets 字典。这里逐条复刻那两个地方的逻辑，
    供轻量路径（sb_rule_need）使用，避免为取 [2] 而重跑整个构建。
    """
    dc = dc or dns_cfg(d)
    geos = {}

    def add(kind, name):
        if kind == "geoip" and name.lower() in ("private", "lan"):
            return  # 翻译成 ip_is_private，不产生规则集条目
        tag, entry = geo_srs_entry(kind, name)
        if tag not in geos:
            geos[tag] = entry

    for kind, name in BUILTIN_GEO:  # mihomo_rules 末尾硬编码分流规则
        add(kind, name)
    for line in d.get("rules") or []:  # 用户自定义规则里的 GEOSITE/GEOIP（fix_rule_policy 只换 target，不过滤）
        line = str(line).strip()
        if line.startswith("#"):
            continue
        # 逻辑规则 AND,((GEOSITE,steam),(NETWORK,TCP)) 里的也要算：以前只看行首，完整构建会引用 geosite-steam，
        # 但下载 / GEO 更新按这里的需求表从不下载它，这条规则永远被当成「规则集未下载」跳过
        for m in re.finditer(r"(?:^|\\()\\s*(GEOSITE|GEOIP)\\s*,\\s*([^,()]+)", line, re.I):
            add(m.group(1).lower(), m.group(2).strip())
    for pe in dc.get("policies") or []:  # sb_dns.policy_cond：DNS 策略里的 geosite 匹配
        kind, names = policy_parts(pe["match"])
        if kind == "geosite":
            for n in names:
                add("geosite", n)
    add("geosite", "cn")  # sb_dns.cn_cond：固定加入
    add("geosite", "private")
    for x in dc.get("fake_filter") or []:  # sb_dns：fake-ip 过滤里的 geosite:
        if x.lower().startswith("geosite:"):
            add("geosite", x[8:])
    return geos


def sb_rule_need(d):
    """轻量计算 sing-box 规则集需求表（等价于 build_singbox(d)[2]）：只依赖 d，不跑完整构建。
    之前多处为取 [2] 而重跑整个 build_singbox（含 build_config、节点/策略组/outbound 全量计算）。
    由 rule-providers 条目 + 显式计算的 GEO 条目组成，与完整构建的 [2] 逐项一致
    （含 SBRules.use_geo / sb_dns 副作用加入的 geo 条目；同完整构建一样过滤掉 kind=="inline"，内嵌规则集无需下载）。"""
    dc = dns_cfg(d)
    need = sb_rule_sets({"rule-providers": build_rule_providers(d, dc)[0]}, [])
    need.update(sb_geo_rule_sets(d, dc))
    return {tag: rsd for tag, rsd in need.items() if rsd["kind"] != "inline"}


def sb_rule_sets(cfg, warn):
    """mihomo rule-providers → sing-box 规则集描述（inline / 广告拦截 / srs / 需要转换）"""
    rule_sets = {}
    for name, prov in cfg["rule-providers"].items():
        if prov.get("type") == "inline":
            payload = prov.get("payload") or []
            r = {}
            for x in payload:
                if prov.get("behavior") == "ipcidr":
                    r.setdefault("ip_cidr", []).append(x)
                else:
                    k, v = _mh_domain(x)
                    r.setdefault(k, []).append(v)
            rule_sets[name] = {"kind": "inline", "rules": [r] if r else []}
        elif name.startswith("ad-") and prov.get("type") == "file":  # 广告拦截列表是本地文件；用户的 http 规则集叫 ad-xxx 照常下载
            rule_sets[name] = {"kind": "adblock", "src": os.path.join(AB_DIR, os.path.basename(prov["path"])), "path": name + ".json"}
        else:
            url = prov.get("url") or ""
            if re.search(r"\\.srs(?:\\?|$)", url):
                rule_sets[name] = {"kind": "srs", "url": url, "path": f"rs-{rs_file(name)}.srs"}
            elif prov.get("format") == "mrs" and "meta-rules-dat" in url and "/meta/" in url:
                rule_sets[name] = {"kind": "srs", "url": re.sub(r"\\.mrs(\\?|$)", r".srs\\1", url.replace("/meta/", "/sing/")),
                                   "path": f"rs-{rs_file(name)}.srs"}
            elif prov.get("format") == "mrs":
                warn.append(f"规则集「{name}」是 mihomo 专用的 .mrs 格式，sing-box 无法使用，已跳过（可改用 .srs 地址）")
            else:
                rule_sets[name] = {"kind": "convert", "url": url, "behavior": prov.get("behavior", "domain"),
                                   "format": prov.get("format", "yaml"), "path": f"rs-{rs_file(name)}.json"}
    return rule_sets


def _has_ip(rules):
    """sing-box source 规则集的规则里有没有目标 IP 条件（含逻辑规则的子规则）"""
    for r in rules or []:
        if isinstance(r, dict) and ("ip_cidr" in r or _has_ip(r.get("rules"))):
            return True
    return False


def sb_ip_sets(ip_sets, rule_sets):
    """引用时需要先解析域名的规则集（mihomo：ipcidr 总是解析，classical 有 IP 条目才解析）。
    已转换好的 classical 文件里没有 IP 条目的去掉，免得多做一次 DNS 解析；还没下载 / .srs 的按需要解析算"""
    out = set()
    for tag in ip_sets:
        rsd = rule_sets.get(tag)
        if not rsd:
            continue
        if rsd["kind"] == "inline":
            if _has_ip(rsd["rules"]):
                out.add(tag)
            continue
        if rsd["kind"] == "convert" and rsd.get("behavior") == "classical":
            try:
                with open(os.path.join(SB_RULE_DIR, rsd["path"])) as f:
                    if not _has_ip(json.load(f).get("rules")):
                        continue
            except (OSError, ValueError, AttributeError):
                pass
        out.add(tag)
    return out


def sb_rule_set_defs(rule_sets, warn):
    """规则集定义：只保留本地已有文件的（缺失的规则及引用跳过，避免启动失败）"""
    defs, missing = [], set()
    for tag, rsd in rule_sets.items():
        if rsd["kind"] == "inline":
            defs.append({"type": "inline", "tag": tag, "rules": rsd["rules"] or [{"domain": ["invalid.invalid"]}]})
            continue
        path = os.path.join(SB_RULE_DIR, rsd["path"])
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            defs.append({"type": "local", "tag": tag, "format": "binary" if path.endswith(".srs") else "source", "path": path})
            skip = _skip_note(path)
            if skip:
                warn.append(f"规则集「{tag}」里有 {skip['n']} 条 sing-box 规则集文件不支持的条目（规则集里不能再引用 GEOSITE / GEOIP / "
                            f"RULE-SET 等），已跳过：" + "、".join(skip.get("sample") or [])[:120])
        else:
            missing.add(tag)
    if missing:
        warn.append("以下规则集文件尚未下载，相关规则暂时跳过：" + "、".join(sorted(missing)[:8]) + ("…" if len(missing) > 8 else ""))
    return defs, missing


def sb_prune_rules(lst, missing):
    """去掉引用了缺失规则集的规则（逻辑规则里有一条缺失就整条去掉）"""
    out = []
    for r in lst:
        r = dict(r)
        if r.get("type") == "logical":
            subs = sb_prune_rules(r["rules"], missing)
            if len(subs) != len(r["rules"]):
                continue
            r["rules"] = subs
        elif "rule_set" in r:
            keep = [x for x in r["rule_set"] if x not in missing]
            if not keep:
                continue
            r["rule_set"] = keep
        out.append(r)
    return out


# GEOIP,private 在规则集文件里不能写成 ip_is_private（headless 规则没有这个字段），换成对应网段
_PRIVATE_CIDRS = ["0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
                  "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8"]
# sing-box headless 规则（规则集文件里的规则）支持的字段
_HEADLESS_KEYS = {"query_type", "network", "domain", "domain_suffix", "domain_keyword", "domain_regex", "source_ip_cidr",
                  "ip_cidr", "source_port", "source_port_range", "port", "port_range", "process_name", "process_path",
                  "process_path_regex", "package_name", "network_type", "wifi_ssid", "wifi_bssid", "invert"}


def _headless(r):
    """路由规则条件 → 规则集文件可用的 headless 规则；有规则集引用（GEOSITE / GEOIP / RULE-SET）等不支持的字段返回 None"""
    if not isinstance(r, dict):
        return None
    if r.get("type") == "logical":
        subs = [_headless(x) for x in r.get("rules") or []]
        if not subs or any(x is None for x in subs):
            return None
        return dict(r, rules=subs)
    r = dict(r)
    if r.pop("ip_is_private", None):
        r["ip_cidr"] = list(_PRIVATE_CIDRS)
    if r.pop("source_ip_is_private", None):
        r["source_ip_cidr"] = list(_PRIVATE_CIDRS)
    if not r or not set(r) <= _HEADLESS_KEYS or set(r) == {"invert"}:
        return None
    if not all(_re2_ok(x) for k in ("domain_regex", "process_path_regex") for x in r.get(k) or []):
        return None
    return r


# Go regexp（RE2）不支持的写法：前后断言、原子组、反向引用、\\Z。mihomo 的 DOMAIN-REGEX 用 regexp2，这些都能用
_NOT_RE2 = re.compile(r"\\(\\?<?[=!]|\\(\\?>|\\(\\?P=|\\\\[1-9]|\\\\Z")


def _re2_ok(rx):
    """正则能否被 sing-box（Go regexp）编译。以前 classical 规则集里的 DOMAIN-REGEX 原样写进规则集文件，
    带前后断言 (?!…) 或写错的正则让 sing-box 加载规则集失败、内核起不来（之后每次保存都校验不过被撤销）"""
    if not isinstance(rx, str) or _NOT_RE2.search(rx):
        return False
    try:
        re.compile(re.sub(r"\\\\[pP]\\{\\w+\\}", "x", rx))  # \\p{Han} 等 Unicode 类 Go 支持、Python re 不支持
    except re.error:
        return False
    return True


def _rule_lines_to_source(text, behavior, fmt, skipped=None):
    """mihomo 规则集文本（yaml payload / text）→ sing-box source 规则集。
    skipped：传入列表时收集转换不了、被跳过的 classical 条目（以前 GEOSITE / GEOIP / 逻辑规则静默丢掉）"""
    t = text.strip()
    items = []
    if fmt == "yaml" or re.search(r"(?m)^payload\\s*:", t):
        y = yaml_lite(t)
        items = [str(x) for x in ((y or {}).get("payload") or [])] if isinstance(y, dict) else []
    else:
        items = [l.strip() for l in t.splitlines() if l.strip() and not l.strip().startswith(("#", "//"))]
    r = {}
    other = {}  # classical 里的端口 / 网络 / 来源地址 / 进程等条件：各自成条（sing-box 同一条规则里这些字段与域名 / IP 是「且」）
    logic = []  # classical 里的 AND / OR / NOT：转成 headless 逻辑规则（规则集里各条规则之间是「或」）
    tr = SBRules({}, set(), {}, {})
    for x in items:
        # 行内注释（「DOMAIN,a.com # 说明」「a.com ! 说明」）：去掉注释保留规则。以前整条被当成无效值跳过，或注释写进了域名
        x = strip_comment(x.strip()).strip("'\\"").strip()
        if not x or x[0] in "#!":
            continue
        if behavior == "ipcidr":
            try:
                r.setdefault("ip_cidr", []).append(str(ipaddress.ip_network(x, strict=False)))
            except ValueError:
                pass
        elif behavior == "domain":
            k, v = _mh_domain(x)
            r.setdefault(k, []).append(v)
        else:
            body = split_rule(x)[0] if LOGICAL_RX.match(x) else x  # 规则集里的逻辑规则没有策略
            c = _headless(tr.match(body))
            # 规则集文件里不能再引用规则集（GEOSITE / GEOIP / RULE-SET），sing-box 加载会报错：跳过并记下来提示
            if c is None:
                if skipped is not None and x and x.split(",")[0].strip().upper() not in ("MATCH",):
                    skipped.append(x)
                continue
            if c.get("type") == "logical" or "invert" in c:
                logic.append(c)
                continue
            # 以前所有条件合并成一条：DST-PORT,443 与 DOMAIN-SUFFIX,a.com 写在同一份列表里，变成「a.com 且 443 端口」才命中
            dst = r if set(c) <= _DST_ADDR_KEYS else other.setdefault(tuple(sorted(c)), {})
            for k, v in c.items():
                dst.setdefault(k, []).extend(v)
    rules = ([r] if r else []) + list(other.values()) + logic
    return {"version": 3, "rules": rules}


# sing-box 规则里属于同一组「或」的目标地址字段（同一条规则里可以合并）
_DST_ADDR_KEYS = {"domain", "domain_suffix", "domain_keyword", "domain_regex", "ip_cidr"}


def _skip_note(path):
    """转换规则集时跳过的条目记录（{"n": 条数, "sample": [...]}，存在 <规则集文件>.skip）"""
    try:
        with open(path + ".skip") as f:
            j = json.load(f)
        return j if isinstance(j, dict) and j.get("n") else None
    except (OSError, ValueError):
        return None


def sb_adblock_source(src):
    """面板广告列表（每行 +.a.com 或 a.com）→ sing-box source 规则集"""
    suf, exact = [], []
    with open(src) as f:
        for line in f:
            line = line.strip()
            if line.startswith("+."):
                suf.append(line[2:])
            elif line:
                exact.append(line)
    r = {}
    if suf:
        r["domain_suffix"] = suf
    if exact:
        r["domain"] = exact
    return {"version": 3, "rules": [r] if r else []}


def sb_rule_count(path):
    """sing-box source 规则集里的条目数（广告拦截页显示「已加载」用；sing-box 的 clash_api 不提供规则集信息）"""
    try:
        with open(path) as f:
            j = json.load(f)
    except (OSError, ValueError):
        return None
    return sum(len(v) for r in j.get("rules") or [] if isinstance(r, dict) for v in r.values() if isinstance(v, list))


def sb_ab_loaded(d):
    """{"ad-xxx": 条目数}：只统计配置里实际引用、且已转换好的广告拦截规则集"""
    out = {}
    for tag, rsd in sb_rule_need(d).items():
        if rsd.get("kind") == "adblock":
            path = os.path.join(SB_RULE_DIR, rsd["path"])
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                out[tag] = sb_rule_count(path)
    return out


ASSETS_WAIT = 150  # 并发下载规则集整体最多等这么多秒


def sb_sync_assets(d, force=False, need=None, wait=None):
    """下载 / 转换 sing-box 需要的规则集文件；返回失败列表。wait：整体最多等多少秒（默认 ASSETS_WAIT）"""
    migrate_ruleset_cache(d)  # 旧版文件名的规则集文件先改名，免得按新文件名全部重新下载
    if need is None:
        need = sb_rule_need(d)
    gh = (d.get("gh_proxy") or "").rstrip("/")
    os.makedirs(SB_RULE_DIR, exist_ok=True)
    bad = []
    bad_lock = threading.Lock()
    late = threading.Event()  # 整体超时后置位：仍卡着的线程迟到时不再写文件

    def one(tag, rsd):
        """单个规则集：下载 / 转换 / 写文件（每个规则集独立文件，线程间无冲突）"""
        path = os.path.join(SB_RULE_DIR, rsd["path"])
        try:
            if rsd["kind"] == "adblock":
                if os.path.isfile(rsd["src"]) and (force or not os.path.isfile(path) or os.path.getmtime(rsd["src"]) > os.path.getmtime(path)):
                    write_json(path, sb_adblock_source(rsd["src"]))
                return
            if os.path.isfile(path) and os.path.getsize(path) > 0 and not force:
                return
            urls = ([gh + "/" + rsd["url"]] if gh and "github" in rsd["url"] else []) + [rsd["url"]]
            data, err = None, ""
            for u in urls:
                for use_proxy in ((True, False) if core_alive() else (False,)):
                    try:
                        dl_check(u, via_proxy=use_proxy)  # SSRF 防护：直连时校验 DNS，走代理时由代理解析（字面内网 IP 永远拦截）
                    except IOError as e:
                        err = str(e)[:120]
                        continue
                    try:
                        with proxy_opener(use_proxy).open(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=30) as r:
                            data = read_full(r, 40 << 20)
                        break
                    except Exception as e:
                        err = str(e)[:120]
                if data is not None:
                    break
            if data is None:
                raise IOError(err or "下载失败")
            if late.is_set():
                return
            if rsd["kind"] in ("geo", "srs"):
                if not data.startswith(b"SRS"):
                    raise ValueError("不是有效的 .srs 文件")
                # 唯一临时文件 + 改名：以前固定用 path + ".tmp"，两个线程同时下载同一个规则集（保存时补下载与定时更新）会互相截断
                _atomic_write(path, data, 0o644)
            else:
                skipped = []
                src = _rule_lines_to_source(data.decode("utf-8", "ignore"), rsd["behavior"], rsd["format"], skipped)
                if not src["rules"]:
                    raise ValueError("没有解析出任何规则")
                write_json(path, src)
                if skipped:  # 跳过的条目记下来，生成配置时在兼容提示里列出
                    write_json(path + ".skip", {"n": len(skipped), "sample": [x[:60] for x in skipped[:3]]})
                elif os.path.exists(path + ".skip"):
                    os.remove(path + ".skip")
        except Exception as e:
            with bad_lock:
                bad.append(f"{tag}：{e}")

    items = list(need.items())
    ths = [threading.Thread(target=one, args=(tag, rsd), daemon=True) for tag, rsd in items]
    for t in ths:
        t.start()
    end = time.time() + (wait or ASSETS_WAIT)  # 整体最多等 ASSETS_WAIT 秒（以前逐个 join(150)，几个都卡住时要等 N×150 秒）
    for t in ths:
        t.join(max(0.1, end - time.time()))
    late.set()
    with bad_lock:  # 返回快照：超时线程之后的结果不影响这里
        return list(bad) + [f"{tag}：下载超时" for (tag, _), t in zip(items, ths) if t.is_alive()]


def rs_changed(prev, d):
    """/api/settings 保存规则集：新增的、或地址 / 类型 / 格式变了（同名替换）的规则集名"""
    def sig(r):
        return r.get("url"), r.get("behavior", "domain"), r.get("format", "yaml")
    old = {r.get("name"): sig(r) for r in (prev or {}).get("rulesets") or [] if isinstance(r, dict)}
    return [r["name"] for r in d.get("rulesets") or [] if isinstance(r, dict) and old.get(r.get("name")) != sig(r)]


def sb_refresh_rulesets(d, names):
    """新增 / 换了地址、类型、格式的规则集立即（强制）下载转换；返回失败列表。
    以前本地已有同名文件（rs-<名>.json）就一直沿用旧地址、旧类型转换出的内容（sb_fetch_missing 只补缺失的文件），
    要等定时更新或手动「更新」才换"""
    want = set(names)
    need = {k: v for k, v in sb_rule_need(d).items() if k in want and v.get("kind") != "adblock"}
    return sb_sync_assets(d, force=True, need=need, wait=MISSING_WAIT) if need else []


def sb_drop_ruleset_files(d, names):
    """当前不是 sing-box 时：删掉这些规则集已转换好的 sing-box 文件，切换到 sing-box 时按新地址重新下载（否则沿用旧内容）"""
    want = set(names)
    for tag, rsd in sb_rule_need(d).items():
        if tag in want and rsd.get("kind") != "adblock":
            for p in (rsd["path"], rsd["path"] + ".skip"):
                try:
                    os.remove(os.path.join(SB_RULE_DIR, p))
                except OSError:
                    pass
    # 旧版文件名（rs-<safe(名)>，不带哈希）的文件一并删掉：否则随后 write_config 里的 migrate_ruleset_cache
    # 把它改名成新文件名，切换到 sing-box 时照样沿用旧地址的内容
    for n in want:
        if isinstance(n, str):
            for ext in RS_SB_EXTS:
                try:
                    os.remove(os.path.join(SB_RULE_DIR, f"rs-{safe(n)}.{ext}"))
                except OSError:
                    pass


MISSING_WAIT = 45  # 保存设置时补下缺失规则集：整体最多等这么多秒（不阻塞太久，下不来的规则照旧跳过并提示）
MISSING_COOLDOWN = 600  # 下载失败的规则集 10 分钟内不再在保存时重试（GitHub 不通时每次保存都要白等）
_MISSING_FAIL = {}


def sb_fetch_missing(d):
    """sing-box：新引用、本地还没有的 GEO / 规则集文件先下载好再生成配置；返回失败列表。
    以前只有切换内核、定时 GEO 更新、手动「更新」规则集才下载：新加的 GEOSITE / GEOIP 自定义规则、规则集订阅
    （如「Apple 直连」「微软 直连」）、引用 geosite 的 DNS 策略保存后提示「已生效」，实际因文件不存在被整条跳过，
    要等下一次 GEO 更新才生效。已有的文件不重下（不受影响），广告列表照旧由 _gen_config 转换"""
    now = time.time()
    need = {}
    for tag, rsd in sb_rule_need(d).items():
        if rsd["kind"] == "adblock":
            continue
        path = os.path.join(SB_RULE_DIR, rsd["path"])
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            _MISSING_FAIL.pop(tag, None)
        elif now - _MISSING_FAIL.get(tag, 0) >= MISSING_COOLDOWN:
            need[tag] = rsd
    if not need:
        return []
    bad = sb_sync_assets(d, need=need, wait=MISSING_WAIT)
    for tag, rsd in need.items():
        path = os.path.join(SB_RULE_DIR, rsd["path"])
        if not (os.path.isfile(path) and os.path.getsize(path) > 0):
            _MISSING_FAIL[tag] = time.time()
    return bad


def write_sb_config(d, path=None):
    os.makedirs(SB_DIR, exist_ok=True)
    if path is None:
        migrate_ruleset_cache(d)
    conf, warn, _ = build_singbox(d)
    SB_STATE["warn"] = warn
    path = path or SB_CONF
    write_atomic(path, conf)
    return path


def check_sb_config(path, binp=None):
    binp = binp or SB_BIN
    if not os.path.isfile(binp):
        return True, ""
    code, out = sh(f"'{binp}' check -c '{path}' -D '{SB_DIR}'", timeout=90)
    if code == 0:
        return True, ""
    lines = [l for l in out.splitlines() if "FATAL" in l or "ERROR" in l or "error" in l.lower()]
    return False, "\\n".join(lines[-3:]) or out[-400:]


def write_active_config(d=None):
    d = d or load()
    if active_core(d) == "singbox":
        return write_sb_config(d)
    return write_config(d)


def core_pid():
    try:
        with open(CORE_PID) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return 0


def _proc_start(pid):
    """进程启动时刻（Unix 时间），取不到返回 None"""
    try:
        with open(f"/proc/{pid}/stat") as f:
            ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat") as f:
            btime = next(int(l.split()[1]) for l in f if l.startswith("btime "))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None


def sb_running_current():
    """正在运行的 sing-box 加载的是不是磁盘上这份配置：进程在跑、控制端口应答，且配置文件在进程启动
    （或这个进程上次成功热重载）之后没有再被改过。判断不了一律返回 False（照旧重载，宁可断一下也不跑旧配置）"""
    pid = core_pid()
    if not pid:
        return False
    try:
        mt = os.path.getmtime(SB_CONF)
    except OSError:
        return False
    start = _proc_start(pid)
    if start is None:
        return False
    ok = SB_STATE.get("reload_ok")
    loaded = max(start, ok[1] if ok and ok[0] == pid else 0)
    return mt <= loaded and core_alive()


def sb_log_error():
    """sing-box 日志里最后一条 FATAL / ERROR"""
    try:
        with open(MIHOMO_LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 16 * 1024))
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        if "FATAL" in line or " ERROR " in line:
            return re.sub(r"^(\\S+\\s+){0,3}(FATAL|ERROR)(\\[\\d+\\])?\\s*", "", line.strip())[-200:]
    return ""


def _log_size():
    try:
        return os.path.getsize(MIHOMO_LOG)
    except OSError:
        return 0


def _log_since(off):
    """日志从 off 起新增的内容（文件被截断 / 清理过则从头读），最多读 64KB"""
    try:
        with open(MIHOMO_LOG, "rb") as f:
            f.seek(0, 2)
            end = f.tell()
            if end < off:
                off = 0
            f.seek(max(off, end - 64 * 1024))
            return f.read().decode("utf-8", "ignore")
    except OSError:
        return ""


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


SB_RELOAD_WAIT = 60.0  # 最多等 60 秒：小路由上 SIGHUP 后先 check 新配置（大订阅 / 规则集要好几秒），再关旧实例、启动新实例
SB_RELOAD_NOMARK = 40.0  # 日志里一直没有「sing-box started」（日志被清）时，控制端口应答且进程在，等满 40 秒才算成功


def sb_hot_reload():
    """sing-box 收到 SIGHUP 会先 check 新配置，通过则关闭旧实例、按新配置重建（透明代理规则不受影响）；进程不在时重启服务。

    以前 SIGHUP 后 0.5 秒控制端口一应答就算成功，应答的可能还是没来得及关闭的旧实例。现在以日志为准：
    SIGHUP 之后出现「sing-box started」且控制端口应答 → 成功；出现「reload service」错误（新配置被拒，旧实例继续跑）
    或进程退出 → 失败，交给 reload_core 回滚。
    v10.2.6 / v8.3.8：以前没等到标记 6 秒后控制端口一应答就算成功，可 sing-box 收到 SIGHUP 后要先 check 新配置，
    这几秒里应答的是旧实例；面板随即把看门狗暂停缩短到 5 秒，旧实例关闭、新实例启动期间控制端口不通，
    看门狗就误判「控制接口无响应（进程仍在）」把核心重启（更新广告规则、订阅后最常见）"""
    pid = core_pid()
    if pid:
        try:
            wd_hold(SB_RELOAD_WAIT + 30, "sing-box 重载配置")  # 重载期间控制端口短暂不可用，看门狗不要当成故障
            off = _log_size()
            os.kill(pid, signal.SIGHUP)
            t0 = time.time()
            while time.time() - t0 < SB_RELOAD_WAIT:
                time.sleep(0.5)
                if not _pid_alive(pid):
                    return False, "sing-box 重载后进程已退出，请查看日志"
                new = _log_since(off)
                m = re.search(r"(?:ERROR|FATAL)[^\\n]*reload service[^\\n]*", new)
                if m:
                    return False, "sing-box 拒绝了新配置：" + m.group(0)[-200:]
                started = "sing-box started" in new
                if (started or time.time() - t0 >= SB_RELOAD_NOMARK) and core_alive():
                    SB_STATE["reload_ok"] = (pid, time.time())
                    wd_hold(5, "sing-box 重载配置", reset=True)  # 已确认恢复：缩短看门狗暂停期
                    return True, "配置已重载"
            return False, "sing-box 重载后未响应，请查看日志"
        except OSError:
            pass
    svc_restart(timeout=90)
    return True, "已写入配置并重启 sing-box"


_VER = {}  # 路径 → ((mtime, 大小), 版本号)；内核切换页 / 更新页每次打开都要查当前版和 .bak，以前每次执行 version（最长 15 秒）


def sb_version(path=None):
    path = path or SB_BIN
    try:
        st = os.stat(path)
    except OSError:
        return ""
    if not os.path.isfile(path):
        return ""
    key = (st.st_mtime_ns, st.st_size)
    hit = _VER.get(path)
    if hit and hit[0] == key:
        return hit[1]
    code, out = sh(f"{shlex.quote(path)} version", timeout=15)
    m = re.search(r"sing-box version (\\S+)", out) if code == 0 else None
    if m:  # 失败（繁忙超时等）不缓存
        _VER[path] = (key, m.group(1))
    return m.group(1) if m else ""


_FORK = {"key": None, "v": False}
_NATIVE = {"key": None, "v": False}
CAP_RETRY = 60  # 执行 version 失败（繁忙超时、内存不足）时不缓存结果，过这么多秒再试


def _cap_cached(cache, path, default, probe):
    """按程序文件 mtime + 大小缓存 sing-box 能力检测，/api/state 轮询不用每次执行 version。
    probe() 返回 (成功?, 值)；失败时返回 default 且不缓存，以免一次超时把错误结果一直留到换内核
    （例：gVisor 误判为有 → 1.15 内核写入 mixed 启动失败，TUN 模式下局域网断网）"""
    try:
        st = os.stat(path)
    except OSError:
        return default
    key = (path, st.st_mtime_ns, st.st_size)
    if cache["key"] == key:
        return cache["v"]
    if cache.get("fail") == key and time.time() < cache.get("retry", 0):
        return default
    ok, v = probe()
    if ok:
        cache.update(key=key, v=v, fail=None)
        return v
    cache.update(fail=key, retry=time.time() + CAP_RETRY)
    return default


def sb_tun_native(path=None):
    """sing-box 1.15+ 有 sing-tun 自有协议栈（不写 stack 时使用，官方已废弃 stack，1.17 移除）"""
    path = path or SB_BIN

    def probe():
        v = sb_version(path)
        m = re.match(r"(\\d+)\\.(\\d+)\\.", v or "")
        return bool(v), bool(m) and (int(m.group(1)), int(m.group(2))) >= (1, 15)
    return _cap_cached(_NATIVE, path, False, probe)


def sb_tun_stack(stack, path=None):
    """sing-box 实际要写的协议栈：没编译 gVisor 时 gvisor / mixed（UDP 走 gVisor）会在启动时报
    「gVisor is not included in this build」退出（sing-box check 不建协议栈、校验照样通过）。1.15+ 官方构建默认都不带 gVisor
    （reF1nd、MiChongs 的 1.15 预览版也没有），这时改用 auto（自有协议栈），旧内核改用 system"""
    if stack in ("gvisor", "mixed") and not sb_has_gvisor(path):
        return "auto" if sb_tun_native(path) else "system"
    return stack if stack in ("auto", "system", "gvisor", "mixed") else "auto"
_GVISOR = {"key": None, "v": True}


def sb_has_gvisor(path=None):
    """sing-box 有没有编译 gVisor（version 输出的 Tags 里有 with_gvisor）。1.14 及以前的官方版、reF1nd 正式版一般有；1.15+ 官方默认不再编译（reF1nd / MiChongs 预览版也没有），
    TUN 用 gvisor / mixed 协议栈会启动失败。读不到 Tags（没装、旧版本不输出）时按有处理，不改用户的设置"""
    path = path or SB_BIN

    def probe():
        code, out = sh(f"{shlex.quote(path)} version", timeout=15)
        m = re.search(r"^Tags:\\s*(\\S+)", out, re.M) if code == 0 else None
        return code == 0, not m or "with_gvisor" in m.group(1).split(",")
    return _cap_cached(_GVISOR, path, True, probe)


def tun_stack_tip():
    """TUN 起不来时的协议栈建议：没编译 gVisor 的 sing-box 选 gvisor 会被自动改掉，建议了也没用"""
    if active_core() == "singbox" and not sb_has_gvisor():
        return "换用「默认」或 system 协议栈" if sb_tun_native() else "换用 system 协议栈"
    return "换用 gvisor 协议栈"
OFFICIAL_VER = re.compile(r"^\\d+\\.\\d+\\.\\d+(?:-(?:alpha|beta|rc)\\.\\d+)?$")


def sb_is_fork(path=None):
    """当前 sing-box 是不是魔改版（版本号带官方没有的后缀，如 1.15.0-alpha.12-xiaobaf14g.8）。
    魔改版才有 loadbalance 出站组；按程序文件的 mtime + 大小缓存，避免每次生成配置都执行一次 version"""
    path = path or SB_BIN

    def probe():
        v = sb_version(path)
        return bool(v), bool(v) and not OFFICIAL_VER.match(v)
    return _cap_cached(_FORK, path, False, probe)


def iso_utc(t):
    g = time.gmtime(t)
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % g[:6]


def sb_fake_providers(d, px=None):
    """sing-box 没有 proxy-provider：按 mihomo /providers/proxies 的格式拼出订阅节点及其延迟，前端无需区分内核。
    px：调用方已经取到的 /proxies 的 proxies 部分（/api/px 传进来，省掉一次向核心要整份 /proxies）"""
    if px is None:
        px = (core_json("/proxies", timeout=5) or {}).get("proxies") or {}
    out = {}
    pn = sb_provider_nodes(d)  # 只算一次：以前每个订阅都重新算一遍全部订阅的节点名单（N 个订阅读 N² 次订阅缓存）
    for s in d["subs"]:
        c = sb_sub_meta(s["name"])
        names = pn.get(s["name"], [])
        upd = c.get("updated")
        out[s["name"]] = {"name": s["name"], "type": "Proxy", "vehicleType": "HTTP",
                          "updatedAt": iso_utc(upd) if upd else "",
                          "subscriptionInfo": c.get("info") or {},
                          "proxies": [dict({"name": n, "type": (px.get(n) or {}).get("type", ""), "history": []}, **(px.get(n) or {}))
                                      for n in names]}
    return {"providers": out}


def sb_fake_rule_providers(d):
    out = {}
    need = sb_rule_need(d)
    for r in d.get("rulesets") or []:
        rsd = need.get(r["name"])
        path = os.path.join(SB_RULE_DIR, rsd["path"]) if rsd else ""
        mt = os.path.getmtime(path) if path and os.path.isfile(path) else 0
        out[r["name"]] = {"name": r["name"], "type": "Rule", "vehicleType": "HTTP", "behavior": r.get("behavior", "domain").capitalize(),
                          "ruleCount": 0, "updatedAt": iso_utc(mt) if mt else ""}
    return {"providers": out}

'''
_SRC['shunt.sb_switch'] = '''"""内核切换"""
import hashlib, io, os, re, shutil, shlex, threading, time

from .base import CONF_DIR, CORE_UPD, SMART_BIN, TPROXY_SH
from .data import load, update
from .errlog import log_err
from .groups import PROV, gcfg
from .mihomo import check_config, dns_cfg, mh_bin, write_config
from .sb_common import CORES, FORK2_API, FORK2_REPO, FORK_API, FORK_ARCHES, FORK_NAME, FORK_REPO, SB_API, SB_BIN, SB_CONF, SB_REPO, SB_STATE, active_core, write_core_file
from .sb_subs import sb_update_subs
from .sb_config import check_sb_config, sb_is_fork, sb_sync_assets, sb_version, write_sb_config
from .core_update import QUIET, CORE_FAIL_LOG, LGBM_FILE, MANUAL_NOTE, _cu, bin_version, copy_replace, core_fail_log, gh_atom_tags, gh_get, lgbm_fetch, mh_fetch, open_url, pick_tag, release_digest
from .sysopt import kill_core_procs, sh, svc_restart
from .notify import WD, core_alive, notify, set_manual_stop
from .migrate import restore_selections, snapshot_selections
def sb_arch():
    m = os.uname().machine
    return {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64", "armv7l": "armv7", "armv8l": "armv7", "armv6l": "armv6",
            "i686": "386", "i386": "386", "riscv64": "riscv64", "loongarch64": "loong64", "s390x": "s390x"}.get(m, "")


# 魔改版的架构名和官方不一样（arm-v7、mipsle-softfloat），压缩包里的程序名也带版本号
FORK_ARCH = {"armv7": "arm-v7", "armv6": "arm-v7"}
CHANNELS = ("stable", "alpha", "fork", "fork_alpha", "fork_mc")
FORK_NONE_MSG = "魔改版 sing-box 目前没有可用的 Release（发布者可能已撤下或正在重新发布），请稍后再试或换其他版本"
FORK_CHANNELS = ("fork", "fork_alpha", "fork_mc")  # 魔改版 reF1nd 正式版 / reF1nd 预览版 / MiChongs


def fork_src(arch=None, channel="fork"):
    """魔改版的下载源：(仓库, API, 要哪种版本 stable/alpha/any)。amd64 / arm64 用 reF1nd（fork 正式版、fork_alpha 预览版），
    其他架构退回 MiChongs（只有预发布版，取最新发布的那个）；fork_mc 一律用 MiChongs"""
    arch = sb_arch() if arch is None else arch
    if arch in FORK_ARCHES and channel != "fork_mc":
        return FORK_REPO, FORK_API, "alpha" if channel == "fork_alpha" else "stable"
    return FORK2_REPO, FORK2_API, "any"


def fork_name(v):
    """按版本号后缀认出是哪家魔改版：-reF1nd 是 reF1nd，-xiaobaf14g 是 MiChongs"""
    v = v or ""
    return "魔改版（MiChongs）" if "xiaobaf" in v else FORK_NAME if "reF1nd" in v else "魔改版"


def _short(e):
    return str(e)[:160]


def _api_pick(rel, want):
    """从 API 的 releases 列表里挑版本：跳过草稿；stable 只要正式版、alpha 只要预发布、any 不挑；
    first_pre：官方预览版——列表里第一个预发布，没有就取最新一个"""
    rel = [x for x in rel if isinstance(x, dict) and not x.get("draft")]
    if want == "first_pre":
        x = next((x for x in rel if x.get("prerelease")), rel[0] if rel else {})
    else:
        x = next((x for x in rel if want == "any" or bool(x.get("prerelease")) == (want == "alpha")), {})
    return str(x.get("tag_name") or "").lstrip("v")


def _remote_tag(api, repo, want, n, none_msg):
    """先查 GitHub API（缓存 5 分钟、限流退避）；API 限流或连不上时改读 github.com 的发布订阅源（releases.atom），不受每小时 60 次限制"""
    try:
        rel = gh_get(f"{api}?per_page={n}", opener=open_url)
        if not isinstance(rel, list):
            raise IOError("GitHub API 返回格式不对")
    except Exception as e:
        api_err = e
    else:
        v = _api_pick(rel, want)
        if not v:
            raise IOError(none_msg)
        return v
    try:
        tags = gh_atom_tags(repo, opener=open_url)
    except Exception as e2:
        raise IOError(f"{_short(api_err)}；发布页：{_short(e2)}") from e2
    v = pick_tag(tags, "alpha" if want == "first_pre" else want) or (tags[0] if want == "first_pre" else "")
    if not v:
        raise IOError(none_msg)
    return v.lstrip("v")


def sb_remote_version(channel, gh):
    """最新正式版 / 预发布版 / 魔改版的版本号（不带 v）。魔改版：reF1nd 取最新正式版；MiChongs 只发预发布，取最新发布的那一个"""
    if channel in FORK_CHANNELS:
        repo, api, want = fork_src(channel=channel)
        return _remote_tag(api, repo, want, 15, FORK_NONE_MSG)
    if channel != "stable":
        return _remote_tag(SB_API, SB_REPO, "first_pre", 10, "获取 sing-box 版本号失败")
    errs = []
    try:
        j = gh_get(SB_API + "/latest", opener=open_url)
        v = str((j if isinstance(j, dict) else {}).get("tag_name") or "").lstrip("v")
        if v:
            return v
    except Exception as e:
        errs.append(_short(e))
    try:
        with open_url(SB_REPO + "/latest", gh, timeout=15) as r:  # API 受限时从跳转地址里取版本号
            m = re.search(r"/tag/v?([\\w.\\-]+)", r.geturl())
        if m:
            return m.group(1)
    except Exception as e:
        errs.append(_short(e))
    try:
        v = pick_tag(gh_atom_tags(SB_REPO, opener=open_url), "stable")
        if v:
            return v.lstrip("v")
    except Exception as e:
        errs.append("发布页：" + _short(e))
    raise IOError("获取 sing-box 版本号失败" + ("：" + "；".join(errs) if errs else ""))


def is_musl():
    import glob
    return bool(glob.glob("/lib/ld-musl-*")) or os.path.isfile("/etc/alpine-release")


def sb_fetch(url, gh, digest=""):
    if not digest:
        gh = ""  # 没有校验值时不走第三方镜像
    with open_url(url, gh, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0)
        buf, n = io.BytesIO(), 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            n += len(chunk)
            if n > 120 * 2**20:
                raise ValueError("下载文件异常过大，已中止")
            buf.write(chunk)
            if total and not getattr(QUIET, "on", False):
                CORE_UPD["pct"] = 10 + int(50 * n / total)
    if total and n < total:
        raise ValueError(f"下载不完整 {n}/{total}")
    if digest and hashlib.sha256(buf.getbuffer()).hexdigest() != digest:
        raise ValueError("SHA256 与 GitHub 不一致（可能被加速镜像篡改）")
    buf.seek(0)
    return buf


def is_404(e):
    """每种下载方式都是 404（加速地址、代理、直连都说没有这个文件）"""
    parts = [x for x in str(e).split("；") if x.strip()]
    return bool(parts) and all("HTTP Error 404" in x for x in parts)


def sb_install_fail_msg(ver, errs, ran):
    """sing-box 安装失败的提示：下载到了但跑不起来 / 上游没有这个文件 / 根本没下载下来，三种原因分开说"""
    detail = "；".join(errs)
    if ran:
        return "下载的 sing-box 无法运行（架构不匹配或文件损坏），未做任何替换：" + detail
    if errs and all("HTTP 404" in x for x in errs):
        return f"GitHub 上找不到 sing-box {ver} 的下载文件（HTTP 404），发布者可能已撤下这个版本或还在上传，未做任何替换。请稍后再试或换其他版本"
    return "下载 sing-box 失败（没能从 GitHub 下载到文件），未做任何替换。请检查网络和当前代理节点后重试：" + detail


def is_sb_member(name):
    """压缩包里的 sing-box 程序：官方叫 sing-box，魔改版叫 sing-box-<版本>-linux-<架构>"""
    b = os.path.basename(name)
    return b == "sing-box" or (b.startswith("sing-box-") and "-linux-" in b and not b.endswith((".txt", ".md", ".json")))


def sb_install(channel, gh, force=False, dest=None):
    """下载 sing-box 到 dest（默认 SB_BIN，旧版本备份为 .bak；内核缓存目录里的不备份）；返回 (版本号, 是否换了新程序)"""
    dest = dest or SB_BIN
    import tarfile
    arch = sb_arch()
    if not arch:
        raise ValueError(f"sing-box 不支持当前 CPU 架构：{os.uname().machine}")
    ver = sb_remote_version(channel, gh)
    cur = sb_version(dest)
    if cur == ver and not force:
        return ver, False
    fork = channel in FORK_CHANNELS
    repo = fork_src(arch, channel)[0] if fork else SB_REPO
    ref1nd = fork and fork_src(arch, channel)[2] != "any"
    if fork and not ref1nd:
        arch = FORK_ARCH.get(arch, arch)
        if arch in ("mipsle", "mips64le"):
            arch += "-softfloat"
    # 1.14 起普通 linux 包依赖 glibc，Alpine(musl) 上跑不起来，优先下 -musl 版
    # reF1nd 只有 -glibc / -musl / -purego（纯 Go，不依赖 libc）三种包；MiChongs 的普通包是静态编译，只有 arm64 另有 -musl
    if ref1nd:
        variants = ["-musl", "-purego", "-glibc"] if is_musl() else ["-glibc", "-purego", "-musl"]
    elif fork:
        variants = ["-musl", ""] if is_musl() and arch == "arm64" else [""]
    else:
        variants = ["-musl", "", "-glibc"] if is_musl() else ["", "-glibc", "-musl"]
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    free = shutil.disk_usage(os.path.dirname(dest)).free
    if free < 80 * 2**20:
        raise ValueError(f"{os.path.dirname(dest)} 剩余空间不足（{free // 2**20} MB，至少需要 80 MB）")
    tmp, errs, nv, ran = dest + ".new", [], "", False
    for v in variants:
        name = f"sing-box-{ver}-linux-{arch}{v}.tar.gz"
        _cu("下载", 10, f"下载 {name}")
        try:
            buf = sb_fetch(f"{repo}/download/v{ver}/{name}", gh, release_digest(repo, "v" + ver, name))
            with tarfile.open(fileobj=buf, mode="r:gz") as tf:
                mem = next((m for m in tf.getmembers() if m.isfile() and is_sb_member(m.name)), None)
                if not mem:
                    raise ValueError("压缩包里没有 sing-box 程序")
                with tf.extractfile(mem) as src, open(tmp, "wb") as f:
                    shutil.copyfileobj(src, f)
        except Exception as e:
            errs.append(f"{name}: " + ("文件不存在（HTTP 404）" if is_404(e) else str(e)[:300]))
            continue
        os.chmod(tmp, 0o755)
        nv = sb_version(tmp)
        if nv:
            break
        _, out = sh(f"{shlex.quote(tmp)} version", timeout=15)
        ran = True
        errs.append(f"{name}: 无法运行 {out.strip()[:100]}")
        os.remove(tmp)
    if not nv:
        raise ValueError(sb_install_fail_msg(ver, errs, ran))
    if dest == SB_BIN and os.path.isfile(SB_BIN):
        shutil.copy2(SB_BIN, SB_BIN + ".bak")
    os.replace(tmp, dest)
    return nv, True


_UNSET = object()


def core_label(kind, var=""):
    from .core_cache import SB_VARIANTS
    return SB_VARIANTS[var][0] if kind == "singbox" and var in SB_VARIANTS else CORES[kind]


def core_bin(kind):
    """内核种类 → 程序路径"""
    return SB_BIN if kind == "singbox" else mh_bin(kind)


def kill_old_cores(kind):
    """切换内核后还在跑的其他内核进程（不是 kind 的程序）全部结束，返回个数。
    rc-service restart 停旧核心时按 pid 文件 + 程序路径匹配，而 /etc/mihomo-panel/core 已改成新内核、init.d 的 command
    已指向新程序（OpenRC 记下的启动命令被 zap 清掉时就会用它）：匹配不上，旧核心没停、继续占着端口，新核心起不来；
    旧核心照样应答控制接口，面板还以为切换成功了"""
    new = os.path.realpath(core_bin(kind))
    old = tuple(b for b in {SB_BIN, mh_bin("mihomo"), mh_bin("smart")} if b and os.path.realpath(b) != new)
    return kill_core_procs(bins=old) if old else 0


def core_switch_job(kind, gh, variant=None):
    """切换内核：准备程序与数据 → 生成并校验配置 → 写入启动选择 → 重启；失败自动换回原内核。
    sing-box 的几个版本（官方 / reF1nd / MiChongs）各有缓存：缓存里有就直接用，没有才下载"""
    from .core_cache import activate_variant, cache_path, ensure_variant, variant_of

    def current_variant():
        return variant_of(sb_version())
    prev = active_core()
    prev_label = core_label(prev, current_variant())
    dns_auto_fix = False  # 切到 sing-box 时是否自动开启了「阻止客户端绕过 DNS」
    bb_prev = _UNSET
    swapped = False  # 是否换了 sing-box 程序（失败时换回 .bak）

    def _unswap():
        nonlocal swapped
        if swapped:
            copy_replace(SB_BIN + ".bak", SB_BIN)
            swapped = False
            if prev == "singbox":
                try:
                    write_sb_config(load())  # 官方版 ↔ 魔改版：负载均衡组按换回的程序重新生成
                except Exception as e:
                    _cu("回滚", None, "✗ 重新生成 sing-box 配置失败：" + str(e)[:120])
    try:
        label = CORES[kind]
        if kind == "singbox":
            var = variant or current_variant() or "official"
            label = core_label(kind, var)
            if current_variant() != var:
                cv = sb_version(cache_path(var))
                if cv:
                    _cu("准备程序", 5, f"使用已下载的 {label} {cv}，不用重新下载")
                else:
                    _cu("下载 " + label, 5, f"设备上还没有 {label}，正在下载")
                    cv, _ = ensure_variant(var, gh)
                    _cu("下载 " + label, 62, f"已下载 {label} {cv}")
                swapped = activate_variant(var)
            d = load()
            bb_prev = (d.get("dns") or {}).get("block_bypass", _UNSET)  # 回滚时恢复原值（原来没有这一项就删掉）
            if not dns_cfg(d)["block_bypass"]:
                # sing-box 1.13+ 移除了 sniff_override_destination，设备 DNS 绕过面板时拿到污染 IP 必死；
                # mihomo 的 sniffer 默认改写目标地址所以不受影响。切到 sing-box 时自动开启防绕过。
                d.setdefault("dns", {})["block_bypass"] = True
                dns_auto_fix = True
            if d["subs"]:
                _cu("下载订阅", 65, "sing-box 不能自己拉取订阅，由面板下载并解析节点")
                ok, bad = sb_update_subs(d=d)
                for x in ok + bad:
                    _cu("下载订阅", None, x)
                if not ok:
                    raise ValueError("订阅全部下载失败，未切换：" + "；".join(bad)[:300])
            _cu("规则集", 75, "下载 sing-box 格式的 GEO / 规则集文件")
            bad = sb_sync_assets(d)
            for x in bad[:5]:
                _cu("规则集", None, "✗ " + x)
            crit = [x for x in bad if x.split("：", 1)[0] in ("geosite-cn", "geoip-cn", "geosite-private")]
            if crit:  # 缺了国内 / 私有地址规则集，相关规则会被裁掉，国内流量全部走「漏网之鱼」代理，宁可不切
                raise ValueError("国内分流规则集下载失败，未切换（可在 设置 里填 GitHub 加速地址后重试）：" + "；".join(crit)[:300])
            tmp = write_sb_config(d, SB_CONF + ".new")
            ok, err = check_sb_config(tmp)
            if not ok:
                os.remove(tmp)
                raise ValueError("sing-box 配置校验失败，未切换：" + err)
            os.replace(tmp, SB_CONF)
        else:
            if kind == "smart" and not bin_version(SMART_BIN):
                _cu("安装 Smart 内核", 5, "设备上还没有 mihomo Smart，正在从 vernesong/mihomo 下载")
                tmp = mh_fetch("smart", "alpha", gh, lambda pc: CORE_UPD.update(pct=5 + int(55 * pc)))[0]
                os.replace(tmp, SMART_BIN)
                _cu("安装 Smart 内核", 62, f"已安装 {bin_version(SMART_BIN)}")
            if not bin_version(mh_bin(kind)):
                raise ValueError(f"设备上没有可用的 {label} 程序，请先在「内核更新」里安装")
            d = dict(load(), core=kind)  # 按目标内核生成（Smart：竞技组改为 smart 类型）
            if kind == "smart" and gcfg(d)["lgbm"] and not os.path.isfile(LGBM_FILE):
                _cu("LightGBM 模型", 64, "下载 LightGBM 模型 Model.bin")
                try:
                    lgbm_fetch(gh)
                except Exception as e:
                    _cu("LightGBM 模型", None, "✗ 模型下载失败，Smart 组先用内置算法：" + str(e)[:120])
            tmp = write_config(d, os.path.join(CONF_DIR, "config.yaml.new"))
            ok, err = check_config(tmp, kind)
            if not ok:
                os.remove(tmp)
                raise ValueError(f"{label} 配置校验失败，未切换：" + err)
            os.replace(tmp, os.path.join(CONF_DIR, "config.yaml"))
        snapshot_selections(force=True)

        def _apply_switch(x):
            x["core"] = kind
            if dns_auto_fix:
                x.setdefault("dns", {})["block_bypass"] = True

        update(_apply_switch)
        write_core_file(kind)
        PROV.update(t=0, data=None)
        _cu("重启核心", 88, f"正在以 {label} 重启核心（局域网会断网几秒）")
        set_manual_stop(False)
        svc_restart(timeout=90)
        if kill_old_cores(kind):  # 旧内核没被停掉：结束它后再启动一次（新核心多半因端口被占已退出）
            log_err("切换内核", Exception(f"切换到 {label} 时旧内核进程仍在运行，已结束并重新启动核心"))
            svc_restart(timeout=90)
        for _ in range(30):
            time.sleep(1)
            if core_alive():
                break
        else:
            log = core_fail_log()  # 切回原内核会重启并清空核心日志，先把报错存下来
            def _undo(x):
                x["core"] = prev
                if dns_auto_fix:  # 切换时自动打开的「阻止客户端绕过 DNS」一并关回去
                    if bb_prev is _UNSET:
                        (x.get("dns") or {}).pop("block_bypass", None)
                    else:
                        x.setdefault("dns", {})["block_bypass"] = bb_prev

            update(_undo)
            write_core_file(prev)
            _unswap()
            svc_restart(timeout=90)
            if kill_old_cores(prev):
                svc_restart(timeout=90)
            raise ValueError(f"{label} 30 秒内未响应，已切回 {prev_label}。" + ("报错：" + log if log else "完整日志见 " + CORE_FAIL_LOG))
        threading.Thread(target=restore_selections, daemon=True).start()
        if dns_auto_fix and d.get("proxy_mode") != "off":
            sh(TPROXY_SH + " apply", timeout=15)  # block_bypass 变化：重载 nftables 劫持任意目标的 53 端口
        msg = f"已切换到 {label}"
        if kind == "singbox" and SB_STATE["warn"]:
            msg += f"（{len(SB_STATE['warn'])} 条兼容提示见设置 → 核心）"
        if dns_auto_fix:
            msg += "；已自动开启「阻止客户端绕过 DNS」（sing-box 要求设备 DNS 经过面板，如不需要可在 设置→DNS 关闭）"
        CORE_UPD.update(ok=True, message=msg)
        notify(f"🔀 内核已切换：{prev_label} → {label}")
    except Exception as e:
        try:
            _unswap()
        except Exception as e2:
            log_err("换回原 sing-box", e2)
        CORE_UPD.update(ok=False, message=str(e))
    finally:
        CORE_UPD["pct"] = 100
        _cu("完成" if CORE_UPD["ok"] else "失败", 100, CORE_UPD["message"])
        CORE_UPD["busy"] = False


def sb_update_job(channel, gh, force=False):
    try:
        cur = sb_version()
        _cu("获取版本", 3, "正在获取 sing-box 最新版本号")
        nv, changed = sb_install(channel, gh, force)
        if not changed:
            CORE_UPD.update(ok=True, message=f"已是最新版本 {nv}，无需更新")
            return
        _cu("校验", 70, f"用 sing-box {nv} 检查当前配置")
        if active_core() == "singbox":
            write_sb_config(load())  # 官方版 ↔ 魔改版：负载均衡组（loadbalance / urltest）按新内核重新生成
            ok, err = check_sb_config(SB_CONF)
            if not ok:
                copy_replace(SB_BIN + ".bak", SB_BIN)
                write_sb_config(load())
                raise ValueError("新版 sing-box 不兼容当前配置，已恢复原版本：" + err)
        note = ""
        if active_core() == "singbox" and WD["manual_stop"]:  # 手动停止了核心：只替换程序，不替用户拉起来
            note = f"（{MANUAL_NOTE}）"
        elif active_core() == "singbox":
            _cu("重启核心", 85, "正在重启核心")
            svc_restart(timeout=90)
            for _ in range(30):
                time.sleep(1)
                if core_alive():
                    break
            else:
                log = core_fail_log()  # 恢复原版本会重启并清空核心日志，先把新版的报错存下来
                copy_replace(SB_BIN + ".bak", SB_BIN)
                write_sb_config(load())
                svc_restart(timeout=90)
                raise ValueError(f"新版 sing-box {nv} 30 秒内未响应，已恢复原版本。" + ("报错：" + log if log else "完整日志见 " + CORE_FAIL_LOG))
        try:
            from .core_cache import remember_current
            remember_current()  # 新程序也存进对应版本的缓存，切走再切回来不用重新下载
        except Exception as e:
            log_err("缓存 sing-box", e)
        kind = fork_name(sb_version()) + "，已开启多节点聚合" if sb_is_fork() else "官方版"
        CORE_UPD.update(ok=True, message=f"sing-box 已从 {cur or '未安装'} 更新到 {nv}（{kind}）" + ("，旧版本已备份" if cur else "") + note)
    except Exception as e:
        CORE_UPD.update(ok=False, message=str(e))
    finally:
        CORE_UPD["pct"] = 100
        _cu("完成" if CORE_UPD["ok"] else "失败", 100, CORE_UPD["message"])
        CORE_UPD["busy"] = False


'''
_SRC['shunt.panel_update'] = '''"""面板在线更新"""
import hashlib, os, re, shlex, shutil, time, urllib.request, urllib.error

from .errlog import log_err
from .base import BASE, PANEL_VERSION, UA, UPD_STATE, dl_check, local_file, upd_begin, upd_busy_msg
from .core_update import gh_api, GhRateLimit
from .sysopt import proxy_opener, sh
from .https import restart_self
from .update_guard import start_update_guard
PANEL_RAW = os.environ.get("PANEL_RAW", "https://raw.githubusercontent.com/Skycnhe/Shunt-release/shunt")
UPD_FILES = ["server.py", "index.html", "tproxy.sh", "selftest.sh", "init.d/shunt", "init.d/shunt-core"]
UPD_LIST = "update-files.txt"  # 仓库里的部署文件清单：以后增减文件只改清单，不用再改更新程序


def remote_file_list(gh, commit, shas):
    """读取仓库里的部署文件清单（必须通过 GitHub 校验）；没有或拿不到校验值时用内置列表"""
    if not shas or UPD_LIST not in shas:
        return list(UPD_FILES)
    raw = fetch_raw(UPD_LIST, gh, commit, shas[UPD_LIST]).decode("utf-8", "replace")
    out = []
    for ln in raw.splitlines():
        f = ln.split("#", 1)[0].strip()
        if not f:
            continue
        if not re.fullmatch(r"[\\w.\\-]+(/[\\w.\\-]+)*", f) or ".." in f.split("/") or f.startswith("."):
            raise ValueError(f"部署清单里有不安全的路径：{f}")
        if f not in shas:
            raise ValueError(f"部署清单里的 {f} 在仓库中不存在")
        out.append(f)
    for need in ("server.py", "index.html"):
        if need not in out:
            raise ValueError(f"部署清单缺少 {need}")
    return out


def panel_manifest():
    """返回 (commit, {路径: git blob sha1})；失败返回 (None, {})"""
    m = re.match(r"https://raw\\.githubusercontent\\.com/([^/]+/[^/]+)/(.+?)/?$", PANEL_RAW)
    if not m:
        return None, {}
    try:
        c = gh_api(f"/repos/{m.group(1)}/commits/{m.group(2)}")
        t = gh_api(f"/repos/{m.group(1)}/git/trees/{c['commit']['tree']['sha']}?recursive=1")
        return c["sha"], {x["path"]: x["sha"] for x in t.get("tree", []) if x.get("type") == "blob"}
    except Exception as e:
        if not isinstance(e, GhRateLimit):
            log_err("获取面板校验值", e)
        return None, {}


def git_blob_sha(data):
    return hashlib.sha1(b"blob %d\\0" % len(data) + data).hexdigest()


def fetch_raw(f, gh, commit=None, sha=None):
    """下载面板文件。有 GitHub 给的校验值时可以走加速镜像，下载后逐个比对；没有校验值时不走镜像"""
    base = PANEL_RAW.rstrip("/")
    if commit:
        base = base.rsplit("/", 1)[0] + "/" + commit
    url = base + "/" + f
    if not sha:
        gh = ""
    tries = [(gh.rstrip("/") + "/" + url, False)] if gh else []
    tries += [(url, True), (url, False)]  # 先经 mihomo 代理，再直连
    errs = []
    for u, use_proxy in tries:
        try:
            dl_check(u, via_proxy=use_proxy)  # SSRF 防护：直连时校验 DNS，走代理时由代理解析（字面内网 IP 永远拦截）
            req = urllib.request.Request(u + ("&" if "?" in u else "?") + "t=" + str(int(time.time())), headers={"User-Agent": UA})
            with proxy_opener(use_proxy).open(req, timeout=20) as r:
                data = r.read(4 << 20)
            if sha and git_blob_sha(data) != sha:
                raise ValueError("文件校验失败（与 GitHub 上的不一致，可能被加速镜像篡改）")
            return data
        except Exception as e:
            errs.append(f"{u.split('/')[2]}{'(代理)' if use_proxy else '(直连)'}: {str(e)[:120]}")
    raise IOError(f"下载 {f} 失败：" + "；".join(errs))  # 每条线路的失败原因都列出，便于判断是代理还是直连的问题


def self_rollback():
    """把 .backup 里的文件（上次更新前的版本）全部放回去"""
    bak = os.path.join(BASE, ".backup")
    if not os.path.isfile(os.path.join(bak, "server.py")):
        return False, "没有可回滚的备份"
    if not upd_begin("panel"):
        return False, upd_busy_msg()
    try:
        for root, _, names in os.walk(bak):
            for n in names:
                src = os.path.join(root, n)
                shutil.copy2(src, local_file(os.path.relpath(src, bak)))
        restart_self()
    finally:
        UPD_STATE["busy"] = False
    return True, "已回滚到更新前的版本，面板正在重启"


def _undo_replace(done, bak):
    """面板更新替换到一半失败：已替换的文件从 .backup 放回，更新前没有的（新增的）删掉"""
    for f in done:
        dst, src = local_file(f), os.path.join(bak, f)
        try:
            if os.path.isfile(src):
                shutil.copy2(src, dst + ".new")
                os.replace(dst + ".new", dst)
            else:  # 新增的文件：删掉
                os.remove(dst)
        except OSError as e:
            log_err("面板更新失败后恢复 " + f, e)


def remote_version(src):
    m = re.search(rb'^PANEL_VERSION = "([^"]+)"', src, re.M)
    return m.group(1).decode() if m else "?"


def self_update(apply=False, gh=""):
    """检查 / 应用 GitHub 上的新版面板：下载全部文件 → 校验 → 备份 → 替换 → 重启面板"""
    # 只检查不替换时不占用忙标志；真正更新时与内核更新 / 切换 / 回滚互斥（内核替换到一半时重启面板会留下半截二进制）
    if apply and not upd_begin("panel"):
        return False, {"message": upd_busy_msg()}
    try:
        files, changed = {}, []
        commit, shas = panel_manifest()
        flist = remote_file_list(gh, commit, shas)
        for f in flist:
            data = fetch_raw(f, gh, commit, shas.get(f))
            files[f] = data
            try:
                with open(local_file(f), "rb") as fh:
                    same = fh.read() == data
            except OSError:
                same = False
            if not same:
                changed.append(f)
        info = {"current": PANEL_VERSION, "remote": remote_version(files["server.py"]), "changed": changed,
                "source": PANEL_RAW, "verified": bool(shas), "commit": (commit or "")[:10]}
        if not apply:
            info["message"] = "已是最新" if not changed else f"有 {len(changed)} 个文件可更新"
            return True, info
        if not changed:
            info["message"] = "已是最新，无需更新"
            return True, info
        try:  # 校验：Python 语法、HTML 完整、shell 语法
            compile(files["server.py"], "server.py", "exec")
        except SyntaxError as e:
            raise ValueError(f"新版 server.py 语法错误：{e}")
        if b"</html>" not in files["index.html"] or b"<script>" not in files["index.html"]:
            raise ValueError("新版 index.html 不完整")
        tmpd = os.path.join(BASE, ".update")
        shutil.rmtree(tmpd, ignore_errors=True)
        for f, data in files.items():
            os.makedirs(os.path.dirname(os.path.join(tmpd, f)), exist_ok=True)
            with open(os.path.join(tmpd, f), "wb") as fh:
                fh.write(data)
            if f.endswith(".sh") or f.startswith("init.d/"):
                code, out = sh("sh -n " + shlex.quote(os.path.join(tmpd, f)))  # f 来自远端清单：引用防注入
                if code != 0:
                    raise ValueError(f"新版 {f} 语法错误：{out[-200:]}")
        bak = os.path.join(BASE, ".backup")
        shutil.rmtree(bak, ignore_errors=True)
        os.makedirs(bak)
        for f in flist:
            if os.path.isfile(local_file(f)):
                os.makedirs(os.path.dirname(os.path.join(bak, f)), exist_ok=True)
                shutil.copy2(local_file(f), os.path.join(bak, f))
        # 版本备份：更新前自动打一个带版本号的 tar.gz（失败不阻断更新）
        try:
            from . import backup as backup_mod
            ok, name = backup_mod.create()
            if not ok:
                log_err("面板在线更新", "更新前自动备份失败：%s" % name)
        except Exception as e:
            log_err("面板在线更新：更新前自动备份异常", e)
        done = []
        try:
            for f in changed:
                dst = local_file(f)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(os.path.join(tmpd, f), dst + ".new")
                os.chmod(dst + ".new", 0o755 if (f.endswith(".sh") or f.startswith("init.d/")) else 0o644)
                os.replace(dst + ".new", dst)
                done.append(f)
        except Exception as e:
            # 替换到一半失败（磁盘满、只读等）：已替换的文件放回旧版，不留下「新 server.py + 旧 index.html」这样的混合版本
            try:
                os.remove(local_file(f) + ".new")
            except OSError:
                pass
            _undo_replace(done, bak)
            raise ValueError(f"替换文件失败，已恢复原文件：{e}")
        shutil.rmtree(tmpd, ignore_errors=True)
        start_update_guard(flist)
        restart_self()
        info["message"] = (f"已更新 {len(changed)} 个文件（{'、'.join(changed)}）" + ("，已通过 GitHub 校验" if shas else "")
                           + "，面板正在重启；新版启动失败会在 90 秒内自动回滚")
        return True, info
    except Exception as e:
        return False, {"message": str(e)}
    finally:
        if apply:
            UPD_STATE["busy"] = False

'''
_SRC['shunt.backup'] = '''# SPDX-License-Identifier: GPL-3.0-or-later
"""版本备份/恢复：把面板文件打包成带版本号的 tar.gz，支持一键回滚。"""
import json
import os
import re
import shutil
import tarfile
import tempfile
import time

from .base import BASE, DATA_FILE, PANEL_VERSION

# 备份目录：跟 data.json 同级
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(DATA_FILE)), "backups")
# 保留最近多少个备份
KEEP_N = 10

# 打包进备份的文件（相对于 BASE / DATA_FILE 所在目录）
_FILES = [
    ("base", "server.py"),
    ("base", "index.html"),
    ("base", "CHANGELOG.md"),
]


def _ensure_dir():
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)


def _fix_perms():
    """备份里有 data.json（订阅 URL、节点密码、TG token）：旧版按默认 umask 建成 644 的收紧为 600"""
    try:
        os.chmod(BACKUP_DIR, 0o700)
        for fn in os.listdir(BACKUP_DIR):
            fp = os.path.join(BACKUP_DIR, fn)
            if fn.endswith(".tar.gz") and os.path.isfile(fp) and os.stat(fp).st_mode & 0o077:
                os.chmod(fp, 0o600)
    except OSError:
        pass


def _backup_name(version, ts=None):
    ts = ts or time.strftime("%Y%m%d-%H%M%S")
    # 版本号里的非法字符替换掉，保证文件名安全
    safe_ver = re.sub(r"[^0-9A-Za-z.\\-]", "_", version or "unknown")
    return "shunt-backup-v%s-%s.tar.gz" % (safe_ver, ts)


def _parse_name(name):
    """从文件名解析出 version。返回 version 或 None。"""
    m = re.match(r"^shunt-backup-v(.+?)-(\\d{8}-\\d{6})(-\\d+)?\\.tar\\.gz$", name)
    return m.group(1) if m else None


def create(version=None, auto=False, keep=None):
    """创建备份。返回 (ok, name_or_message)。

    version: 打上的版本号，默认当前 PANEL_VERSION。
    auto: 是否自动备份（更新前调用），失败不抛异常。
    keep: 清理旧备份时必须保留的文件名（恢复时保住正要恢复的那个）。
    """
    version = version or PANEL_VERSION
    try:
        _ensure_dir()
        name = _backup_name(version)
        path = os.path.join(BACKUP_DIR, name)
        # 同名极小概率冲突（同一秒两次），加后缀
        i = 1
        while os.path.exists(path):
            name = _backup_name(version)[:-7] + "-%d.tar.gz" % i
            path = os.path.join(BACKUP_DIR, name)
            i += 1
        # 一开始就以 600 创建（不经过默认 umask 的 644 窗口）；O_EXCL 防止同名覆盖
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as fo, tarfile.open(fileobj=fo, mode="w:gz") as tf:
                for kind, rel in _FILES:
                    src = os.path.join(BASE, rel) if kind == "base" else rel
                    if os.path.isfile(src):
                        tf.add(src, arcname=rel)
                if os.path.isfile(DATA_FILE):
                    tf.add(DATA_FILE, arcname="data.json")
        except BaseException:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        cleanup(protect=keep)
        return True, name
    except Exception as e:
        # 自动备份失败不阻断调用方
        return False, str(e)


def list():
    """返回备份列表 [{name, version, size, mtime}]，按时间倒序。"""
    _ensure_dir()
    _fix_perms()
    out = []
    try:
        for fn in os.listdir(BACKUP_DIR):
            if not fn.endswith(".tar.gz"):
                continue
            ver = _parse_name(fn)
            if not ver:
                continue
            fp = os.path.join(BACKUP_DIR, fn)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            out.append({"name": fn, "version": ver, "size": st.st_size,
                        "mtime": int(st.st_mtime)})
    except OSError:
        pass
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def delete(name):
    """删除指定备份。返回 (ok, message)。"""
    if not name or "/" in name or "\\\\" in name or ".." in name:
        return False, "非法文件名"
    if not _parse_name(name):
        return False, "不是有效的备份文件"
    fp = os.path.join(BACKUP_DIR, name)
    if not os.path.isfile(fp):
        return False, "备份不存在"
    try:
        os.unlink(fp)
        return True, "已删除"
    except OSError as e:
        return False, str(e)


def cleanup(keep=KEEP_N, protect=None):
    """只保留最近 keep 个，删除最旧的（protect 指定的文件不删）。"""
    allb = list()
    items = [it for it in allb if it["name"] != protect]
    for it in items[keep - (len(allb) - len(items)):]:
        try:
            os.unlink(os.path.join(BACKUP_DIR, it["name"]))
        except OSError:
            pass


def restore(name, check_data=None, apply_data=None):
    """恢复指定备份。返回 (ok, message)。

    流程：解压到临时目录 → check_data 校验备份里的 data.json（不通过则什么都不改）→ 自动备份当前版本（防呆）
    → 覆盖 server.py/index.html，data.json 交给 apply_data 写入（没给时原样覆盖）。恢复 server.py 后需重启面板进程才生效。
    check_data(dict) 返回要写入的 dict，数据无效时抛 ValueError（提示直接给用户看）。
    """
    if not name or "/" in name or "\\\\" in name or ".." in name:
        return False, "非法文件名"
    if not _parse_name(name):
        return False, "不是有效的备份文件"
    fp = os.path.join(BACKUP_DIR, name)
    if not os.path.isfile(fp):
        return False, "备份不存在"
    try:
        with tarfile.open(fp, "r:gz") as tf:
            members = tf.getmembers()
            # 安全检查：不允许绝对路径、..、symlink/hardlink
            for m in members:
                if m.name.startswith("/") or ".." in m.name:
                    return False, "备份文件内容非法"
                if m.issym() or m.islnk():
                    return False, "备份文件含非法链接"
            # 解压到唯一临时目录再搬，避免半截覆盖和并发冲突
            tmpd = tempfile.mkdtemp(prefix="restore_", dir=BACKUP_DIR)
            try:
                try:
                    tf.extractall(tmpd, filter="data")
                except TypeError:  # Python 3.12 / 3.11.4 之前没有 filter 参数；成员上面已逐个检查过
                    tf.extractall(tmpd)
                src_data = os.path.join(tmpd, "data.json")
                new_data = None
                if os.path.isfile(src_data) and check_data is not None:
                    # 与「恢复设置」相同的校验：形状不对 / 生成不了配置的数据不写进 data.json，当前数据保持不变
                    try:
                        with open(src_data, encoding="utf-8") as f:
                            raw = json.load(f)
                        new_data = check_data(raw)
                    except ValueError as e:
                        return False, "备份里的设置无效，未恢复（当前设置未改动）：%s" % e
                # 防呆：先备份当前。已有 10 个备份时新备份会挤掉最旧的一个——恰好是要恢复的这个时以前会把它删掉
                ok, cur = create(keep=name)
                if not ok:
                    return False, "备份当前版本失败，中止恢复：%s" % cur
                # 搬文件
                for rel in ("server.py", "index.html", "CHANGELOG.md"):
                    src = os.path.join(tmpd, rel)
                    if os.path.isfile(src):
                        dst = os.path.join(BASE, rel)
                        shutil.copyfile(src, dst + ".new")
                        os.chmod(dst + ".new", 0o644)
                        os.replace(dst + ".new", dst)
                if new_data is not None and apply_data is not None:
                    apply_data(new_data)
                elif os.path.isfile(src_data):
                    shutil.copyfile(src_data, DATA_FILE + ".new")
                    os.chmod(DATA_FILE + ".new", 0o600)
                    os.replace(DATA_FILE + ".new", DATA_FILE)
            finally:
                shutil.rmtree(tmpd, ignore_errors=True)
        return True, "已恢复到 %s（已自动备份当前版本 %s）。" % (name, cur)
    except Exception as e:
        return False, "恢复失败：%s" % e
'''
_SRC['shunt.health'] = '''"""节点健康检查：定时测速，连续失败的节点标红/自动隐藏。"""
import json
import threading
import time
from urllib.parse import quote

from .base import BUILTIN_POLICIES, G_MANUAL, HC
from .corehttp import core, core_json
from .data import load, update
from .nodeprobe import fill_absent
from .sb_common import active_core

HEALTH_STATE = {"running": False, "last_run": 0, "total": 0, "checked": 0}

DEFAULT_CFG = {"enabled": False, "time": "03:00", "warn_days": 3, "hide_days": 7}


def health_cfg(d=None):
    """读取健康检查配置（data.json 的 health_cfg 字段）"""
    d = d or load()
    cfg = dict(DEFAULT_CFG)
    cfg.update(d.get("health_cfg") or {})
    return cfg


def save_health_cfg(cfg):
    """与其他写入共用 data.update()（全局锁内读-改-写、原子落盘），不会和同时保存的设置互相覆盖"""
    update(lambda d: d.update(health_cfg=dict(cfg)))


def _today():
    return time.strftime("%Y-%m-%d")


def record_one(rec, ok, latency=0):
    """更新单个节点的健康记录。成功：清零并取消隐藏；失败：按天去重计数。"""
    today = _today()
    if ok:
        rec["fail_days"] = 0
        rec["fail_dates"] = []
        rec["hidden"] = False
        rec["last_ok"] = int(time.time())
        if latency:
            rec["latency"] = latency
    else:
        dates = rec.get("fail_dates") or []
        if today not in dates:
            dates.append(today)
            # 只保留最近 30 天的记录
            dates = sorted(dates)[-30:]
        rec["fail_dates"] = dates
        rec["fail_days"] = len(dates)
        rec["last_fail"] = int(time.time())
    return rec


def node_level(rec, cfg=None):
    """节点健康等级：ok / warn / bad / hidden"""
    cfg = cfg or DEFAULT_CFG
    if rec.get("hidden"):
        return "hidden"
    fail_days = rec.get("fail_days", 0)
    if fail_days >= cfg.get("hide_days", 7):
        return "hidden"
    if fail_days >= cfg.get("warn_days", 3):
        return "bad"
    if fail_days > 0:
        return "warn"
    return "ok"


def apply_results(results, d=None, universe=None):
    """合并一次全量测速结果：{node_name: latency_ms(0=失败)}。
    universe：当前全部节点名；给出时只删除不在其中的节点记录（失败节点保留并计数），
    不给时按 results 的键清理（兼容旧调用）。
    d 给出时只在这份数据上合并（不落盘）；否则在 data.update() 的锁内读-改-写 data.json"""
    out = {}

    def merge(dd):
        cfg = health_cfg(dd)
        health = dd.get("health") or {}
        for name, latency in results.items():
            rec = health.get(name) or {"fail_days": 0, "fail_dates": [], "hidden": False}
            record_one(rec, latency > 0, latency)
            # 自动隐藏达到阈值的节点
            if rec["fail_days"] >= cfg.get("hide_days", 7):
                rec["hidden"] = True
            health[name] = rec
        # 清理已删除节点的记录
        all_nodes = set(results.keys()) if universe is None else set(universe) | set(results.keys())
        for name in list(health.keys()):
            if name not in all_nodes:
                del health[name]
        dd["health"] = health
        out["health"] = health

    if d is not None:
        merge(d)
    else:
        update(merge)
    return out["health"]


def merge_delay(members, code, data):
    """把 /group/…/delay 的应答整理成 {节点: 延迟(0=失败或缺席)}。
    mihomo 与 sing-box 的 clash API 都只返回测速成功的节点，失败的直接缺席；
    mihomo 全部失败时返回 504，sing-box 返回空对象。全集取自组的 all 成员。
    mihomo 测完整组，缺席即失败；sing-box 同时只测 10 个、共用超时，缺席可能只是没轮到测，调用方要用 nodeprobe.fill_absent 补测"""
    ok = data if code == 200 and isinstance(data, dict) else {}
    nodes = [n for n in (members or list(ok)) if isinstance(n, str) and n not in BUILTIN_POLICIES]
    results = {}
    for n in nodes:
        v = ok.get(n)
        results[n] = v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0
    return results


def unhide(name):
    """手动取消隐藏节点，清零计数重新评估"""
    found = []

    def fn(d):
        health = d.get("health") or {}
        rec = health.get(name)
        if rec:
            rec["hidden"] = False
            rec["fail_days"] = 0
            rec["fail_dates"] = []
            d["health"] = health
            found.append(True)
    if name not in (load().get("health") or {}):
        return False
    update(fn)
    return bool(found)


def get_records(d=None):
    """返回所有节点的健康记录"""
    d = d or load()
    return d.get("health") or {}


HEALTH_PROBE_BUDGET = 240  # sing-box 逐个补测的时间上限（每天一次的后台检查，比告警复核宽松；需小于 base.MASS_MAX_AGE）


def run_check():
    """执行一次全量健康检查：经核心 delay 接口批量测速"""
    HEALTH_STATE["running"] = True
    HEALTH_STATE["checked"] = 0
    try:
        # 用「🖐️ 手动选择」组测速（含全部节点），不影响 url-test 组的固定选择
        pg = core_json("/proxies/" + quote(G_MANUAL))
        if not pg:
            return False, "手动选择组不存在"
        members = pg.get("all") or []
        code, raw = core("GET", f"/group/{quote(G_MANUAL)}/delay?url={quote(HC)}&timeout=5000", timeout=180)
        if code not in (200, 504):  # 504：mihomo 全部节点超时，按全部失败记录
            return False, f"测速接口返回 {code}"
        data = {}
        if code == 200:
            try:
                data = json.loads(raw)
            except Exception:
                return False, "测速结果解析失败"
        results = merge_delay(members, code, data)
        if not results:
            return False, "手动选择组里没有节点"
        universe = list(results)
        HEALTH_STATE["total"] = len(results)
        undecided = 0
        if code == 200 and active_core() == "singbox":
            # sing-box 整组测速同时只测 10 个、共用超时，没轮到测的节点也不在结果里：缺席不等于失败，逐个补测；
            # 补测也没有结论的节点（核心重载中、预算内没测完）本轮不记录，免得把可用节点记一天失败
            fails = sum(1 for v in results.values() if not v)
            HEALTH_STATE["checked"] = len(results) - fails
            base_n = HEALTH_STATE["checked"]
            results = fill_absent(results, HEALTH_PROBE_BUDGET, progress=lambda k: HEALTH_STATE.update(checked=base_n + k))
            undecided = len(universe) - len(results)
            if not results:
                return False, "核心没有返回测速结果，本次未记录"
        HEALTH_STATE["checked"] = len(universe)
        apply_results(results, universe=universe)
        HEALTH_STATE["last_run"] = int(time.time())
        ok_count = sum(1 for v in results.values() if v > 0)
        if undecided:
            return True, f"检查完成：{ok_count}/{len(universe)} 个节点正常，{undecided} 个未测出结果（本次不记录）"
        return True, f"检查完成：{ok_count}/{len(results)} 个节点正常"
    finally:
        HEALTH_STATE["running"] = False


_BG_LOCK = threading.Lock()


def run_check_bg():
    """后台线程执行检查，API 立即返回。
    「查看 running → 起线程 → 线程里才置 running」之间有空档：连点两下会同时跑两次全量测速（路由器上并发翻倍、
    health 记录被两次合并）。现在在锁内先置位"""
    with _BG_LOCK:
        if HEALTH_STATE["running"]:
            return False
        HEALTH_STATE["running"] = True

    def job():
        try:
            run_check()
        except Exception as e:
            from .errlog import log_err
            log_err("节点健康检查", e, tb=True)
        finally:
            HEALTH_STATE["running"] = False
    try:
        threading.Thread(target=job, daemon=True).start()
    except Exception:
        HEALTH_STATE["running"] = False
        raise
    return True
'''
_SRC['shunt.core_update'] = '''"""内核（mihomo）更新"""
import hashlib, json, os, re, shutil, shlex, sys, threading, time, urllib.request, urllib.error
from collections import deque
from urllib.parse import quote, unquote

from .errlog import log_err
from .base import CONF_DIR, CORE_UPD, LGBM_URL, MIHOMO_LOG, UA, dl_check, upd_begin, upd_busy_msg
from .data import load
from .mihomo import mh_bin, write_config
from .core import reload_core
from .sb_common import CORES, active_core
from .sysopt import proxy_opener, sh, svc_restart
from .notify import WD, core_alive, notify
CORE_REPO = os.environ.get("CORE_REPO", "https://github.com/MetaCubeX/mihomo/releases")
CORE_CHANNELS = {"stable": "latest/download", "alpha": "download/Prerelease-Alpha"}
SMART_REPO = os.environ.get("SMART_REPO", "https://github.com/vernesong/mihomo/releases")  # 只发布 Alpha 预览版
LGBM_FILE = os.path.join(CONF_DIR, "Model.bin")


def core_arch():
    m = os.uname().machine
    if m == "x86_64":
        try:  # 支持 AVX2 等 x86-64-v3 指令集的 CPU 用 v3 版本更快，否则用兼容版
            flags = open("/proc/cpuinfo").read()
            if all(f in flags for f in (" avx2", " bmi2", " fma", " movbe")):
                return "amd64-v3"
        except OSError:
            pass
        return "amd64-compatible"
    return {"aarch64": "arm64", "arm64": "arm64", "armv7l": "armv7", "armv8l": "armv7", "armv6l": "armv6",
            "i686": "386", "i386": "386", "riscv64": "riscv64", "loongarch64": "loong64-abi2"}.get(m, "")


def bin_version(path):
    """执行 mihomo -v，返回版本号（如 v1.19.32 / alpha-9f053c4）；不能运行时返回空串"""
    if not os.path.isfile(path):
        return ""
    code, out = sh(f"{shlex.quote(path)} -v", timeout=15)
    m = re.search(r"Mihomo\\s+Meta\\s+(\\S+)", out) if code == 0 else None
    return m.group(1) if m else ""


def open_url(url, gh, timeout=20, headers=None):
    """GitHub 下载：先走加速地址，再经 mihomo 代理，最后直连。headers 只加在 GitHub 自己的地址上（Token 绝不发给加速镜像）"""
    tries = [(gh.rstrip("/") + "/" + url, False)] if gh else []
    tries += [(url, True), (url, False)]
    errs = []
    for u, use_proxy in tries:
        try:
            eff_proxy = use_proxy and core_alive()
            dl_check(u, via_proxy=eff_proxy)  # SSRF 防护：直连时校验 DNS，走代理时由代理解析（字面内网 IP 永远拦截）
            hd = {"User-Agent": UA}
            if headers and u == url:
                hd.update(headers)
            req = urllib.request.Request(u, headers=hd)
            return proxy_opener(eff_proxy).open(req, timeout=timeout)
        except Exception as e:
            errs.append(f"{u.split('/')[2]}{'(代理)' if use_proxy else ''}: {str(e)[:120]}")
    raise IOError("；".join(errs))  # 每种方式的错误都保留：只看最后一次直连的错误容易误判原因


class GhRateLimit(IOError):
    """GitHub API 限流：未登录每个出口 IP 每小时 60 次，家宽 CGNAT / 机场共享出口很容易被别人用完。
    这不是面板故障，校验值拿不到时更新照样进行（只是不走加速镜像），所以不记入「面板内部错误」"""


GH_RL = {"until": 0}
GH_RL_WAIT = 900  # 限流后 15 分钟内不再请求 GitHub API（再请求只会继续 403）
GH_CACHE = {}  # url -> (时间, 结果)：版本列表 5 分钟内重复查询直接用上次的结果（「全部检查」一次要查 6 个内核）
GH_CACHE_TTL = 300
GH_CACHE_MAX = 64
_GH_LOCK = threading.Lock()
GH_TOKEN_HINT = "；可在「设置 → 备份与更新 → 面板更新」填 GitHub Token（每小时 5000 次）"


def gh_token():
    """可选的 GitHub Token（设置里填写，或环境变量 SHUNT_GH_TOKEN）：只发给 api.github.com，额度从每 IP 60 次 / 小时变成 5000 次"""
    t = os.environ.get("SHUNT_GH_TOKEN", "")
    if not t:
        try:
            t = load().get("gh_token") or ""
        except Exception:
            t = ""
    return t.strip() if isinstance(t, str) else ""


def _rl_msg(mins, tok):
    return f"GitHub API 暂时限流（{'Token 额度已用完' if tok else '未登录每个 IP 每小时 60 次'}），约 {mins} 分钟后恢复" + ("" if tok else GH_TOKEN_HINT)


def _cache_get(key, ttl=GH_CACHE_TTL):
    with _GH_LOCK:
        hit = GH_CACHE.get(key)
    return hit[1] if hit and time.time() - hit[0] < ttl else None


def _cache_put(key, val):
    with _GH_LOCK:
        if len(GH_CACHE) >= GH_CACHE_MAX:
            for k in sorted(GH_CACHE, key=lambda k: GH_CACHE[k][0])[:GH_CACHE_MAX // 2]:
                GH_CACHE.pop(k, None)
        GH_CACHE[key] = (time.time(), val)


def gh_get(url, timeout=15, cache=True, opener=None):
    """GET 一个 GitHub API 地址（完整 URL）并解析 JSON：可选缓存 5 分钟、限流后 15 分钟内不再请求、有 Token 时带上。
    opener 默认 open_url（测试 / 调用方可以换成自己模块里的 open_url）"""
    if cache:
        hit = _cache_get(url)
        if hit is not None:
            return hit
    tok = gh_token()
    if time.time() < GH_RL["until"]:
        raise GhRateLimit(_rl_msg(max(1, int(GH_RL["until"] - time.time()) // 60 + 1), tok))
    kw = {"headers": {"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json"}} if tok else {}
    try:
        with (opener or open_url)(url, "", timeout=timeout, **kw) as r:
            j = json.loads(r.read(8 << 20))
    except IOError as e:
        low = str(e).lower()
        if "rate limit" in low:
            GH_RL["until"] = time.time() + GH_RL_WAIT
            raise GhRateLimit(_rl_msg(GH_RL_WAIT // 60, tok)) from e
        if tok and "http error 401" in low:
            raise IOError("GitHub Token 无效或已过期（HTTP 401），请在「面板更新」里重新填写或清空") from e
        raise
    if cache:
        _cache_put(url, j)
    return j


def gh_api(path, timeout=15, cache=False):
    """直连或经 mihomo 代理访问 GitHub API（绝不走第三方加速镜像：校验值必须来自 GitHub 本身）"""
    return gh_get("https://api.github.com" + path, timeout=timeout, cache=cache)


PRE_TAG = re.compile(r"-(alpha|beta|rc|pre)", re.I)


def gh_atom_tags(repo_url, timeout=15, opener=None):
    """不走 API 的版本列表：github.com/<仓库>/releases.atom（新 → 旧的版本标签，不受 API 每小时 60 次的限制）。
    订阅源里看不出哪个是预发布，调用方按标签名里的 -alpha / -beta / -rc 判断"""
    url = repo_url.rstrip("/") + ".atom"
    hit = _cache_get(url)
    if hit is not None:
        return hit
    with (opener or open_url)(url, "", timeout=timeout) as r:
        txt = r.read(4 << 20).decode("utf-8", "ignore")
    tags = []
    for m in re.finditer(r"/releases/tag/([^\\"'<>\\s]+)", txt):
        t = unquote(m.group(1))
        if t not in tags:
            tags.append(t)
    if not tags:
        raise IOError("GitHub 发布页里没有版本信息")
    _cache_put(url, tags)
    return tags


def pick_tag(tags, want):
    """want：stable 只要正式版、alpha 只要预发布、any 取最新一个；没有符合的返回空串"""
    for t in tags:
        pre = bool(PRE_TAG.search(t))
        if want == "any" or pre == (want == "alpha"):
            return t
    return ""


def _cached_digest(repo, tag, name):
    """查版本列表时 API 已经带回了每个文件的 digest：直接从缓存里找，省一次 API 请求"""
    pre = f"https://api.github.com/repos/{repo}/releases"
    with _GH_LOCK:
        vals = [v for k, (_, v) in GH_CACHE.items() if k.startswith(pre)]
    for v in vals:
        for rel in (v if isinstance(v, list) else [v]):
            if isinstance(rel, dict) and rel.get("tag_name") == tag:
                for a in rel.get("assets") or []:
                    if isinstance(a, dict) and a.get("name") == name and str(a.get("digest") or "").startswith("sha256:"):
                        return a["digest"][7:]
    return ""


def release_digest(repo_url, tag, name):
    """GitHub Release 资产的 SHA256（GitHub 自动计算，API 的 digest 字段）；拿不到返回空串"""
    m = re.match(r"https://github\\.com/([^/]+/[^/]+)/releases", repo_url)
    if not m:
        return ""
    hit = _cached_digest(m.group(1), tag, name)
    if hit:
        return hit
    try:
        j = gh_api(f"/repos/{m.group(1)}/releases/tags/{quote(tag)}", cache=True)
        for a in j.get("assets") or []:
            if a.get("name") == name and str(a.get("digest") or "").startswith("sha256:"):
                return a["digest"][7:]
    except Exception as e:
        if not isinstance(e, GhRateLimit):
            log_err("获取内核校验值", e)
    return ""


class HashReader:
    """边读边算 SHA256 的包装（流式解压时用）"""
    def __init__(self, r, prog=None, total=0):
        self.r, self.h, self.n, self.prog, self.total = r, hashlib.sha256(), 0, prog, total

    def read(self, k=-1):
        b = self.r.read(k)
        self.h.update(b)
        self.n += len(b)
        if self.total and self.prog:
            self.prog(min(1.0, self.n / self.total))
        return b


def core_remote_version(channel, gh, kind="mihomo"):
    url = f"{SMART_REPO}/download/Prerelease-Alpha/version.txt" if kind == "smart" else f"{CORE_REPO}/{CORE_CHANNELS[channel]}/version.txt"
    with open_url(url, gh) as r:
        v = r.read(200).decode(errors="ignore").strip()
    if not re.match(r"^[\\w.\\-]+$", v):
        raise IOError("获取到的版本号无效：" + v[:40])
    return v


def core_check(channel, gh, kind="mihomo"):
    arch, b = core_arch(), mh_bin(kind)
    if kind == "smart":
        channel = "alpha"
    info = {"current": bin_version(b) or "未安装", "arch": arch or os.uname().machine, "channel": channel,
            "backup": bin_version(b + ".bak"), "remote": "", "core": kind}
    try:
        info["remote"] = core_remote_version(channel, gh, kind)
    except Exception as e:
        info["message"] = "获取最新版本失败：" + str(e)
        return False, info
    info["latest"] = info["remote"] == info["current"]
    info["message"] = "已是最新" if info["latest"] else f"可更新到 {info['remote']}"
    return True, info


QUIET = threading.local()  # 后台预下载内核时置 QUIET.on：不往「内核更新 / 切换」的进度和日志里写


def _cu(stage, pct=None, msg=None):
    if getattr(QUIET, "on", False):
        return
    CORE_UPD["stage"] = stage
    if pct is not None:
        CORE_UPD["pct"] = pct
    CORE_UPD["log"].appendleft(f"{time.strftime('%H:%M:%S')} {msg or stage}")


MANUAL_NOTE = "核心已手动停止，没有替你启动；下次启动核心时使用新程序"


def _core_swap_and_restart(src, label, kind="mihomo"):
    """把 src 换成正式核心并重启；30 秒内控制接口没起来就自动换回原来的核心。
    返回 (成功, 提示)：成功时提示非空表示核心被手动停止、只替换了程序"""
    B = mh_bin(kind)
    bak = B + ".bak"
    had_old = os.path.isfile(B)
    if had_old and src != bak:
        shutil.copy2(B, bak)
    if src == bak:  # 回滚：当前核心与备份互换，方便再“回滚”回来
        tmp = B + ".swap"
        shutil.copy2(B, tmp)
        os.replace(bak, B)
        os.replace(tmp, bak)
    else:
        os.replace(src, B)
    if active_core() != kind:  # 这个内核没在运行：只替换程序，切换过去时生效
        _cu("完成", 95, f"已替换为 {label}（当前内核是 {CORES[active_core()]}，切到 {CORES[kind]} 时生效）")
        return True, ""
    if WD["manual_stop"]:  # 用户手动停止了核心：只替换程序，不替用户把核心拉起来（与定时重启、保存设置一致）
        _cu("完成", 95, f"已替换为 {label}（{MANUAL_NOTE}）")
        return True, MANUAL_NOTE
    _cu("重启核心", 90, f"已替换为 {label}，正在重启核心")
    svc_restart(timeout=90)
    for _ in range(30):
        time.sleep(1)
        if core_alive():
            return True, ""
    log = core_fail_log()  # 回滚重启会清空核心日志，先把新核心的报错存下来
    if had_old and src != bak:  # 新核心起不来 → 自动恢复
        _cu("自动回滚", 95, "新核心 30 秒内未响应，正在恢复原核心")
        copy_replace(bak, B)
        svc_restart(timeout=90)
        return False, "新核心启动失败，已自动恢复为原核心。" + ("报错：" + log if log else "完整日志见 " + CORE_FAIL_LOG)
    return False, "核心重启后 30 秒内未响应。" + ("报错：" + log if log else "完整日志见 " + CORE_FAIL_LOG)


CORE_FAIL_LOG = os.path.join(os.path.dirname(MIHOMO_LOG), "shunt-core-fail.log")


def core_fail_log(n=3):
    """新核心启动失败、准备回滚时调用：必须在回滚重启之前取日志（init.d 的 start_pre 会清空核心日志，以前回滚后再读只剩旧核心的启动日志）。
    把核心日志末尾 60 行另存到 shunt-core-fail.log，报错行记入面板日志 / 诊断，返回最后 n 行报错（去掉颜色码）供提示用"""
    raw = sh(f"tail -n 60 {shlex.quote(MIHOMO_LOG)}")[1]
    raw = re.sub(r"\\x1b\\[[0-9;]*m", "", raw)
    try:
        with open(CORE_FAIL_LOG, "w") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " 核心启动失败时的日志\\n" + raw)
    except OSError:
        pass
    errs = [l.strip() for l in raw.splitlines() if re.search(r"FATAL|ERROR|panic|level=(?:error|fatal)", l)]
    msg = "\\n".join(errs[-n:])[-400:]
    log_err("核心启动失败", msg or raw.strip()[-300:] or "核心日志为空（进程可能没能启动）")
    return msg


def copy_replace(src, dst):
    """复制到临时文件再改名替换：dst 可能是正在运行（卡住 / 反复重启）的程序，直接覆盖写会因 ETXTBSY（Text file busy）失败，
    回滚恰恰在新程序起不来时发生，以前这一步失败会让回滚落空"""
    tmp = dst + ".rb"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def mh_fetch(kind, channel, gh, prog=None):
    """下载 mihomo / mihomo Smart 到 <程序>.new 并校验能运行；返回 (临时文件, 版本号)"""
    import gzip
    B = mh_bin(kind)
    arch = core_arch()
    if not arch:
        raise ValueError(f"不支持的 CPU 架构：{os.uname().machine}")
    ver = core_remote_version(channel, gh, kind)
    free = shutil.disk_usage(os.path.dirname(B)).free
    if free < 80 * 2**20:
        raise ValueError(f"{os.path.dirname(B)} 剩余空间不足（{free // 2**20} MB，至少需要 80 MB）")
    tag = "Prerelease-Alpha" if kind == "smart" or channel == "alpha" else ver
    url = f"{SMART_REPO if kind == 'smart' else CORE_REPO}/download/{tag}/mihomo-linux-{arch}-{ver}.gz"
    name = url.rsplit('/', 1)[-1]
    digest = release_digest(SMART_REPO if kind == "smart" else CORE_REPO, tag, name)
    if not digest and gh:
        _cu("下载", None, "拿不到 GitHub 校验值，不使用加速镜像（避免下载到被篡改的文件）")
        gh = ""
    _cu("下载", None, f"下载 {name}")
    # 后台预下载（core_cache，QUIET.on）与前台「内核更新 / 切换」可能同时下载同一个内核：以前都写 <程序>.new，
    # 两边交错写坏同一个文件，一方失败时的清理还会删掉另一方正在写的文件。后台用自己的临时文件
    tmp = B + (".prefetch" if getattr(QUIET, "on", False) else ".new")
    try:
        with open_url(url, gh, timeout=30) as r:
            hr = HashReader(r, prog, int(r.headers.get("Content-Length") or 0))
            with gzip.GzipFile(fileobj=hr) as gz, open(tmp, "wb") as f:
                size = 0
                while True:
                    chunk = gz.read(1 << 16)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 200 * 2**20:
                        raise ValueError("解压后的文件异常过大，已中止")
                    f.write(chunk)
            while hr.read(1 << 16):  # gzip 尾部之后若还有数据也计入校验
                pass
        if digest and hr.h.hexdigest() != digest:
            raise ValueError("内核文件 SHA256 与 GitHub 不一致（可能被加速镜像篡改），未做任何替换")
        _cu("下载", None, "SHA256 校验通过" if digest else "未能获取 GitHub 校验值，已改为直连 / 代理下载")
        os.chmod(tmp, 0o755)
        nv = bin_version(tmp)
        if not nv:
            raise ValueError("新核心无法运行（架构不匹配或文件损坏），未做任何替换")
        return tmp, nv
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def core_update_job(channel, gh, force=False, kind="mihomo"):
    B = mh_bin(kind)
    tmp = B + ".new"
    try:
        _cu("获取版本", 2, "正在获取最新版本号")
        ver = core_remote_version(channel, gh, kind)
        cur = bin_version(B)
        if ver == cur and not force:
            CORE_UPD.update(ok=True, message=f"已是最新版本 {cur}，无需更新")
            return
        CORE_UPD["pct"] = 5
        tmp, nv = mh_fetch(kind, channel, gh, lambda pc: CORE_UPD.update(pct=5 + int(70 * pc)))
        _cu("校验", 84, f"新核心 {nv}，正在用它检查配置")
        if active_core() == kind:  # 正在用：拿当前配置校验；没在用：按它的格式生成一份再校验
            cfg = os.path.join(CONF_DIR, "config.yaml")
        else:
            cfg = write_config(dict(load(), core=kind), os.path.join(CONF_DIR, "config.yaml.chk"))
        code, out = sh(f"'{tmp}' -t -d '{CONF_DIR}' -f '{cfg}'", timeout=90)
        if cfg.endswith(".chk"):
            os.remove(cfg)
        if code != 0:
            raise ValueError("新核心不兼容当前配置，未做任何替换：" + out[-300:])
        ok, err = _core_swap_and_restart(tmp, nv, kind)
        if not ok:
            raise ValueError(err)
        CORE_UPD.update(ok=True, message=f"{CORES[kind]} 已从 {cur or '无'} 更新到 {nv}" + ("，旧版本已备份，可一键回滚" if cur else "")
                        + (f"（{err}）" if err else ""))
        tg = load()
        if tg.get("tg_token") and tg.get("tg_chat"):
            notify(f"⬆️ {CORES[kind]} 内核已更新：{cur or '无'} → {nv}")
    except Exception as e:
        CORE_UPD.update(ok=False, message=str(e))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
        CORE_UPD["pct"] = 100
        _cu("完成" if CORE_UPD["ok"] else "失败", 100, CORE_UPD["message"])
        CORE_UPD["busy"] = False


def lgbm_fetch(gh):
    """下载 LightGBM 模型到 /etc/mihomo/Model.bin（约 9 MB，Smart 组 uselightgbm 用）"""
    tmp = LGBM_FILE + ".new"
    try:
        digest = release_digest(SMART_REPO, "LightGBM-Model", "Model.bin")
        with open_url(LGBM_URL, gh if digest else "", timeout=60) as r0, open(tmp, "wb") as f:
            r = HashReader(r0)
            n = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                n += len(chunk)
                if n > 64 * 2**20:
                    raise ValueError("模型文件异常过大，已中止")
                f.write(chunk)
        if n < 1 << 20:
            raise ValueError("下载的模型文件不完整")
        if digest and r.h.hexdigest() != digest:
            raise ValueError("模型文件 SHA256 与 GitHub 不一致")
        os.replace(tmp, LGBM_FILE)
        return n
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def lgbm_fetch_reload():
    try:
        lgbm_fetch(load().get("gh_proxy") or "")
        if active_core() == "smart":
            reload_core()
    except Exception as e:
        print(f"LightGBM 模型下载失败：{e}", file=sys.stderr)


def core_rollback_job(kind="mihomo"):
    try:
        B = mh_bin(kind)
        old = bin_version(B + ".bak")
        if not old:
            raise ValueError("没有可回滚的内核备份")
        cur = bin_version(B)
        ok, err = _core_swap_and_restart(B + ".bak", old, kind)
        if not ok:
            raise ValueError(err)
        CORE_UPD.update(ok=True, message=f"已回滚：{cur} → {old}（{cur} 保留为备份）" + (f"；{err}" if err else ""))
    except Exception as e:
        CORE_UPD.update(ok=False, message=str(e))
    finally:
        CORE_UPD["pct"] = 100
        _cu("完成" if CORE_UPD["ok"] else "失败", 100, CORE_UPD["message"])
        CORE_UPD["busy"] = False


def core_job_start(fn, *args):
    if not upd_begin("core"):  # 锁内检查并置忙：与面板更新、其他内核任务互斥
        return False, upd_busy_msg()
    CORE_UPD.update(stage="开始", pct=0, ok=None, message="", t=int(time.time()))
    CORE_UPD["log"].clear()
    threading.Thread(target=fn, args=args, daemon=True).start()
    return True, "已开始"


def core_upd_status():
    return {k: (list(v) if isinstance(v, deque) else v) for k, v in CORE_UPD.items()}

'''
_SRC['shunt.core_cache'] = '''"""内核缓存：几个 sing-box 版本（官方版 / reF1nd 正式版 / reF1nd 预览版 / MiChongs）各存一份，mihomo / Smart 也提前装好。
切换内核时直接用缓存里的程序，不用再下载；后台定时检查，只有上游发了新版本才重新下载。
正在运行的那个程序（SB_BIN、当前内核的 mihomo）不会被后台替换，在「已下载的内核」里它那一行点「更新」手动更新。"""
import os, re, shutil, threading, time

from .base import CORE_UPD, MIHOMO_BIN, SMART_BIN
from .data import load, update
from .errlog import log_err
from .mihomo import mh_bin
from .sb_common import FORK_ARCHES, SB_BIN, active_core
from .sb_config import sb_version
from .core_update import QUIET, bin_version, core_remote_version, copy_replace, mh_fetch
from .sb_switch import sb_arch, sb_install, sb_remote_version

CACHE_DIR = os.environ.get("SB_CACHE", "/usr/local/lib/shunt-cores")
# 变体 → (名称, 下载通道)
SB_VARIANTS = {
    "official": ("sing-box 官方版", "stable"),
    "ref": ("sing-box reF1nd", "fork"),
    "ref_alpha": ("sing-box reF1nd 预览版", "fork_alpha"),
    "mc": ("sing-box MiChongs", "fork_mc"),
}
PREFETCH_EVERY = 12 * 3600  # 后台检查间隔
PREFETCH_FIRST = 180        # 面板启动后多久做第一次检查（等核心和网络起来）
MIN_FREE = 150 * 2**20      # 剩余空间少于这么多时不做后台预下载
CACHE_LOCK = threading.Lock()  # 同一时间只有一个任务写缓存目录
STATE = {"busy": False, "last": 0, "log": [], "remote": {}, "checked": 0}
_PRE = re.compile(r"alpha|beta|rc", re.I)


def variants():
    """当前架构可用的 sing-box 变体：reF1nd 只发 amd64 / arm64，其他架构只有官方版和 MiChongs"""
    a = sb_arch()
    return [k for k in SB_VARIANTS if a in FORK_ARCHES or k not in ("ref", "ref_alpha")]


def variant_of(v):
    """按版本号认出是哪个 sing-box：-xiaobaf 是 MiChongs，-reF1nd 是 reF1nd（带 alpha / beta / rc 为预览版），其余为官方版"""
    v = v or ""
    if not v:
        return ""
    if "xiaobaf" in v:
        return "mc"
    if "reF1nd" in v:
        return "ref_alpha" if _PRE.search(v) else "ref"
    return "official"


def cache_path(var):
    return os.path.join(CACHE_DIR, "sing-box-" + var)


def current_variant():
    return variant_of(sb_version())


def sb_channel(var):
    """变体的下载通道；官方版缓存里本来是预览版时继续跟预览版，不被后台换成正式版"""
    if var == "official" and _PRE.search(sb_version(cache_path(var)) or ""):
        return "alpha"
    return SB_VARIANTS[var][1]


def cached_versions():
    return {k: sb_version(cache_path(k)) for k in variants()}


def remember_current():
    """把正在用的 sing-box 存进对应变体的缓存（换到别的变体后还能秒切回来）"""
    v = sb_version()
    var = variant_of(v)
    if not var or sb_version(cache_path(var)) == v:
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        copy_replace(SB_BIN, cache_path(var))
    except OSError as e:
        log_err("缓存当前 sing-box", e)


def ensure_variant(var, gh, refresh=False):
    """确保缓存里有这个变体；refresh=True 时检查上游，有新版本才重新下载。返回 (版本号, 是否下载了新程序)"""
    if var not in variants():
        raise ValueError("当前 CPU 架构没有这个 sing-box 版本")
    p = cache_path(var)
    with CACHE_LOCK:
        cur = sb_version(p)
        if cur and not refresh:
            return cur, False
        return sb_install(sb_channel(var) if cur else SB_VARIANTS[var][1], gh, dest=p)


def activate_variant(var):
    """把缓存里的变体换成正在用的 sing-box（原程序备份为 .bak，失败可回滚）。返回是否真的换了"""
    if variant_of(sb_version()) == var and sb_version():
        return False
    remember_current()
    src = cache_path(var)
    if not sb_version(src):
        raise ValueError(f"缓存里没有 {SB_VARIANTS[var][0]}")
    with CACHE_LOCK:
        if os.path.isfile(SB_BIN):
            shutil.copy2(SB_BIN, SB_BIN + ".bak")
        copy_replace(src, SB_BIN)
    return True


def _mh_channel(kind):
    if kind == "smart":
        return "alpha"
    return "alpha" if "alpha" in (bin_version(MIHOMO_BIN) or "") else "stable"


def check_all(gh):
    """查一遍各内核上游的最新版本号（不下载）。返回 {内核: 最新版本或错误}"""
    out = {}
    QUIET.on = True
    try:
        for var in variants():
            try:
                out["sb_" + var] = sb_remote_version(sb_channel(var), gh)
            except Exception as e:
                out["sb_" + var] = "✗ " + str(e)[:120]
        for kind in ("mihomo", "smart"):
            try:
                out[kind] = core_remote_version(_mh_channel(kind), gh, kind)
            except Exception as e:
                out[kind] = "✗ " + str(e)[:120]
    finally:
        QUIET.on = False
    STATE.update(remote=out, checked=int(time.time()))
    return out


def _mh_refresh(kind, gh, force=False):
    """mihomo / Smart：没装就装；装了且不是正在运行的内核时，有新版本才替换。返回提示文本或空串"""
    B = mh_bin(kind)
    cur = bin_version(B)
    if cur and active_core() == kind:
        return ""  # 正在运行的内核不在后台替换，用「内核更新」手动更新
    ver = core_remote_version(_mh_channel(kind), gh, kind)
    if cur == ver and not force:
        return ""
    tmp, nv = mh_fetch(kind, _mh_channel(kind), gh)
    os.replace(tmp, B)
    return f"{'mihomo Smart' if kind == 'smart' else 'mihomo'} {cur or '未安装'} → {nv}"


def prefetch(gh, only=None, force=False):
    """下载 / 更新缓存。only 为 None 时处理全部内核，否则只处理 only 里的（sb_<变体> / mihomo / smart）。返回 (成功, 失败) 两个列表"""
    ok, bad = [], []
    items = ["sb_" + v for v in variants()] + ["mihomo", "smart"]
    for it in items:
        if only and it not in only:
            continue
        try:
            if it.startswith("sb_"):
                var = it[3:]
                old = sb_version(cache_path(var))
                if not only and not old:
                    continue  # 只更新用过（已下载）的版本，没用过的不下载，切换到它时再下载
                if var == current_variant():
                    continue  # 正在使用的版本由它那一行的「更新」替换（校验 + 重启），后台不动
                nv, changed = ensure_variant(var, gh, refresh=True) if not force else (sb_install(sb_channel(var), gh, force=True, dest=cache_path(var))[0], True)
                if changed:
                    ok.append(f"{SB_VARIANTS[var][0]} {old or '未下载'} → {nv}")
            else:
                if not only and not bin_version(mh_bin(it)):
                    continue  # 没装过的 mihomo / Smart 不在后台安装，切换到它时再下载
                msg = _mh_refresh(it, gh, force)
                if msg:
                    ok.append(msg)
        except Exception as e:
            bad.append(f"{it}: {str(e)[:200]}")
    return ok, bad


def prefetch_enabled(d=None):
    return (d or load()).get("core_prefetch", True) is not False


_JOB_LOCK = threading.Lock()


def prefetch_job(gh, only=None, force=False, quiet=True):
    """后台线程里执行预下载；quiet=True 不写「内核更新」进度（定时任务），手动触发时写 STATE['log']"""
    with _JOB_LOCK:  # 定时任务与手动按钮可能同时起线程：检查与置位要原子，否则两个线程同时下载、互相覆盖同一个缓存文件
        if STATE["busy"]:
            return False
        STATE.update(busy=True, log=[])
    QUIET.on = True
    try:
        ok, bad = prefetch(gh, only, force)
        STATE["log"] = ["✓ " + x for x in ok] + ["✗ " + x for x in bad] or ["所有内核都已是最新"]
        if ok or bad:
            print(time.strftime("%F %T"), "[core-cache]", "；".join(STATE["log"])[:500], flush=True)
    except Exception as e:
        STATE["log"] = ["✗ " + str(e)[:200]]
    finally:
        QUIET.on = False
        STATE.update(busy=False, last=int(time.time()))
    return True


def prefetch_start(gh, only=None, force=False):
    if STATE["busy"]:
        return False, "正在下载内核，请稍候"
    if CORE_UPD["busy"]:
        return False, "内核正在更新或切换，请稍后再试"
    threading.Thread(target=prefetch_job, args=(gh, only, force), daemon=True).start()
    return True, "已开始在后台下载"


def prefetch_due():
    """定时任务调用：开启了自动下载、到了间隔、没有其他内核任务在跑、空间够时才执行"""
    if STATE["busy"] or CORE_UPD["busy"] or not prefetch_enabled():
        return False
    first = STATE["last"] == 0
    if first and time.time() - START < PREFETCH_FIRST:
        return False
    if not first and time.time() - STATE["last"] < PREFETCH_EVERY:
        return False
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        if shutil.disk_usage(CACHE_DIR).free < MIN_FREE:
            STATE.update(last=int(time.time()), log=[f"✗ {CACHE_DIR} 剩余空间不足 150 MB，跳过自动下载"])
            return False
    except OSError:
        return False
    return True


def status():
    vs = cached_versions()
    cv = current_variant()
    if cv in vs:
        vs[cv] = sb_version()  # 正在使用的那个显示实际运行的程序版本
    return {"dir": CACHE_DIR, "enabled": prefetch_enabled(), "busy": STATE["busy"], "last": STATE["last"], "log": STATE["log"],
            "checked": STATE["checked"], "remote": STATE["remote"],
            "usage": usage(),
            "items": [{"id": "sb_" + k, "variant": k, "label": SB_VARIANTS[k][0], "version": vs.get(k, "")} for k in variants()]
            + [{"id": k, "label": "mihomo Smart" if k == "smart" else "mihomo", "version": bin_version(mh_bin(k))} for k in ("mihomo", "smart")]}


def _fsize(p):
    try:
        return os.path.getsize(p) if os.path.isfile(p) else 0
    except OSError:
        return 0


def _bak_files():
    return [b + ".bak" for b in BAK_BINS]


def _extra_files():
    """清理时可删的其他程序：不在使用的 mihomo Smart（切换到 Smart 时会重新下载）"""
    return [] if active_core() == "smart" else [SMART_BIN]


def usage():
    """内核相关文件占用（字节）：cache 缓存的 sing-box 各版本、bak 内核更新留下的旧版本备份、extra 不在使用的其他内核"""
    cache = sum(_fsize(cache_path(k)) for k in SB_VARIANTS)
    bak = sum(_fsize(p) for p in _bak_files())
    extra = sum(_fsize(p) for p in _extra_files())
    return {"cache": cache, "bak": bak, "extra": extra, "total": cache + bak + extra}


def clean():
    """只保留正在用的最新内核：删除缓存里的 sing-box 各版本（含当前版本的副本）、各内核的 .bak 旧版本、不在使用的其他内核。
    返回 (成功, 提示)。删除后切换到别的版本会重新下载，内核更新失败也不能再一键回滚到旧版本"""
    with _JOB_LOCK:
        if STATE["busy"] or CORE_UPD["busy"]:
            return False, "内核正在下载、更新或切换，请稍后再试"
        STATE["busy"] = True
    freed, n, bad = 0, 0, []
    try:
        with CACHE_LOCK:
            for p in [cache_path(k) for k in SB_VARIANTS] + _bak_files() + _extra_files():
                sz = _fsize(p)
                if not os.path.isfile(p):
                    continue
                try:
                    os.remove(p)
                    freed += sz
                    n += 1
                except OSError as e:
                    bad.append(f"{p}: {e}")
    finally:
        STATE["busy"] = False
    if bad:
        return False, f"已删除 {n} 个文件、释放 {freed / 2**20:.1f} MB；以下文件删除失败：" + "；".join(bad)[:300]
    return True, f"已删除 {n} 个文件、释放 {freed / 2**20:.1f} MB，只保留正在用的内核" if n else "没有可清理的旧内核"


def set_enabled(on):
    update(lambda x: x.__setitem__("core_prefetch", bool(on)))


BAK_BINS = (SB_BIN, MIHOMO_BIN, SMART_BIN)
START = time.time()
'''
_SRC['shunt.sysopt'] = '''"""Alpine 系统优化"""
import http.client, json, os, re, ssl, subprocess, threading, time, urllib.request, urllib.error
from collections import deque
from urllib.parse import urlsplit

from .base import CORE_PREP, CORE_SVC, MIHOMO_BIN, MIXED, PANEL_DIR, SMART_BIN, SVC, TESTS, UA, SafeRedirectHandler, dl_check, wd_hold
from .data import load, update
PROC_SYS = os.environ.get("PANEL_PROC_SYS", "/proc/sys")
SYS_ROOT = os.environ.get("PANEL_SYS_ROOT", "/sys")
ETC = os.environ.get("PANEL_ETC", "/etc")
OPT_SYSCTL = os.path.join(ETC, "sysctl.d", "98-shunt-optimize.conf")
OPT_LOCALD = os.path.join(ETC, "local.d", "shunt-optimize.start")
# OpenRC 只读 /etc/conf.d/<服务名>：v7.7.6 核心服务改名 shunt-core 后，旧的 conf.d/mihomo 不再生效，
# 「系统优化 → 文件句柄」写的 rc_ulimit 对核心失效，sing-box 停在默认 1024 个句柄，连接一多控制接口就卡死
OPT_CONFD = os.path.join(ETC, "conf.d", CORE_SVC)
OPT_CONFD_OLD = os.path.join(ETC, "conf.d", "mihomo")
OPT_LOCK = threading.Lock()


def mem_mb():
    try:
        with open("/proc/meminfo") as f:
            return int(re.search(r"MemTotal:\\s+(\\d+)", f.read()).group(1)) // 1024
    except Exception:
        return 512


def rsys(key):
    try:
        with open(os.path.join(PROC_SYS, key.replace(".", "/"))) as f:
            return " ".join(f.read().split())
    except OSError:
        return None


def wsys(key, val):
    try:
        with open(os.path.join(PROC_SYS, key.replace(".", "/")), "w") as f:
            f.write(str(val))
        return ""
    except OSError as e:
        return f"{key}: {e.strerror or e}"


def rfile(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def wfile(path, val):
    try:
        with open(path, "w") as f:
            f.write(str(val))
        return True
    except OSError:
        return False


def net_ifaces():
    """物理网卡（有 device 链接，排除 lo / 虚拟网卡 / TUN）"""
    base = os.path.join(SYS_ROOT, "class", "net")
    try:
        return sorted(n for n in os.listdir(base) if os.path.exists(os.path.join(base, n, "device")))
    except OSError:
        return []


def cpu_govs():
    return sorted(glob_paths(os.path.join(SYS_ROOT, "devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor")))


_CT = {}


def in_container():
    """LXC / Docker 等容器：内核由宿主机共用，大部分全局参数、/sys、系统时间都无权修改"""
    if "v" not in _CT:
        v = rfile("/run/systemd/container")
        if not v and (os.path.exists("/dev/.lxc-boot-id") or " lxcfs " in rfile("/proc/self/mountinfo")
                      or re.search(r"/lxc", rfile("/proc/1/cgroup"))):
            v = "lxc"
        if not v and (os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
                      or re.search(r"/(docker|kubepods)", rfile("/proc/1/cgroup"))):
            v = "docker"
        _CT["v"] = v
    return _CT["v"]


def path_writable(path):
    """把当前值原样写回，判断是否有权修改（容器里 /proc/sys、/sys 多数只读）"""
    try:
        with open(path) as f:
            cur = f.read().strip()
        with open(path, "w") as f:
            f.write(cur)
        return True
    except OSError:
        return False


_SYSW = {}


def sys_writable(key):
    if key not in _SYSW:
        path = os.path.join(PROC_SYS, key.replace(".", "/"))
        if not os.path.exists(path):
            return False  # 模块未加载时参数不存在，不缓存，加载后再判断
        _SYSW[key] = path_writable(path)
    return _SYSW[key]


def nofile_target():
    """mihomo 文件句柄目标值：不能超过本机（容器）的硬上限，否则 rc_ulimit 会让核心启动失败"""
    try:
        import resource
        hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    except Exception:
        hard = -1
    return 1048576 if hard < 0 or hard >= 1048576 else hard


def glob_paths(pat):
    import glob
    return glob.glob(pat)


def opt_items():
    """按内存 / CPU 计算推荐值。每项：id、名称、说明、sysctl 目标值、是否支持"""
    mem, ncpu = mem_mb(), os.cpu_count() or 1
    buf = 16 << 20 if mem >= 1024 else 8 << 20  # quic-go（Hysteria2 / TUIC）建议 UDP 缓冲 ≥7.5MB
    ct = max(32768, min(262144, mem * 256))
    bbr_ok = "bbr" in (rsys("net.ipv4.tcp_available_congestion_control") or "") or os.path.exists(
        f"/lib/modules/{os.uname().release}/kernel/net/ipv4/tcp_bbr.ko") or bool(
        glob_paths(f"/lib/modules/{os.uname().release}/kernel/net/ipv4/tcp_bbr.ko*"))
    govs = cpu_govs()
    gov_ok = bool(govs) and "performance" in rfile(os.path.join(os.path.dirname(govs[0]), "scaling_available_governors")) if govs else False
    ct_name = in_container()
    if ct_name:  # 容器里没有自己的内核：模块要在宿主机上加载
        bbr_ok = "bbr" in (rsys("net.ipv4.tcp_available_congestion_control") or "")
    gov_ok = gov_ok and path_writable(govs[0])
    nof = nofile_target()
    rpsq = [q for q in glob_paths(os.path.join(SYS_ROOT, "class/net/*/queues/rx-*/rps_cpus"))
            if q.split("/class/net/")[1].split("/")[0] in net_ifaces()]
    rps_ok = ncpu > 1 and bool(rpsq) and path_writable(rpsq[0])
    host = "宿主机" + ("（PVE / LXC）" if ct_name == "lxc" else "")
    items = [
        {"id": "bbr", "name": "BBR 拥塞控制", "rec": True, "ok": bbr_ok,
         "why": "" if bbr_ok else f"宿主机内核未加载 tcp_bbr，请在{host}上执行 modprobe tcp_bbr" if ct_name else "内核没有 tcp_bbr 模块",
         "desc": "TCP 改用 BBR + fq 队列，丢包或长距离线路下的下载速度明显更高",
         "sysctl": {"net.core.default_qdisc": "fq", "net.ipv4.tcp_congestion_control": "bbr"}},
        {"id": "buf", "name": "网络缓冲区", "rec": True, "ok": True,
         "desc": f"加大 TCP/UDP 收发缓冲到 {buf >> 20}MB（按 {mem}MB 内存计算），提升大带宽和 Hysteria2 / TUIC 等 QUIC 协议速度",
         "sysctl": {"net.core.rmem_max": buf, "net.core.wmem_max": buf, "net.core.rmem_default": 262144,
                    "net.core.wmem_default": 262144, "net.ipv4.tcp_rmem": f"4096 131072 {buf}",
                    "net.ipv4.tcp_wmem": f"4096 65536 {buf}", "net.ipv4.udp_rmem_min": 16384, "net.ipv4.udp_wmem_min": 16384,
                    "net.core.netdev_max_backlog": 16384, "net.core.somaxconn": 8192, "net.ipv4.tcp_max_syn_backlog": 8192}},
        {"id": "tcp", "name": "TCP 连接优化", "rec": True, "ok": True,
         "desc": "开启 TCP Fast Open 和 MTU 探测（避免某些线路卡在握手），空闲后不降速，扩大可用端口，快速回收 TIME_WAIT",
         "sysctl": {"net.ipv4.tcp_fastopen": 3, "net.ipv4.tcp_mtu_probing": 1, "net.ipv4.tcp_slow_start_after_idle": 0,
                    "net.ipv4.tcp_tw_reuse": 1, "net.ipv4.tcp_fin_timeout": 15, "net.ipv4.ip_local_port_range": "10000 65535",
                    "net.ipv4.tcp_keepalive_time": 600, "net.ipv4.tcp_notsent_lowat": 131072}},
        {"id": "conntrack", "name": "连接跟踪表", "rec": True, "ok": rsys("net.netfilter.nf_conntrack_max") is not None,
         "why": "nf_conntrack 未加载", "desc": f"连接数上限提高到 {ct}，已建立连接的超时从 5 天缩短到 2 小时，避免设备多时连接表被占满导致断流",
         "sysctl": {"net.netfilter.nf_conntrack_max": ct, "net.netfilter.nf_conntrack_tcp_timeout_established": 7200,
                    "net.netfilter.nf_conntrack_udp_timeout": 60, "net.netfilter.nf_conntrack_udp_timeout_stream": 180}},
        {"id": "fd", "name": "文件句柄上限", "rec": True, "ok": nof > 1024, "why": "本机文件句柄硬上限只有 1024，需在宿主机上调高",
         "desc": f"核心可同时打开的连接数固定为 {nof}，并提高系统总句柄数，设备多或 BT 下载时不再报 too many open files（下次重启核心生效）。"
                 "未开启时启动脚本也会按本机硬上限给核心兜底，不会停在 1024",
         "sysctl": {"fs.file-max": 1048576, "fs.nr_open": 1048576}},
        {"id": "rps", "name": "多核网络分流 (RPS)", "rec": ncpu > 1, "ok": rps_ok,
         "why": "单核 CPU 无需开启" if ncpu <= 1 else "未找到物理网卡" if not rpsq else f"网卡参数只读，请在{host}上设置",
         "desc": f"把网卡收包分摊到全部 {ncpu} 个 CPU 核心，单队列网卡（树莓派、多数 ARM 盒子、虚拟机）满速时不再单核 100%",
         "sysctl": {"net.core.rps_sock_flow_entries": 32768}},
        {"id": "gov", "name": "CPU 性能模式", "rec": False, "ok": gov_ok,
         "why": f"CPU 调频由{host}控制" if govs and ct_name else "CPU 不支持调频或没有 performance 模式",
         "desc": "CPU 一直跑在最高频率，延迟更低更稳定；功耗和发热会增加，散热差的设备慎开", "sysctl": {}},
        {"id": "ntp", "name": "时间同步", "rec": True, "ok": not ct_name, "why": "容器和宿主机共用系统时钟，请在宿主机上校时",
         "desc": "安装并开机启动 chrony 自动校时。系统时间不准会导致 TLS 握手失败、VMess / Trojan 等节点全部不可用",
         "sysctl": {}},
    ]
    for it in items:  # 只保留有权修改的参数；容器里 net.core.*、fs.* 等全局参数由宿主机决定
        allk = it["sysctl"]
        if not allk or not it["ok"]:
            continue
        w = {k: v for k, v in allk.items() if sys_writable(k)}
        it["skipped"] = [k for k in allk if k not in w]
        it["sysctl"] = w
        if not w and it["id"] != "fd":
            it["ok"] = False
            it["why"] = f"这些内核参数在容器里只读，由{host}决定" if ct_name else "内核参数只读"
        elif it["skipped"]:
            it["part"] = f"有 {len(it['skipped'])}/{len(allk)} 个参数只读（{'由' + host + '决定' if ct_name else '内核不允许修改'}），已跳过"
    return items


def ntp_running():
    return sh("pidof chronyd || pidof ntpd")[0] == 0


def opt_status():
    d = load()
    on = set(d.get("sysopt") or [])
    out = []
    for it in opt_items():
        cur, hit = {}, 0
        for k, v in it["sysctl"].items():
            c = rsys(k)
            cur[k] = c
            if c is not None and c == " ".join(str(v).split()):
                hit += 1
        live = bool(it["sysctl"]) and hit == len(it["sysctl"])
        if it["id"] == "fd":
            live = (live or not it["sysctl"]) and f'rc_ulimit="-n {nofile_target()}"' in rfile(OPT_CONFD)
        elif it["id"] == "rps":
            live = live and it["ok"] and all(int(rfile(q, "0").replace(",", "") or "0", 16) != 0
                                            for q in glob_paths(os.path.join(SYS_ROOT, "class/net/*/queues/rx-*/rps_cpus"))
                                            if q.split("/class/net/")[1].split("/")[0] in net_ifaces())
        elif it["id"] == "gov":
            live = it["ok"] and all(rfile(g) == "performance" for g in cpu_govs())
        elif it["id"] == "ntp":
            live = ntp_running()
        show = {}
        if it["id"] == "bbr":
            show = {"当前": rsys("net.ipv4.tcp_congestion_control") or "-", "队列": rsys("net.core.default_qdisc") or "-"}
        elif it["id"] == "buf":
            show = {"当前上限": f"{int(rsys('net.core.rmem_max') or 0) >> 10} KB"}
        elif it["id"] == "conntrack":
            show = {"已用 / 上限": f"{rsys('net.netfilter.nf_conntrack_count') or '-'} / {rsys('net.netfilter.nf_conntrack_max') or '-'}"}
        elif it["id"] == "gov" and it["ok"]:
            show = {"当前": rfile(cpu_govs()[0])}
        out.append({k: it[k] for k in ("id", "name", "desc", "rec", "ok")} | {
            "why": "" if it["ok"] else it.get("why", ""), "enabled": it["id"] in on, "active": live, "show": show,
            "part": it.get("part", "") if it["ok"] else ""})
    return {"items": out, "mem": mem_mb(), "cpu": os.cpu_count() or 1, "kernel": os.uname().release, "container": in_container()}


def opt_migrate_confd(on=None):
    """服务改名后把旧 conf.d/mihomo 里的 rc_ulimit 搬到 conf.d/shunt-core（已迁移的设备每次面板启动时补一次）。
    只在「系统优化」里文件句柄开着（data.sysopt 含 fd）时才搬；旧文件里的 rc_ulimit 行处理完就删掉（核心服务已不读它）。
    以前不删也不看开关：用户关掉文件句柄后，面板一重启又从旧文件搬回来，状态页显示「已生效」"""
    if OPT_CONFD == OPT_CONFD_OLD:
        return False
    old = rfile(OPT_CONFD_OLD)
    m = re.search(r'^rc_ulimit="[^"\\n]*"', old, re.M)
    if not m:
        return False
    if on is None:
        on = set(load().get("sysopt") or [])
    cur, moved = rfile(OPT_CONFD), False
    if "fd" in on and not re.search(r"^rc_ulimit=", cur, re.M):
        os.makedirs(os.path.dirname(OPT_CONFD), exist_ok=True)
        if not wfile(OPT_CONFD, (cur + "\\n" if cur else "") + m.group(0) + "\\n"):
            return False  # 没搬成：旧文件先留着，下次再试
        moved = True
    rest = "\\n".join(ln for ln in old.splitlines() if not ln.startswith("rc_ulimit=")).strip()
    wfile(OPT_CONFD_OLD, rest + "\\n" if rest else "")
    return moved


def opt_fix_ulimit():
    """旧版写死 rc_ulimit="-n 1048576"：在硬上限更低的 LXC 容器里会让核心启动失败，按实际上限改小"""
    txt = rfile(OPT_CONFD)
    m = re.search(r'^rc_ulimit="-n (\\d+)"', txt, re.M)
    if m and int(m.group(1)) > nofile_target():
        wfile(OPT_CONFD, txt.replace(m.group(0), f'rc_ulimit="-n {nofile_target()}"') + ("" if txt.endswith("\\n") else "\\n"))


def _opt_locald(on):
    """开机脚本：RPS 与 CPU 调频不是 sysctl，由 /etc/local.d 在每次启动时重新写入"""
    lines = ["#!/bin/sh", "# 由 Shunt 面板生成：系统优化（开机执行）"]
    if "rps" in on:
        n = os.cpu_count() or 1
        lines += [f"MASK={((1 << n) - 1):x}",
                  "for q in /sys/class/net/*/queues/rx-*; do",
                  '  i=${q#/sys/class/net/}; i=${i%%/*}; [ -e "/sys/class/net/$i/device" ] || continue',
                  '  echo $MASK > "$q/rps_cpus" 2>/dev/null; echo 4096 > "$q/rps_flow_cnt" 2>/dev/null',
                  "done"]
    if "gov" in on:
        lines += ["for g in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor; do echo performance > \\"$g\\" 2>/dev/null; done"]
    if "bbr" in on:
        lines += ["modprobe tcp_bbr 2>/dev/null; modprobe sch_fq 2>/dev/null"]
    if "conntrack" in on:
        lines += ["modprobe nf_conntrack 2>/dev/null"]
    # 开机时 sysctl 服务可能早于模块加载（conntrack / bbr 参数会写入失败），这里再应用一次
    lines += ["[ -f " + OPT_SYSCTL + " ] && sysctl -q -p " + OPT_SYSCTL + " 2>/dev/null", "exit 0"]
    return "\\n".join(lines) + "\\n"


def opt_apply(ids):
    """应用选中的优化项（未选中的恢复原值）；返回 (ok, 消息, 每项错误)"""
    with OPT_LOCK:
        items = {it["id"]: it for it in opt_items()}
        want = [i for i in ids if i in items and items[i]["ok"]]
        d = load()
        prev = set(d.get("sysopt") or [])
        orig = dict(d.get("sysopt_orig") or {})
        mods = list(d.get("sysopt_mods") or [])
        errs = []
        if "bbr" in want and not in_container():
            sh("modprobe tcp_bbr; modprobe sch_fq")
            mf = os.path.join(ETC, "modules")
            if "tcp_bbr" not in rfile(mf).split():
                try:
                    with open(mf, "a") as f:
                        f.write("tcp_bbr\\n")
                    mods.append("tcp_bbr")
                except OSError as e:
                    errs.append(f"写入 /etc/modules 失败：{e}")
        if "conntrack" in want and not in_container():
            sh("modprobe nf_conntrack")
        # sysctl：先记住原值，再写入；取消的项恢复原值
        conf = ["# 由 Shunt 面板生成：系统优化（取消请在面板「设置 → 系统优化」中关闭）"]
        for iid, it in items.items():
            for k, v in it["sysctl"].items():
                if iid in want:
                    if k not in orig and rsys(k) is not None:
                        orig[k] = rsys(k)
                    e = wsys(k, v)
                    if e:
                        errs.append(e)
                    conf.append(f"{k} = {v}")
                elif iid in prev and k in orig:
                    wsys(k, orig[k])
        try:
            os.makedirs(os.path.dirname(OPT_SYSCTL), exist_ok=True)
            if len(conf) > 1:
                with open(OPT_SYSCTL, "w") as f:
                    f.write("\\n".join(conf) + "\\n")
            elif os.path.exists(OPT_SYSCTL):
                os.remove(OPT_SYSCTL)
        except OSError as e:
            errs.append(f"写入 {OPT_SYSCTL} 失败：{e}")
        # mihomo 文件句柄
        try:
            txt = rfile(OPT_CONFD)
            txt = "\\n".join(l for l in txt.splitlines() if not l.startswith("rc_ulimit=")).strip()
            if "fd" in want:
                txt = (txt + "\\n" if txt else "") + f'rc_ulimit="-n {nofile_target()}"'
            if txt or os.path.exists(OPT_CONFD):
                os.makedirs(os.path.dirname(OPT_CONFD), exist_ok=True)
                with open(OPT_CONFD, "w") as f:
                    f.write(txt + "\\n" if txt else "")
        except OSError as e:
            errs.append(f"写入 {OPT_CONFD} 失败：{e}")
        # RPS
        mask = f"{((1 << (os.cpu_count() or 1)) - 1):x}"
        for ifc in net_ifaces():
            for q in glob_paths(os.path.join(SYS_ROOT, "class/net", ifc, "queues/rx-*")):
                key = "rps:" + q
                if "rps" in want:
                    orig.setdefault(key, rfile(os.path.join(q, "rps_cpus"), "0"))
                    if not (wfile(os.path.join(q, "rps_cpus"), mask) and wfile(os.path.join(q, "rps_flow_cnt"), 4096)):
                        errs.append(f"{ifc} RPS 写入失败")
                elif "rps" in prev and key in orig:
                    wfile(os.path.join(q, "rps_cpus"), orig[key])
                    wfile(os.path.join(q, "rps_flow_cnt"), 0)
        # CPU 调频
        for g in cpu_govs():
            key = "gov:" + g
            if "gov" in want:
                orig.setdefault(key, rfile(g))
                if not wfile(g, "performance"):
                    errs.append("CPU 调频写入失败")
            elif "gov" in prev and key in orig:
                wfile(g, orig[key])
        # 开机脚本
        try:
            if set(want) - {"ntp"}:
                os.makedirs(os.path.dirname(OPT_LOCALD), exist_ok=True)
                with open(OPT_LOCALD, "w") as f:
                    f.write(_opt_locald(set(want)))
                os.chmod(OPT_LOCALD, 0o755)
                sh("rc-update add local default")
            elif os.path.exists(OPT_LOCALD):
                os.remove(OPT_LOCALD)
        except OSError as e:
            errs.append(f"写入开机脚本失败：{e}")
        # 时间同步
        if "ntp" in want and "ntp" not in prev and not ntp_running():
            code, out = sh("apk add --no-cache chrony && rc-update add chronyd default && rc-service chronyd start", timeout=120)
            if code != 0:
                errs.append("安装 chrony 失败：" + out[-150:])
        elif "ntp" in prev and "ntp" not in want:
            sh("rc-service chronyd stop; rc-update del chronyd default")
        if not want:  # 全部关闭：清掉记录，下次重新读取原值
            mf = os.path.join(ETC, "modules")
            if mods:
                ls = [l for l in rfile(mf).splitlines() if l.strip() not in mods]
                wfile(mf, "\\n".join(ls) + ("\\n" if ls else ""))
            orig, mods = {}, []
        update(lambda x: x.update({"sysopt": want, "sysopt_orig": orig, "sysopt_mods": mods}))
        fd_changed = ("fd" in want) != ("fd" in prev)
        msg = f"已应用 {len(want)} 项优化" if want else "已全部恢复为系统默认值"
        if fd_changed:
            msg += "；文件句柄上限在下次重启核心后生效"
        return not errs, msg + ("" if not errs else "；部分失败：" + "；".join(errs[:4])), errs


def speed_test(proxy):
    """经代理 / 直连下载 Cloudflare 测速文件，返回 Mbps"""
    url = "https://speed.cloudflare.com/__down?bytes=50000000"
    t0 = time.time()
    n = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with proxy_opener(proxy).open(req, timeout=10) as r:
            ttfb = time.time() - t0
            t1 = time.time()
            while time.time() - t1 < 10:
                b = r.read(1 << 16)
                if not b:
                    break
                n += len(b)
            dt = max(time.time() - t1, 0.001)
        return {"ok": True, "mbps": round(n * 8 / dt / 1e6, 1), "mb": round(n / 1e6, 1), "ttfb": int(ttfb * 1000)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120], "mb": round(n / 1e6, 1)}


_NFT = {"ok": None, "t": 0}


def nft_ok():
    """内核是否支持 nftables（sing-box 的 TUN auto_redirect 依赖它，不支持时 sing-box 直接退出）"""
    if _NFT["ok"] is None or (not _NFT["ok"] and time.time() - _NFT["t"] > 60):
        try:
            p = subprocess.run(["nft", "list", "tables"], capture_output=True, timeout=5)
            _NFT["ok"] = p.returncode == 0
        except Exception:
            _NFT["ok"] = False
        _NFT["t"] = time.time()
    return _NFT["ok"]


def sh(cmd, timeout=60):
    """shell=True 是有意的：大量调用方依赖 shell 特性（管道、重定向、&& 等），不要改成 shell=False。
    约定：拼进 cmd 的不可信输入（远端文件名、环境变量、用户输入）调用方必须先用 shlex.quote() 包裹。"""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:
        return 1, str(e)


_ANSI = re.compile(r"\\x1b\\[[0-9;]*m")
_RC_BUSY = re.compile(r"already (?:starting|stopping)|is (?:starting|stopping)|has started, but is inactive", re.I)


SVC_OP = threading.RLock()  # 核心服务控制（启动 / 停止 / 重启）全局互斥：面板按钮、看门狗、定时任务、切换 / 更新内核共用，避免两个 rc-service 并发互相打断


PROC = os.environ.get("PANEL_PROC", "/proc")
CORE_BINS = None  # 核心可执行文件路径；None 时取 mihomo / mihomo Smart / sing-box 的默认路径（测试可覆盖）
_CHECK_ARGS = {"-t", "check", "version", "-v", "format", "rule-set", "generate", "merge"}  # selftest / 校验 / 查版本，不是常驻核心


def core_procs(bins=None):
    """所有正在运行的核心进程 pid（按可执行文件识别，不依赖 pid 文件）。
    OpenRC 被 zap、服务迁移或手动在 SSH 里运行过的核心不在 pid 文件里：rc-service stop 停不掉它，
    它占着 9090 / 7890 端口，面板就一直显示「运行中」，新启动的核心也会因端口被占立即退出。
    bins：只找这些程序的进程（切换内核后找旧内核用）"""
    if bins is None:
        bins = CORE_BINS
    if bins is None:
        from .sb_common import SB_BIN
        bins = (MIHOMO_BIN, SMART_BIN, SB_BIN)
    want = {os.path.realpath(b) for b in bins if b}
    out = []
    try:
        names = os.listdir(PROC)
    except OSError:
        return out
    me = os.getpid()
    for n in names:
        if not n.isdigit() or int(n) == me:
            continue
        try:
            exe = os.readlink(os.path.join(PROC, n, "exe"))
            with open(os.path.join(PROC, n, "cmdline"), "rb") as f:
                args = f.read().split(b"\\0")
        except OSError:
            continue
        if not args[0]:  # 命令行为空：进程正在 exec（程序已换、命令行还没就位）或已是僵尸，认不出是不是 -t 校验，先不算
            continue  # 以前这一刻的 mihomo -t 被当成常驻核心（全量单元测试负载高时 Strays.test_detect 偶发多出一个）
        if exe.endswith(" (deleted)"):  # 内核更新替换了二进制，旧进程还在跑
            exe = exe[:-10]
        if os.path.realpath(exe) not in want and exe not in want:
            continue
        if any(a.decode("utf-8", "ignore") in _CHECK_ARGS for a in args[1:]):
            continue
        out.append(int(n))
    return out


def kill_core_procs(grace=15, bins=None):
    """结束全部核心进程（bins：只结束这些程序的）：先 TERM（sing-box 正常退出时才把 Fake-IP 映射写进 cache.db），
    grace 秒后还在就 KILL。返回结束的个数"""
    import signal
    pids = core_procs(bins)
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    end = time.time() + grace
    while time.time() < end and any(_alive(p) for p in pids):
        time.sleep(0.2)
    for p in pids:
        if _alive(p):
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
    return len(pids)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # 已退出但还没被回收的僵尸进程不算
        with open(os.path.join(PROC, str(pid), "stat")) as f:
            return f.read().rsplit(")", 1)[-1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def _rc_once(cmd, timeout):
    import signal, tempfile
    with tempfile.TemporaryFile() as f:
        try:
            # 输出写临时文件而不是管道：核心 / start_post 拉起的子进程会继承管道，用 capture_output 时
            # 调用方要一直等到这些子进程退出（或 90 秒超时），按钮看起来就像「没反应」
            # 单独的进程组：超时时连同 rc-service / openrc-run 子进程一起杀掉，不留一个仍在 stopping 的孤儿
            #（核心本身由 start-stop-daemon 另起会话，不在这个进程组里，不受影响）
            pr = subprocess.Popen(cmd, shell=True, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                                  start_new_session=True)
            try:
                code = pr.wait(timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(pr.pid, signal.SIGKILL)
                except OSError:
                    pr.kill()
                pr.wait()
                code = 124
        except OSError as e:
            return 1, str(e)
        f.seek(0)
        out = _ANSI.sub("", f.read(64 << 10).decode("utf-8", "ignore")).strip()
    return code, (out + ("\\n（命令超时）" if code == 124 else "")).strip()


def rc_run(act, timeout=90):
    """rc-service <核心> start|stop|restart。OpenRC 卡在 starting / stopping 时先 zap 再重试一次；
    超时（124）时先 zap，避免 OpenRC 一直停在 starting / stopping，下次操作直接报 already stopping"""
    with SVC_OP:
        if act in ("start", "restart"):
            _keep_core_log(act)
        prep = act in ("start", "restart") and _core_prep()
        try:
            return _rc_run(act, timeout)
        finally:
            if prep:  # init.d 的 start_post 用完已删；没走到那一步（stop 失败等）也不留给以后 SSH 里的 rc-service
                try:
                    os.remove(CORE_PREP)
                except OSError:
                    pass


def _keep_core_log(act):
    """start_pre 会清空核心日志：启动前先把上次运行的日志末尾另存（logclean.core_log_keep），崩溃原因不随重启丢失"""
    try:
        from .logclean import core_log_keep
        core_log_keep("启动核心" if act == "start" else "重启核心")
    except Exception as e:
        print("keep core log", e, flush=True)


def _core_prep():
    """见 cli.core_prep：失败只记日志，init.d 照旧 --gen"""
    if not CORE_PREP:
        return False
    try:
        from .cli import core_prep
        return core_prep()
    except Exception as e:
        from .errlog import log_err
        log_err("启动核心前生成配置", e)
        return False


def _rc_run(act, timeout):
    code, out = _rc_once(f"{SVC} {act}", timeout)
    if code != 0 and code != 124 and _RC_BUSY.search(out):
        # zap 只清 OpenRC 的状态，不碰进程：先把核心进程结束掉，否则旧核心变成没人管的孤儿，
        # 之后 stop 停不掉它、新核心又抢不到端口
        kill_core_procs()
        _rc_once(f"{SVC} zap", 15)
        code, out = _rc_once(f"{SVC} {act}", timeout)
        out = "（服务状态卡住，已自动 zap 后重试）\\n" + out
    elif code == 124:
        if act == "stop":
            kill_core_procs()
        _rc_once(f"{SVC} zap", 15)
    return code, out


def svc_restart(timeout=90, why="面板重启核心", cancel=None):
    """重启核心服务。重启及启动后的加载期（大规则集、Fake-IP 缓存）内看门狗暂停判定，
    否则启动慢一点就会被看门狗当成「无响应」再重启一次，形成连环重启。
    cancel：拿到服务锁后再调用一次，返回真就放弃重启（看门狗用：等锁期间用户点了「停止」，不能再把核心拉起来）"""
    with SVC_OP:
        if cancel is not None and cancel():
            return 1, "已取消：核心已被手动停止"
        wd_hold(timeout + 60, why)
        try:
            return rc_run("restart", timeout=timeout)
        finally:
            wd_hold(60, why, reset=True)  # 命令返回后只再给 60 秒加载时间
            try:
                from .ipinfo import reset as ip_reset
                ip_reset()  # 概览 IP 信息：重启后重新查，出口可能已变
            except Exception:
                pass


def proxy_opener(proxy=True):
    hs = [urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{MIXED}", "https": f"http://127.0.0.1:{MIXED}"} if proxy else {})]
    # SafeRedirectHandler 替换默认的重定向处理器：跟随 302 前校验目标为公网地址（SSRF 防护）；
    # 走代理时跳过本地 DNS 校验（由代理自己解析），字面内网 IP 仍拦截
    return urllib.request.build_opener(*hs, SafeRedirectHandler(via_proxy=proxy))


_RTT_CTX = None  # site_rtt 复用：SSL context 创建含 CA 加载，约几十 ms；context 本身线程安全


def _rtt_ctx():
    global _RTT_CTX
    if _RTT_CTX is None:
        _RTT_CTX = ssl.create_default_context()
    return _RTT_CTX


def _rtt_open(url, timeout=6):
    """经 mixed 端口建立一条连接并发出第一次请求（DNS、TCP、TLS 握手与首包都在这里，不计入延迟）。
    返回 (连接, 请求路径, 请求头, 第一次总耗时 ms, 连接是否可复用)；连接由调用方关闭"""
    u = urlsplit(url)
    https = u.scheme == "https"
    port = u.port or (443 if https else 80)
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    hdr = {"User-Agent": "curl/8", "Accept": "*/*"}
    if https:
        c = http.client.HTTPSConnection("127.0.0.1", MIXED, timeout=timeout, context=_rtt_ctx())
        c.set_tunnel(u.hostname, port)
    else:  # 普通 HTTP 走代理时请求行用完整 URL
        c = http.client.HTTPConnection("127.0.0.1", MIXED, timeout=timeout)
        path = f"http://{u.hostname}:{port}{path}"
    try:
        st = time.time()
        c.request("HEAD", path, headers=hdr)
        r = c.getresponse()
        r.read()
        return c, path, hdr, int((time.time() - st) * 1000), not r.will_close
    except BaseException:
        c.close()
        raise


def _rtt_second(c, path, hdr, first):
    """在已建立的连接上再请求一次，只计这一次的往返；失败时退回第一次的总耗时"""
    try:
        st = time.time()
        c.request("HEAD", path, headers=hdr)
        c.getresponse().read()
        return max(1, int((time.time() - st) * 1000))
    except Exception:
        return first


def site_rtt(url, timeout=6):
    """经 mixed 端口建立一条连接（DNS、TCP、TLS 握手不计时），在同一连接上再请求一次，只计这一次的往返。
    与核心 unified-delay 的测法一致，也和浏览器复用连接时的实际体验一致；第二次请求失败时退回第一次的总耗时"""
    c, path, hdr, first, reuse = _rtt_open(url, timeout)
    try:
        return _rtt_second(c, path, hdr, first) if reuse else first
    finally:
        c.close()


def direct_rtt(timeout=6):
    """直连（不走代理）延迟，与概览「百度」卡片同一口径：经 mixed 端口连上百度，复用连接只计第二次请求的往返。
    规则 / 直连模式下百度按 geosite:cn 走 DIRECT，测到的就是直连；全局模式下 mixed 端口会走代理，
    这时改由核心测 DIRECT 出站（mihomo 有 unified-delay；sing-box 没有，测两次取小）。
    v7.5.8～v7.6.0 在 sing-box 下一律由核心测，sing-box 的测法含 TCP 握手且地址不同，直连显示 139 ms 而百度卡片 31 ms"""
    from .base import DIRECT_HC
    from urllib.parse import quote
    from .corehttp import core, core_json
    try:
        mode = str((core_json("/configs", timeout=3) or {}).get("mode") or "rule").lower()
    except (OSError, ValueError, TypeError, AttributeError):
        mode = "rule"
    if mode != "global":
        try:
            ms = site_rtt(DIRECT_HC, timeout=timeout)
            if ms and ms > 0:
                return ms
        except (OSError, http.client.HTTPException, ValueError):
            pass
    got = []
    for _ in range(2):
        try:
            code, raw = core("GET", f"/proxies/DIRECT/delay?url={quote(DIRECT_HC)}&timeout={timeout * 1000}", timeout=timeout + 2)
            ms = int(json.loads(raw).get("delay") or 0) if code == 200 else 0
        except (OSError, ValueError, TypeError, AttributeError):
            ms = 0
        if ms <= 0:
            break
        got.append(ms)
    if got:
        return min(got)
    try:
        from .ipinfo import _phys_iface
        iface = _phys_iface()
    except (OSError, ImportError):
        iface = None
    try:
        cmd = ["curl", "-s", "-I", "-o", "/dev/null", "-o", "/dev/null", "-w", "%{time_total}\\\\n", "--max-time", str(timeout), DIRECT_HC, DIRECT_HC]
        if iface:
            cmd[1:1] = ["--interface", iface]
        out = subprocess.check_output(cmd, text=True, timeout=timeout * 2 + 2).split()
        ms = int(float(out[-1]) * 1000) if out else 0
        return max(1, ms) if ms > 0 else -1
    except (OSError, ValueError, subprocess.SubprocessError):
        return -1


SITE_HIST, SITE_LOCK, SITE_LAST = {}, threading.Lock(), {"t": 0, "r": None}
SITE_HIST_N = 30  # 每个站点保留最近 30 次（约 30 分钟）


def delay_test(force=False):
    """多个浏览器同时打开概览时 20 秒内复用同一次结果，历史只记一次。
    double-checked locking：缓存检查在锁内，整个多线程测速在锁外，写回前再检查一次"""
    if not force:
        with SITE_LOCK:
            if SITE_LAST["r"] is not None and time.time() - SITE_LAST["t"] < 20:
                return SITE_LAST["r"]
    tests = load()["tests"] or TESTS
    out = [None] * len(tests)

    conns = [None] * len(tests)

    def one(i, t):
        try:
            conns[i] = _rtt_open(t["url"])
        except Exception:
            conns[i] = None

    # 建连、TLS 握手和首个请求并发进行（省时间）；计时的第二次请求逐个进行：
    # 6 个站点同时计时，其他连接的 TLS 握手会占满路由器 CPU，把计时请求拖慢（实测百度 32 ms 显示成 128 ms）
    ths = [threading.Thread(target=one, args=(i, t)) for i, t in enumerate(tests)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    for i, t in enumerate(tests):
        ms = -1
        if conns[i] is not None:
            c, path, hdr, first, reuse = conns[i]
            try:
                ms = _rtt_second(c, path, hdr, first) if reuse else first
            except Exception:
                ms = -1
            finally:
                c.close()
        out[i] = {"name": t["name"], "url": t["url"], "ms": ms}
    with SITE_LOCK:
        if not force and SITE_LAST["r"] is not None and time.time() - SITE_LAST["t"] < 20:
            return SITE_LAST["r"]  # 测速期间已有更新鲜的结果，直接复用，丢弃本次结果
        names = {o["name"] for o in out}
        for k in list(SITE_HIST):
            if k not in names:
                del SITE_HIST[k]
        for o in out:
            h = SITE_HIST.setdefault(o["name"], deque(maxlen=SITE_HIST_N))
            h.append(o["ms"])
            o["hist"] = list(h)
        SITE_LAST.update(t=time.time(), r=out)
        return out


# 站点图标：经代理下载 https://<host>/favicon.ico，失败再试 Google favicon 服务；缓存 7 天，失败的 1 小时内不重试
FAV_DIR = os.path.join(PANEL_DIR, "favicons")
FAV_FAIL = {}
FAV_FAIL_MAX = 2000  # 失败记录上限：超限淘汰最旧的，防止无界增长


def img_type(b):
    """按魔数识别位图图标，返回 Content-Type；SVG、HTML 等可执行脚本的格式一律不认（防 XSS）"""
    if b[:4] == b"\\x00\\x00\\x01\\x00":
        return "image/x-icon"
    if b[:8] == b"\\x89PNG\\r\\n\\x1a\\n":
        return "image/png"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if b[:3] == b"\\xff\\xd8\\xff":
        return "image/jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_img(b):
    return img_type(b) is not None


def fav_hosts(d=None):
    """允许取图标的站点：只限「延迟站点」列表里出现过的 host，防止面板被当作任意 URL 的同源代理"""
    tests = (d or load()).get("tests") or TESTS
    out = set()
    for t in tests:
        try:
            h = (urlsplit(str(t.get("url") or "")).hostname or "").lower()
        except (AttributeError, ValueError):
            continue
        if h:
            out.add(h)
    return out


def favicon(url, allowed=None):
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None, None
    if not re.fullmatch(r"[a-z0-9.-]{1,253}", host):
        return None, None
    if host not in (fav_hosts() if allowed is None else allowed):
        return None, None
    path = os.path.join(FAV_DIR, host)
    try:
        if time.time() - os.path.getmtime(path) < 7 * 86400:
            with open(path, "rb") as f:
                b = f.read()
            ct = img_type(b)
            if ct:
                return b, ct
            os.remove(path)  # 旧版本缓存的 SVG 等非位图：删掉重新获取
    except OSError:
        pass
    if time.time() - FAV_FAIL.get(host, 0) < 3600:
        return None, None
    op = proxy_opener()
    root = ".".join(host.split(".")[-2:])  # api.telegram.org 没有图标时用 telegram.org 的
    tries = [f"https://{host}/favicon.ico"] + ([f"https://{root}/favicon.ico", f"https://www.{root}/favicon.ico"] if root != host else [])
    for u in tries + [f"https://www.google.com/s2/favicons?domain={host}&sz=64"]:
        try:
            dl_check(u, via_proxy=True)  # SSRF 防护：走代理下载，跳过本地 DNS 校验（字面内网 IP 仍拦截）
            with op.open(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=8) as r:
                b = r.read(256 * 1024)
            if len(b) > 60 and _is_img(b):
                os.makedirs(FAV_DIR, exist_ok=True)
                with open(path, "wb") as f:
                    f.write(b)
                return b, img_type(b)
        except Exception:
            pass
    if len(FAV_FAIL) >= FAV_FAIL_MAX:
        del FAV_FAIL[min(FAV_FAIL, key=FAV_FAIL.get)]  # 淘汰最旧的一条
    FAV_FAIL[host] = time.time()
    return None, None
'''
_SRC['shunt.notify'] = '''"""Telegram 通知"""
import json, os, signal, socket, threading, time, urllib.request, urllib.error
from collections import deque
from urllib.parse import quote

from .base import CORE_UPD, G_MANUAL, HC, MIHOMO_LOG, NOTIFY_FILE, PANEL_DIR, TPROXY_SH, WD_HOLD, mass_testing
from .data import load, read_json, write_json
from .corehttp import core, core_json
from .nodeprobe import confirm_all_down
from .sb_common import CORE_PID, CORES, active_core
from .sysopt import SVC_OP, proxy_opener, sh, svc_restart
def tg_send(text, d=None):
    d = d or load()
    if not d.get("tg_token") or not d.get("tg_chat"):
        return False, "未配置 Telegram"
    body = json.dumps({"chat_id": d["tg_chat"], "text": f"[Shunt@{socket.gethostname()}]\\n{text}"}).encode()
    url = f"https://api.telegram.org/bot{d['tg_token']}/sendMessage"
    err = ""
    for use_proxy in (True, False):  # 先走代理，核心挂了再直连
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with proxy_opener(use_proxy).open(req, timeout=10) as r:
                if json.loads(r.read()).get("ok"):
                    return True, "已发送"
        except urllib.error.HTTPError as e:
            try:
                return False, json.loads(e.read()).get("description", str(e))
            except Exception:
                return False, str(e)
        except Exception as e:
            err = str(e)
    return False, "发送失败：" + err


def notify(text):
    threading.Thread(target=tg_send, args=(text,), daemon=True).start()


# ---------------------------------------------------------------- 故障自动直连 (watchdog) + 定期告警
# 「手动停止」标志落盘：以前只在内存里，面板一重启（在线更新、切 HTTPS、rc-service shunt restart）标志就丢了，
# 看门狗随即把用户手动停掉的核心又拉起来
STOP_FLAG = os.path.join(PANEL_DIR, "core_stopped")
# 看门狗事件落盘：以前只在内存里，面板一重启记录就没了。只留最近 WD_EVENTS_MAX 条，满了自动删掉最旧的
WD_EVENTS_FILE = os.path.join(PANEL_DIR, "wd_events.json")
WD_EVENTS_MAX = 20


def _load_wd_events():
    ev = read_json(WD_EVENTS_FILE, [])
    if not isinstance(ev, list):
        ev = []
    ev = [e for e in ev if isinstance(e, dict) and isinstance(e.get("t"), int) and isinstance(e.get("msg"), str)]
    return deque(ev[:WD_EVENTS_MAX], maxlen=WD_EVENTS_MAX)


WD = {"status": "ok", "fails": 0, "events": _load_wd_events(), "last_check": 0, "manual_stop": os.path.isfile(STOP_FLAG),
      "tp_off": False}


WD_STABLE = 300  # 看门狗重启后核心连续正常这么多秒，才算真正恢复（失败计数清零）


def set_manual_stop(on):
    """置位 / 清除「用户手动停止了核心」：内存 + 标志文件"""
    WD["manual_stop"] = bool(on)
    try:
        if on:
            with open(STOP_FLAG, "w") as f:
                f.write(str(int(time.time())))
        elif os.path.exists(STOP_FLAG):
            os.remove(STOP_FLAG)
    except OSError:
        pass


def wd_counts(events=None):
    """记录里的自动重启 / 切换直连 / 恢复次数"""
    c = {"restart": 0, "direct": 0, "recover": 0}
    # list() 在 C 层一次拷完：直接遍历 deque 时看门狗线程 appendleft 会抛「deque mutated during iteration」，/api/state 偶发 500
    for e in list(WD["events"] if events is None else events):
        k = e.get("kind")
        if k in c:
            c[k] += 1
    return c


def wd_event(msg, push=True, kind=""):
    e = {"t": int(time.time()), "msg": msg}
    if kind:
        e["kind"] = kind
    WD["events"].appendleft(e)  # deque(maxlen=20)：第 21 条进来时最旧的一条自动删除
    try:
        write_json(WD_EVENTS_FILE, list(WD["events"]))
    except OSError:
        pass
    print(time.strftime("%F %T"), "[watchdog]", msg, flush=True)
    if push:
        notify(msg)


def core_alive(timeout=5):
    code, _ = core("GET", "/version", timeout=timeout)
    return code == 200


def core_alive_confirmed(retries=2, gap=3):
    """看门狗用：一次 /version 超时不算无响应，再隔几秒复查，连续都失败才判定故障。
    小路由上 sing-box 加载大规则集、重载配置或 GC 时会短暂卡几秒；以前一次超时就重启，
    重启若被强制结束（kill），sing-box 的 Fake-IP 映射全部丢失，客户端随后大量「missing fakeip record」"""
    if core_alive():
        return True
    for _ in range(retries):
        time.sleep(gap)
        code, _ = core("GET", "/version", timeout=8)
        if code == 200:
            return True
    return False


def core_proc_alive():
    """核心进程是否还在（pid 文件 + kill 0）。用来区分「进程退出」和「进程在但控制接口卡住」"""
    try:
        with open(CORE_PID) as f:
            os.kill(int(f.read().strip()), 0)
        return True
    except Exception:
        return False


def core_fd_usage():
    """核心进程已用 / 上限文件句柄，如「1021/1024」；读不到返回空串。句柄用尽是控制接口无响应最常见的原因"""
    try:
        with open(CORE_PID) as f:
            pid = int(f.read().strip())
        used = len(os.listdir(f"/proc/{pid}/fd"))
        lim = ""
        with open(f"/proc/{pid}/limits") as f:
            for line in f:
                if line.startswith("Max open files"):
                    lim = line.split()[3]
        return f"{used}/{lim}" if lim else str(used)
    except Exception:
        return ""


HANG_LOG = os.environ.get("PANEL_HANG_LOG", "/var/log/shunt-core-hang.log")


def core_hang_dump(why):
    """控制接口无响应、进程仍在：重启前把核心日志末尾和 Go 协程栈（SIGQUIT）存进 HANG_LOG。
    start_pre 会清空核心日志，不存下来就无从知道核心当时卡在哪。只保留最近一次（最多约 512KB）"""
    try:
        with open(CORE_PID) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
    except Exception:
        return False
    def tail(n):
        try:
            with open(MIHOMO_LOG, "rb") as f:
                f.seek(0, 2)
                end = f.tell()
                f.seek(max(0, end - n))
                return end, f.read()
        except OSError:
            return 0, b""
    off, before = tail(64 * 1024)
    try:
        os.kill(pid, signal.SIGQUIT)  # Go 运行时收到 SIGQUIT 把所有协程栈打到 stderr（即核心日志）后退出
    except OSError:
        pass
    for _ in range(20):
        time.sleep(0.25)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    time.sleep(0.3)
    end, _ = tail(0)
    dump = b""
    if end > off:
        try:
            with open(MIHOMO_LOG, "rb") as f:
                f.seek(off)
                dump = f.read(448 * 1024)
        except OSError:
            pass
    try:
        with open(HANG_LOG, "wb") as f:
            f.write(("===== %s %s =====\\n----- 核心日志末尾 -----\\n" % (time.strftime("%F %T"), why)).encode())
            f.write(before)
            f.write("\\n----- SIGQUIT 协程栈 -----\\n".encode())
            f.write(dump)
        return True
    except OSError:
        return False


def core_last_error():
    """核心日志末尾 16KB 里最后一条 FATAL / ERROR / panic，写进看门狗事件，便于事后查原因"""
    try:
        with open(MIHOMO_LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 16 * 1024))
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        if ("FATAL" in line or " ERROR " in line or "panic:" in line or "level=error" in line or "level=fatal" in line) \\
                and "missing fakeip record" not in line:
            return line.strip()[-160:]
    return ""


def watchdog_tick():
    if CORE_UPD["busy"]:  # 内核更新过程中核心会重启，不算故障
        return
    if time.time() < WD_HOLD["until"]:  # 面板自己正在重启 / 重载核心（含启动后的加载期）
        return
    d = load()
    WD["last_check"] = int(time.time())
    if not d.get("watchdog", True):
        WD["status"] = "off"
        # 以前：看门狗撤规则直连（tp_off）期间关掉看门狗，tp_off 再也没人清，局域网一直直连、之后保存设置也不再重载透明代理规则。
        # 现在核心在跑时补开规则并清标志（核心没响应时保持直连，不把流量导向卡死的核心）
        if WD["tp_off"] and not WD["manual_stop"] and core_alive():
            WD["tp_off"] = False
            if d.get("proxy_mode") != "off":
                sh(TPROXY_SH + " apply")
        return
    if mass_testing():  # 全节点测速进行中：核心高负载，/version 可能超时，暂停故障判定
        return
    redirecting = d["proxy_mode"] != "off"  # TProxy 或 TUN（TUN 模式下 tproxy.sh 仍负责 DNS 重定向）
    cname = CORES.get(active_core(d), "mihomo")
    if core_alive_confirmed():
        # 核心在跑（开机自启、SSH 手动 start）：「手动停止」已过时，恢复看门狗。
        # 只在没人控制服务时清（拿不到锁说明正在停止 / 启动，不能清）
        if WD["manual_stop"] and SVC_OP.acquire(blocking=False):
            try:
                if core_proc_alive():
                    set_manual_stop(False)
            finally:
                SVC_OP.release()
        if WD["status"] in ("restarting", "direct"):
            msg = f"{cname} 核心已恢复"
            if WD["tp_off"] and redirecting:
                code, out = sh(TPROXY_SH + " apply")
                msg += "，透明代理已重新开启" if code == 0 else "，但重新开启透明代理失败：" + out[-120:]
            WD["tp_off"] = False
            wd_event(msg, kind="recover")
        elif WD["tp_off"]:  # 撤规则直连期间核心被手动停止、又由用户启动（status 已不是 direct）：补开规则并清标志，
            WD["tp_off"] = False  # 否则 tp_off 一直留着，之后保存设置时面板永远不再重载透明代理规则
            if redirecting:
                sh(TPROXY_SH + " apply")
        WD["status"] = "ok"
        # 看门狗重启后核心要稳定运行 WD_STABLE 秒才把失败计数清零：以前一响应就清零，核心起来几十秒就崩溃（OOM、
        # 坏节点触发 panic）时会无限「重启 → 恢复 → 再崩」，每轮都推一条 Telegram；现在连续崩溃照样累计，第 3 次起改为直连 + 每 5 分钟重试
        if time.time() - WD.get("last_restart", 0) >= WD_STABLE:
            WD["fails"] = 0
        return
    if WD["manual_stop"]:
        WD["status"] = "stopped"
        return
    # core_alive_confirmed 最长要二十多秒，这期间可能开始了热重载（SIGHUP，设置 WD_HOLD）/ 内核更新 / 用户点了停止：
    # 只在 tick 开头查一次的话，会把重载中短暂不通的核心当成卡死，SIGQUIT 杀掉（Fake-IP 映射来不及写进 cache.db）再重启
    if _wd_yield():
        return
    # 拿服务锁再动手（重启、SIGQUIT 现场、撤透明代理）：拿不到说明面板 / 定时任务正在启停核心，这一轮跳过
    if not SVC_OP.acquire(blocking=False):
        return
    try:
        if _wd_yield():  # 拿锁前的瞬间可能刚开始重载 / 刚被手动停止
            return
        _wd_fail(d, cname)
    finally:
        SVC_OP.release()


def _wd_yield():
    """看门狗该让路：内核更新中、面板正在重启 / 重载核心（WD_HOLD）、核心被手动停止"""
    return bool(CORE_UPD["busy"]) or time.time() < WD_HOLD["until"] or bool(WD["manual_stop"])


def _wd_fail(d, cname):
    """确认核心无响应后的处理（调用方已持有 SVC_OP）：前 2 次自动重启，之后撤透明代理直连，每 5 分钟再试一次"""
    redirecting = d["proxy_mode"] != "off"
    WD["fails"] += 1
    if WD["fails"] <= 2:
        WD["status"] = "restarting"
        why = "控制接口无响应（进程仍在）" if core_proc_alive() else "进程已退出"
        err = core_last_error()
        fd = core_fd_usage() if core_proc_alive() else ""
        if fd:
            why += f"，文件句柄 {fd}"
        if fd and core_hang_dump(why):
            why += f"，现场已存到 {HANG_LOG}"
        wd_event(f"{cname} 核心{why}，第 {WD['fails']} 次自动重启" + (f"；日志最后一条错误：{err}" if err else ""),
                 push=WD["fails"] == 1, kind="restart")
        WD["last_restart"] = time.time()
        svc_restart(timeout=60, why="看门狗重启核心", cancel=lambda: WD["manual_stop"])
        return
    if WD["status"] != "direct":
        WD["status"] = "direct"
        if redirecting:
            # 以前只 stop：核心崩溃 / 被 KILL 时 TUN 的 auto-route 策略路由（2022 表，strict_route 的 unreachable）留着，
            # 说是「局域网直连」实际照样断网。现在顺带清残留（leftovers 只在没有核心进程时才动）
            sh(TPROXY_SH + " stop leftovers")
            WD["tp_off"] = True
        wd_event(f"{cname} 核心重启 2 次仍无响应，已关闭透明代理，局域网暂时直连上网", kind="direct")
    elif WD["fails"] % 10 == 0:  # 直连状态下每 5 分钟再尝试拉起一次
        WD["last_restart"] = time.time()
        svc_restart(timeout=60, why="看门狗重启核心", cancel=lambda: WD["manual_stop"])
        if redirecting:
            # rc 的 start_post 不管核心是否真的起来都会重新挂上规则（command_background 下启动总算成功）：
            # 一律撤掉，局域网保持直连；核心若已恢复，下一轮确认后再重新开启
            # TUN 模式下核心一启动就接管路由，无法“暂不接管”，下一轮检查会正常恢复
            sh(TPROXY_SH + " stop leftovers")  # 核心没起来又崩溃时清掉它留下的 TUN 策略路由（有核心进程时不动）
            WD["tp_off"] = True


def load_notified():
    return read_json(NOTIFY_FILE, {})


NODE_CONFIRM_BUDGET = 90    # 逐个节点复核最多用多少秒（期间看门狗按「全节点测速中」暂停判定）


def check_nodes():
    """节点选择里所有节点都超时 → 通知（只在状态变化时发）"""
    d = load()
    if d.get("mode") == "direct" or not (d.get("tg_token") and d.get("tg_chat")):
        return  # 没配置 Telegram 时不必每 10 分钟测一遍全部节点
    # 用「🖐️ 手动选择」（select，含全部节点）测速：对 url-test 组调用 /group/…/delay 会被 mihomo 清除用户的📌固定选择
    pg = core_json("/proxies/" + quote(G_MANUAL))
    if not pg:
        return
    code, raw = core("GET", f"/group/{quote(G_MANUAL)}/delay?url={quote(HC)}&timeout=5000", timeout=30)
    # 只有 200（逐个节点的结果）和 504（mihomo：全部节点超时）是测速结论；502 等是核心没应答（重载 / 重启中、请求超时），
    # 以前也当成「全部超时」，会误发一条「所有节点均测速超时」，下一轮又发「已恢复」
    if code not in (200, 504):
        return
    try:
        res = json.loads(raw) if code == 200 else {}
    except Exception:
        return
    if not isinstance(res, dict):
        return
    all_down = not any(v for v in res.values() if isinstance(v, int) and v > 0)
    st = load_notified()
    if all_down and not st.get("allnodes") and code == 200:
        # 200 却没有一个可用节点：sing-box 全部超时就是这样（也可能只是没轮到测），逐个节点复核后再告警
        all_down = confirm_all_down((pg.get("all") or []) if isinstance(pg, dict) else [], NODE_CONFIRM_BUDGET)
        if all_down is None:
            return
    if all_down != bool(st.get("allnodes")):
        st["allnodes"] = all_down
        write_json(NOTIFY_FILE, st)
        notify("⚠️ 节点选择中的所有节点均测速超时，请检查订阅或网络" if all_down else "✅ 节点已恢复可用")
'''
_SRC['shunt.latency'] = '''"""节点延迟缓存"""
import base64, json, os, re, threading, time, urllib.request, urllib.error
from urllib.parse import quote

from .errlog import log_err
from .base import G_MANUAL, G_SEL, HC, NOTIFY_FILE, PANEL_DIR, UA, WD_INTERVAL, dl_check, read_full
from .data import load, read_json, write_json
from .core import refresh_regions
from .corehttp import core, core_json
from .sb_common import active_core
from .sb_config import sb_fake_providers
from .sysopt import proxy_opener
from .nodeprobe import fill_group
from .notify import WD, check_nodes, core_alive, load_notified, notify, watchdog_tick
# 核心只在内存里保存最近几次测速记录：重载配置、重启、切换内核后全部清空，订阅节点的记录又分散在
# /proxies 与 /providers/proxies 两处，前端轮询时就会「一会有一会没有」。面板把每个节点最近一次的
# 测速结果（任意测速地址中时间最新的一条）缓存到文件，核心暂时没有记录时用缓存补上。
DELAY_FILE = os.path.join(PANEL_DIR, "delays.json")
BG_FILE = os.path.join(PANEL_DIR, "bg.img")  # 外观：自定义背景图（所有设备共用）
BG_MAX = 8 * 2**20
DELAYS = {"data": None, "saved": 0, "dirty": False}
RETEST = {"pending": False, "last": 0}
RETEST_BUDGET = 90  # 重载后补测（sing-box 逐个补测缺席节点）的时间上限


def _hist_last(p):
    """节点所有测速记录（history 与各地址的 extra.history）里时间最新的一条 → (延迟, 时间串)"""
    best = None
    lists = [p.get("history") or []] + [v.get("history") or [] for v in (p.get("extra") or {}).values() if isinstance(v, dict)]
    for h in lists:
        if h and isinstance(h[-1], dict) and "delay" in h[-1]:
            e = h[-1]
            if best is None or str(e.get("time") or "") > best[1]:
                best = (int(e.get("delay") or 0), str(e.get("time") or ""))
    return best


def delay_merge(px, prov_ok=True):
    """用核心的最新记录更新缓存，并给每个节点写入 _d（延迟）/ _dt（测速时间）。
    多个 /api/px 请求与 retest_soon 后台线程会同时调用：缓存的读改写和落盘都在 _DELAY_LOCK 内，
    不再出现「dictionary changed size during iteration」和两个线程同写一个 .tmp。
    prov_ok=False（订阅节点列表没取到）时不清理「已不存在」的节点，免得把订阅节点的缓存延迟全删掉"""
    with _DELAY_LOCK:
        if DELAYS["data"] is None:
            DELAYS["data"] = read_json(DELAY_FILE, {}) or {}
        data, now = DELAYS["data"], int(time.time())
        for name, p in px.items():
            if p.get("all") is not None:  # 策略组的延迟取当前节点，前端处理
                continue
            r = _hist_last(p)
            if r:
                old = data.get(name)
                if not old or old.get("ts") != r[1]:
                    data[name] = {"d": r[0], "ts": r[1], "at": now}
                    DELAYS["dirty"] = True
            c = data.get(name)
            if c:
                p["_d"], p["_dt"] = c["d"], c["at"]
        if px and prov_ok:
            for n in [n for n in data if n not in px]:  # 节点已不存在
                data.pop(n)
                DELAYS["dirty"] = True
        if DELAYS["dirty"] and now - DELAYS["saved"] > 30:
            try:
                write_json(DELAY_FILE, data)
                DELAYS.update(saved=now, dirty=False)
            except OSError as e:
                log_err("保存延迟缓存", e)
    return px


_DELAY_LOCK = threading.Lock()


def fix_global():
    """mihomo 的「全局」模式走内置 GLOBAL 组，它默认选第一项 DIRECT（全局模式反而不走代理）：没选过代理时指向「🚀 节点选择」"""
    if active_core() == "singbox":  # sing-box 的 Global 已直接指向节点选择
        return
    g = core_json("/proxies/GLOBAL", timeout=5) or {}
    if g.get("now") in (None, "", "DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE") and G_SEL in (g.get("all") or []):
        core("PUT", "/proxies/GLOBAL", {"name": G_SEL}, timeout=5)


def retest_soon(wait=6):
    """重载 / 重启后核心的测速记录清空：稍后在后台用「🖐️ 手动选择」（select 组，不会清除 url-test 的固定）测一遍全部节点"""
    if RETEST["pending"] or time.time() - RETEST["last"] < 60:
        return
    RETEST["pending"] = True

    def run():
        try:
            for _ in range(20):
                time.sleep(wait if _ == 0 else 3)
                if core_alive():
                    break
            fix_global()
            if core_json("/proxies/" + quote(G_MANUAL), timeout=5):
                code, raw = core("GET", f"/group/{quote(G_MANUAL)}/delay?url={quote(HC)}&timeout=5000", timeout=60)
                if active_core() == "singbox":  # sing-box 整组测速只测到一部分节点：缺席的逐个补测（全局限并发）
                    fill_group(G_MANUAL, code, raw, RETEST_BUDGET)
            RETEST["last"] = time.time()
            proxies_merged()
        except Exception as e:
            log_err("定时测速", e)
        finally:
            RETEST["pending"] = False
    threading.Thread(target=run, daemon=True).start()


def bg_save(data_url):
    """data:image/...;base64,xxx → 保存背景图；只接受 JPEG / PNG / WebP / GIF"""
    m = re.match(r"data:image/[\\w.+-]+;base64,(.+)$", str(data_url or ""), re.S)
    if not m:
        return False, "图片格式不正确"
    try:
        raw = base64.b64decode(m.group(1), validate=False)
    except Exception:
        return False, "图片数据无法解析"
    if len(raw) > BG_MAX:
        return False, "图片不能超过 8MB"
    if bg_type(raw[:16]) is None:
        return False, "只支持 JPG / PNG / WebP / GIF"
    _bg_write(raw)
    return True, "背景图已保存"


_BG_LOCK = threading.Lock()


def _bg_write(raw):
    """写背景图：上传与每日刷新 / 链接下载可能同时发生，以前共用 bg.img.tmp，两边各自 truncate + 写同一个文件，
    换上去的可能是两张图拼起来的坏文件（只显示出一部分）"""
    with _BG_LOCK:
        tmp = f"{BG_FILE}.tmp{threading.get_ident()}"
        try:
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, BG_FILE)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


BG_META = BG_FILE + ".json"  # 链接背景图：{"url": 链接, "daily": 每天刷新, "t": 上次下载时间}


def bg_meta():
    try:
        with open(BG_META) as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def bg_meta_save(m):
    try:
        if m:
            tmp = BG_META + ".tmp"
            with open(tmp, "w") as f:
                json.dump(m, f, ensure_ascii=False)
            os.replace(tmp, BG_META)
        elif os.path.exists(BG_META):
            os.remove(BG_META)
    except OSError as e:
        print("bg meta save error", e, flush=True)


def bg_fetch(url):
    """由路由器下载图片链接保存为背景图（先走代理，失败再直连）。必应每日壁纸这类接口会 302 跳到真实图片，urllib 自动跟随"""
    url = str(url or "").strip()
    if not re.match(r"https?://\\S+$", url):
        return False, "请输入 http(s) 开头的图片链接"
    try:
        dl_check(url)  # SSRF 防护：禁止内网目标
    except IOError as e:
        return False, str(e)
    raw, err = None, ""
    for proxy in (True, False):
        # 每次新建 Request：走代理时 urllib 会把代理地址写进 Request，复用会让直连也连到代理端口
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})
        try:
            with proxy_opener(proxy).open(req, timeout=20) as r:
                raw = read_full(r, BG_MAX + 1)
            break
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
        except Exception as e:
            err = str(e)[:120]
    if raw is None:
        return False, "下载失败：" + (err or "无法连接")
    if len(raw) > BG_MAX:
        return False, "图片不能超过 8MB"
    if bg_type(raw[:16]) is None:
        return False, "链接返回的不是 JPG / PNG / WebP / GIF 图片"
    _bg_write(raw)
    return True, "背景图已下载到路由器"


def bg_type(head):
    if head[:3] == b"\\xff\\xd8\\xff":
        return "image/jpeg"
    if head[:8] == b"\\x89PNG\\r\\n\\x1a\\n":
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"GIF8":
        return "image/gif"
    return None


def proxies_merged():
    """/proxies + 订阅节点（mihomo 在 /providers/proxies，sing-box 由面板拼出）合并成一份，并补上缓存的延迟"""
    a = core_json("/proxies", timeout=8)
    if a is None:
        return None
    px = dict(a.get("proxies") or {})
    try:
        pj = prov_json(px)
        prov, prov_ok = pj.get("providers") or {}, bool(pj)  # 核心没应答时 prov_json() 返回 {}
    except Exception:
        prov, prov_ok = {}, False
    for pv in prov.values():
        for n in pv.get("proxies") or []:
            if n and n.get("name"):
                cur = px.get(n["name"])
                if not cur:
                    px[n["name"]] = n
                elif not _hist_last(cur) and _hist_last(n):  # 同名节点：保留有测速记录的那份
                    cur["history"], cur["extra"] = n.get("history") or [], n.get("extra") or {}
    delay_merge(px, prov_ok)
    nodes = [p for p in px.values() if p.get("all") is None and p.get("type") not in ("Direct", "Reject", "RejectDrop", "Pass", "Compatible", "Dns")]
    if nodes and not any("_d" in p for p in nodes):  # 一个延迟都没有（刚启动 / 刚重载）：后台补测
        retest_soon(1)
    return px


def prov_json(px=None):
    """订阅节点（mihomo /providers/proxies 格式）；sing-box 由面板按缓存拼出同样的结构。
    px：调用方刚取到的 /proxies（sing-box 拼结构时复用，不再为同一个请求向核心再要一次几百 KB 的 /proxies）"""
    if active_core() == "singbox":
        return sb_fake_providers(load(), px)
    return core_json("/providers/proxies") or {}


def px_slim(px):
    """/api/px 应答瘦身：每个节点 / 策略组的 history 与各测速地址的 extra.history 只留最后一条（前端只用最后一条）。
    mihomo 每个地址保留最近 10 条记录，500 个节点、2 个测速地址时应答约 1.2 MB（压缩后 34 KB，压缩本身在路由器上也要几十毫秒）；
    瘦身后约 1/5。原地修改：px 是本次请求新解析出来的"""
    for p in px.values():
        if not isinstance(p, dict):
            continue
        h = p.get("history")
        if isinstance(h, list) and len(h) > 1:
            p["history"] = h[-1:]
        ex = p.get("extra")
        if isinstance(ex, dict):
            for k, v in list(ex.items()):
                if isinstance(v, dict) and isinstance(v.get("history"), list) and len(v["history"]) > 1:
                    ex[k] = dict(v, history=v["history"][-1:])
    return px


def check_subs():
    prov = prov_json().get("providers", {})
    names = {s["name"] for s in load()["subs"]}
    st = load_notified()
    changed = False
    now = time.time()
    for name, p in prov.items():
        si = p.get("subscriptionInfo") or {}
        if name not in names or not si:
            continue
        exp = int(si.get("Expire") or 0)
        k = "exp:" + name
        if exp and 0 < exp - now < 3 * 86400 and st.get(k) != exp:
            st[k] = exp
            changed = True
            notify(f"⏰ 订阅「{name}」将于 {time.strftime('%Y-%m-%d %H:%M', time.localtime(exp))} 到期")
        total, used = int(si.get("Total") or 0), int(si.get("Upload") or 0) + int(si.get("Download") or 0)
        k = "traffic:" + name
        high = total > 0 and used / total > 0.9
        if high and not st.get(k):
            notify(f"📶 订阅「{name}」流量已用 {used / total:.0%}（{used / 2**30:.1f} / {total / 2**30:.1f} GB）")
        if high != bool(st.get(k)):
            st[k] = high
            changed = True
    if changed:
        write_json(NOTIFY_FILE, st)


def monitor_loop():
    n = 0
    while True:
        time.sleep(WD_INTERVAL)
        n += 1
        for fn, every in ((watchdog_tick, 1), (check_nodes, 20), (check_subs, 120), (refresh_regions, 20)):
            if n % every == 0 or (fn is check_subs and n == 2):
                try:
                    if fn is watchdog_tick or WD["status"] in ("ok", "off"):
                        fn()
                except Exception as e:
                    print("monitor error", fn.__name__, e, flush=True)
'''
_SRC['shunt.stats'] = '''"""流量统计"""
import copy, threading, time

from .base import STATS_FILE
from .data import read_json, write_json
from .core import conns_snapshot
KEY_MAX = 1000  # 每天按设备 / 节点分别最多记多少个（60 天 × 上千个 IPv6 源地址时 stats.json 和内存会一直涨）
OTHER = "其他"


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = read_json(STATS_FILE, {"days": {}})
        self.data.setdefault("days", {})
        self.conns, self.tot, self.saved = {}, None, time.time()

    def bucket(self, day):
        return self.data["days"].setdefault(day, {"up": 0, "down": 0, "dev": {}, "node": {}})

    def poll(self):
        c = conns_snapshot(timeout=5)
        if c is None:
            self.conns, self.tot = {}, None
            return
        ut, dt = int(c.get("uploadTotal") or 0), int(c.get("downloadTotal") or 0)
        cur = {}
        with self.lock:
            b = self.bucket(time.strftime("%Y-%m-%d"))
            first = self.tot is None
            if not first:
                b["up"] += ut - self.tot[0] if ut >= self.tot[0] else ut  # 核心重启后计数归零
                b["down"] += dt - self.tot[1] if dt >= self.tot[1] else dt
            self.tot = (ut, dt)
            for x in c.get("connections") or []:
                up, dn = int(x.get("upload") or 0), int(x.get("download") or 0)
                cur[x["id"]] = (up, dn)
                if first:
                    continue
                pu, pd = self.conns.get(x["id"], (0, 0))
                du, dd = max(0, up - pu), max(0, dn - pd)
                if not du and not dd:
                    continue
                src = (x.get("metadata") or {}).get("sourceIP") or "?"
                node = (x.get("chains") or ["DIRECT"])[0]
                for key, k in (("dev", src), ("node", node)):
                    m = b[key]
                    if k not in m and len(m) >= KEY_MAX:  # IPv6 隐私地址 / 大量订阅节点：每天的条目数封顶，其余并入「其他」
                        k = OTHER
                    e = m.setdefault(k, [0, 0])
                    e[0] += du
                    e[1] += dd
            self.conns = cur
        if time.time() - self.saved > 60:
            self.flush()

    def flush(self):
        with self.lock:
            keep = sorted(self.data["days"])[-60:]
            self.data["days"] = {k: self.data["days"][k] for k in keep}
            write_json(STATS_FILE, self.data)
        self.saved = time.time()

    def report(self):
        with self.lock:
            days = copy.deepcopy(self.data["days"])
        today, month = time.strftime("%Y-%m-%d"), time.strftime("%Y-%m")

        def agg(keys):
            r = {"up": 0, "down": 0, "dev": {}, "node": {}}
            for k in keys:
                b = days[k]
                r["up"] += b["up"]
                r["down"] += b["down"]
                for key in ("dev", "node"):
                    for n, (u, dn) in b[key].items():
                        e = r[key].setdefault(n, [0, 0])
                        e[0] += u
                        e[1] += dn
            for key in ("dev", "node"):
                r[key] = sorted(([n, u, dn] for n, (u, dn) in r[key].items()), key=lambda x: -(x[1] + x[2]))[:100]
            return r
        return {"today": agg([k for k in days if k == today]), "month": agg([k for k in days if k.startswith(month)]),
                "days": [{"date": k, "up": days[k]["up"], "down": days[k]["down"]} for k in sorted(days)],
                "arp": arp_table()}

    def loop(self):
        while True:
            try:
                self.poll()
            except Exception as e:
                print("stats error", e, flush=True)
            time.sleep(5)


def arp_table():
    out = {}
    try:
        with open("/proc/net/arp") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if len(p) >= 4 and p[3] != "00:00:00:00:00:00":
                    out[p[0]] = p[3]
    except OSError:
        pass
    return out


STATS = None
'''
_SRC['shunt.unlock'] = '''"""流媒体解锁检测"""
import http.client, re, threading, urllib.request, urllib.error

from .base import UA
from .sysopt import proxy_opener
def fetch(url, timeout=10, headers=None):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    try:
        with proxy_opener().open(req, timeout=timeout) as r:
            try:
                raw = r.read(600000)
            except http.client.IncompleteRead as e:
                raw = e.partial
            return r.status, r.geturl(), raw.decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200000).decode("utf-8", "ignore")
        except Exception:
            body = ""
        return e.code, e.geturl() if hasattr(e, "geturl") else url, body


def cf_loc(host):
    try:
        _, _, b = fetch(f"https://{host}/cdn-cgi/trace", timeout=8)
        m = re.search(r"^loc=([A-Z]{2})", b, re.M)
        ip = re.search(r"^ip=(\\S+)", b, re.M)
        return (m.group(1) if m else ""), (ip.group(1) if ip else "")
    except Exception:
        return "", ""


def ck_egress():
    loc, ip = cf_loc("www.cloudflare.com")
    return {"ok": bool(loc), "text": ip or "无法获取", "region": loc}


def ck_netflix():
    code, url, body = fetch("https://www.netflix.com/title/81280792")
    m = re.search(r'"requestCountry":\\{"id":"([A-Z]{2})"', body) or re.search(r'"country":"([A-Z]{2})"', body) \\
        or re.search(r"netflix\\.com/([a-z]{2})(?:-[a-z]{2})?/title", url)
    region = (m.group(1).upper() if m else "")
    if code == 200:
        return {"ok": True, "text": "完整解锁", "region": region}
    if code == 404:
        return {"ok": "partial", "text": "仅自制剧", "region": region}
    if code == 403:
        return {"ok": False, "text": "不可用（403）"}
    return {"ok": False, "text": f"未知（HTTP {code}）"}


def ck_chatgpt():
    _, _, b1 = fetch("https://ios.chat.openai.com/")
    code2, _, b2 = fetch("https://chatgpt.com/")
    region, _ = cf_loc("chatgpt.com")
    text = b1 + b2
    if "unsupported_country" in text:
        return {"ok": False, "text": "地区不支持", "region": region}
    if re.search(r"VPN|blocked|Access denied", b1, re.I) and "cf-" not in b1[:2000]:
        return {"ok": False, "text": "IP 被封锁", "region": region}
    if code2 in (200, 403) and region:
        return {"ok": True, "text": "可用", "region": region}
    return {"ok": False, "text": f"未知（HTTP {code2}）", "region": region}


def ck_disney():
    code, url, body = fetch("https://www.disneyplus.com/")
    if "unavailable" in url or "not available in your region" in body.lower():
        return {"ok": False, "text": "地区不支持"}
    m = re.search(r'"countryCode"\\s*:\\s*"([A-Z]{2})"', body) or re.search(r'Region:\\s*([A-Z]{2})', body) \\
        or re.search(r"disneyplus\\.com/(?:[a-z]{2}-)?([a-z]{2})(?:/|$)", url)
    region = m.group(1).upper() if m else ""
    if code == 200:
        return {"ok": True, "text": "可访问" if not region else "可用", "region": region}
    return {"ok": False, "text": f"不可用（HTTP {code}）", "region": region}


def ck_youtube():
    code, url, body = fetch("https://www.youtube.com/premium", headers={"Cookie": "CONSENT=YES+1"})
    if "www.google.cn" in body:
        return {"ok": False, "text": "不可用", "region": "CN"}
    m = re.search(r'"countryCode":"([A-Z]{2})"', body) or re.search(r'"INNERTUBE_CONTEXT_GL":"([A-Z]{2})"', body)
    region = m.group(1) if m else ""
    if "Premium is not available in your country" in body:
        return {"ok": False, "text": "Premium 不可用", "region": region}
    if code == 200 and ("ad-free" in body.lower() or "premium" in body.lower()):
        return {"ok": True, "text": "Premium 可用", "region": region}
    return {"ok": False, "text": f"未知（HTTP {code}）", "region": region}


def ck_gemini():
    code, _, body = fetch("https://gemini.google.com/")
    if re.search(r'45631641(?:,null,true|\\\\?":true)', body):
        return {"ok": True, "text": "可用"}
    if code == 200:
        return {"ok": False, "text": "地区可能不支持"}
    return {"ok": False, "text": f"不可用（HTTP {code}）"}


UNLOCK = [("出口位置", ck_egress), ("Netflix", ck_netflix), ("ChatGPT", ck_chatgpt), ("Disney+", ck_disney),
          ("YouTube Premium", ck_youtube), ("Gemini", ck_gemini)]


def unlock_test():
    out = [None] * len(UNLOCK)

    def one(i, name, fn):
        try:
            r = fn()
        except Exception as e:
            r = {"ok": False, "text": "检测失败：" + (str(e)[:60] or e.__class__.__name__)}
        r["name"] = name
        out[i] = r
    ths = [threading.Thread(target=one, args=(i, n, f)) for i, (n, f) in enumerate(UNLOCK)]
    [t.start() for t in ths]
    [t.join(30) for t in ths]
    return [o or {"name": UNLOCK[i][0], "ok": False, "text": "超时"} for i, o in enumerate(out)]
'''
_SRC['shunt.speedtest'] = '''"""「测速」页：国内（直连）/ 国外（经代理）带宽测速

- 国内：不走代理，直连下载国内大厂 CDN 上的大文件（腾讯、360），延迟测百度（与概览「直连」同一口径）
- 国外：经 mixed 端口按当前规则走代理，下载 Cloudflare 测速文件，延迟测 Google generate_204
- 每次多条连接并发下载 10 秒（单连接测不满百兆以上的宽带），文件读完就重新请求，结果是 10 秒内的平均速度，另记每秒的峰值
- 国内 12 条连接，同时分摊到所有能连上的下载源：腾讯等国内 CDN 对单条连接限速（约 25M），v8.7.0 只开 4 条连一个源时会卡在 100M 左右
- 国外 4 条连接，只用第一个能连上的下载源（经代理，多源没有意义）
- 测速在后台线程跑，前端轮询 GET /api/speed 显示实时速度；两项不能同时测（会互相抢带宽），同一时间只跑一个
- 最近 30 次结果保存在 speed_hist.json
"""
import os, threading, time, urllib.request

from .errlog import log_err
from .base import PANEL_DIR, UA
from .data import read_json, write_json
from .sysopt import direct_rtt, proxy_opener, site_rtt

HIST_FILE = os.path.join(PANEL_DIR, "speed_hist.json")
HIST_N = 30
DURATION = 10      # 下载计时（秒）
CONNS = 4          # 默认并发连接数（各项可用 conns 覆盖）
CHUNK = 1 << 16
KINDS = {
    "cn": {"name": "国内", "proxy": False, "ping": None, "conns": 12, "multi": True, "urls": [
        "https://dldir1.qq.com/weixin/Windows/WeChatSetup.exe",
        "https://down.360safe.com/se/360se_setup.exe",
        "https://dldir1v6.qq.com/weixin/Windows/WeChatSetup.exe",
    ]},
    "intl": {"name": "国外", "proxy": True, "ping": "https://www.google.com/generate_204", "conns": 4, "multi": False, "urls": [
        "https://speed.cloudflare.com/__down?bytes=50000000",  # Cloudflare 单次上限 50MB，读完重新请求
        "http://cachefly.cachefly.net/100mb.test",
    ]},
}
LOCK = threading.Lock()
JOB = {"kind": "", "running": False, "phase": "", "start": 0, "bytes": 0, "mbps": 0, "ms": None, "src": "", "result": {}}


def _host(u):
    return u.split("/")[2]


def _open(url, proxy, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity", "Cache-Control": "no-cache"})
    return proxy_opener(proxy).open(req, timeout=timeout)


def _probe(u, proxy):
    try:
        r = _open(u, proxy)
        if r.status == 200:
            return u, r, ""
        r.close()
        return u, None, f"{_host(u)} HTTP {r.status}"
    except Exception as e:
        return u, None, f"{_host(u)}：{str(e)[:80]}"


def _pick(k):
    """返回能连上的下载源 [(url, 已打开的响应)] 和最后的错误。multi 的项同时试所有源、全部保留（按列表顺序）；
    否则依次试，取第一个能连上的"""
    err = "无可用下载源"
    if not k.get("multi"):
        for u in k["urls"]:
            u, r, e = _probe(u, k["proxy"])
            if r:
                return [(u, r)], ""
            err = e
        return [], err
    res = [None] * len(k["urls"])
    lk, late = threading.Lock(), [False]

    def one(i, u):
        r = _probe(u, k["proxy"])
        with lk:
            if not late[0]:
                res[i] = r
                return
        if r[1]:  # 10 秒后才连上的源：已经没人用它了，关掉响应，不留一条一直挂着的下载连接
            try:
                r[1].close()
            except Exception:
                pass
    ths = [threading.Thread(target=one, args=(i, u), daemon=True) for i, u in enumerate(k["urls"])]
    [t.start() for t in ths]
    end = time.time() + 10
    [t.join(max(0, end - time.time())) for t in ths]  # 合计最多等 10 秒（以前每个源各等 10 秒，最坏 30 秒）
    with lk:
        late[0] = True
    ok = [(u, r) for u, r, e in filter(None, res) if r]
    errs = [e for u, r, e in filter(None, res) if not r]
    return ok, (errs[-1] if errs else err)


def _ping(k):
    try:
        if k["proxy"]:
            ms = site_rtt(k["ping"])
        else:
            ms = direct_rtt()
        return ms if ms and ms > 0 else -1
    except Exception:
        return -1


def run(kind, duration=DURATION, conns=None):
    """同步测一次，返回结果字典（后台线程里调用；单元测试直接调用）"""
    k = KINDS[kind]
    conns = conns or k.get("conns") or CONNS
    JOB.update(phase="ping", bytes=0, mbps=0, ms=None, src="")
    ms = _ping(k)
    JOB.update(ms=ms, phase="connect")
    srcs, err = _pick(k)
    if not srcs:
        return {"ok": False, "kind": kind, "t": int(time.time()), "ms": ms, "error": err}
    src = " + ".join(dict.fromkeys(_host(u) for u, _ in srcs))
    got = [0]
    lock = threading.Lock()
    t0 = time.time()
    JOB.update(src=src, phase="download", start=t0)
    end = t0 + duration
    errs = []

    def worker(url, r):
        while time.time() < end:
            try:
                if r is None:
                    r = _open(url, k["proxy"])
                while time.time() < end:
                    b = r.read(CHUNK)
                    if not b:
                        break
                    with lock:
                        got[0] += len(b)
            except Exception as e:
                errs.append(str(e)[:80])
                if len(errs) > conns * 3:
                    return
                time.sleep(0.3)
            finally:
                try:
                    r and r.close()
                except Exception:
                    pass
                r = None

    # 连接轮流分给各下载源；探测时打开的响应直接交给前几条连接用
    ths = [threading.Thread(target=worker, args=(srcs[i % len(srcs)][0], srcs[i][1] if i < len(srcs) else None), daemon=True) for i in range(conns)]
    [t.start() for t in ths]
    peak, last_b, last_t = 0.0, 0, t0
    while any(t.is_alive() for t in ths):
        time.sleep(0.5)
        now, n = time.time(), got[0]
        if now - last_t >= 1:
            peak = max(peak, (n - last_b) * 8 / (now - last_t) / 1e6)
            last_b, last_t = n, now
        JOB.update(bytes=n, mbps=round(n * 8 / max(now - t0, 0.001) / 1e6, 1))
        if now > end + 5:  # 卡住的连接不再等
            break
    dt = min(max(time.time() - t0, 0.001), duration)
    n = got[0]
    mbps = round(n * 8 / dt / 1e6, 1)
    res = {"ok": n > 0, "kind": kind, "t": int(time.time()), "ms": ms, "mbps": mbps, "peak": round(max(peak, mbps), 1),
           "mb": round(n / 1e6, 1), "src": src, "conns": conns}
    if not n:
        res["error"] = errs[-1] if errs else "没有收到数据"
    return res


def hist():
    h = read_json(HIST_FILE, [])
    return h if isinstance(h, list) else []


def _save(res):
    h = (hist() + [res])[-HIST_N:]
    try:
        write_json(HIST_FILE, h)
    except OSError as e:
        log_err("speedtest", e)


def _job(kinds):
    try:
        for kind in kinds:
            JOB.update(kind=kind, phase="ping", bytes=0, mbps=0, ms=None, src="")
            try:
                res = run(kind)
            except Exception as e:
                log_err("speedtest", e)
                res = {"ok": False, "kind": kind, "t": int(time.time()), "error": str(e)[:120]}
            JOB["result"][kind] = res
            _save(res)
    finally:
        JOB.update(running=False, kind="", phase="")


def start(kind):
    """kind：cn / intl / all（先国内后国外）。已在测速时不重复开始，返回 False"""
    kinds = ["cn", "intl"] if kind == "all" else [kind] if kind in KINDS else []
    if not kinds:
        return False
    with LOCK:
        if JOB["running"]:
            return False
        JOB.update(running=True, kind=kinds[0], phase="ping", start=time.time(), bytes=0, mbps=0, ms=None, src="")
        for k in kinds:
            JOB["result"].pop(k, None)
    threading.Thread(target=_job, args=(kinds,), daemon=True).start()
    return True


def status():
    h = hist()
    last = {}
    for r in h:
        last[r.get("kind")] = r
    last.update(JOB["result"])
    return {"running": JOB["running"], "kind": JOB["kind"], "phase": JOB["phase"], "elapsed": round(time.time() - JOB["start"], 1) if JOB["running"] else 0,
            "duration": DURATION, "live": {"mbps": JOB["mbps"], "ms": JOB["ms"], "mb": round(JOB["bytes"] / 1e6, 1), "src": JOB["src"]},
            "last": {k: last.get(k) for k in KINDS}, "hist": h[::-1]}
'''
_SRC['shunt.ruletest'] = '''"""规则测试"""
import ipaddress, json, re, socket, threading, time
from urllib.parse import quote

from .base import MIXED
from .corehttp import core_json
from .logstream import core_stream
def clean_target(t):
    t = t.strip()
    t = re.sub(r"^[a-z][a-z0-9+.\\-]*://", "", t, flags=re.I).split("/")[0].split("?")[0].split("#")[0]
    t = t.rpartition("@")[2]  # http://user:pw@host/ 里的用户信息不是目标（以前把「user:pw@host」整个当域名测）
    if t.startswith("["):
        return t[1:].split("]")[0]
    if t.count(":") == 1:
        t = t.split(":")[0]
    return t.lower().rstrip(".")  # 「example.com.」（FQDN 写法）与 example.com 是同一个域名


def is_ip(s):
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def chain_of(name, proxies):
    out, seen = [name], {name}
    while proxies.get(out[-1], {}).get("now") and proxies[out[-1]]["now"] not in seen:
        out.append(proxies[out[-1]]["now"])
        seen.add(out[-1])
    return out


def live_rule(host):
    """经 mixed 端口对目标发起 CONNECT，同时监听核心日志：
    优先从 /connections 读取命中规则与链路；节点不通时从日志里的 "dial X (match Rule/payload)" 读取"""
    logs, stop = [], {}

    def reader():
        try:
            core_stream("/logs?level=info", logs.append, stop)
        except Exception:
            pass
    threading.Thread(target=reader, daemon=True).start()
    time.sleep(0.3)
    s = None
    try:  # 连不上 mixed 端口（核心没在跑 / 端口被占）也要走 finally 停掉日志线程：以前每测一次就泄漏一个永远挂着的日志连接
        s = socket.create_connection(("127.0.0.1", MIXED), timeout=8)
        hp = f"[{host}]:443" if ":" in host else f"{host}:443"
        port = str(s.getsockname()[1])
        s.sendall(f"CONNECT {hp} HTTP/1.1\\r\\nHost: {hp}\\r\\n\\r\\n".encode())
        try:
            first = s.recv(256).decode("latin1").split("\\r\\n")[0]
        except socket.timeout:
            first = "(等待超时)"
        src = re.compile(r"(?:127\\.0\\.0\\.1|\\[?::1\\]?):" + port + r"\\b")
        for _ in range(10):
            c = core_json("/connections") or {}
            for x in c.get("connections") or []:
                m = x.get("metadata") or {}
                if str(m.get("sourcePort")) == port and m.get("sourceIP") in ("127.0.0.1", "::1"):
                    return {"method": "live", "rule": x.get("rule"), "payload": x.get("rulePayload", ""),
                            "chain": list(reversed(x.get("chains") or [])), "note": "连接已建立"}
            for line in list(logs):
                try:
                    msg = json.loads(line).get("payload", "")
                except Exception:
                    continue
                if not src.search(msg):
                    continue
                m = re.search(r"dial (.+?) \\(match (\\w+)/(.*?)\\) .*? error: (.*)", msg)
                if m:
                    proxies = (core_json("/proxies") or {}).get("proxies") or {}
                    return {"method": "live", "rule": m.group(2), "payload": m.group(3),
                            "chain": chain_of(m.group(1), proxies), "note": "节点连接失败：" + m.group(4)[:200]}
                m = re.search(r"match (\\w+)\\((.*?)\\) using (.+)$", msg)
                if m:
                    proxies = (core_json("/proxies") or {}).get("proxies") or {}
                    pol = m.group(3).split("[")[0].strip()
                    return {"method": "live", "rule": m.group(1), "payload": m.group(2),
                            "chain": chain_of(pol, proxies), "note": "连接已结束"}
            time.sleep(0.3)
        return {"error": "未捕获到连接（" + first + "）"}
    finally:
        if s is not None:
            s.close()
        stop["stop"] = True
        try:
            stop["sock"].shutdown(socket.SHUT_RDWR)
        except (KeyError, OSError):
            pass


def static_rule(host):
    rules = (core_json("/rules") or {}).get("rules") or []
    proxies = (core_json("/proxies") or {}).get("proxies") or {}
    ips, uncertain = None, []

    def get_ips():
        nonlocal ips
        if ips is None:
            if is_ip(host):
                ips = [ipaddress.ip_address(host)]
            else:  # 经 mihomo 的 DNS 解析（按 nameserver-policy 分流），不用本机系统 DNS，避免把国外域名泄露给本机上游
                ips = []
                for qt in ("A", "AAAA"):
                    j = core_json(f"/dns/query?name={quote(host)}&type={qt}", timeout=8) or {}
                    for a in j.get("Answer") or []:
                        if a.get("type") in (1, 28) and is_ip(str(a.get("data"))):
                            ips.append(ipaddress.ip_address(a["data"]))
        return ips

    for r in rules:
        t = re.sub(r"[-_]", "", str(r.get("type", ""))).lower()
        pl = str(r.get("payload", ""))
        hit = None
        if t == "domain":
            hit = host == pl.lower()
        elif t == "domainsuffix":
            hit = host == pl.lower() or host.endswith("." + pl.lower())
        elif t == "domainkeyword":
            hit = pl.lower() in host
        elif t == "domainregex":
            try:
                hit = bool(re.search(pl, host))
            except re.error:
                hit = None
        elif t in ("ipcidr", "ipcidr6"):
            try:
                net = ipaddress.ip_network(pl, strict=False)
                hit = any(i in net for i in get_ips() if i.version == net.version)
            except ValueError:
                hit = None
        elif t == "geoip" and pl.lower() in ("private", "lan"):
            hit = any(i.is_private for i in get_ips())
        elif t == "dstport":
            hit = False
            for seg in re.split(r"[/,]", pl):
                a, _, z = seg.partition("-")
                z = z.strip() or a
                if a.strip().isdigit() and z.isdigit() and int(a) <= 443 <= int(z):
                    hit = True
        elif t == "match":
            hit = True
        geo_lan = t == "geoip" and pl.lower() in ("private", "lan")  # 上面已按解析出的 IP 判断过，不再列为「无法确定」
        if hit is None or (t in ("geosite", "geoip", "ruleset", "and", "or", "not", "srcipcidr", "processname",
                                "processpath", "inport", "intype", "network", "srcport", "inname", "inuser") and not geo_lan):
            if len(uncertain) < 8:
                uncertain.append(f"{r.get('type')},{pl} → {r.get('proxy')}")
            continue
        if hit:
            return {"method": "static", "rule": r.get("type"), "payload": pl, "chain": chain_of(r.get("proxy"), proxies),
                    "uncertain": uncertain}
    return {"method": "static", "rule": "-", "payload": "", "chain": [], "uncertain": uncertain}


def rule_test(target):
    host = clean_target(target)
    if not host:
        raise ValueError("请输入域名或 IP")
    res, live_err = None, ""
    try:
        res = live_rule(host)
        if "error" in res:
            live_err, res = res["error"], None
    except Exception as e:
        live_err = str(e)
    if res is None:
        res = static_rule(host)
        res["live_error"] = live_err
    res["host"] = host
    return res

'''
_SRC['shunt.logstream'] = '''"""实时日志 (SSE)"""
import secrets, socket, time

from .base import CTRL_HOST, CTRL_PORT
from .data import load

TICKETS = {}  # 日志流（EventSource 无法带请求头）用的一次性票据


def core_stream(path, sink, stop, idle=None):
    """以原始 socket 读取 mihomo 的 chunked JSON 行流，逐行回调 sink；stop 被置位或连接断开时结束。
    idle: 超过该秒数没有数据视为断线（/traffic /memory 每秒推送，可用来发现半开连接）"""
    d = load()
    s = socket.create_connection((CTRL_HOST, CTRL_PORT), timeout=10)
    stop["sock"] = s
    if stop.get("stop"):  # 调用方在连接建立前就要求停止（浏览器已关掉日志页）：它的 shutdown 落空了，这里不能再无限期读下去
        s.close()
        return
    try:  # 任何路径（正常结束、非 200、解析出错）都关掉 socket，不靠引用计数回收
        s.settimeout(idle)
        s.sendall(f"GET {path} HTTP/1.1\\r\\nHost: {CTRL_HOST}\\r\\nAuthorization: Bearer {d['secret']}\\r\\nConnection: close\\r\\n\\r\\n".encode())
        f = s.makefile("rb")
        status = f.readline().decode("latin1")
        chunked = False
        while True:
            h = f.readline()
            if h in (b"\\r\\n", b"\\n", b""):
                break
            if h.lower().startswith(b"transfer-encoding:") and b"chunked" in h.lower():
                chunked = True
        if " 200" not in status:
            raise IOError("mihomo 返回 " + status.strip())
        buf = b""
        while not stop.get("stop"):
            if chunked:
                size_line = f.readline()
                if not size_line:
                    break
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    break
                data = f.read(size)
                f.readline()
            else:
                data = f.readline()
                if not data:
                    break
            buf += data
            while b"\\n" in buf:
                line, buf = buf.split(b"\\n", 1)
                if line.strip():
                    sink(line.decode("utf-8", "ignore"))
    finally:
        s.close()


def ticket_new():
    """日志流（EventSource 无法带请求头）用的一次性票据，60 秒有效"""
    now = time.time()
    for k in [k for k, v in TICKETS.items() if v <= now]:
        TICKETS.pop(k, None)
    t = secrets.token_urlsafe(18)
    TICKETS[t] = now + 60
    return t


def ticket_use(t):
    exp = TICKETS.pop(str(t or ""), 0)
    return exp > time.time()
'''
_SRC['shunt.adblock'] = '''"""广告拦截（AdGuard Home 风格的域名过滤）"""
import copy, hashlib, ipaddress, json, os, re, secrets, threading, time, urllib.request, urllib.error
from collections import deque
from urllib.parse import quote

from .errlog import log_err
from .base import AB_DIR, AB_STATS_FILE, CONF_DIR, FAKEIP_NETS, UA, dl_check, read_full, udp_dns_query
from .data import load, read_json, write_json
from .mihomo import build_config
from .core import reload_core
from .corehttp import core
from .sb_common import active_core
from .notify import core_alive
from .sb_config import sb_sync_assets
from .sysopt import proxy_opener
from .ruletest import clean_target
from .logstream import core_stream
from .ruleutil import DOMAIN_RX, adblock_providers, idna_domain, strip_comment
# 订阅 AdGuard/ABP（||domain^、@@||domain^）、hosts（0.0.0.0 domain）与纯域名列表，转换为 mihomo
# behavior=domain / format=text 的本地 rule-provider 文件：
#   ||a.com^ → "+.a.com"（含子域）；hosts / 纯域名 → "a.com"（仅该域名，与 AdGuard Home 语义一致）
# 规则：AND,((RULE-SET,ad-xxx),(NOT,((RULE-SET,ad-allow)))),REJECT  —— 白名单优先，且被放行的域名继续走正常分流
CLASH_RX = re.compile(r"^(domain|domain-suffix),([^,\\s]+)(?:,.*)?$")
# ||a.com^ 拦截 a.com 及子域；|a.com^（AdGuard DNS 语法）只拦截 a.com 本身
ABP_RX = re.compile(r"^(@@)?(\\|\\|?)([^\\^/|$]+)\\^\\|?(?:\\$(.*))?$")
HOSTS_IPS = {"0.0.0.0", "127.0.0.1", "::", "::1", "0:0:0:0:0:0:0:0", "::0", "0", "255.255.255.255"}
HOSTS_SKIP = {"localhost", "localhost.localdomain", "local", "broadcasthost", "ip6-localhost", "ip6-loopback",
              "ip6-localnet", "ip6-mcastprefix", "ip6-allnodes", "ip6-allrouters", "ip6-allhosts", "0.0.0.0"}
AB_STATE = {"updating": False, "msg": "", "last": 0}
AB_LOCK = threading.Lock()


def fmt_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def list_id(url):
    return hashlib.md5(url.strip().encode()).hexdigest()[:8]


def norm_domain(s):
    s = idna_domain(s.strip()).lower().rstrip(".")
    if not s or "*" in s or not DOMAIN_RX.match(s):
        return None
    return s


# 元素隐藏 / 脚本注入规则（example.com##.ad、#@#、#?#、#$#、#%#）：只作用于页面元素，不能当成整站拦截
COSMETIC_RX = re.compile(r"#[@$?%]{0,3}#")
AB_PARSER = 4  # 解析器版本：变了就让已下载的列表尽快按新规则重新转换（见 schedule.ab_last_update）


AB_MODS = {"important", "badfilter"}  # 能用域名规则等价表达的修饰符；其余（$client= / $dnstype= / $denyallow= …）整条跳过


def parse_filter(text, plain_suffix=False, imp=None):
    """解析过滤列表文本。返回 (block_suffix, block_exact, allow_suffix, allow_exact, skipped)。
    - 行内注释（「||a.com^ # 说明」「a.com ! 说明」）去掉后照常解析，以前整行被当成无法识别跳过
    - 中文域名转成 punycode
    - $badfilter：取消本列表里同一条规则（同样的 @@、|| / |、域名和 $important；只看本列表）
    - $important：拦截规则照常放进 block，并另记到 imp["bs"] / imp["be"]，列表的 @@ 例外压不住它（见 ab_compose）；
      @@…$important 例外照常放进 allow，并记到 imp["as"] / imp["ae"]，连 $important 拦截也能放行。imp 为 None 时不收集"""
    bs, be, as_, ae = set(), set(), set(), set()
    skipped = 0
    abp = []  # [(allow, exact, dom, important)]：$badfilter 可能写在被取消的规则后面，最后统一处理
    bad = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "!#[":
            continue
        line = strip_comment(line)
        low = line.lower()
        if low.startswith("payload:"):
            continue
        if COSMETIC_RX.search(low):  # 以前按 # 截断后把「baike.baidu.com##.ad」当成拦截整个 baike.baidu.com
            skipped += 1
            continue
        if low.startswith("- "):  # Clash YAML payload：  - DOMAIN-SUFFIX,a.com / - '+.a.com'
            low = low[2:].strip().strip("'\\"")
            if not low:
                continue
        cm = CLASH_RX.match(low)
        if cm:  # Clash 规则列表：DOMAIN / DOMAIN-SUFFIX 可用，KEYWORD / IP 类无法用域名列表表达
            dom = norm_domain(cm.group(2))
            if dom:
                (bs if cm.group(1) == "domain-suffix" else be).add(dom)
            else:
                skipped += 1
            continue
        if re.match(r"^(?:domain-keyword|domain-regex|ip-cidr6?|ip-asn|geoip|geosite|process-name|user-agent|url-regex|dst-port|src-ip-cidr),", low):
            skipped += 1
            continue
        if low.startswith("|") or low.startswith("@@"):
            m = ABP_RX.match(low)
            if not m:
                # 允许 @@domain / @@|domain^ 之类的简写出现在用户白名单里
                if plain_suffix and low.startswith("@@") and norm_domain(low[2:].strip("|^")):
                    as_.add(norm_domain(low[2:].strip("|^")))
                else:
                    skipped += 1
                continue
            allow, pipes, dom, mods = m.groups()
            mset = {x.strip() for x in (mods or "").split(",")} - {""}
            if mset - AB_MODS:
                skipped += 1  # $client= / $dnstype= / $denyallow= 等修饰符无法用域名规则等价表达
                continue
            dom = norm_domain(dom)
            if not dom:
                skipped += 1
                continue
            # 以前 |a.com^ / @@|a.com^ 被当成无法识别跳过，allow_exact 永远为空；用户白名单里的 @@|a.com^ 仍按后缀（兼容旧行为）
            exact = pipes == "|" and not plain_suffix
            key = (bool(allow), exact, dom, "important" in mset)
            if "badfilter" in mset:
                bad.add(key)
            else:
                abp.append(key)
            continue
        if low[0] in "|/" or any(c in low for c in "$^*/\\\\"):
            skipped += 1  # 正则、URL 规则、通配符等
            continue
        parts = low.split("#", 1)[0].split()
        if len(parts) >= 2 and parts[0] in HOSTS_IPS:
            for h in parts[1:]:
                dom = norm_domain(h)
                if dom and dom not in HOSTS_SKIP:
                    be.add(dom)
                elif h not in HOSTS_SKIP:
                    skipped += 1
        elif len(parts) == 1:
            dom = norm_domain(parts[0].lstrip("+."))
            if not dom:
                skipped += 1
            elif plain_suffix or parts[0].startswith("+.") or parts[0].startswith("."):
                bs.add(dom)
            else:
                be.add(dom)
        else:
            skipped += 1
    for key in abp:
        if key in bad:
            continue
        allow, exact, dom, important = key
        if allow:
            (ae if exact else as_).add(dom)
        else:
            (be if exact else bs).add(dom)
        if important and imp is not None:
            imp.setdefault(("a" if allow else "b") + ("e" if exact else "s"), set()).add(dom)
    return bs, be, as_, ae, skipped


def parents(dom, include_self):
    parts = dom.split(".")
    for i in range(0 if include_self else 1, len(parts)):
        yield ".".join(parts[i:])


def prune(suffix, exact):
    """去掉已被上级 +. 规则覆盖的条目"""
    suffix2 = {s for s in suffix if not any(p in suffix for p in parents(s, False))}
    exact2 = {e for e in exact if not any(p in suffix for p in parents(e, True))}
    return suffix2, exact2


def domain_lines(suffix, exact):
    suffix, exact = prune(suffix, exact)
    return sorted(["+." + x for x in suffix] + list(exact)), len(suffix) + len(exact)


def write_lines(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        f.write("\\n".join(lines) + ("\\n" if lines else ""))
    os.replace(path + ".tmp", path)


def ab_meta():
    return read_json(os.path.join(AB_DIR, "meta.json"), {})


def download(url, timeout=60, limit=40 << 20, gh=None):
    """依次尝试：GitHub 加速地址（设置了且是 GitHub 链接时）→ 经代理（mixed 端口，核心在跑时）→ 直连。
    全部失败时把每条路的原因都报出来（以前只报最后一条：国内直连 raw.githubusercontent.com 被 DNS 污染成 0.0.0.0，
    显示成「目标地址非公网，已拦截（SSRF 防护）」，真正的代理失败原因被盖掉）"""
    if gh is None:
        try:
            gh = (load().get("gh_proxy") or "").rstrip("/")
        except Exception:
            gh = ""
    gh = gh.rstrip("/")
    urls = ([gh + "/" + url] if gh and "github" in url else []) + [url]
    alive = core_alive()
    errs = []
    for u in urls:
        for use_proxy in ((True, False) if alive else (False,)):
            how = ("加速" if u != url else "") + ("经代理" if use_proxy else "直连")
            try:
                dl_check(u, via_proxy=use_proxy)  # SSRF 防护：直连时校验 DNS，走代理时由代理解析（字面内网 IP 永远拦截）
            except IOError as e:
                msg = str(e)
                if not use_proxy and "SSRF" in msg:
                    msg = "DNS 解析到非公网地址（可能被污染）"
                errs.append(f"{how}：{msg[:100]}")
                continue
            try:
                req = urllib.request.Request(u, headers={"User-Agent": UA})
                with proxy_opener(use_proxy).open(req, timeout=timeout) as r:
                    data = read_full(r, limit + 1)
                    if len(data) > limit:
                        raise ValueError("文件过大（超过 40MB）")
                    return data.decode("utf-8", "ignore")
            except Exception as e:
                errs.append(f"{how}：{str(e)[:100]}")
    if not alive:
        errs.append("核心未运行，未走代理")
    raise IOError("；".join(errs)[:300] or "下载失败")


def ab_fetch(lst, late=None, sfx=".new"):
    """下载并转换一个列表，写入 <id>.txt / <id>.allow.txt，返回 meta。late 置位（整体超时）后不再写文件"""
    text = download(lst["url"])
    imp = {}
    bs, be, as_, ae, skipped = parse_filter(text, imp=imp)
    lines, n = domain_lines(bs, be)
    alines, na = domain_lines(as_, ae)
    if not n and not na:
        raise ValueError("没有解析出任何域名，确认是 AdGuard / hosts / 域名列表格式")
    if late is not None and late.is_set():
        raise IOError("下载超时")
    sfx = sfx if late is not None else ""  # 并发更新：先写 .new，由调用方在锁内确认没超时再换上
    write_lines(os.path.join(AB_DIR, lst["id"] + ".txt" + sfx), lines)
    write_lines(os.path.join(AB_DIR, lst["id"] + ".allow.txt" + sfx), alines)
    # $important 拦截 / @@…$important 例外：单独存一份，由 ab_compose 汇总成 important.txt / allow-important.txt
    write_lines(os.path.join(AB_DIR, lst["id"] + ".imp.txt" + sfx), domain_lines(imp.get("bs", set()), imp.get("be", set()))[0])
    write_lines(os.path.join(AB_DIR, lst["id"] + ".impallow.txt" + sfx), domain_lines(imp.get("as", set()), imp.get("ae", set()))[0])
    return {"count": n, "allow": na, "skipped": skipped, "updated": int(time.time()), "error": "", "parser": AB_PARSER}


AB_LIST_FILES = (".txt", ".allow.txt", ".imp.txt", ".impallow.txt")  # 每个订阅列表转换出的文件


# 分范围的自定义黑名单：(设置键, 文件名)；对应 rule-provider ad-customcn / ad-customproxy
AB_SCOPED_BLACK = (("black_cn", "custom-cn.txt"), ("black_proxy", "custom-proxy.txt"))


def _read_domains(path, suffix, exact):
    """读 domain_lines 写出的文件（+.a.com / a.com）并入两个集合；文件不存在忽略"""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("+."):
                    suffix.add(line[2:])
                elif line:
                    exact.add(line)
    except OSError:
        pass


def ab_compose(d):
    """根据用户黑/白名单与各列表的 @@ 规则生成 custom.txt / allow.txt，
    以及 $important：important.txt（不限范围的已启用列表里的 $important 拦截）和 allow-important.txt
    （能压过 $important 的放行：用户白名单 + 列表里的 @@…$important）。列表普通的 @@ 例外压不住 $important 拦截；
    限定了范围（只拦国内直连 / 国外代理）的列表里的 $important 按普通拦截处理"""
    ab = d["adblock"]
    bs, be, _, _, _ = parse_filter("\\n".join(ab.get("black") or []), plain_suffix=True)
    write_lines(os.path.join(AB_DIR, "custom.txt"), domain_lines(bs, be)[0])
    for key, fn in AB_SCOPED_BLACK:  # 分范围的自定义黑名单：空的写成空文件，adblock_providers 不会引用
        bs, be, _, _, _ = parse_filter("\\n".join(ab.get(key) or []), plain_suffix=True)
        write_lines(os.path.join(AB_DIR, fn), domain_lines(bs, be)[0])
    ws, we, was, wae, _ = parse_filter("\\n".join(ab.get("white") or []), plain_suffix=True)
    suffix, exact = ws | was, we | wae
    isuf, iex = set(suffix), set(exact)  # 用户白名单永远优先，$important 也压不过
    bsuf, bex = set(), set()
    for l in ab.get("lists", []):
        if not l.get("enabled", True):
            continue
        _read_domains(os.path.join(AB_DIR, l["id"] + ".allow.txt"), suffix, exact)
        _read_domains(os.path.join(AB_DIR, l["id"] + ".impallow.txt"), isuf, iex)
        if l.get("scope", "all") not in ("cn", "proxy"):
            _read_domains(os.path.join(AB_DIR, l["id"] + ".imp.txt"), bsuf, bex)
    write_lines(os.path.join(AB_DIR, "allow.txt"), domain_lines(suffix, exact)[0])
    write_lines(os.path.join(AB_DIR, "important.txt"), domain_lines(bsuf, bex)[0])
    # 没有 $important 拦截时放行名单没用：写成空文件，adblock_providers 不引用（免得多一个规则集）
    write_lines(os.path.join(AB_DIR, "allow-important.txt"), domain_lines(isuf, iex)[0] if bsuf or bex else [])


def config_text(d):
    return json.dumps(build_config(d), ensure_ascii=False, indent=2)


def ab_apply():
    """列表文件变化后让核心生效：配置结构没变时只刷新对应 rule-provider，否则走完整的校验 + 重载"""
    d = load()
    if active_core(d) == "singbox":  # 广告列表转换成 sing-box 规则集后重载
        sb_sync_assets(d)
        AB_CACHE.clear()
        return reload_core()
    try:
        with open(os.path.join(CONF_DIR, "config.yaml")) as f:
            same = f.read() == config_text(d)
    except OSError:
        same = False
    if same:
        for name in adblock_providers(d)[0]:
            core("PUT", "/providers/rules/" + quote(name, safe=""), timeout=60)
        AB_CACHE.clear()
        return True, "规则已刷新"
    AB_CACHE.clear()
    return reload_core()


AB_WAIT = 150  # 并发下载广告列表整体最多等这么多秒


def adblock_update(ids=None, apply=True):
    """下载全部（或指定）启用的列表；在后台线程里调用"""
    with AB_LOCK:
        if AB_STATE["updating"]:
            return False, "正在更新中"
        AB_STATE.update(updating=True, msg="正在下载规则列表…")
    try:
        d = load()
        lists = [l for l in d["adblock"]["lists"] if l.get("enabled", True) and (not ids or l["id"] in ids)]
        meta = ab_meta()
        res = {}
        res_lock = threading.Lock()
        late = threading.Event()
        # 临时文件带本次更新的随机后缀：上一轮超时还卡着的线程迟到后清理半成品时，以前会删掉这一轮同名的 .new，
        # 这一轮就换不上新列表（meta 却记成已更新），或只换上 .txt 没换上 .allow.txt
        tag = ".new." + secrets.token_hex(4)

        def fail(l, err):
            m = dict(meta.get(l["id"]) or {})
            m.update(error=err, checked=int(time.time()))
            return m

        def one(l):
            try:
                r = ab_fetch(l, late, tag)
            except Exception as e:
                r = fail(l, str(e)[:200])
            with res_lock:
                new = [os.path.join(AB_DIR, l["id"] + s) for s in AB_LIST_FILES]
                if not late.is_set():
                    if not r.get("error"):
                        for p in new:
                            if os.path.exists(p + tag):
                                os.replace(p + tag, p)
                    res[l["id"]] = r
                for p in new:  # 迟到 / 失败留下的半成品
                    if os.path.exists(p + tag):
                        os.remove(p + tag)
        ths = [threading.Thread(target=one, args=(l,), daemon=True) for l in lists]
        [t.start() for t in ths]
        end = time.time() + AB_WAIT  # 整体最多等 AB_WAIT 秒（以前逐个 join(150)，几个列表都卡住时要等 N×150 秒）
        [t.join(max(0.1, end - time.time())) for t in ths]
        with res_lock:  # 之后迟到的线程既不写列表文件也不改结果，meta 与磁盘上的文件一致
            late.set()
            res = dict(res)
            for l in lists:
                if l["id"] not in res:
                    res[l["id"]] = fail(l, "下载超时")
        meta.update(res)
        keep = {l["id"] for l in load()["adblock"]["lists"]}  # 已删除的列表不再留 meta（否则旧解析器标记会让每次重启都全部重下）
        meta = {k: v for k, v in meta.items() if k in keep}
        os.makedirs(AB_DIR, exist_ok=True)
        write_json(os.path.join(AB_DIR, "meta.json"), meta)
        ab_compose(load())
        bad = [l["name"] for l in lists if (res.get(l["id"]) or {}).get("error")]
        ok, msg = ab_apply() if apply else (True, "")
        AB_STATE["last"] = int(time.time())
        AB_STATE["msg"] = ("部分列表下载失败：" + "、".join(bad) if bad else "已更新 %d 个列表" % len(lists)) + ("" if ok else "；" + msg)
        return ok and not bad, AB_STATE["msg"]
    finally:
        AB_STATE["updating"] = False


AB_CACHE = {}  # 列表路径 → (mtime, 后缀集合, 精确集合)：只给「查询域名」用，空闲 AB_IDLE 秒后整体释放
AB_IDLE = 300
_AB_GC = {"last": 0.0, "timer": None}
_AB_GC_LOCK = threading.Lock()


def _ab_touch():
    """记一次使用；没有待执行的释放定时器就起一个。几十万条的 HaGeZi / anti-AD 列表转成 set 要占几十 MB，
    以前查一次就常驻到下次更新，现在 5 分钟没人查就释放"""
    with _AB_GC_LOCK:
        _AB_GC["last"] = time.time()
        if _AB_GC["timer"] is None:
            _ab_arm(AB_IDLE)


def _ab_arm(delay):
    t = threading.Timer(delay, _ab_gc)
    t.daemon = True
    _AB_GC["timer"] = t
    t.start()


def _ab_gc():
    with _AB_GC_LOCK:
        left = _AB_GC["last"] + AB_IDLE - time.time()
        if left > 1:
            _ab_arm(left)
            return
        _AB_GC["timer"] = None
        AB_CACHE.clear()


def ab_sets(path):
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return set(), set()
    _ab_touch()
    c = AB_CACHE.get(path)
    if c and c[0] == mt:
        return c[1], c[2]
    suf, ex = set(), set()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("+."):
                suf.add(line[2:])
            elif line:
                ex.add(line)
    AB_CACHE[path] = (mt, suf, ex)
    return suf, ex


def ab_match(host, sets):
    suf, ex = sets
    if host in ex:
        return host
    for p in parents(host, True):
        if p in suf:
            return "+." + p
    return None


def ab_check(target):
    if not isinstance(target, str):
        raise ValueError("请输入有效域名")
    host = clean_target(target)
    if not norm_domain(host):
        raise ValueError("请输入有效域名")
    d = load()
    ab = d["adblock"]
    names = {l["id"]: l["name"] for l in ab["lists"]}
    blocked, allowed = [], []
    ws, we, was, wae, _ = parse_filter("\\n".join(ab.get("white") or []), plain_suffix=True)
    r = r_white = ab_match(host, (ws | was, we | wae))
    if r:
        allowed.append({"name": "自定义白名单", "rule": r})
    for l in ab["lists"]:
        if not l.get("enabled", True):
            continue
        r = ab_match(host, ab_sets(os.path.join(AB_DIR, l["id"] + ".allow.txt")))
        if r:
            allowed.append({"name": l["name"] + "（@@ 例外）", "rule": r})
    bs, be, _, _, _ = parse_filter("\\n".join(ab.get("black") or []), plain_suffix=True)
    r = ab_match(host, (bs, be))
    if r:
        blocked.append({"name": "自定义黑名单", "rule": r})
    for key, label in (("black_cn", "自定义黑名单（只拦国内直连）"), ("black_proxy", "自定义黑名单（只拦国外代理）")):
        bs, be, _, _, _ = parse_filter("\\n".join(ab.get(key) or []), plain_suffix=True)
        r = ab_match(host, (bs, be))
        if r:
            blocked.append({"name": label, "rule": r})
    for l in ab["lists"]:
        if not l.get("enabled", True):
            continue
        r = ab_match(host, ab_sets(os.path.join(AB_DIR, l["id"] + ".txt")))
        if r:
            sc = {"cn": "（只拦国内直连）", "proxy": "（只拦国外代理）"}.get(l.get("scope"), "")
            blocked.append({"name": names[l["id"]] + sc, "rule": r})
    # $important：不限范围的列表里的 $important 拦截，只有用户白名单和 @@…$important 能放行（与 ab_compose 一致）
    imp_hit, imp_ok = [], bool(r_white)
    for l in ab["lists"]:
        if not l.get("enabled", True):
            continue
        if l.get("scope", "all") not in ("cn", "proxy"):
            r = ab_match(host, ab_sets(os.path.join(AB_DIR, l["id"] + ".imp.txt")))
            if r:
                imp_hit.append({"name": names[l["id"]] + "（$important）", "rule": r})
        r = ab_match(host, ab_sets(os.path.join(AB_DIR, l["id"] + ".impallow.txt")))
        if r:
            imp_ok = True
    for x in imp_hit:  # 同一个列表的同一条规则在上面已经列过：换成带 $important 标记的名字
        blocked = [b for b in blocked if not (b["rule"] == x["rule"] and x["name"].startswith(b["name"]))] + [x]
    hit = bool(blocked) and (not allowed or (bool(imp_hit) and not imp_ok))
    return {"host": host, "enabled": bool(ab.get("enabled")), "blocked_by": blocked, "allowed_by": allowed,
            "blocked": hit and bool(ab.get("enabled"))}


def is_fake_ip(host):
    """Fake-IP 网段（198.18.0.0/15 等）的地址：没反查到域名的连接，不能当成「被拦截的域名」进排行"""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return any(ip in n for n in FAKEIP_NETS)


class AdStats:
    """订阅 mihomo info 日志，统计命中广告规则 (REJECT) 的连接；另按小时统计全部连接请求数、拦截数和 DNS 解析耗时（概览「域名过滤」）"""
    RX = re.compile(r"--> (\\S+?):\\d+ match .*?RuleSet[,(](ad-[0-9a-z]+)\\).* using REJECT")
    RX_ALL = re.compile(r"--> \\S+ (?:match|doesn't match)")
    REQ_BATCH = 100  # req 计数攒批：攒够这么多行或持锁时一次性合并，减少逐行加锁

    def __init__(self):
        self.lock = threading.Lock()
        self.data = read_json(AB_STATS_FILE, {"days": {}})
        self.data.setdefault("days", {})
        self.data.setdefault("hours", {})
        self.data.setdefault("dns5", {})
        self.dns_recent = deque(self.data.get("dns_recent") or [], maxlen=60)  # 最近 60 次原始耗时，刚启动时画曲线用
        self.saved = time.time()
        self._req_batch = 0  # 未持锁攒批的 req 增量（sink 系单线程调用，无竞争）

    def hour(self):
        return self.data["hours"].setdefault(time.strftime("%Y-%m-%d %H"), {"req": 0, "blk": 0, "dns": [0, 0]})

    def dns_sample(self, ms):
        with self.lock:
            h = self.hour()
            h["dns"][0] += ms
            h["dns"][1] += 1
            t = time.localtime()
            k = time.strftime("%Y-%m-%d %H:", t) + "%02d" % (t.tm_min // 5 * 5)  # 5 分钟一段，24 小时 288 段
            b = self.data["dns5"].setdefault(k, [0, 0])
            b[0] += ms
            b[1] += 1
            self.dns_recent.append(ms)

    def _merge_req_locked(self):
        """调用方已持锁：把攒批的 req 增量一次性合并"""
        if self._req_batch:
            self.hour()["req"] += self._req_batch
            self._req_batch = 0

    def hit(self, host, prov):
        if host.startswith("["):
            host = host[1:-1]
        with self.lock:
            self._merge_req_locked()
            b = self.data["days"].setdefault(time.strftime("%Y-%m-%d"), {"count": 0, "top": {}, "lists": {}})
            b["count"] += 1
            if host != "?" and not is_fake_ip(host):  # 反查不到域名的 Fake-IP / 未知连接只计总数，不进排行
                b["top"][host] = b["top"].get(host, 0) + 1
            key = prov[3:]
            b["lists"][key] = b["lists"].get(key, 0) + 1
            self.hour()["blk"] += 1
            if len(b["top"]) > 5000:  # 防止长尾域名无限增长
                b["top"] = dict(sorted(b["top"].items(), key=lambda x: -x[1])[:2000])

    def sink(self, line):
        # 子串预过滤：两个正则都要求 payload 含 "-->"，绝大多数日志行直接跳过 json 解析
        if "-->" not in line:
            return
        try:
            msg = json.loads(line).get("payload", "")
        except Exception:
            return
        if self.RX_ALL.search(msg):
            self._req_batch += 1
            if self._req_batch >= self.REQ_BATCH:
                with self.lock:
                    self._merge_req_locked()
        if "REJECT" in msg:
            m = self.RX.search(msg)
            if m:
                self.hit(m.group(1), m.group(2))
        if time.time() - self.saved > 60:
            self.flush()

    SB_LINE = re.compile(r"^\\[(\\d+) \\d+ms\\] (.*)$")
    SB_TO = re.compile(r"inbound/(\\w+)\\[([^\\]]+)\\]: inbound (?:packet )?connection to (\\S+?):\\d+$")
    SB_SNIFF = re.compile(r"router: sniffed (?:packet )?protocol: \\w+, domain: (\\S+)")
    SB_FAKE = re.compile(r"router: found fakeip domain: (\\S+)")
    SB_REJ = re.compile(r"router: match\\[\\d+\\] .*?\\brule_set=(ad-[0-9a-z]+).*=> reject")
    SB_QN = re.compile(r"dns: exchange (\\S+?)\\.? IN ")
    SB_DREJ = re.compile(r"dns: match\\[\\d+\\] .*?\\brule_set=(ad-[0-9a-z]+).*=> (?:predefined|reject)")

    def sink_sb(self, line):
        """sing-box debug 日志：按连接 ID 记住目标域名，命中 ad- 规则集的 reject / DNS 拦截记为一次拦截"""
        # 子串预过滤：各分支的 body 关键字，绝大多数 debug 行直接跳过 json 解析
        if "inbound/" not in line and "sniffed" not in line and "reject" not in line and "dns: " not in line and "fakeip" not in line:
            return
        try:
            msg = json.loads(line).get("payload", "")
        except Exception:
            return
        m = self.SB_LINE.match(msg)
        if not m:
            return
        cid, body = m.group(1), m.group(2)
        hosts = self.__dict__.setdefault("_sb_hosts", {})
        if len(hosts) > 5000:
            hosts.clear()
        m = self.SB_TO.search(body)
        if m:
            if m.group(2) != "dns-in":
                hosts[cid] = m.group(3)
                self._req_batch += 1
                if self._req_batch >= self.REQ_BATCH:
                    with self.lock:
                        self._merge_req_locked()
        elif "sniffed" in body:
            m = self.SB_SNIFF.search(body)
            if m:
                hosts[cid] = m.group(1)
        elif "found fakeip domain" in body:
            # 透明代理进来的目标是 198.18.x.x，sing-box 按 Fake-IP 映射还原出域名再匹配规则；
            # 以前只记了入站的 IP，嗅探不到域名（QUIC、非 TLS 等）时排行里就出现 198.18.0.11 这样的地址
            m = self.SB_FAKE.search(body)
            if m and (cid not in hosts or is_fake_ip(hosts[cid])):
                hosts[cid] = m.group(1)
        elif "=> reject" in body:
            m = self.SB_REJ.search(body)
            if m:
                self.hit(hosts.pop(cid, "?"), m.group(1))
        elif body.startswith("dns: "):
            m = self.SB_QN.search(body)
            if m:
                hosts["q" + cid] = m.group(1)
            else:
                m = self.SB_DREJ.search(body)
                if m:
                    self.hit(hosts.pop("q" + cid, "?"), m.group(1))
        if time.time() - self.saved > 60:
            self.flush()

    def flush(self):
        with self.lock:
            self._merge_req_locked()
            keep = sorted(self.data["days"])[-2:]
            self.data["days"] = {k: self.data["days"][k] for k in keep}
            hk = sorted(self.data["hours"])[-48:]
            self.data["hours"] = {k: self.data["hours"][k] for k in hk}
            dk = sorted(self.data["dns5"])[-288:]
            self.data["dns5"] = {k: self.data["dns5"][k] for k in dk}
            self.data["dns_recent"] = list(self.dns_recent)
            for b in self.data["days"].values():
                b["top"] = dict(sorted(b["top"].items(), key=lambda x: -x[1])[:500])
            write_json(AB_STATS_FILE, self.data)
        self.saved = time.time()

    def today(self):
        with self.lock:
            return int((self.data["days"].get(time.strftime("%Y-%m-%d")) or {}).get("count", 0))

    def report(self):
        with self.lock:
            days = copy.deepcopy(self.data["days"])
            hours = copy.deepcopy(self.data["hours"])
            d5 = dict(self.data["dns5"])
            recent = list(self.dns_recent)
        b = days.get(time.strftime("%Y-%m-%d"), {"count": 0, "top": {}, "lists": {}})
        now = int(time.time() // 3600 * 3600)
        h24 = []
        for i in range(23, -1, -1):  # 最近 24 个小时段，缺的补 0
            k = time.strftime("%Y-%m-%d %H", time.localtime(now - i * 3600))
            e = hours.get(k) or {"req": 0, "blk": 0, "dns": [0, 0]}
            h24.append({"h": k[-2:], "req": e["req"], "blk": e["blk"], "dns": round(e["dns"][0] / e["dns"][1], 1) if e["dns"][1] else None})
        top = [(k, v) for k, v in b["top"].items() if k != "?" and not is_fake_ip(k)]  # 旧版本记下的 Fake-IP 也不显示
        return {"today": b["count"], "top": sorted(top, key=lambda x: -x[1])[:20], "lists": b["lists"],
                "days": [{"date": k, "count": days[k]["count"]} for k in sorted(days)], "hours": h24,
                "dns": self.dns_report(d5, recent)}

    @staticmethod
    def dns_report(d5, recent):
        """解析耗时：24 小时 288 个 5 分钟段（null = 该段没采样）；有效段不足 2 个时（刚安装 / 刚重启）用最近的原始采样画曲线"""
        now = int(time.time() // 300 * 300)
        seg = []
        for i in range(287, -1, -1):
            t = time.localtime(now - i * 300)
            b = d5.get(time.strftime("%Y-%m-%d %H:", t) + "%02d" % (t.tm_min // 5 * 5))
            seg.append(round(b[0] / b[1], 1) if b and b[1] else None)
        vals = [v for v in seg if v is not None]
        tot = sum(b[0] for b in d5.values() if b[1])
        cnt = sum(b[1] for b in d5.values() if b[1])
        return {"avg": round(tot / cnt, 1) if cnt else None, "last": recent[-1] if recent else None, "samples": cnt,
                "series": seg if len(vals) >= 2 else recent[-30:], "mode": "5min" if len(vals) >= 2 else "recent",
                "core": active_core()}

    def dns_probe(self):
        """经核心解析器查随机子域名（绕过缓存）：国内域名走直连 DNS、国外域名走代理 DNS，记录平均耗时"""
        sb = active_core() == "singbox"  # sing-box 的 Clash API 没有 /dns/query，改为直接向 dns-in(1053) 发 UDP 查询
        ms = []
        for base in ("baidu.com", "google.com"):
            name = "shunt-%s.%s" % (secrets.token_hex(4), base)
            st = time.time()
            if sb:
                # 查 TXT 而不是 A：fake-ip 模式下 A / AAAA 由 dns-fake 立即返回假地址，以前测出来恒为 1~2 ms，
                # 还每分钟往 Fake-IP 映射表里塞两个随机域名；TXT 照常按国内 / 国外分流到真实上游
                ok = udp_dns_query(name, 1053, timeout=8, qtype=16)
            else:
                code, _ = core("GET", f"/dns/query?name={name}&type=A", timeout=8)
                ok = code == 200
            if ok:
                ms.append(max(1, int((time.time() - st) * 1000)))
        if ms:
            self.dns_sample(round(sum(ms) / len(ms)))

    def dns_loop(self):
        """启动后先连测 3 次（每 10 秒）让曲线马上有数据，之后每 60 秒一次"""
        time.sleep(8)
        n = 0
        while True:
            try:
                self.dns_probe()
            except Exception as e:
                log_err("广告拦截 DNS 统计", e)
            n += 1
            time.sleep(10 if n < 3 else 60)

    def loop(self):
        while True:
            try:
                sb = active_core() == "singbox"  # sing-box 的规则命中只在 debug 级别日志里
                core_stream("/logs?level=debug" if sb else "/logs?level=info", self.sink_sb if sb else self.sink, {})
            except OSError:  # 内核未运行 / 断线，稍后重连
                pass
            except Exception as e:
                log_err("广告拦截统计", e)
            time.sleep(3)


AD_STATS = None

'''
_SRC['shunt.live'] = '''"""实时速率 / 内存（订阅 /traffic 与 /memory 流）"""
import json, socket, time

from .errlog import log_err
from .core import conns_snapshot
from .logstream import core_stream


LIVE = {"up": 0, "down": 0, "upTotal": 0, "downTotal": 0, "inuse": 0, "oslimit": 0, "t": 0}


def live_loop(path):
    def sink(line):
        try:
            j = json.loads(line)
        except Exception:
            return
        if path == "/memory":
            if j.get("inuse"):  # 首条消息固定为 0
                LIVE["inuse"], LIVE["oslimit"] = j["inuse"], j.get("oslimit", 0)
        else:
            LIVE.update({k: j.get(k, 0) for k in ("up", "down")})
            if "upTotal" in j or "downTotal" in j:
                LIVE.update(upTotal=j.get("upTotal", 0), downTotal=j.get("downTotal", 0))
            elif time.time() - LIVE.get("_tt", 0) > 2:  # sing-box 的 /traffic 不带累计值，从 /connections 取
                LIVE["_tt"] = time.time()
                c = conns_snapshot(timeout=3) or {}
                LIVE.update(upTotal=int(c.get("uploadTotal") or 0), downTotal=int(c.get("downloadTotal") or 0))
            LIVE["t"] = time.time()
    while True:
        try:
            core_stream(path, sink, {}, idle=15)
        except OSError:  # 内核未运行 / 断线，稍后重连
            pass
        except Exception as e:
            log_err("实时速率", e)
        LIVE.update(up=0, down=0, t=0) if path == "/traffic" else LIVE.update(inuse=0)
        time.sleep(3)


def memory_once():
    """不依赖后台线程：读取 /memory 流的前两行（首行恒为 0）"""
    got = []
    stop = {}

    def sink(line):
        try:
            j = json.loads(line)
        except Exception:
            return
        got.append(j)
        if j.get("inuse") or len(got) >= 2:
            stop["stop"] = True
            try:
                stop["sock"].shutdown(socket.SHUT_RDWR)
            except (KeyError, OSError):
                pass
    try:
        core_stream("/memory", sink, stop, idle=3)
    except Exception:
        pass
    vals = [g for g in got if g.get("inuse")]
    return vals[-1] if vals else {"inuse": 0, "oslimit": 0}
'''
_SRC['shunt.logclean'] = '''"""日志自动清理"""
import os, re, time

from .base import LOG_FILES, MIHOMO_LOG
from .data import load
LOG_STATE = {"last": 0, "trimmed": []}
# init.d/shunt-core 的 start_pre 每次启动都 `: > mihomo.log` 清空核心日志：核心崩溃后看门狗 / 面板一重启，崩溃原因就没了。
# 面板在 rc-service start / restart 之前把当前日志末尾另存到 mihomo.log.prev（追加，最多 PREV_KEEP 行，按次分隔），
# 「日志」页在当前日志很短（刚重启）时把它一起显示
PREV_LOG = MIHOMO_LOG + ".prev"
PREV_RUN_LINES = 200
PREV_KEEP = 600
_ANSI = re.compile(r"\\x1b\\[[0-9;]*m")
_FATAL = re.compile(r"panic:|FATAL|level=fatal|fatal error:")


def _tail_lines(path, n, cap=128 * 1024):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - cap))
            raw = f.read()
    except OSError:
        return []
    lines = _ANSI.sub("", raw.decode("utf-8", "ignore")).splitlines()
    if size > cap and lines:
        lines = lines[1:]  # 第一行可能被截断
    return lines[-n:]


def core_log_keep(why="重启核心"):
    """启动 / 重启核心之前调用：把当前核心日志最后 PREV_RUN_LINES 行追加到 PREV_LOG（先写临时文件再改名）。
    日志为空（已经被清过）时不动 PREV_LOG，免得连续重启把真正的崩溃记录覆盖掉。
    上次运行里有 panic / fatal 时记入面板错误（「诊断」页可见）。返回是否保存了"""
    cur = _tail_lines(MIHOMO_LOG, PREV_RUN_LINES)
    if not any(l.strip() for l in cur):
        return False
    old = _tail_lines(PREV_LOG, PREV_KEEP)
    head = f"===== {time.strftime('%F %T')} {why}前，上次运行的最后 {len(cur)} 行 ====="
    keep = old[-max(0, PREV_KEEP - len(cur) - 1):] if PREV_KEEP > len(cur) + 1 else []
    tmp = f"{PREV_LOG}.tmp{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\\n".join(keep + [head] + cur) + "\\n")
        os.replace(tmp, PREV_LOG)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        print("save prev core log", e, flush=True)
        return False
    try:  # 已存过：清空当前日志，免得 init.d 的 start_pre 再存一遍
        open(MIHOMO_LOG, "w").close()
    except OSError:
        pass
    bad = [l.strip() for l in cur if _FATAL.search(l)]
    if bad:
        from .errlog import log_err
        log_err("核心上次运行的致命错误", bad[-1][-280:])
    return True


def core_log_text(n=300):
    """「日志」页显示的核心日志：当前日志末尾 n 行；当前日志不足 n 行（刚重启过）时，前面补上 PREV_LOG 的末尾，
    这样核心崩溃 / 被重启后仍能看到上次退出前的报错"""
    cur = _tail_lines(MIHOMO_LOG, n)
    if len(cur) >= n:
        return "\\n".join(cur)
    prev = _tail_lines(PREV_LOG, n - len(cur))
    if not prev:
        return "\\n".join(cur)
    return "\\n".join(prev + [f"===== 以上为上次运行的日志（{PREV_LOG}），以下为本次运行 ====="] + cur)


def log_sizes():
    out = []
    for p in LOG_FILES + ([PREV_LOG] if os.path.exists(PREV_LOG) else []):
        try:
            out.append({"path": p, "size": os.path.getsize(p)})
        except OSError:
            out.append({"path": p, "size": -1})
    return out


def trim_log(path, keep=200):
    """原地截断，只保留最后 keep 行。写入方（OpenRC output_log）以 O_APPEND 打开，截断后会继续追加到新末尾"""
    try:
        with open(path, "rb+") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            tail = f.read().splitlines(keepends=True)[-keep:]
            data = b"".join(tail)
            f.seek(0)
            f.write(data)
            f.truncate()
        return size - len(data)
    except OSError:
        return 0


def log_clean(force=False):
    limit = float(load().get("log_limit") or 5) * 1024 * 1024
    freed = 0
    for x in log_sizes():
        if x["size"] > 0 and (force or x["size"] > limit):
            freed += trim_log(x["path"])
    LOG_STATE["last"] = int(time.time())
    return freed


def log_loop():
    while True:
        try:
            freed = log_clean()
            if freed:
                print(time.strftime("%F %T"), f"[log] 日志超过上限，已截断释放 {freed // 1024} KB", flush=True)
        except Exception as e:
            print("log clean error", e, flush=True)
        time.sleep(60)
'''
_SRC['shunt.schedule'] = '''"""定时任务"""
import json, os, re, threading, time
from collections import deque
from urllib.parse import quote

from .errlog import log_err
from .base import BUILTIN_POLICIES, G_MANUAL, HC, PANEL_DIR
from .data import load, read_json, write_json
from .core import refresh_regions, reload_core
from .corehttp import core, core_json
from .nodeprobe import fill_absent
from .sb_common import active_core
from .notify import core_alive
from .groups import sb_sub_cache
from .sb_subs import sb_update_subs
from .sb_config import sb_sync_assets
from .sysopt import svc_restart
from .notify import WD
from .latency import bg_fetch, bg_meta, bg_meta_save
from .adblock import AB_PARSER, AB_STATE, ab_meta, adblock_update
from .health import HEALTH_STATE, health_cfg, run_check as health_run_check
SCHED = {"events": deque(maxlen=30), "done": {}}
# 每天定点任务的最近执行记录 {任务: {"date": 日期, "at": 当时的设定时刻}}，落盘到 sched_done.json：
# 面板重启后不会把今天已跑过的任务再跑一遍
DAILY_FILE = os.path.join(PANEL_DIR, "sched_done.json")
DAILY = {"data": None}
_DAILY_LOCK = threading.Lock()
HHMM = re.compile(r"([01]\\d|2[0-3]):[0-5]\\d")


def _daily():
    if DAILY["data"] is None:
        d = read_json(DAILY_FILE, {})
        DAILY["data"] = d if isinstance(d, dict) else {}
    return DAILY["data"]


def _daily_mark(k, today, at):
    _daily()[k] = {"date": today, "at": at}
    try:
        write_json(DAILY_FILE, DAILY["data"])
    except OSError as e:
        log_err("保存定时任务记录", e)


def daily_due(k, at, now_hm, today):
    """每天定点任务是否该执行：到点或已过点（当天补跑）且今天还没跑过；返回 True 时已记为今天执行过（不会重复触发）。

    - 原来要求当前 HH:MM 正好等于设定值：循环被长任务卡住跨过那一分钟，当天就漏跑了
    - 第一次见到这个任务、或设定时刻刚被改过，而今天的时刻已经过了：记为今天已执行，从下一次开始
      （刚升级 / 开机 / 改时间时不会立刻把今天错过的任务全跑一遍，比如定时重启）
    - 设定时刻无效（如 25:00）或为空：永不触发"""
    if not at or not HHMM.fullmatch(at):
        return False
    with _DAILY_LOCK:
        rec = _daily().get(k)
        if not isinstance(rec, dict):
            rec = None
        if rec is not None and rec.get("date") == today:
            if rec.get("at") != at:  # 今天已经跑过、之后改了时刻：只更新记录，明天按新时刻
                _daily_mark(k, today, at)
            return False
        if now_hm < at:  # 还没到点：记下设定时刻，到点时才认得出「这是没改过的时刻」
            if rec is None or rec.get("at") != at:
                _daily_mark(k, (rec or {}).get("date") or "", at)
            return False
        if rec is None or rec.get("at") != at:
            _daily_mark(k, today, at)
            return False
        _daily_mark(k, today, at)
        return True


def sched_event(msg):
    SCHED["events"].appendleft({"t": int(time.time()), "msg": msg})
    print(time.strftime("%F %T"), "[schedule]", msg, flush=True)


MH_SAME_NOTE = "（节点与更新前完全相同：mihomo 拿到的订阅内容没有变化，或机场回复了 304 未修改）"


def mh_provider_names(name):
    """mihomo 里某个订阅（proxy-provider）当前的节点名列表；核心没应答 / 没有这个订阅时返回 None"""
    j = core_json("/providers/proxies/" + quote(name, safe=""), timeout=5)
    if not isinstance(j, dict) or not isinstance(j.get("proxies"), list):
        return None
    return [x.get("name") for x in j["proxies"] if isinstance(x, dict)]


def mh_unchanged_note(before, after):
    """mihomo 更新订阅前后节点名单完全一样时给出提示。mihomo 默认开启 ETag：机场 / CDN 回复 304，
    或返回的内容与本地缓存逐字节相同，mihomo 只刷新「更新时间」、继续用本地 ./providers/<名>.yaml，
    更新接口照样回 204。以前面板只报「已更新」，看不出节点其实没换"""
    if before and after is not None and before == after:
        return f"：{len(after)} 个节点" + MH_SAME_NOTE
    return f"：{len(after)} 个节点" if after is not None else ""


def mh_update_subs(names):
    """mihomo：让核心重新拉取订阅；返回 (成功列表「名称：节点数（提示）」, 失败列表「名称：原因」)"""
    okl, bad = [], []
    for nm in names:
        before = mh_provider_names(nm)
        code, raw = core("PUT", "/providers/proxies/" + quote(nm, safe=""), timeout=120)
        if code >= 300:
            bad.append(nm + "：" + raw.decode(errors="ignore")[:100])
        else:
            okl.append(nm + mh_unchanged_note(before, mh_provider_names(nm)))
    return okl, bad


LATENCY_PROBE_BUDGET = 60  # sing-box 定时测速逐个补测的时间上限（定时任务在 sched_loop 里同步执行，不能拖太久）


def run_task(name):
    d = load()
    if name == "sub_update" and active_core(d) == "singbox":
        ok, bad = sb_update_subs(d=d)
        try:
            refresh_regions()
        except Exception as e:
            log_err("定时任务：刷新地区分组", e)
        return not bad, "订阅已更新" if not bad else "以下订阅更新失败：" + "；".join(bad)
    if name == "geo_update" and active_core(d) == "singbox":
        bad = sb_sync_assets(d, force=True)
        ok, msg = reload_core()
        if bad:
            return False, "部分规则集更新失败：" + "；".join(bad)[:200]
        # 重载失败时以前仍报「GEO / 规则集已更新」（事件里显示「✗ GEO / 规则集已更新」），看不出是重载出了问题
        return ok, "GEO / 规则集已更新" if ok else "规则集已下载，但重载核心失败：" + str(msg)[:200]
    if name == "sub_update":
        _, errs = mh_update_subs([s["name"] for s in d["subs"]])
        bad = [e.split("：", 1)[0] for e in errs]
        try:
            refresh_regions()
        except Exception as e:
            log_err("定时任务：刷新地区分组", e)
        return not bad, "订阅已更新" if not bad else "以下订阅更新失败：" + "、".join(bad)
    if name == "core_restart":
        if WD["manual_stop"]:  # 用户手动停止了核心：定时重启不能替用户把它拉起来
            return True, "核心已手动停止，跳过定时重启"
        code, out = svc_restart(timeout=90, why="定时重启核心", cancel=lambda: WD["manual_stop"])
        return code == 0, "核心已重启" if code == 0 else out[-200:]
    if name == "geo_update":
        code, raw = core("POST", "/configs/geo", {}, timeout=180)
        return code < 300, "GEO 数据库已更新" if code < 300 else raw.decode(errors="ignore")[:200]
    if name == "latency":
        # GLOBAL 只含 config 里的节点和策略组，订阅节点要经「🖐️ 手动选择」（含全部节点）测速
        code, raw = core("GET", f"/group/GLOBAL/delay?url={quote(HC)}&timeout=5000", timeout=120)
        n = 0
        pg = core_json("/proxies/" + quote(G_MANUAL))
        if pg:
            c2, r2 = core("GET", f"/group/{quote(G_MANUAL)}/delay?url={quote(HC)}&timeout=5000", timeout=120)
            try:
                res = json.loads(r2) if c2 == 200 else {}
            except Exception:
                res = {}
            res = {k: v for k, v in res.items() if isinstance(v, int) and not isinstance(v, bool) and v > 0} if isinstance(res, dict) else {}
            if c2 == 200 and active_core() == "singbox":
                # sing-box 整组测速同时只测 10 个、共用超时：没轮到测的节点不在结果里，也没有刷新延迟，逐个补测
                members = [m for m in ((pg.get("all") if isinstance(pg, dict) else None) or []) if isinstance(m, str) and m not in BUILTIN_POLICIES]
                res = fill_absent({m: res.get(m, 0) for m in members}, LATENCY_PROBE_BUDGET)
            n = sum(1 for v in res.values() if v > 0)
            code = 200 if (code == 200 or c2 in (200, 504)) else code
        return code == 200, f"节点测速完成，{n} 个节点可达"
    if name == "adblock":
        return adblock_update()
    if name == "health":
        if HEALTH_STATE["running"]:  # 手动触发的检查还在跑：不叠加第二次全量测速
            return True, "节点健康检查正在进行，跳过本次"
        return health_run_check()
    return False, "未知任务"


TASK_NAMES = {"sub_update": "定时更新订阅", "core_restart": "定时重启核心", "geo_update": "定时更新 GEO",
              "latency": "自动测速", "adblock": "更新广告规则", "health": "节点健康检查"}


def sched_loop():
    n = 0
    while True:
        time.sleep(20)
        n += 1
        try:
            d = load()
            sc = d.get("schedule") or {}
            now = time.strftime("%H:%M")
            today = time.strftime("%Y-%m-%d")
            due = [k for k in ("sub_update", "core_restart", "geo_update") if daily_due(k, str(sc.get(k) or ""), now, today)]
            # 节点健康检查：每天到点且启用时执行（已过点、今天还没跑也补跑）
            hcfg = health_cfg(d)
            if hcfg.get("enabled") and daily_due("health", str(hcfg.get("time") or ""), now, today):
                due.append("health")
            mins = int(sc.get("latency") or 0)
            if mins and time.time() - SCHED["done"].get("latency", 0) >= mins * 60:
                due.append("latency")
            if active_core(d) == "singbox" and d["subs"]:  # mihomo 的订阅由核心按间隔自动更新；sing-box 由面板代劳
                iv = max(int(d.get("sub_interval") or 86400), 600)
                last = min([sb_sub_cache(s["name"]).get("updated", 0) for s in d["subs"]] or [0])
                if time.time() - max(last, SCHED["done"].get("sb_subs", 0)) >= iv:
                    SCHED["done"]["sb_subs"] = time.time()
                    ok_, bad_ = sb_update_subs(d=d)
                    try:  # 出错不能打断这一轮：上面 daily_due 已把到点的定点任务记成今天执行过，跳过就漏一天
                        refresh_regions()
                    except Exception as e:
                        log_err("订阅自动更新：刷新地区分组", e)
                    if bad_:
                        sched_event("订阅自动更新：✗ " + "；".join(bad_)[:200])
            try:
                _bg_tick()
            except Exception as e:
                print("background image error", e, flush=True)
            ab = d["adblock"]
            if ab.get("enabled") and ab.get("lists") and int(ab.get("interval") or 0):
                if time.time() - max(AB_STATE["last"], ab_last_update()) >= int(ab["interval"]):
                    due.append("adblock")
                elif AB_STATE.get("retry") and time.time() >= AB_STATE["retry"]:
                    due.append("adblock")  # 上次有列表下载失败：10 分钟后补一次，最多 AB_RETRY_MAX 次
            try:
                from .core_cache import prefetch_due, prefetch_job
                if prefetch_due():  # 自动下载 / 更新全部内核（不动正在运行的那个）
                    threading.Thread(target=prefetch_job, args=(d.get("gh_proxy") or "",), daemon=True).start()
            except Exception as e:
                print("core prefetch error", e, flush=True)
            for k in due:
                if k == "latency":
                    SCHED["done"][k] = time.time()
                if k == "adblock":
                    AB_STATE["last"] = int(time.time())  # 失败也等下一个周期，避免频繁重试
                    AB_STATE["retry"] = 0
                    if not core_alive():  # 刚开机 / 核心重启中：先等核心起来，否则国外列表只能直连（常被污染）
                        AB_STATE["retry"] = time.time() + 60
                        continue
                try:  # 一个任务抛异常不能连累同一轮的其他任务（它们已被 daily_due 记成今天执行过，跳过就漏一天）
                    ok, msg = run_task(k)
                except Exception as e:
                    log_err("定时任务：" + TASK_NAMES[k], e)
                    ok, msg = False, f"执行出错：{str(e)[:150]}"
                if k == "adblock":
                    tries = AB_STATE.get("retries", 0)  # 自己的计数：以前复用了循环计数 n，广告规则一更新就把它改成重试次数
                    if not ok and tries < AB_RETRY_MAX:
                        AB_STATE.update(retry=time.time() + 600, retries=tries + 1)
                    elif ok:
                        AB_STATE["retries"] = 0
                if k != "latency" or not ok:
                    sched_event(f"{TASK_NAMES[k]}：{'✓' if ok else '✗'} {msg}")
        except Exception as e:
            print("schedule error", e, flush=True)


def _bg_tick():
    """每天更新一次背景图（在 sched_loop 里调用；出错不影响同一轮的定时任务）"""
    bm = bg_meta()
    if bm.get("daily") and bm.get("url") and time.time() - max(int(bm.get("t") or 0), SCHED["done"].get("bg", 0)) >= 86400:
        SCHED["done"]["bg"] = time.time()  # 失败也等一天，避免反复重试
        if bg_fetch(bm["url"])[0]:
            bm["t"] = int(time.time())
            bg_meta_save(bm)


AB_RETRY_MAX = 3  # 广告列表部分下载失败后的补下次数（每次间隔 10 分钟）


def ab_last_update():
    off = {l["id"] for l in load()["adblock"]["lists"] if not l.get("enabled", True)}
    m = {k: v for k, v in ab_meta().items() if k not in off}  # 停用列表的旧 meta 不影响定时更新（已删除列表的 meta 在更新时清掉）
    if any(isinstance(v, dict) and v.get("updated") and not v.get("error") and int(v.get("parser") or 1) < AB_PARSER for v in m.values()):
        return 0  # 旧解析器转换的列表（可能误拦整站）：当作从未更新，定时任务会尽快重新下载
    return max([v.get("updated", 0) for v in m.values() if isinstance(v, dict)] or [0])
'''
_SRC['shunt.devices'] = '''"""设备名称"""
import ipaddress, os, re, socket, threading, time
from collections import deque

from .base import LEASE_FILES, MIHOMO_LOG
from .data import load
from .stats import arp_table
from .ruletest import is_ip
RDNS = {}  # ip -> (name, 过期时间)
RDNS_Q = deque()
RDNS_EV = threading.Event()


def read_leases():
    out = {}
    for path in LEASE_FILES:
        try:
            with open(path) as f:
                for line in f:
                    p = line.split()
                    if len(p) >= 4 and is_ip(p[2]):
                        out[p[2]] = (p[1].lower(), "" if p[3] == "*" else p[3])
        except OSError:
            pass
    return out


def read_hosts():
    out = {}
    try:
        with open("/etc/hosts") as f:
            for line in f:
                p = line.split("#", 1)[0].split()
                if len(p) >= 2 and is_ip(p[0]) and not ipaddress.ip_address(p[0]).is_loopback:
                    out.setdefault(p[0], p[1])
    except OSError:
        pass
    return out


def rdns_loop():
    while True:
        RDNS_EV.wait(30)
        RDNS_EV.clear()
        while RDNS_Q:
            ip = RDNS_Q.popleft()
            name = ""
            try:  # 不再改全局 socket 超时：那会让同时建立的订阅下载、核心更新等连接也只剩 2 秒
                name = socket.gethostbyaddr(ip)[0]
            except (OSError, UnicodeError, ValueError):  # 主机名含非法字符时抛 UnicodeError：以前整个反向解析线程退出，之后设备名再也不解析
                pass
            if name and (name == ip or name.endswith(".in-addr.arpa")):
                name = ""
            RDNS[ip] = (name.split(".")[0] if name.endswith((".lan", ".local", ".home", ".localdomain")) else name, time.time() + 1800)
        # 顺手清理过期条目：RDNS 只增不减会导致无界增长；device_map 对缺失 key 会重新入队，语义不变
        now = time.time()
        for ip in [k for k, v in RDNS.items() if v[1] <= now]:
            RDNS.pop(ip, None)


def device_map(ips):
    d = load()
    custom = {k.lower(): v for k, v in (d.get("devices") or {}).items()}
    arp, leases, hosts = arp_table(), read_leases(), read_hosts()
    out = {}
    for ip in ips:
        if not ip or ip == "?":
            continue
        mac = (arp.get(ip) or leases.get(ip, ("", ""))[0] or "").lower()
        name, src = custom.get(ip.lower()) or (custom.get(mac) if mac else None), "备注"
        if not name and leases.get(ip, ("", ""))[1]:
            name, src = leases[ip][1], "DHCP"
        if not name and hosts.get(ip):
            name, src = hosts[ip], "hosts"
        if not name:
            r = RDNS.get(ip)
            if r and r[1] > time.time():
                name, src = r[0], "反向解析"
            elif (not r or r[1] <= time.time()) and is_ip(ip) and ipaddress.ip_address(ip).is_private and ip not in RDNS_Q:
                RDNS[ip] = ("", time.time() + 300)
                RDNS_Q.append(ip)
                RDNS_EV.set()
        out[ip] = {"name": name or "", "mac": mac, "src": src if name else ""}
    return out


def tun_runtime_error(dev, wait=4):
    """等待 TUN 网卡出现；失败时从核心日志里找出原因"""
    for _ in range(wait * 2):
        if os.path.exists(f"/sys/class/net/{dev}/tun_flags"):  # 必须是 TUN 网卡，避免与 eth0 等同名网卡误判
            return ""
        time.sleep(0.5)
    try:
        with open(MIHOMO_LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 64 * 1024))
            lines = f.read().decode("utf-8", "ignore").splitlines()
        for line in reversed(lines):
            if "TUN" in line and ("error" in line.lower() or "fatal" in line.lower()):
                m = re.search(r'msg="(.*)"', line)
                return (m.group(1) if m else line)[-240:]
    except OSError:
        pass
    return f"未检测到网卡 {dev}"
'''
_SRC['shunt.diag'] = '''"""网络诊断"""
import ipaddress
import json, os, re, secrets, shutil, socket, threading, time, urllib.request, urllib.error

from .errlog import ERRS
from .base import CONF_DIR, FAKEIP_NETS, TPROXY_SH
from .data import load
from .mihomo import check_config, dns_cfg, mh_bin, safe_dev
from .core import conns_snapshot, core
from .sb_common import CORES, SB_BIN, SB_CONF, SB_STATE, active_core
from .sb_config import check_sb_config, core_pid, tun_stack_tip
from .sysopt import proxy_opener, sh
def dns_probe(server=("127.0.0.1", 1053), name="www.baidu.com"):
    """向 mihomo DNS 端口发一个最小的 A 查询"""
    qid = secrets.randbits(16)
    pkt = qid.to_bytes(2, "big") + b"\\x01\\x00\\x00\\x01\\x00\\x00\\x00\\x00\\x00\\x00"
    pkt += b"".join(bytes([len(x)]) + x.encode() for x in name.split(".")) + b"\\x00\\x00\\x01\\x00\\x01"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(4)
    try:
        st = time.time()
        s.sendto(pkt, server)
        while True:  # 丢弃 ID 不符的迟到报文；短于 12 字节的报文以前抛 IndexError
            data = s.recv(1500)
            if len(data) >= 12 and int.from_bytes(data[:2], "big") == qid and data[2] & 0x80:
                break
        rcode, an = data[3] & 0x0F, int.from_bytes(data[6:8], "big")
        return rcode == 0 and an > 0, int((time.time() - st) * 1000), rcode
    finally:
        s.close()


def _scrub(s):
    """诊断页脱敏：异常信息里可能带文件绝对路径或 URL 中的账号密码，展示前替换"""
    s = re.sub(r"'/[^']*'", "'<路径>'", s)  # Python 异常常用引号包裹路径
    s = re.sub(r'"/[^"]*"', '"<路径>"', s)
    s = re.sub(r"(https?://[^/\\s:]+:)[^@/\\s]+@", r"\\1***@", s)
    return s


def dns_bypass_devices(conns=None):
    """检测 DNS 绕过面板的设备。

    原理：Fake-IP 模式下，设备 DNS 经过面板时拿到的目标是 198.18.x.x；
    若某连接嗅探到了域名（sniffHost），但目标 IP 是公网真实 IP，
    说明该设备自己解析了域名（DNS 没经过面板，如用了主路由 DNS / 私人 DNS）。
    sing-box 1.13+ 移除了 sniff_override_destination，这种情况拿污染 IP 必死；
    mihomo 的 sniffer 会改写目标地址所以不受影响。

    返回 {设备IP: {域名, ...}}；conns 为空时返回 {}。"""
    if conns is None:
        conns = conns_snapshot(timeout=5)
    if not conns:
        return {}
    devs = {}
    for x in conns.get("connections") or []:
        md = x.get("metadata") or {}
        sniff = (md.get("sniffHost") or "").strip().lower()
        dip = (md.get("destinationIP") or "").strip()
        sip = (md.get("sourceIP") or "").strip()
        if not sniff or not dip or not sip:
            continue
        try:
            ip = ipaddress.ip_address(dip)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            continue
        if any(ip in n for n in FAKEIP_NETS):
            continue
        # 公网 IP + 嗅探到域名 = 设备 DNS 未经过面板
        devs.setdefault(sip, set()).add(sniff)
    return devs


def diagnose():
    d = load()
    mode = d["proxy_mode"]
    out = []

    def item(name, ok, detail, fix=""):
        out.append({"name": name, "ok": ok, "detail": detail, "fix": fix if ok is not True else ""})

    recent = [e for e in ERRS if time.time() - e["t"] < 86400]
    if recent:
        item(f"面板内部错误（24 小时内 {len(recent)} 条）", "warn",
             "；".join(f"{time.strftime('%m-%d %H:%M', time.localtime(e['t']))} {e['where']}：{_scrub(e['msg'])}" for e in recent[:5])[:600],
             "完整记录在 /var/log/mihomo-panel.log，反馈问题时请附上")

    code, ver = core("GET", "/version", timeout=5)
    if code != 200:  # 加载大规则集 / 重载 / GC 时会短暂卡几秒，复查一次再下结论（与看门狗一致）
        time.sleep(2)
        code, ver = core("GET", "/version", timeout=8)
    alive = code == 200
    sb = active_core(d) == "singbox"
    if alive:
        item(f"{CORES[active_core(d)]} 核心", True, json.loads(ver).get("version", ""))
    elif core_pid():
        item(f"{CORES[active_core(d)]} 核心", False, "进程在运行，但控制器（Clash API）无响应：内核可能卡死或内存不足",
             "执行 rc-service shunt-core restart；反复出现请用 dmesg | grep -i oom 查是否被系统杀掉，或换回稳定版内核")
    else:
        item(f"{CORES[active_core(d)]} 核心", False, "核心进程未运行（可能已崩溃或被系统杀掉）",
             "在顶部点「启动」，或执行 rc-service shunt-core restart 并查看 /var/log/mihomo.log")
    if sb:
        if os.path.isfile(SB_BIN) and os.path.isfile(SB_CONF):
            ok, err = check_sb_config(SB_CONF)
            item("配置校验 (sing-box check)", ok, "通过" if ok else err[-200:], "在设置中撤销最近的修改，或切回 mihomo")
        if SB_STATE["warn"]:
            item("sing-box 兼容提示", "warn", "；".join(SB_STATE["warn"])[:300], "详见 设置 → 核心 → 内核切换")
    cfg = os.path.join(CONF_DIR, "config.yaml")
    if not sb and os.path.isfile(mh_bin(active_core(d))) and os.path.isfile(cfg):
        ok, err = check_config(cfg)
        item("配置校验 (mihomo -t)", ok, "通过" if ok else err[-200:], "在设置中撤销最近的修改，或恢复备份")
    try:
        fwd = open("/proc/sys/net/ipv4/ip_forward").read().strip() == "1"
    except OSError:
        fwd = False
    item("IPv4 转发", fwd if mode != "off" else ("warn" if not fwd else True), "已开启" if fwd else "未开启",
         "sysctl -w net.ipv4.ip_forward=1，并写入 /etc/sysctl.conf")
    _, st = sh(TPROXY_SH + " status", timeout=10)
    st = st.strip().splitlines()[-1] if st.strip() else "off"
    if mode == "tproxy":
        item("TProxy 规则", st == "tproxy", "nftables 规则已加载" if st == "tproxy" else "未检测到 nftables TProxy 规则",
             "在设置里重新选择「TProxy」，或执行 /opt/mihomo-panel/tproxy.sh start；需要 nftables 与 nft_tproxy 模块")
    elif mode == "tun":
        dev = safe_dev((d.get("tun") or {}).get("device"))
        has_tun = os.path.exists("/dev/net/tun")
        item("TUN 模块", has_tun, "/dev/net/tun 存在" if has_tun else "缺少 /dev/net/tun",
             "modprobe tun，并把 tun 加入 /etc/modules（install.sh 会自动处理）")
        up = os.path.isdir("/sys/class/net/" + dev)
        item(f"TUN 网卡 {dev}", up, "已创建" if up else "未找到该网卡",
             "确认核心以 root 运行且日志里没有 TUN 报错；" + tun_stack_tip() + "再试")
        item("DNS 重定向 (53 → 1053)", st == "dns" or "warn", "已加载" if st == "dns" else "未加载，局域网设备需把 DNS 指向本机 1053 或开启后重试",
             "执行 /opt/mihomo-panel/tproxy.sh dns")
    else:
        item("代理方式", "warn", "已关闭透明代理，仅 7890 端口（HTTP/SOCKS）可用", "在设置里选择 TProxy 或 TUN")
    if alive:
        try:
            ok, ms, rc = dns_probe()
            item("DNS 服务 (127.0.0.1:1053)", ok, f"解析 www.baidu.com 用时 {ms} ms" if ok else f"返回码 {rc}",
                 "检查 设置 → DNS 中的直连 DNS 是否可达")
        except Exception as e:
            item("DNS 服务 (127.0.0.1:1053)", False, str(e)[:120], "确认核心 DNS 已监听 1053 端口")
        for host, label in (("www.baidu.com", "国内域名解析"), ("www.google.com", "国外域名解析")):
            c2, raw = core("GET", f"/dns/query?name={host}&type=A", timeout=10)
            try:
                j = json.loads(raw)
                ans = [a["data"] for a in j.get("Answer") or [] if a.get("type") == 1]
                ok = c2 == 200 and j.get("Status") == 0 and bool(ans)
                item(label, ok, ", ".join(ans[:2]) if ok else f"失败（Status {j.get('Status')}）",
                     "检查直连 / 代理 DNS 设置" + ("；代理 DNS 依赖节点可用" if "google" in host else ""))
            except Exception:
                item(label, False, "查询失败", "检查 DNS 设置")
        if sb and dns_cfg(d)["mode"] == "fake-ip":
            # sing-box 1.13+ 无 sniff_override_destination：设备 DNS 绕过面板时，
            # 规则按嗅探域名命中了代理组，但实际连的是污染 IP，必定失败（mihomo 不受影响）
            bypass = dns_bypass_devices()
            if bypass:
                detail = "；".join(f"{sip}（{len(v)} 个域名，如 {sorted(v)[0]}）"
                                   for sip, v in list(bypass.items())[:5])
                item("DNS 绕过检测", "warn", f"以下设备 DNS 未经过面板：{detail}",
                     "将该设备的 DNS 指向旁路由（或主路由 DHCP 下发旁路由 DNS），或在 设置→DNS 开启「阻止客户端绕过 DNS」")

    def reach(url, use_proxy):
        st = time.time()
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "curl/8"})
            try:
                proxy_opener(use_proxy).open(req, timeout=8).close()
            except urllib.error.HTTPError:
                pass
            return True, int((time.time() - st) * 1000)
        except Exception as e:
            return False, str(e)[:100]
    res = {}
    ths = [threading.Thread(target=lambda k=k, a=a: res.__setitem__(k, reach(*a))) for k, a in
           (("direct", ("https://www.baidu.com", False)), ("cn", ("https://www.baidu.com", True)),
            ("proxy", ("https://www.gstatic.com/generate_204", True)))]
    [t.start() for t in ths]
    [t.join(12) for t in ths]
    for k, label, fix in (("direct", "本机直连外网", "检查旁路由的网关 / DNS 设置（应指向主路由）"),
                          ("cn", "经代理访问国内", "核心或 7890 端口异常，查看日志"),
                          ("proxy", "经代理访问国外", "当前节点不可用：去「节点」测速并切换，或更新订阅")):
        ok, v = res.get(k, (False, "超时"))
        item(label, ok, f"{v} ms（完整请求：DNS + TCP + TLS + 首包，比概览的复用连接延迟高）" if ok else v, fix)
    try:
        du = shutil.disk_usage(CONF_DIR)
        free = du.free / 2**20
        item("磁盘空间", True if free > 100 else "warn" if free > 20 else False, f"剩余 {free:.0f} MB",
             "清理日志（设置 → 日志清理）或扩容")
    except OSError:
        pass
    return out
'''
_SRC['shunt.dnsleak'] = '''"""DNS 防泄露"""
import copy, ipaddress, json, os, re, secrets, socket, threading, time

from .base import udp_dns_query, CN_DNS, DEFAULT_FAKE_FILTER, DNS_DEFAULT, G_SEL, TPROXY_SH
from .data import load
from .mihomo import dns_cfg, dns_policy_of, policy_parts
from .corehttp import core
from .sysopt import sh
from .notify import core_alive
from .unlock import fetch
from .sb_common import active_core
RESOLV_FILE = os.environ.get("PANEL_RESOLV", "/etc/resolv.conf")
CN_GEO = re.compile(r"(?i)^(?:cn|private|.*@cn|.*-cn|tld-cn|geolocation-cn)$")
CN_DOMAIN = re.compile(r"(?i)^[+*.]*[\\w.\\-]*\\.(?:cn|lan|local|arpa|home|localdomain)$")


def is_cn_dns(x):
    x = x.split("#", 1)[0]
    return bool(CN_DNS.search(x)) or x.startswith(("system", "dhcp://"))


def antileak_dns(cur):
    """一键防泄露：fake-ip + respect-rules + 默认上游走代理 DNS + 国内域名走直连 DNS + 阻止客户端绕过"""
    dc = copy.deepcopy(cur)
    dc.update(mode="fake-ip", respect_rules=True, policy=True, block_bypass=True)
    dc["direct"] = [x for x in dc["direct"] if "#" not in x] or list(DNS_DEFAULT["direct"])
    proxy = []
    for x in dc["proxy"]:
        if is_cn_dns(x) or x.startswith("rcode://"):
            continue  # 国内 / 本机 DNS 不能作为代理 DNS
        if not dns_policy_of(x):
            x = x.split("#", 1)[0] + "#" + G_SEL
        proxy.append(x)
    dc["proxy"] = proxy or list(DNS_DEFAULT["proxy"])
    dc["pserver"] = [x for x in dc["pserver"] if "#" not in x and is_cn_dns(x)]
    if not dc["fake_filter"]:
        dc["fake_filter"] = list(DEFAULT_FAKE_FILTER)
    dc["policies"] = [pe for pe in dc["policies"] if not leaky_policy(pe)]
    return dc


def leaky_policy(pe):
    """自定义 nameserver-policy 把非国内域名交给国内 DNS"""
    kind, parts = policy_parts(pe["match"])
    if kind == "rule-set" or all((CN_GEO if kind == "geosite" else CN_DOMAIN).match(p) for p in parts):
        return False
    return any(is_cn_dns(x) for x in pe["servers"]) and not all(x.startswith("rcode://") for x in pe["servers"])


def host_match(pattern, name):
    pattern = pattern.lower()
    if pattern.startswith("+."):
        return name == pattern[2:] or name.endswith(pattern[1:])
    if pattern.startswith("."):
        return name.endswith(pattern)
    if "*" in pattern:
        return bool(re.fullmatch(re.escape(pattern).replace(r"\\*", r"[^.]+"), name))
    return name == pattern


def dns_route(name, d):
    """估算一个域名由哪组上游解析（GEOSITE 需要核心数据库，这里按规则顺序给出说明）"""
    dc = dns_cfg(d)
    short = lambda xs: "、".join(x.replace("https://", "").replace("/dns-query", "") for x in xs[:2]) + (" 等" if len(xs) > 2 else "")
    for h in dc["hosts"]:
        if host_match(h["domain"], name):
            return f"hosts 静态记录（{h['domain']} → {', '.join(h['value'][:3])}，不查询上游）"
    for pe in dc["policies"]:
        kind, parts = policy_parts(pe["match"])
        for part in parts if kind == "domain" else []:
            if host_match(part, name):
                return f"自定义策略 {part} → {short(pe['servers'])}"
    geo = [pe["match"] for pe in dc["policies"] if policy_parts(pe["match"])[0] != "domain"]
    head = ("先匹配自定义 " + "、".join(geo[:3]) + "；") if geo else ""
    if not dc["proxy"]:
        return head + f"全部走直连 DNS（{short(dc['direct'])}）——未设置代理 DNS，国外域名会泄露"
    if dc["policy"]:
        return head + f"属于 geosite:cn / private → 直连 DNS（{short(dc['direct'])}）；其他域名 → 代理 DNS（{short(dc['proxy'])}）"
    return head + f"全部走代理 DNS（{short(dc['proxy'])}）"


def dns_probe_ip(name, server=("127.0.0.1", 1053)):
    """向 mihomo DNS 端口查询 A 记录，返回 (rcode, [IP])"""
    qid = secrets.randbits(16)
    pkt = qid.to_bytes(2, "big") + b"\\x01\\x00\\x00\\x01\\x00\\x00\\x00\\x00\\x00\\x00"
    pkt += b"".join(bytes([len(x)]) + x.encode() for x in name.split(".")) + b"\\x00\\x00\\x01\\x00\\x01"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(4)
    try:
        s.sendto(pkt, server)
        data = _recv_reply(s, qid)
    finally:
        s.close()
    rcode, ips, ok = parse_a_records(data)
    if not ok:
        raise ValueError(f"DNS 应答格式错误（{len(data)} 字节）")
    return rcode, ips


def _recv_reply(s, qid):
    """读取与 qid 匹配的 DNS 应答：丢弃 ID 不符的迟到 / 串号报文，直到超时"""
    while True:
        data = s.recv(1500)
        if len(data) >= 12 and int.from_bytes(data[:2], "big") == qid and data[2] & 0x80:
            return data


def parse_a_records(data):
    """解析应答里的 A 记录，返回 (rcode, [IP], 完整)；截断 / 格式错误时不再抛 IndexError"""
    if len(data) < 12:
        return 0, [], False
    rcode, an = data[3] & 0x0F, int.from_bytes(data[6:8], "big")
    pos, ips = [12], []

    def skip_name():
        i = pos[0]
        while i < len(data):
            n = data[i]
            if n == 0:
                pos[0] = i + 1
                return True
            if n & 0xC0 == 0xC0:
                pos[0] = i + 2
                return pos[0] <= len(data)
            i += n + 1
        return False
    if not skip_name():  # 问题部分
        return rcode, ips, False
    pos[0] += 4
    for _ in range(an):
        if not skip_name() or pos[0] + 10 > len(data):
            return rcode, ips, False
        i = pos[0]
        typ, rdlen = int.from_bytes(data[i:i + 2], "big"), int.from_bytes(data[i + 8:i + 10], "big")
        i += 10
        if i + rdlen > len(data):
            return rcode, ips, False
        if typ == 1 and rdlen == 4:
            ips.append(".".join(str(b) for b in data[i:i + 4]))
        pos[0] = i + rdlen
    return rcode, ips, True


def resolv_servers():
    try:
        with open(RESOLV_FILE) as f:
            return [l.split()[1] for l in f if l.strip().startswith("nameserver") and len(l.split()) > 1]
    except OSError:
        return []


def _leak_probe(name):
    """让核心解析一个随机子域名，触发上游查询。mihomo 用 /dns/query；sing-box 的 Clash API 没有这个接口，
    改为向 dns-in(1053) 发 TXT 查询（A / AAAA 在 fake-ip 模式下直接返回假地址，不会查上游）"""
    try:
        if active_core() == "singbox":
            udp_dns_query(name, qtype=16, timeout=10)
        else:
            core("GET", f"/dns/query?name={name}&type=A", timeout=10)
    except Exception:
        pass


def leak_live():
    """经代理访问 bash.ws 的 DNS 泄露测试：用 mihomo 的解析器（/dns/query，与客户端走同样的上游分流）
    解析一组随机子域名，再读取 bash.ws 记录到的递归解析器 IP / 国家"""
    out = {"ok": None, "resolvers": [], "egress": [], "error": "", "source": "bash.ws"}
    try:
        code, _, tid = fetch("https://bash.ws/id", timeout=10)
        tid = tid.strip()
        if code != 200 or not re.fullmatch(r"[a-z0-9]{6,40}", tid):
            raise IOError(f"获取测试 ID 失败（HTTP {code}）")
        ths = [threading.Thread(target=_leak_probe, args=(f"{i}.{tid}.bash.ws",)) for i in range(1, 9)]
        [t.start() for t in ths]
        [t.join(12) for t in ths]
        time.sleep(1)
        code, _, body = fetch(f"https://bash.ws/dnsleak/test/{tid}?json", timeout=15)
        items = json.loads(body)
        if isinstance(items, dict):  # 没收到任何解析请求时 bash.ws 返回 {"error": "No DNS servers found..."}，以前当列表遍历报 'str' object has no attribute 'get'
            out["error"] = "bash.ws 没有记录到解析请求（代理 DNS 可能不可用）"
            return out
        for x in items if isinstance(items, list) else []:
            if not isinstance(x, dict):
                continue
            e = {"ip": x.get("ip"), "country": (x.get("country") or "").upper(), "country_name": x.get("country_name") or "",
                 "org": x.get("asn") or x.get("org") or ""}
            if x.get("type") == "dns":
                e["cn"] = e["country"] == "CN"
                out["resolvers"].append(e)
            elif x.get("type") == "ip":
                out["egress"].append(e)
        if not out["resolvers"]:
            out["error"] = "bash.ws 没有记录到解析请求（代理 DNS 可能不可用）"
        else:
            out["ok"] = not any(r["cn"] for r in out["resolvers"])
    except Exception as e:
        out["error"] = "在线检测失败：" + (str(e)[:160] or e.__class__.__name__)
    return out


def leak_check(live=True):
    d = load()
    dc = dns_cfg(d)
    items = []

    def item(name, ok, detail, fix=""):
        items.append({"name": name, "ok": ok, "detail": detail, "fix": fix if ok is not True else ""})

    item("增强模式", True if dc["mode"] == "fake-ip" else "warn",
         "fake-ip：客户端只拿到 198.18.x.x，域名由 mihomo 按规则处理" if dc["mode"] == "fake-ip" else
         "redir-host：客户端拿到真实 IP，可能绕过按域名分流", "改用 fake-ip")
    ns = dc["proxy"] or dc["direct"]
    bad_ns = [x for x in ns if is_cn_dns(x)]
    unrouted = [x for x in dc["proxy"] if not dns_policy_of(x) and not dc["respect_rules"]]
    if not dc["proxy"]:
        item("默认上游（未知 / 国外域名）", False, "未设置代理 DNS，所有域名都发给直连 DNS：" + "、".join(dc["direct"][:2]),
             "添加代理 DNS，如 https://1.1.1.1/dns-query#🚀 节点选择")
    elif bad_ns:
        item("默认上游（未知 / 国外域名）", False, "代理 DNS 里有国内 / 本机解析器：" + "、".join(bad_ns[:3]), "从代理 DNS 中删除它们，国内 DNS 只放在直连 DNS")
    elif unrouted:
        item("默认上游（未知 / 国外域名）", "warn", "这些代理 DNS 没有指定 #策略组，且未开启 respect-rules，会直连发出：" + "、".join(unrouted[:2]),
             "开启 respect-rules，或在地址后加 #🚀 节点选择")
    else:
        item("默认上游（未知 / 国外域名）", True, "代理 DNS 经节点查询：" + "、".join(dc["proxy"][:2]))
    item("国内域名分流", True if dc["policy"] else "warn",
         "geosite:cn / private → 直连 DNS，其余全部走代理 DNS（不再按 geolocation-!cn 白名单，未知域名也不会发给国内 DNS）"
         if dc["policy"] else "国内域名也走代理 DNS：不泄露，但国内 CDN 解析可能不是最近节点", "开启「国内域名走直连 DNS」")
    # sing-box 没有 respect-rules：开启时生成配置把没写 #策略组 的国外 DNS 指定经节点选择发出（v8.10.10 前直连发出，这里却显示正常）
    sb = active_core(d) == "singbox"
    item("respect-rules", True if dc["respect_rules"] else "warn",
         (f"sing-box：没写 #策略组 的代理 DNS / DNS 策略上游经 {G_SEL} 发出，国内 / 内网 DNS 直连" if sb else
          "DNS 查询连接遵循分流规则，节点域名用国内 DoH 解析（proxy-server-nameserver）") if dc["respect_rules"] else "未开启",
         "开启 respect-rules")
    item("fallback / 系统 hosts", True, "未使用 fallback（避免同时向国内外 DNS 发同一查询）；use-system-hosts 已关闭；prefer-h3 已关闭")
    lp = [pe["match"] for pe in dc["policies"] if leaky_policy(pe)]
    if lp:
        item("自定义 DNS 策略", False, "以下非国内域名被交给国内 DNS：" + "、".join(lp[:3]), "改为代理 DNS，或删除这些策略")
    item("阻止客户端绕过 DNS", True if dc["block_bypass"] else "warn",
         "已拒绝局域网设备的 DoT / DoQ（853）与常见公共 DoH 服务器" if dc["block_bypass"] else
         "未开启：浏览器 / 手机的“安全 DNS”、私人 DNS 可能直连国内 DoH，绕过 mihomo", "开启「阻止客户端绕过 DNS」")
    mode = d["proxy_mode"]
    if mode == "off":
        item("DNS 劫持", "warn", "透明代理已关闭，客户端 DNS 不经过 mihomo", "在设置里选择 TProxy 或 TUN，并把客户端 DNS 设为旁路由")
    else:
        _, st = sh(TPROXY_SH + " status", timeout=10)
        st = st.strip().splitlines()[-1] if st.strip() else "off"
        okh = st in ("tproxy", "dns") or mode == "tun"
        item("DNS 劫持", True if okh else False, ("TUN dns-hijack any:53" if mode == "tun" else "发往旁路由 53 端口的查询重定向到 1053")
             if okh else "未检测到 53 → 1053 重定向规则", "执行 /opt/mihomo-panel/tproxy.sh apply")
    has6 = bool(re.search(r"inet6 [23]", sh("ip -o -6 addr show scope global 2>/dev/null")[1]))
    if not d.get("ipv6"):
        item("IPv6", "warn" if has6 else True, "本机有公网 IPv6，但 IPv6 透明代理未开启：客户端可能通过主路由下发的 IPv6 DNS（RDNSS）绕过"
             if has6 else "未开启 IPv6，mihomo DNS 不返回 AAAA，与透明代理一致",
             "在主路由关闭 IPv6 DNS 下发 / DHCPv6，或开启 IPv6 透明代理")
    else:
        item("IPv6", True, "IPv6 透明代理已开启，DNS 同时处理 AAAA")
    item("域名嗅探", True if d.get("sniffer", True) else "warn", "已开启：按 IP 发起的连接也能识别域名分流" if d.get("sniffer", True)
         else "未开启：Fake-IP 过滤的域名、直接按 IP 的连接无法按域名分流", "设置 → 核心服务 → 开启域名嗅探")
    rs = resolv_servers()
    item("旁路由本机解析", True if rs else "warn",
         ("/etc/resolv.conf：" + "、".join(rs[:3]) + "。面板自身的外网请求经 mihomo 代理（远端解析），规则测试改用 mihomo DNS，本机只解析国内 / 直连目标")
         if rs else "/etc/resolv.conf 没有 nameserver", "把 /etc/resolv.conf 指向主路由或国内 DNS（如 223.5.5.5）")
    out = {"items": items, "live": None, "fakeip": None}
    if live and core_alive():
        try:
            rc, ips = dns_probe_ip("www.google.com")
            fake = bool(ips) and all(ipaddress.ip_address(i) in ipaddress.ip_network("198.18.0.0/15") for i in ips)
            out["fakeip"] = {"name": "www.google.com", "ips": ips, "rcode": rc, "fake": fake}
            if dc["mode"] == "fake-ip":
                item("客户端查询测试", True if fake else "warn", f"www.google.com → {', '.join(ips) or '无记录'}" +
                     ("（Fake-IP，客户端查询不触发上游解析）" if fake else ""), "检查 Fake-IP 过滤列表是否包含该域名")
        except Exception as e:
            item("客户端查询测试", False, f"向 127.0.0.1:1053 查询失败：{e}", "确认核心 DNS 已监听 1053")
        out["live"] = leak_live()
        lv = out["live"]
        if lv["ok"] is True:
            item("在线泄露检测", True, "上游解析器：" + "、".join(f"{r['ip']}（{r['country'] or '?'} {r['org'][:30]}）" for r in lv["resolvers"][:4]))
        elif lv["ok"] is False:
            cn = [r for r in lv["resolvers"] if r["cn"]]
            item("在线泄露检测", False, "发现国内解析器：" + "、".join(f"{r['ip']}（{r['org'][:30]}）" for r in cn[:4]), "点击「一键应用防泄露设置」")
        else:
            item("在线泄露检测", "warn", lv["error"], "确认节点可用后重试；离线时以上配置检查仍然有效")
    elif live:
        item("在线泄露检测", "warn", "核心未运行，只做了配置检查", "启动核心后重试")
    out["bad"] = sum(1 for x in items if x["ok"] is False)
    out["warn"] = sum(1 for x in items if x["ok"] == "warn")
    return out
'''
_SRC['shunt.ipinfo'] = '''"""公网 IP 与归属地查询（概览页展示）"""
import json
import re
import shlex
import socket
import subprocess
import threading
import time

from .base import MIXED
from .corehttp import core
from .errlog import log_err
from .sysopt import proxy_opener, sh

IP_ECHO = "https://api.ipify.org"  # 返回纯文本公网 IP
GEO_URL = "http://ip-api.com/json/%s?lang=zh-CN&fields=status,country,regionName,city"

_CACHE = {"t": 0, "v": None, "fail": 0, "next": 0}
_TTL = 600  # 两边都查到才缓存 10 分钟；公网 IP 变化不频繁
_LOCK = threading.Lock()


def _backoff(n):
    """查询失败后的重试间隔：5、10、20、40、60、60… 秒"""
    return min(60, 5 * 2 ** max(0, n - 1))


def reset():
    """核心重启 / 切换后作废缓存：出口可能变了，失败的结果也立刻允许重查"""
    _CACHE.update(t=0, v=None, fail=0, next=0)
    _GEN[0] += 1


def core_ready():
    """代理出口查询的前提：核心控制接口有响应、mixed 端口能连上。重启瞬间两者都不在，此时查只会白等超时"""
    try:
        code, _ = core("GET", "/version", timeout=3)
        if code != 200:
            return False
        with socket.create_connection(("127.0.0.1", MIXED), timeout=2):
            return True
    except Exception:
        return False


def _fetch(url, via_proxy, timeout=10):
    try:
        with proxy_opener(via_proxy).open(url, timeout=timeout) as r:
            return r.read().decode(errors="ignore").strip()
    except Exception:
        return ""


def _phys_iface():
    """物理网卡：优先默认路由的非 TUN dev；TUN 接管默认路由时，退化为第一个 UP 的非 TUN/非 lo 网卡。
    TUN 模式会拦截本机全部流量，直连请求必须绑定物理网卡才能绕过 TUN 拿到真实公网 IP。"""
    try:
        out = subprocess.check_output(["ip", "-4", "route", "show", "default"],
                                      text=True, timeout=5)
        for line in out.splitlines():
            m = re.search(r"\\bdev (\\S+)", line)
            if m and not _is_tun(m.group(1)):
                return m.group(1)
    except Exception:
        pass
    try:
        out = subprocess.check_output(["ip", "-o", "link", "show"], text=True, timeout=5)
        for line in out.splitlines():
            m = re.search(r"^\\d+: (\\S+?):.*state UP", line)
            if m and m.group(1) != "lo" and not _is_tun(m.group(1)):
                return m.group(1)
    except Exception:
        pass
    return None


def _is_tun(dev):
    return (dev.startswith("tun") or dev.startswith("utun") or dev == "mihomo"
            or dev.startswith("docker") or dev.startswith("br-") or dev.startswith("veth"))


def _fetch_cn_ip():
    """经代理端口取国内 IP 查询服务。若代理按规则直连中国网站，可拿到真实公网 IP；
    响应自带中文归属地（格式：当前 IP：x.x.x.x  来自于：...），无需再查 geo。"""
    try:
        with proxy_opener(True).open("https://myip.ipip.net", timeout=10) as r:
            txt = r.read().decode(errors="ignore")
        m = re.search(r"当前 IP：([0-9.]+)\\s*来自于：(.+)", txt)
        if m:
            return m.group(1).strip(), m.group(2).strip()
    except Exception as e:
        log_err("出口 IP 查询 (myip.ipip.net)", e)
    return "", ""


def _fetch_direct_ip():
    """直连取公网 IP：
    1. 优先 https://1.1.1.1/cdn-cgi/trace（固定 IP 无需 DNS，不受 fake-ip 污染），绑定物理网卡绕过 TUN
    2. 回退 api.ipify.org（DNS 可能被 fake-ip 污染）
    3. 回退普通直连"""
    dev = _phys_iface()
    if dev:
        code, out = sh(f"curl -s --interface {shlex.quote(dev)} --max-time 10 https://1.1.1.1/cdn-cgi/trace",
                       timeout=15)
        if code == 0:
            m = re.search(r"^ip=([0-9a-fA-F:.]+)", out, re.M)
            if m:
                return m.group(1)
        code, out = sh(f"curl -s --interface {shlex.quote(dev)} --max-time 10 {IP_ECHO}", timeout=15)
        out = out.strip()
        if code == 0 and re.fullmatch(r"[0-9a-fA-F:.]+", out):
            return out
    # 不绑网卡、也不走代理：1.1.1.1 是字面 IP，不依赖本机 DNS（核心重启时本机 DNS 可能指向还没起来的核心）
    code, out = sh("curl -s --noproxy '*' --max-time 8 https://1.1.1.1/cdn-cgi/trace", timeout=12)
    m = re.search(r"^ip=([0-9a-fA-F:.]+)", out or "", re.M) if code == 0 else None
    if m:
        return m.group(1)
    return _fetch(IP_ECHO, False)


_LOC_SUFFIX = re.compile(r"(?:省|市|自治区|特别行政区|壮族自治区|回族自治区|维吾尔自治区)$")


def loc_dedup(loc):
    """归属地去掉相邻重复的地名：「中国 重庆 重庆 电信」→「中国 重庆 电信」，「北京市 北京」也算重复"""
    out = []
    for p in (loc or "").split():
        prev = out[-1] if out else ""
        # 完全相同，或只差一个「市 / 省」后缀（重庆市 / 重庆）才算重复；吉林省 / 吉林市 是两级地名，保留
        if out and (p == prev or (_LOC_SUFFIX.sub("", p) == _LOC_SUFFIX.sub("", prev) and (p == _LOC_SUFFIX.sub("", p) or prev == _LOC_SUFFIX.sub("", prev)))):
            if len(p) < len(out[-1]):
                out[-1] = p  # 保留更短的写法（重庆 而不是 重庆市）
            continue
        out.append(p)
    return " ".join(out)


GEO_URL2 = "https://ipwho.is/%s?lang=zh-CN"  # 备用归属地服务（HTTPS）


def _geo_parse(txt):
    try:
        j = json.loads(txt or "{}")
    except ValueError:
        return ""
    if j.get("status") == "success":  # ip-api.com
        return " ".join(p for p in (j.get("country"), j.get("regionName"), j.get("city")) if p)
    if j.get("success") is True:  # ipwho.is
        return " ".join(p for p in (j.get("country"), j.get("region"), j.get("city")) if p)
    return ""


def _geo(ip):
    """归属地：ip-api.com 直连 → 经内核 mixed 端口 → 备用 ipwho.is。
    以前只直连 ip-api.com 一次：路由器本机 DNS 指向内核时，ip-api.com 可能被解析成 Fake-IP 而直连不通，
    或撞上 ip-api 免费版每分钟 45 次的限额，代理出口就只有 IP 没有归属地"""
    if not ip:
        return ""
    ready = None
    for url, via_proxy in ((GEO_URL, False), (GEO_URL, True), (GEO_URL2, True), (GEO_URL2, False)):
        if via_proxy:
            if ready is None:
                ready = core_ready()
            if not ready:
                continue
        loc = _geo_parse(_fetch(url % ip, via_proxy, timeout=8))
        if loc:
            return loc
    log_err("IP 归属地查询", RuntimeError(f"{ip} 所有归属地服务都没有结果"))
    return ""


def _query():
    """代理出口强制经核心 mixed 端口；本地公网先经代理取国内服务（规则直连中国网站时拿到真实 IP，自带中文归属地），
    拿不到或与出口相同时改为绕过核心直连查询。两边并行，避免串行超时累加到半分钟以上"""
    res = {}

    def proxy_side():
        ip = _fetch(IP_ECHO, True) if core_ready() else ""
        res["proxy"] = {"ip": ip, "loc": loc_dedup(_geo(ip))}

    t = threading.Thread(target=proxy_side, daemon=True)
    t.start()
    direct_ip, direct_loc = _fetch_cn_ip() if core_ready() else ("", "")
    t.join(40)
    proxy_ip = (res.get("proxy") or {}).get("ip", "")
    if not direct_ip or direct_ip == proxy_ip:
        direct_ip = _fetch_direct_ip()
        direct_loc = _geo(direct_ip)
    return {"direct": {"ip": direct_ip, "loc": loc_dedup(direct_loc)},
            "proxy": res.get("proxy") or {"ip": "", "loc": ""}}


_EMPTY = {"direct": {"ip": "", "loc": ""}, "proxy": {"ip": "", "loc": ""}}
_WAIT = 45  # 别人正在查时最多等这么久（一次查询最慢约 40 秒）
_INFLIGHT = None  # 正在进行的查询：threading.Event，查完 set
_GEN = [0]  # reset() 加一：重启前发出的查询结果不再写进缓存


def _from_cache(now):
    """缓存可用就返回（失败退避期内带 retry），否则 None。调用方持有 _LOCK"""
    v = _CACHE["v"]
    if v is None:
        return None
    if not _CACHE["fail"] and now - _CACHE["t"] < _TTL:
        return v
    if _CACHE["fail"] and now < _CACHE["next"]:
        return dict(v, retry=max(1, int(_CACHE["next"] - now + 0.999)))
    return None


def _store(v, gen):
    """记下查询结果，返回给调用方的值。调用方持有 _LOCK"""
    now = time.time()
    if v["direct"]["ip"] and v["proxy"]["ip"]:
        if gen == _GEN[0]:
            # 归属地缺一边时只缓存 2 分钟（_TTL - 120 秒前视为过期），下次再补查
            _CACHE.update(t=now if v["direct"]["loc"] and v["proxy"]["loc"] else now - _TTL + 120, v=v, fail=0, next=0)
        return v
    if gen != _GEN[0]:
        return dict(v, retry=5)
    _CACHE["fail"] += 1
    wait = _backoff(_CACHE["fail"])
    _CACHE.update(t=now, v=v, next=now + wait)
    return dict(v, retry=wait)


def get_ipinfo(query=None):
    """返回 {"direct": {"ip","loc"}, "proxy": {"ip","loc"}, "retry": 秒}；查不到的一边 ip 为空串。
    以前查询失败（核心刚重启、开机网络未就绪）的空结果也缓存 10 分钟，前端又只在进概览时拉一次，于是一直显示「-」。
    现在只有两边都查到才缓存；有失败就按 5→60 秒退避重查，retry 告诉前端多久后再来拉。
    以前整个查询（最慢约 40 秒）都在 _LOCK 里，几个页面 / 请求同时来就一个接一个排队，最后一个要等几分钟。
    现在同一时间只查一次：其他调用方有旧结果就先拿旧结果（带 retry 让前端稍后再拉），没有就等这次查询，最多等 _WAIT 秒"""
    global _INFLIGHT
    query = query or _query
    with _LOCK:
        hit = _from_cache(time.time())
        if hit is not None:
            return hit
        ev, owner = _INFLIGHT, _INFLIGHT is None
        if owner:
            ev = _INFLIGHT = threading.Event()
        gen = _GEN[0]
        stale = _CACHE["v"]
    if not owner:
        if stale is not None:
            return dict(stale, retry=3)
        ev.wait(_WAIT)
        with _LOCK:
            hit = _from_cache(time.time())
            v = _CACHE["v"]
        if hit is not None:
            return hit
        return dict(v if v is not None else _EMPTY, retry=3 if not ev.is_set() else 5)
    v = None
    try:
        v = query()
    except Exception as e:
        log_err("出口 IP 查询", e)
    finally:
        with _LOCK:
            out = _store(v if isinstance(v, dict) and "direct" in v and "proxy" in v else _EMPTY, gen)
            _INFLIGHT = None
        ev.set()
    return out
'''
_SRC['shunt.migrate'] = '''"""v5 → v6 升级：保留节点选择"""
import os, time
from urllib.parse import quote

from .base import CONF_DIR, PANEL_DIR
from .data import legacy_map, load, read_json, write_json
from .core import reload_core
from .corehttp import core, core_json
from .sb_common import active_core
from .notify import core_alive
SEL_FILE = os.path.join(PANEL_DIR, "selected.json")


def snapshot_selections(force=False):
    """记录各手动选择组当前选中的节点 / 分组（升级改名前调用）"""
    if os.path.isfile(SEL_FILE) and not force:
        return None
    j = core_json("/proxies", timeout=5)
    if not j:
        return None
    sel = {n: p["now"] for n, p in (j.get("proxies") or {}).items() if p.get("type") == "Selector" and p.get("now") and n != "GLOBAL"}
    write_json(SEL_FILE, sel)
    return sel


def restore_selections():
    """按新名称恢复升级前的选择；返回恢复的组数"""
    sel = read_json(SEL_FILE, None)
    if not sel:
        return 0
    mp = legacy_map(load()["groups_cfg"].get("strategy"))
    proxies = (core_json("/proxies", timeout=5) or {}).get("proxies") or {}
    n = 0
    for g, now in sel.items():
        g2, now2 = mp.get(g, g), mp.get(now, now)
        p = proxies.get(g2) or {}
        if p.get("type") == "Selector" and now2 in (p.get("all") or []) and p.get("now") != now2:
            code, _ = core("PUT", "/proxies/" + quote(g2, safe=""), {"name": now2})
            n += code < 300
    try:
        os.remove(SEL_FILE)
    except OSError:
        pass
    print(time.strftime("%F %T"), f"[upgrade] 已按新名称恢复 {n} 个策略组的选择", flush=True)
    return n


def startup_migrate():
    """面板升级后首次启动：核心还在跑旧配置（旧组名）时，先记下选择，再生成新配置并恢复选择"""
    for _ in range(30):
        if core_alive():
            break
        time.sleep(2)
    else:
        return
    cur = {} if active_core() == "singbox" else read_json(os.path.join(CONF_DIR, "config.yaml"), None) or {}
    old = set(legacy_map())
    if any(g.get("name") in old for g in cur.get("proxy-groups") or []):
        snapshot_selections()
        ok, msg = reload_core()
        print(time.strftime("%F %T"), "[upgrade] 已按新版策略组重新生成配置：", ok, msg[:120], flush=True)
        time.sleep(1)
    if os.path.isfile(SEL_FILE):
        restore_selections()
'''
_SRC['shunt.https'] = '''"""HTTPS"""
import os, shlex, socket, ssl, sys
from http.server import ThreadingHTTPServer

from .base import CERT, KEY, PANEL_DIR, PANEL_SVC
from .sysopt import sh


def _lan_ip():
    """本机局域网 IP：UDP connect 只查路由出口地址，不实际发包；拿不到返回空串"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:  # connect 失败（无默认路由）时也关掉 socket
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return ""


def gen_cert():
    if not sh("command -v openssl")[1]:
        sh("apk add --no-cache openssl", timeout=120)
    os.makedirs(PANEL_DIR, exist_ok=True)
    san = "DNS:shunt.lan"
    ip = _lan_ip()
    if ip:
        san += f",IP:{ip}"
    code, out = sh(f"openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj '/CN=Shunt' "
                   f"-addext 'subjectAltName={san}' "
                   f"-keyout '{KEY}' -out '{CERT}'", timeout=120)
    if code == 0:
        os.chmod(KEY, 0o600)
    return code == 0, out


def https_ready(d):
    return bool(d.get("https")) and os.path.isfile(CERT) and os.path.isfile(KEY)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        e = sys.exc_info()[1]
        if isinstance(e, (ssl.SSLError, ConnectionError, socket.timeout, BrokenPipeError)):
            return  # 浏览器用 http 访问 https 端口、客户端断开等，静默忽略
        super().handle_error(request, client_address)


_DEFAULT_RESTART = "rc-service " + PANEL_SVC + " restart"  # 可信默认值（含参数，直接拼）


def restart_self():
    cmd = os.environ.get("PANEL_RESTART")
    if cmd is None:
        cmd = _DEFAULT_RESTART
    else:
        cmd = shlex.quote(cmd)  # 环境变量来源不可信：整体引用为单个词，防止注入
    sh(f"(sleep 1; {cmd}) >/dev/null 2>&1 &")
'''
_SRC['shunt.access'] = '''"""访问控制"""
import ipaddress, os, time

from .data import load
# 面板没有密码：只接受局域网来源，Host 必须是 IP / 局域网名（防 DNS 重绑定），改动类请求必须带自定义头（防跨站请求）
LAN_EXTRA = [ipaddress.ip_network(n) for n in ("100.64.0.0/10",)]  # CGNAT / Tailscale
LAN_SUFFIX = (".lan", ".local", ".home", ".internal", ".home.arpa", ".localdomain", ".router", ".intranet", ".ts.net")
ACL = {"t": 0, "public": False, "hosts": set()}


def acl_cfg():
    if time.time() - ACL["t"] > 5:
        d = load()
        env = {h.strip().lower() for h in os.environ.get("PANEL_ALLOW_HOSTS", "").split(",") if h.strip()}
        ACL.update(t=time.time(), public=bool(d.get("allow_public")) or os.environ.get("PANEL_ALLOW_PUBLIC") == "1",
                   hosts={str(h).strip().lower() for h in d.get("allow_hosts") or [] if str(h).strip()} | env)
    return ACL


def ip_is_lan(ip):
    try:
        a = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return a.is_private or a.is_loopback or a.is_link_local or any(a in n for n in LAN_EXTRA)


def host_name(host):
    h = (host or "").strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h[1:]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def host_ok(host):
    h = host_name(host).rstrip(".")
    if not h:
        return False  # 空 Host 拒绝
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    a = acl_cfg()
    # 单标签主机名只放行 localhost（防 DNS 重绑定绕过）；其余单标签需用户显式加入允许列表
    return a["public"] or h == "localhost" or h.endswith(LAN_SUFFIX) or h in a["hosts"]
'''
_SRC['shunt.auth'] = '''"""可选的面板密码（默认关闭，行为与以前完全一样）。

设置后所有 /api 请求都要先登录：浏览器用登录后下发的 HttpOnly 会话 Cookie，脚本用请求头
`X-Shunt-Auth: <密码>` 或 `Authorization: Bearer <密码>`。X-Shunt 头、Origin、来源地址 / Host 检查照旧先做。
data.json 里只存 PBKDF2 哈希（panel_password）；会话令牌 = 过期时间 + 以密码哈希为密钥的 HMAC，
不落盘、面板重启后仍有效，改密码 / 关闭密码后旧会话全部失效。
"""
import hashlib, hmac, secrets, threading, time
from http.cookies import SimpleCookie

from .data import load, update
from .access import acl_cfg

COOKIE = "shunt_session"
SESSION_TTL = 30 * 86400
ITER = 60000  # 路由器 CPU 上约几百毫秒；只在登录 / 改密码时算，脚本请求头的密码校验结果有缓存
MIN_LEN = 4
MAX_LEN = 256
FAIL_MAX, FAIL_LOCK = 5, 60  # 同一来源连续输错 5 次锁 60 秒
AUTH = {"t": 0, "hash": ""}
_FAILS = {}  # ip → [连续失败次数, 锁定到期时间]
_OK = {}  # (哈希, sha256(密码)) → True：请求头带密码的脚本不必每次算 PBKDF2
_LOCK = threading.Lock()


def hash_password(pw, salt=None, it=ITER):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), it)
    return f"pbkdf2_sha256${it}${salt}${dk.hex()}"


def verify_password(pw, stored):
    if not isinstance(pw, str) or not pw or not isinstance(stored, str) or not stored:
        return False
    key = (stored, hashlib.sha256(pw.encode("utf-8", "surrogatepass")).hexdigest())
    if _OK.get(key):
        return True
    try:
        algo, it, salt, _ = stored.split("$")
        ok = algo == "pbkdf2_sha256" and hmac.compare_digest(hash_password(pw, salt, int(it)), stored)
    except (ValueError, TypeError):
        return False
    if ok:
        if len(_OK) > 16:
            _OK.clear()
        _OK[key] = True
    return ok


def auth_hash(fresh=False):
    """当前密码哈希（空串 = 没有设置密码）；缓存 5 秒，与 acl_cfg 一致"""
    if fresh or time.time() - AUTH["t"] > 5:
        h = load().get("panel_password")
        AUTH.update(t=time.time(), hash=h if isinstance(h, str) else "")
    return AUTH["hash"]


def auth_on():
    return bool(auth_hash())


def set_password(pw):
    """设置 / 清除（pw 为空）面板密码；返回新的哈希。格式不对抛 ValueError"""
    pw = pw or ""
    if not isinstance(pw, str):
        raise ValueError("密码格式不正确")
    if pw and not (MIN_LEN <= len(pw) <= MAX_LEN):
        raise ValueError(f"密码长度应为 {MIN_LEN}～{MAX_LEN} 个字符")
    h = hash_password(pw) if pw else ""
    update(lambda d: d.update(panel_password=h))
    AUTH.update(t=time.time(), hash=h)
    _OK.clear()
    return h


def session_new(stored=None, now=None):
    stored = auth_hash() if stored is None else stored
    exp = int((now or time.time()) + SESSION_TTL)
    sig = hmac.new(stored.encode(), f"shunt-session:{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def session_ok(token, stored=None, now=None):
    stored = auth_hash() if stored is None else stored
    if not stored or not isinstance(token, str) or "." not in token:
        return False
    exp, _, sig = token.partition(".")
    try:
        if int(exp) < (now or time.time()):
            return False
    except ValueError:
        return False
    good = hmac.new(stored.encode(), f"shunt-session:{exp}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, sig)


def cookie_token(headers):
    raw = headers.get("Cookie") or ""
    if COOKIE not in raw:
        return ""
    try:
        c = SimpleCookie()
        c.load(raw)
    except Exception:
        return ""
    return c[COOKIE].value if COOKIE in c else ""


def header_password(headers):
    pw = headers.get("X-Shunt-Auth") or ""
    if not pw:
        a = headers.get("Authorization") or ""
        if a[:7].lower() == "bearer ":
            pw = a[7:].strip()
    return pw


def request_ok(headers):
    """没设密码时永远放行；设了密码时要有效会话 Cookie 或正确的密码请求头"""
    stored = auth_hash()
    if not stored:
        return True
    if session_ok(cookie_token(headers), stored):
        return True
    pw = header_password(headers)
    return bool(pw) and verify_password(pw, stored)


def login_locked(ip):
    """返回还要等几秒（0 = 没锁）"""
    with _LOCK:
        f = _FAILS.get(ip)
        return max(0, int(f[1] - time.time()) + 1) if f and f[1] > time.time() else 0


def login_attempt(ip, pw):
    """校验登录密码，记录失败次数；返回 (成功?, 会话令牌或提示)"""
    wait = login_locked(ip)
    if wait:
        return False, f"密码错误次数过多，请 {wait} 秒后再试"
    stored = auth_hash(fresh=True)
    if not stored:
        return True, ""
    if verify_password(pw, stored):
        with _LOCK:
            _FAILS.pop(ip, None)
        return True, session_new(stored)
    with _LOCK:
        if len(_FAILS) > 1000:
            _FAILS.clear()
        f = _FAILS.setdefault(ip, [0, 0])
        f[0] += 1
        if f[0] >= FAIL_MAX:
            f[0], f[1] = 0, time.time() + FAIL_LOCK
    return False, "密码错误"


def cookie_header(token, secure=False, clear=False):
    parts = [f"{COOKIE}={'' if clear else token}", "Path=/", "HttpOnly", "SameSite=Strict",
             "Max-Age=" + ("0" if clear else str(SESSION_TTL))]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def exposure_warning(d=None):
    """允许外网访问 / 允许了额外域名但没有设置密码时的提醒（空串 = 不需要提醒）"""
    if auth_on():
        return ""
    a = acl_cfg()
    if a["public"]:
        why = "已允许外网访问面板（--allow-public on）"
    elif a["hosts"]:
        why = "已允许通过域名 " + "、".join(sorted(a["hosts"])[:3]) + " 访问面板（--allow-host）"
    else:
        return ""
    return (why + "，但面板没有设置密码，能打开这个地址的人都能管理代理。请在 设置 → 通知与安全 → 面板密码 设置密码，"
            "或在路由器上执行：python3 /opt/mihomo-panel/server.py --password <新密码>")

'''
_SRC['shunt.update_guard'] = '''"""面板更新失败自动回滚"""
import os

from .errlog import log_err
from .base import AUTO_TAIL, BASE, G_AUTO, G_DIRECT, G_MANUAL, G_SEL, LB_PREFIX, LB_TAIL, PANEL_VERSION, PORT, SIDE_GROUPS, local_file
from .data import LB_ALL, map_refs
from .sysopt import sh
GUARD = os.path.join(BASE, ".update-guard.sh")
ROLLBACK_NOTE = os.path.join(BASE, ".rollback-note")


def update_guard_script(files):
    """更新后由独立的 shell 脚本检查新面板：90 秒内一直打不开就把 .backup 里的旧版本放回去并重启"""
    bak = os.path.join(BASE, ".backup")
    cps = "\\n".join(f"  [ -f '{bak}/{f}' ] && cp '{bak}/{f}' '{local_file(f)}'" for f in files)
    restart = os.environ.get("PANEL_RESTART", "rc-service shunt restart 2>/dev/null || rc-service mihomo-panel restart")  # 回滚时服务可能已改名
    return f"""#!/bin/sh
# 由 Shunt 面板生成：在线更新后自动检查，新版启动失败时回滚
sleep 30
i=0
while [ $i -lt 6 ]; do
  python3 - <<'PY' && {{ rm -f "$0"; exit 0; }}
import ssl, sys, urllib.request
ctx = ssl._create_unverified_context()
op = urllib.request.build_opener(urllib.request.ProxyHandler({{}}), urllib.request.HTTPSHandler(context=ctx))
for u in ("http://127.0.0.1:{PORT}/manifest.webmanifest", "https://127.0.0.1:{PORT}/manifest.webmanifest"):
    try:
        op.open(u, timeout=5)
        sys.exit(0)
    except Exception:
        pass
sys.exit(1)
PY
  i=$((i+1)); sleep 10
done
{cps}
echo "新版面板 90 秒内没有启动成功，已自动恢复为 v{PANEL_VERSION}" > '{ROLLBACK_NOTE}'
echo "[update-guard] 新版面板启动失败，已回滚到 v{PANEL_VERSION}" >> /var/log/mihomo-panel.log
{restart} >/dev/null 2>&1
rm -f "$0"
"""


def start_update_guard(files):
    try:
        with open(GUARD, "w") as f:
            f.write(update_guard_script(files))
        os.chmod(GUARD, 0o755)
        sh(f"(command -v setsid >/dev/null && setsid sh '{GUARD}' || sh '{GUARD}') >/dev/null 2>&1 &")
    except OSError as e:
        log_err("更新回滚守护", e)


def group_kind(n, custom=()):
    if n in custom:
        return "custom"
    if n == G_SEL:
        return "select"
    if n == G_MANUAL:
        return "manual"
    if n == G_AUTO:
        return "auto"
    if n == G_DIRECT:
        return "direct"
    if n in SIDE_GROUPS:
        return "service"
    if n.startswith(LB_PREFIX) and n.endswith((LB_TAIL,) + tuple(LB_ALL.values())):
        return "lb"
    if n.endswith(AUTO_TAIL):
        return "region"
    return "other"


def group_meta(d, cfg):
    cn = {g["name"] for g in d.get("custom_groups") or []}
    meta = {}
    for g in cfg["proxy-groups"]:
        n = g["name"]
        meta[n] = {"kind": group_kind(n, cn), "type": g["type"], "strategy": g.get("strategy", ""), "filter": g.get("filter", ""),
                   "subs": bool(g.get("use")), "icon": g.get("icon", "")}
    return meta


def rename_policy(d, old, new):
    """策略组改名 / 删除（new 为节点选择）时同步更新自定义规则、规则集、其他自定义组、DNS 的引用；返回受影响条目数"""
    return map_refs(d, {old: new}, drop={old} if new == G_SEL else ())
'''
_SRC['shunt.svcmigrate'] = '''"""v7.7.6 服务改名：mihomo-panel → shunt（面板），mihomo → shunt-core（核心）。
旧设备在线更新后由新面板在后台执行一次：装好新 init 脚本、换开机启动项、停旧服务起新服务，成功后删掉旧脚本；
新服务起不来就恢复旧服务。核心原本是手动停止的，迁移后保持停止。"""
import os, threading, time

from .base import BASE, wd_hold
from .errlog import log_err
from .sysopt import sh

MIGRATE_SH = os.path.join(BASE, ".svc-migrate.sh")
SCRIPT = """#!/bin/sh
# 由 Shunt 面板生成：服务改名迁移（mihomo-panel → shunt，mihomo → shunt-core），执行一次后自删
exec >>/var/log/mihomo-panel.log 2>&1
echo "[svc-migrate] $(date '+%F %T') 服务改名：mihomo-panel → shunt，mihomo → shunt-core"
B={base}
for s in shunt shunt-core; do cp "$B/init.d/$s" "/etc/init.d/$s" && chmod +x "/etc/init.d/$s" || {{ echo "[svc-migrate] 复制 $s 失败"; rm -f "$0"; exit 1; }}; done
core_on=0; rc-service mihomo status >/dev/null 2>&1 && core_on=1
rc-update del mihomo default >/dev/null 2>&1; rc-update del mihomo-panel default >/dev/null 2>&1
rc-update add shunt-core default >/dev/null 2>&1; rc-update add shunt default >/dev/null 2>&1
if [ $core_on = 1 ]; then
  rc-service mihomo stop
  if ! rc-service shunt-core start; then
    echo "[svc-migrate] shunt-core 启动失败，恢复旧服务"
    rc-service shunt-core zap >/dev/null 2>&1
    rc-update del shunt-core default; rc-update del shunt default; rc-update add mihomo default; rc-update add mihomo-panel default
    rm -f /etc/init.d/shunt /etc/init.d/shunt-core
    rc-service mihomo start; rm -f "$0"; exit 1
  fi
fi
rc-service mihomo-panel stop   # 结束当前（旧服务名下的）面板进程
sleep 1
if rc-service shunt start; then
  rc-service mihomo zap >/dev/null 2>&1; rc-service mihomo-panel zap >/dev/null 2>&1
  rm -f /etc/init.d/mihomo /etc/init.d/mihomo-panel
  echo "[svc-migrate] 完成：rc-service shunt / shunt-core"
else
  echo "[svc-migrate] shunt 启动失败，恢复旧面板服务"
  rc-update del shunt default; rc-update add mihomo-panel default
  rc-service mihomo-panel start
fi
rm -f "$0"
"""


def need_migrate():
    return (BASE == "/opt/mihomo-panel" and os.path.isfile("/etc/init.d/mihomo-panel")
            and not os.path.isfile("/etc/init.d/shunt")
            and os.path.isfile(os.path.join(BASE, "init.d", "shunt")) and os.path.isfile(os.path.join(BASE, "init.d", "shunt-core"))
            and hasattr(os, "geteuid") and os.geteuid() == 0)


def svc_migrate(delay=20):
    """面板启动 delay 秒后（让在线更新的回滚守护先确认新版能打开）在后台跑迁移脚本"""
    if not need_migrate():
        return False

    def run():
        time.sleep(delay)
        try:
            with open(MIGRATE_SH, "w") as f:
                f.write(SCRIPT.format(base=BASE))
            os.chmod(MIGRATE_SH, 0o755)
            wd_hold(180, "服务改名迁移")
            sh(f"(command -v setsid >/dev/null && setsid sh '{MIGRATE_SH}' || sh '{MIGRATE_SH}') >/dev/null 2>&1 &")
        except OSError as e:
            log_err("服务改名迁移", e)
    threading.Thread(target=run, daemon=True).start()
    return True
'''
_SRC['shunt.static'] = '''"""静态资源：gzip + ETag"""
import base64, gzip, hashlib, os, re

from .base import BASE
STATIC = {}
STATIC_TYPES = {"index.html": "text/html", "icon.svg": "image/svg+xml", "icon-180.png": "image/png", "icon-512.png": "image/png"}
MANIFEST = {"name": "Shunt 分流", "short_name": "Shunt", "description": "mihomo 旁路由面板", "start_url": "/", "scope": "/",
            "display": "standalone", "background_color": "#f2f2f7", "theme_color": "#0a84ff",
            "icons": [{"src": "/icon-180.png", "sizes": "180x180", "type": "image/png"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}


def static_file(name):
    """返回 (原始, gzip, etag, 类型)；按文件修改时间缓存。图标文件缺失时（旧版面板在线更新上来）从 index.html 内嵌的图标取"""
    path = os.path.join(BASE, name)
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        if not name.endswith(".png"):
            return None
        idx = static_file("index.html")
        m = re.search(rb'rel="apple-touch-icon" href="data:image/png;base64,([A-Za-z0-9+/=]+)"', idx[0]) if idx else None
        if not m:
            return None
        raw = base64.b64decode(m.group(1))
        return raw, None, '"i%s"' % hashlib.md5(raw).hexdigest()[:16], "image/png"
    c = STATIC.get(name)
    if c and c[0] == key:
        return c[1]
    with open(path, "rb") as f:
        raw = f.read()
    gz = gzip.compress(raw, 6) if not name.endswith(".png") else None
    val = (raw, gz, '"%s"' % hashlib.md5(raw).hexdigest()[:16], STATIC_TYPES.get(name, "application/octet-stream"))
    STATIC[name] = (key, val)
    return val
'''
_SRC['shunt.webapi_base'] = '''"""HTTP 接口公共部分：路由表 ROUTES / @api_route、敏感值打码、状态视图、订阅缓存同步、启停任务等（webapi*.py 共用）"""
import json, os, threading, time
from urllib.parse import urlparse

from .errlog import log_err
from . import adblock
from .base import BUILTIN_POLICIES, SVC, TPROXY_SH, CONF_DIR, wd_hold
from .data import config_version, load, region_lb_set, safe
from .links import reserved_names
from .groups import NODE_LIST_MAX, all_node_names, node_name_ok, provider_nodes, provider_rev, rx, sb_sub_outbounds, sub_orig_nodes, sub_removed
from .mihomo import build_config, dns_cfg, drop_provider_cache
from .corehttp import core
from .sb_common import active_core
from .sb_subs import parse_sub_text, sb_update_subs
from .sb_config import build_singbox, sb_is_fork
from .sysopt import SVC_OP, core_procs, kill_core_procs, rc_run, sh, svc_restart
from .notify import WD, core_alive, core_proc_alive, set_manual_stop
from .latency import retest_soon
from .update_guard import group_meta
ROUTES = {}  # (方法, 路径) → H 的处理函数；api() 先处理前缀 / 条件路由，再查这张表

# GET 也要求 X-Shunt 头的机密接口：返回内容含订阅 URL / 节点密码 / TG token 等机密，局域网零凭证可拖库
SENSITIVE_GET = ("/api/backup", "/api/backup/list", "/api/config", "/api/history")  # 兼容：现在所有 /api GET 都要求请求头
OPEN_GET = ("/api/favicon", "/api/logstream")  # <img> / EventSource 带不了自定义头（logstream 用一次性票据）
# 设置了面板密码时不需要登录的接口：登录本身、登录状态；日志流用 /api/logticket（要登录）换来的一次性票据
AUTH_OPEN = ("/api/auth/status", "/api/auth/login", "/api/logstream")

_SVC_CACHE = {}  # {"v": (t, st, code, version)}：rc-service 状态 + 核心 /version 缓存 10 秒
_SVC_LOCK = threading.Lock()  # 启动 / 停止 / 重启同一时间只允许一个
SVC_JOB = {"busy": False, "act": "", "ok": None, "message": "", "t": 0}  # 最近一次启停操作，GET /api/service 返回给前端轮询
SVC_NAMES = {"start": "启动", "stop": "停止", "restart": "重启"}


def tp_apply():
    """按当前代理方式重载 nftables 规则。核心被手动停止时不挂规则：否则局域网流量会被导向不存在的核心端口
    （init.d 的 start_post 会在下次启动核心时自动加载）。
    看门狗因核心无响应已撤掉规则、让局域网直连（WD.tp_off）时同样不挂：以前这时保存绕过设备 / IPv6 / DNS 防绕过、
    恢复设置或快照都会把规则重新挂上，流量导向卡死的核心，局域网断网直到看门狗下一次重试（最长约 5 分钟）。
    核心恢复后看门狗按当时的设置重新开启规则"""
    if WD["manual_stop"] or WD["tp_off"]:
        return 0, ""
    return sh(TPROXY_SH + " apply")


def _svc_do(act):
    """执行启停并确认结果，返回 (成功?, 提示)"""
    if act == "stop":
        with SVC_OP:  # 看门狗 / 定时任务正在重启核心时，等它做完再停，不并发跑两个 rc-service
            set_manual_stop(True)  # 拿到锁后置位（并落盘）：之后看门狗不会再把它拉起来，面板重启后也记得
            wd_hold(150, "手动停止")
            try:
                code, out = rc_run("stop", timeout=90)
                for _ in range(80):  # rc-service 的返回值不可靠，以 pid 为准，每 0.1 秒查一次，最多等 8 秒
                    if not core_proc_alive():
                        break
                    time.sleep(0.1)
                # pid 文件之外还有核心进程（OpenRC 被 zap 过、旧服务名留下的、SSH 手动运行的）：rc-service 管不到，
                # 以前这里直接报「已停止」，实际核心还占着端口在跑，顶栏一直显示运行中
                stray = kill_core_procs() if not core_proc_alive() else 0
                if not core_proc_alive() and not core_procs():
                    if stray:
                        log_err("api/service stop", Exception(f"结束了 {stray} 个不在 pid 文件里的残留核心进程"))
                    return True, "核心已停止" + (f"（另外结束了 {stray} 个不受服务管理的残留核心进程）" if stray else "")
            finally:
                wd_hold(0, "手动停止", reset=True)
        set_manual_stop(False)  # 没停下来：交还给看门狗，避免核心卡死后再也没人拉起
        log_err("api/service stop", Exception(f"停止后核心进程仍在（rc 返回 {code}）：{out[-200:]}"))
        return False, "核心进程仍在运行，停止失败：" + (out[-200:] or "无输出")
    set_manual_stop(False)
    with SVC_OP:
        if not core_proc_alive() and core_procs():  # 服务没在跑却有核心进程：残留孤儿占着端口，新核心会起不来
            n = kill_core_procs()
            log_err("api/service " + act, Exception(f"启动前结束了 {n} 个不受服务管理的残留核心进程"))
    if act == "restart":
        code, out = svc_restart(timeout=90, why="手动重启")  # 与看门狗、切换内核同一入口：含看门狗暂停和 IP 信息重查
    else:
        with SVC_OP:
            wd_hold(150, "手动启动")
            code, out = rc_run("start", timeout=90)
            wd_hold(60, "手动启动", reset=True)
    if code == 124:
        return False, f"核心{SVC_NAMES[act]}超时，请 SSH 检查 `{SVC} status` 和核心日志"
    # 后台启动的服务 rc 总返回 0，要自己确认：控制接口一应答就算成功（start_post 已等到端口就绪，通常第一次就成）；
    # 进程在但接口还没起来（加载大规则集）最多等 8 秒，每 0.1 秒查一次，期间进程退出立即判失败
    for i in range(80):
        if not core_proc_alive():
            if i >= 6:  # 前 0.6 秒 pid 文件可能还没写好
                break
        elif core_alive(timeout=2):
            retest_soon()
            return True, f"核心已{SVC_NAMES[act]}"
        time.sleep(0.1)
    else:
        if core_proc_alive():
            retest_soon()
            return True, f"核心已{SVC_NAMES[act]}（控制接口仍在加载）"
    return False, "核心没有运行起来，请查看核心日志：" + (out[-200:] or "无输出")


def _svc_job(act):
    try:
        ok, msg = _svc_do(act)
    except Exception as e:
        log_err("api/service " + act, e)
        ok, msg = False, f"{SVC_NAMES[act]}失败：{e}"
    finally:
        _SVC_CACHE.pop("v", None)  # 状态缓存作废，前端马上拿到真实运行状态
    SVC_JOB.update(busy=False, ok=ok, message=msg, t=time.time())
    _SVC_LOCK.release()
_GRP_CACHE = {}  # {"v": ((配置版本, 订阅节点名单版本), groups, gmeta)}：避免每次 /api/state 完整 build_config


def _svc_status():
    """/api/state 每 15 秒/标签页轮询：起 shell 查服务状态 + 调核心 /version，10 秒内复用缓存"""
    now = time.time()
    v = _SVC_CACHE.get("v")
    if v is not None and now - v[0] < 10:
        return v[1], v[2], v[3]
    # 启停 / 重启进行中（面板按钮、看门狗、定时任务、切换内核都持有 SVC_OP）：不再调 rc-service status。
    # OpenRC 在服务 stopping/starting 期间会让 status 等独占锁，以前 /api/state 跟着卡到重启做完（最长 60 秒），
    # 前端每次 loadState 都在等它，表现为点「重启」后整个面板卡死。这期间按 pid 判断，且不写缓存
    if not SVC_OP.acquire(blocking=False):
        alive = core_proc_alive()
        code, ver = core("GET", "/version", timeout=2) if alive else (502, b"")
        st = "started" if alive and code == 200 else "starting"
        try:
            version = json.loads(ver).get("version", "-") if code == 200 else "-"
        except Exception:
            version = "-"
        return st, code, version
    try:
        _, st = sh(SVC + " status", timeout=5)
    finally:
        SVC_OP.release()
    code, ver = core("GET", "/version", timeout=3)
    try:
        version = json.loads(ver).get("version", "-") if code == 200 else "-"
    except Exception:
        version = "-"
    _SVC_CACHE["v"] = (now, st, code, version)
    return st, code, version


def api_route(path, *methods):
    def deco(fn):
        for m in methods:
            assert (m, path) not in ROUTES, (m, path)
            ROUTES[(m, path)] = fn
        return fn
    return deco


_MASK_WORDS = ("password", "passwd", "uuid", "token", "psk", "secret", "privatekey")


def _mask_val(v):
    """敏感值掩盖中间：长值露头 3 + 尾 2，短值全打码"""
    s = str(v)
    return f"{s[:3]}******{s[-2:]}" if len(s) > 6 else "******"


def mask_secret(v):
    """/api/state 里的 TG token：空值保持空（前端据此显示「未配置」），非空打码"""
    return _mask_val(v) if isinstance(v, str) and v else (v or "")


def mask_url(u):
    """订阅 URL 打码：保留协议和主机（去掉 user:pass@），路径与查询串（通常带 token）打码"""
    if not isinstance(u, str) or not u:
        return u or ""
    try:
        sp = urlparse(u)
    except ValueError:
        return _mask_val(u)
    head = f"{sp.scheme}://{sp.netloc}"
    if not sp.scheme or not sp.netloc or not u.startswith(head):
        return _mask_val(u)
    rest = u[len(head):]
    host = f"{sp.scheme}://{sp.netloc.rpartition('@')[2]}"
    return host + ("/" + _mask_val(rest.lstrip("/")) if rest.strip("/") else rest)


def unmask(new, cur, masker):
    """前端把打码后的占位值原样提交回来 = 没改：换回服务端保存的原值"""
    if isinstance(new, str) and isinstance(cur, str) and cur and new.strip() == masker(cur):
        return cur
    return new


def state_subs(subs):
    return [dict(s, url=mask_url(s.get("url"))) if isinstance(s, dict) else s for s in subs or []]


def _mask_cfg(o):
    """递归掩盖配置里的敏感字段（key 含 password/uuid/token/psk/secret/privatekey 等，去掉 -_ 后匹配）"""
    if isinstance(o, dict):
        return {k: (_mask_val(v) if isinstance(v, str) and any(w in k.lower().replace("-", "").replace("_", "")
                                                              for w in _MASK_WORDS) else _mask_cfg(v))
                for k, v in o.items()}
    if isinstance(o, list):
        return [_mask_cfg(x) for x in o]
    return o


def config_view(d=None, masked=True):
    """配置文本视图（/api/config）：masked=True 时递归掩盖节点 password/uuid 等敏感字段"""
    d = d or load()
    if active_core(d) == "singbox":
        cfg = build_singbox(d)[0]
    else:
        cfg = build_config(d)
    if masked:
        cfg = _mask_cfg(cfg)
    return json.dumps(cfg, ensure_ascii=False, indent=2)


def find_cycle(groups):
    """策略组之间的循环引用，返回环路径或 None"""
    gmap = {g["name"]: [m for m in g.get("proxies", [])] for g in groups}
    state, stack = {}, []

    def dfs(n):
        state[n] = 1
        stack.append(n)
        for m in gmap.get(n, []):
            if m in gmap:
                if state.get(m) == 1:
                    return stack[stack.index(m):] + [m]
                if not state.get(m):
                    c = dfs(m)
                    if c:
                        return c
        state[n] = 2
        stack.pop()
        return None

    for n in gmap:
        if not state.get(n):
            c = dfs(n)
            if c:
                return c
    return None


def validate_groups(d):
    """生成配置后检查自定义组：重名、成员不存在、循环引用"""
    errs = []
    nodes = all_node_names(d)
    fixed = reserved_names()
    seen = set()
    for cg in d.get("custom_groups") or []:
        n = cg["name"]
        if n in seen:
            errs.append(f"策略组重名：{n}")
        if n in fixed:
            errs.append(f"「{n}」与内置策略组或策略同名")
        if n in nodes:
            errs.append(f"「{n}」与节点同名")
        seen.add(n)
    cfg = build_config(d, strict=True)
    names = {g["name"] for g in cfg["proxy-groups"]} | BUILTIN_POLICIES | {p["name"] for p in cfg["proxies"]}
    for cg in d.get("custom_groups") or []:
        miss = [m for m in cg["proxies"] if m not in names]
        if miss:
            errs.append(f"「{cg['name']}」的成员不存在：" + "、".join(miss[:5]))
        if cg["name"] in cg["proxies"]:
            errs.append(f"「{cg['name']}」不能包含自己")
    cyc = find_cycle(cfg["proxy-groups"])
    if cyc:
        errs.append("策略组循环引用：" + " → ".join(cyc) + "（加入分流组候选的自定义组不能再引用 YouTube / Google 等分流组，可关闭“加入分流组候选”）")
    return errs


def ad_today():
    """顶栏「广告拦截」：今天（本地自然日）拦截数，与概览「今日拦截广告」同一口径"""
    return adblock.AD_STATS.today() if adblock.AD_STATS else 0


def sb_group_view(groups, meta, fork=None):
    """sing-box 下的策略组视图：官方内核下地区负载均衡并入自动优选（从名单和 meta 里去掉），
    负载均衡 / 故障转移按自动测速运行（meta 改标 url-test，sb_from 记原类型）。
    魔改版内核（多节点聚合）：负载均衡组保留，只有故障转移按自动测速。原地修改 meta，返回过滤后的组名单"""
    fork = sb_is_fork() if fork is None else fork
    lbs = set() if fork else region_lb_set()
    down = ("fallback",) if fork else ("load-balance", "fallback")
    for n in list(meta):
        if n in lbs:
            meta.pop(n)
        elif meta[n]["type"] in down:
            meta[n].update(sb_from=meta[n]["type"], type="url-test")
    return [g for g in groups if g not in lbs]


def state_groups(d):
    """/api/state 的策略组名单与元数据。按（配置版本, 订阅节点名单版本）缓存：订阅下载完成、节点增删后地区组会变，
    只按配置版本缓存会让「目标策略」下拉框一直缺新地区组。生成失败时返回空名单（记日志），不让整个面板 500"""
    provider_nodes()  # 先刷新订阅节点名单（60 秒内存缓存），名单变化会推进 provider_rev()
    key = (config_version(), provider_rev(), sb_is_fork())
    gv = _GRP_CACHE.get("v")
    if gv is not None and gv[0] == key:
        return gv[1], gv[2]
    try:
        cfg = build_config(d)
        groups = [g["name"] for g in cfg["proxy-groups"]]
        gmeta = group_meta(d, cfg)
        if active_core(d) == "singbox":
            groups = sb_group_view(groups, gmeta)
    except Exception as e:
        log_err("/api/state：生成策略组", e, tb=True)
        return [], {}
    _GRP_CACHE["v"] = (key, groups, gmeta)
    return groups, gmeta


def state_nodes(d):
    """手动节点的摘要；残缺的条目跳过，不让整个 /api/state 500"""
    out = []
    for n in d.get("nodes") or []:
        try:
            px = n["proxy"]
            out.append({"name": px["name"], "type": px["type"], "server": px["server"], "port": px["port"]})
        except (KeyError, TypeError):
            continue
    return out


def parse_node_items(b):
    """{items: [{sub, name}]} → ([(订阅名, 节点原名)], 错误提示)"""
    items = b.get("items") if isinstance(b, dict) else None
    if not isinstance(items, list) or not items:
        return [], "请选择要删除的节点"
    if len(items) > NODE_LIST_MAX:
        return [], f"一次最多删除 {NODE_LIST_MAX} 个节点"
    out = []
    for x in items:
        if not isinstance(x, dict) or not isinstance(x.get("sub"), str) or not x["sub"] or not node_name_ok(x.get("name")):
            return [], "节点格式不正确（需要订阅名和节点名）"
        k = (x["sub"], x["name"])
        if k not in out:
            out.append(k)
    return out, ""


def sub_nodes_view(d):
    """/api/subs/nodes：[{name, known, nodes: [{name, orig, groups}], removed}]"""
    refs = {}
    for g in d.get("custom_groups") or []:
        for x in (g.get("sub_nodes") or []) if isinstance(g, dict) else []:
            if isinstance(x, dict):
                refs.setdefault((x.get("sub"), x.get("name")), []).append(g.get("name"))
    if active_core(d) == "singbox":
        lists = {k: [(tag, str(p.get("name") or "")) for tag, p in v] for k, v in sb_sub_outbounds(d).items()}
    else:
        pn = provider_nodes()
        lists = {k: [(n, n) for n in v or [] if isinstance(n, str)] for k, v in pn.items()}
    known = sub_orig_nodes(d)
    out = []
    for s in d["subs"]:
        gone = sub_removed(s)
        nodes = [{"name": disp, "orig": o, "groups": refs.get((s["name"], o), [])} for disp, o in lists.get(s["name"], []) if o not in gone]
        out.append({"name": s["name"], "known": s["name"] in known, "nodes": nodes, "removed": gone})
    return {"subs": out, "core": active_core(d)}


SUB_FETCH_KEYS = ("url", "filter", "exclude")  # sing-box：面板下载订阅时就按筛选 / 排除过滤，缓存里只有过滤后的节点


def sub_caches_sync(prev, d):
    """恢复设置 / 配置快照 / 版本备份改动了订阅时，按订阅处理旧缓存（与 POST/DELETE /api/subs 一致）。
    以前只有订阅页的添加 / 编辑 / 删除会删 mihomo 的 ./providers/<名>.yaml：恢复出一份换了链接的订阅后，
    mihomo 有本地文件就先用，按文件时间等满更新间隔（默认 24 小时）才下载新链接；sing-box 则一直用面板缓存里旧链接的节点。
    返回 sing-box 需要重新下载的订阅名（新增 / 链接、筛选、排除变化）"""
    old = {s["name"]: s for s in (prev or {}).get("subs") or [] if isinstance(s, dict) and s.get("name")}
    new = {s["name"]: s for s in d.get("subs") or [] if isinstance(s, dict) and s.get("name")}
    for n, s in old.items():
        if n not in new or new[n].get("url") != s.get("url"):
            drop_provider_cache(n)
    for n in new:
        if n not in old:
            drop_provider_cache(n)  # 以前删掉的同名订阅留下的缓存
    return [n for n, s in new.items() if n not in old or any(s.get(k) != old[n].get(k) for k in SUB_FETCH_KEYS)]


def sub_refetch(names):
    """sing-box 下重新下载 sub_caches_sync 返回的订阅；返回附加提示（失败原因）"""
    if not names or active_core() != "singbox":
        return ""
    _, bad = sb_update_subs(names)
    return ("；订阅下载失败（稍后自动重试或在订阅页手动更新）：" + "；".join(bad)[:300]) if bad else ""


def removed_only_left(name, flt, exc, gone):
    """mihomo：编辑订阅的筛选 / 排除后，provider 里是否只剩「已从订阅删除」的节点。
    这样 provider 过滤后一个节点都没有，mihomo 解析失败、订阅之后每次更新都失败（v8.4.1 禁止删光节点的同一个问题，
    以前改筛选正则就能绕过去）。按 mihomo 缓存的订阅原文判断；没有缓存、正则含反引号（mihomo 多条写法）时不判断"""
    if not gone or "`" in flt or "`" in exc:
        return False
    try:
        with open(os.path.join(CONF_DIR, "providers", safe(name) + ".yaml"), encoding="utf-8", errors="ignore") as f:
            nodes, _ = parse_sub_text(f.read())
    except (OSError, ValueError):
        return False
    rf, re_ = (rx(flt) if flt else None), (rx(exc) if exc else None)
    if (flt and rf is None) or (exc and re_ is None):
        return False
    left = {str(p.get("name")) for p in nodes if (not rf or rf.search(str(p.get("name")))) and not (re_ and re_.search(str(p.get("name"))))}
    return bool(left) and left <= set(gone)


RESTORE_KEYS = ("mode", "tproxy", "subs", "rules", "rulesets", "bypass", "tests", "sub_interval", "region_groups",
                "nodes", "ipv6", "watchdog", "tg_token", "tg_chat", "proxy_mode", "tun", "log_limit", "adblock", "dns",
                "devices", "schedule", "groups_cfg", "custom_groups", "sniffer", "gh_proxy")


def renames_note(renames):
    """「；已自动改名：A → A_2、B → B_2」，没有改名时为空"""
    return ("；已自动改名：" + "、".join(f"{a} → {b}" for a, b in renames)) if renames else ""


def probe_data(d):
    """按生成配置与 /api/state 的方式完整走一遍：会抛异常的数据不让写进 data.json。返回错误提示或 None"""
    try:
        cfg = build_config(d)
        group_meta(d, cfg)
        dns_cfg(d)
        for n in d.get("nodes") or []:
            n["proxy"]["name"], n["proxy"]["type"], n["proxy"]["server"], n["proxy"]["port"]
        if active_core(d) == "singbox":
            build_singbox(d)
    except Exception as e:
        log_err("保存前检查配置", e)
        return f"数据格式不正确，未保存：{type(e).__name__}: {e}"[:300]
    return None
'''
_SRC['shunt.webapi_nodes'] = '''"""HTTP 接口：节点 / 策略组 / 连接 / 实时数据 / 设备（类 H 的一部分，由 webapi.H 继承）"""
import copy, os, re, threading, time

from . import stats
from .base import BUILTIN_POLICIES, GROUP_TYPES, G_SEL, LB_STRATEGIES, PRIMARY_REGIONS, REGIONS
from .data import auto_name, lb_names, load, update
from .links import add_links
from .groups import all_node_names, clean_custom_group, gcfg, region_plan, rx
from .mihomo import build_config
from .core import conns_snapshot, refresh_regions, reload_core
from .sb_common import active_core
from .sb_config import sb_is_fork
from .core_update import LGBM_FILE, lgbm_fetch_reload
from .latency import RETEST, proxies_merged, px_slim
from .stats import arp_table
from .ruletest import is_ip
from .live import LIVE, memory_once
from .devices import device_map, read_leases
from .update_guard import group_meta, rename_policy
from .webapi_base import api_route, validate_groups, sb_group_view


class NodesAPI:
    """路由处理方法：@api_route 在类定义时登记到 ROUTES，方法里的 self 是 webapi.H 的实例"""

    def groups_info(self):
        d = load()
        cfg = build_config(d)
        names = [p["name"] for p in cfg["proxies"]]
        plan = region_plan(d, names, list(cfg["proxy-providers"]))
        gnames = [g["name"] for g in cfg["proxy-groups"]]
        meta = group_meta(d, cfg)
        sb = active_core(d) == "singbox"
        if sb:
            gnames = sb_group_view(gnames, meta)
        regions = [{"name": r[0], "total": r[4], "manual": len(r[3]), "unknown": r[5],
                    "groups": [g for g in gnames if g in [auto_name(r[0])] + lb_names(r[0])]} for r in plan]
        sel = next((g.get("proxies") or [] for g in cfg["proxy-groups"] if g["name"] == G_SEL), [])
        if sb:
            sel = [x for x in sel if x in gnames or x in BUILTIN_POLICIES]
        return self.send(200, {"cfg": gcfg(d), "region_groups": d["region_groups"], "custom": d["custom_groups"],
                               "regions": regions, "groups": gnames, "manual": names, "nodes": all_node_names(d),
                               "meta": meta, "core": active_core(d), "fork": sb and sb_is_fork(), "types": GROUP_TYPES, "strategies": LB_STRATEGIES,
                               "region_names": [r[0] for r in REGIONS], "primary": PRIMARY_REGIONS, "selector": sel})

    def save_groups_cfg(self, b):
        gc = gcfg(load())
        for k in ("lb", "extra", "other", "lazy", "lgbm"):
            if k in b:
                gc[k] = bool(b[k])
        try:
            if "interval" in b:
                gc["interval"] = int(b["interval"])
            if "tolerance" in b:
                gc["tolerance"] = int(b["tolerance"])
        except (TypeError, ValueError):
            return self.send(400, {"message": "测速间隔 / 容差必须是数字"})
        if not 30 <= gc["interval"] <= 86400:
            return self.send(400, {"message": "测速间隔应在 30–86400 秒之间"})
        if not 0 <= gc["tolerance"] <= 1000:
            return self.send(400, {"message": "容差应在 0–1000 ms 之间"})
        if "url" in b:
            u = str(b["url"]).strip()
            if not re.match(r"^https?://\\S+$", u):
                return self.send(400, {"message": "测速地址必须是 http(s) 链接"})
            gc["url"] = u

        def fn(d):
            d["groups_cfg"] = gc
            if "region_groups" in b:
                d["region_groups"] = bool(b["region_groups"])
        test = load()
        fn(test)
        errs = validate_groups(test)
        if errs:
            return self.send(400, {"message": "；".join(errs[:3])})
        prev = update(fn)
        ok, msg = reload_core(prev)
        if ok and gc["lgbm"] and active_core() == "smart" and not os.path.isfile(LGBM_FILE):
            threading.Thread(target=lgbm_fetch_reload, daemon=True).start()
            msg = "策略组设置已生效，LightGBM 模型正在后台下载，下载完会自动重载"
        return self.reply(ok, "策略组设置已生效" if ok else msg)

    def save_custom_group(self, b):
        try:
            g = clean_custom_group(b)
        except ValueError as e:
            return self.send(400, {"message": str(e)})
        orig = str(b.get("orig") or "").strip()
        cur = load()
        if orig and not any(x["name"] == orig for x in cur["custom_groups"]):
            return self.send(404, {"message": f"策略组不存在：{orig}"})
        if g["name"] != orig and any(x["name"] == g["name"] for x in cur["custom_groups"]):
            return self.send(400, {"message": f"已存在同名策略组：{g['name']}"})
        info = {"refs": 0}

        def fn(d):
            if orig:
                d["custom_groups"] = [g if x["name"] == orig else x for x in d["custom_groups"]]
                if g["name"] != orig:
                    info["refs"] = rename_policy(d, orig, g["name"])
            else:
                d["custom_groups"].append(g)
        test = copy.deepcopy(cur)
        fn(test)
        errs = validate_groups(test)
        if errs:
            return self.send(400, {"message": "；".join(errs[:3])})
        prev = update(fn)
        ok, msg = reload_core(prev)
        if not ok:
            return self.send(500, {"message": msg})
        extra = f"，已同步更新 {info['refs']} 处引用" if info["refs"] else ""
        return self.send(200, {"message": ("已保存策略组 " if orig else "已创建策略组 ") + g["name"] + extra})

    def delete_custom_group(self, b):
        name = str(b.get("name") or "")
        cur = load()
        if not any(x["name"] == name for x in cur["custom_groups"]):
            return self.send(404, {"message": f"策略组不存在：{name}"})
        users = [x["name"] for x in cur["custom_groups"] if name in x.get("proxies", [])]
        if users and not b.get("force"):
            return self.send(409, {"message": f"「{name}」被其他策略组引用：{'、'.join(users)}。确认删除会同时从这些组中移除它", "users": users})
        info = {"refs": 0}

        def fn(d):
            d["custom_groups"] = [x for x in d["custom_groups"] if x["name"] != name]
            info["refs"] = rename_policy(d, name, G_SEL)
        prev = update(fn)
        ok, msg = reload_core(prev)
        if not ok:
            return self.send(500, {"message": msg})
        extra = f"，{info['refs']} 处引用已改为「{G_SEL}」" if info["refs"] else ""
        return self.send(200, {"message": f"已删除 {name}{extra}"})

    @api_route("/api/groups", "GET")
    def api_groups_get(self, m, p, b, q):
        return self.groups_info()

    @api_route("/api/groups/cfg", "PUT")
    def api_groups_cfg_put(self, m, p, b, q):
        return self.save_groups_cfg(b)

    @api_route("/api/groups", "POST")
    def api_groups_post(self, m, p, b, q):
        return self.save_custom_group(b)

    @api_route("/api/groups", "DELETE")
    def api_groups_delete(self, m, p, b, q):
        return self.delete_custom_group(b)

    @api_route("/api/groups/preview", "POST")
    def api_groups_preview_post(self, m, p, b, q):
        flt = str(b.get("filter") or "").strip()
        if not flt:
            return self.send(200, {"matched": [], "total": 0})
        r = rx(flt)
        if r is None:
            return self.send(400, {"message": "正则无效"})
        d = load()
        manual = [n["proxy"]["name"] for n in d["nodes"]]
        allnodes = all_node_names(d) if b.get("subs", True) else manual
        hit = [n for n in allnodes if r.search(n)]
        return self.send(200, {"matched": hit[:200], "total": len(hit), "of": len(allnodes)})

    @api_route("/api/groups/refresh", "POST")
    def api_groups_refresh_post(self, m, p, b, q):
        ok, msg = refresh_regions()
        if not ok and msg == "核心未运行":  # 与其他「核心未运行」一样返回 502
            return self.send(502, {"message": msg})
        return self.reply(ok, msg)

    @api_route("/api/direct_delay", "GET")
    def api_direct_delay_get(self, m, p, b, q):
        """直连国内延迟：DIRECT 是国内直连，单独测百度"""
        from .sysopt import direct_rtt
        return self.send(200, {"ms": direct_rtt()})

    @api_route("/api/px", "GET")
    def api_px_get(self, m, p, b, q):
        px = proxies_merged()
        if px is None:
            return self.send(502, {"message": "核心未运行或无法连接"})
        return self.send(200, {"proxies": px_slim(px), "retesting": RETEST["pending"]})

    @api_route("/api/nodes", "POST")
    def api_nodes_post(self, m, p, b, q):
        d = load()
        added, errs = add_links(str(b.get("links") or ""), d["nodes"], d)
        if not added:
            return self.send(400, {"message": "没有可导入的节点" + ("：\\n" + "\\n".join(errs) if errs else "")})
        prev = update(lambda d: d["nodes"].extend(added))
        ok, msg = reload_core(prev)
        msg = (f"已导入 {len(added)} 个节点" if ok else msg) + ("；忽略：" + "；".join(errs) if errs else "")
        return self.reply(ok, msg)

    @api_route("/api/nodes", "DELETE")
    def api_nodes_delete(self, m, p, b, q):
        # names 必须是文本列表：以前传成字符串会被拆成单个字符、传成数字 / 对象直接 500
        b = b if isinstance(b, dict) else {}
        names = b.get("names") if "names" in b else [b.get("name")]
        if not isinstance(names, list) or not names or not all(isinstance(x, str) and x for x in names):
            return self.send(400, {"message": "names 必须是节点名称列表"})
        names = set(names)
        prev = update(lambda d: d.update(nodes=[n for n in d["nodes"] if n["proxy"]["name"] not in names]))
        return self.reply(*reload_core(prev))

    @api_route("/api/conns", "GET")
    def api_conns_get(self, m, p, b, q):
        # 精简后的连接列表：只保留前端用到的字段（/connections 走 2 秒共享快照）
        c = conns_snapshot(timeout=5)
        if c is None:
            return self.send(502, {"message": "核心未运行或无法连接"})
        keep = ("sourceIP", "host", "sniffHost", "destinationIP", "destinationPort", "network")
        out = [{"id": x.get("id"), "start": x.get("start"), "upload": x.get("upload", 0), "download": x.get("download", 0),
                "rule": x.get("rule"), "rulePayload": x.get("rulePayload"), "chains": x.get("chains"),
                "metadata": {k: (x.get("metadata") or {}).get(k) for k in keep}} for x in c.get("connections") or []]
        return self.send(200, {"connections": out})

    @api_route("/api/conns/stats", "GET")
    def api_conns_stats_get(self, m, p, b, q):
        """概览只要两个数字：连接数和来源设备数，不必把整份连接列表（几十到几百 KB）传给前端"""
        c = conns_snapshot(timeout=5)
        if c is None:
            return self.send(502, {"message": "核心未运行或无法连接"})
        conns = c.get("connections") or []
        devs = {(x.get("metadata") or {}).get("sourceIP") for x in conns}
        devs.discard(None)
        devs.discard("")
        return self.send(200, {"count": len(conns), "devices": len(devs)})

    @api_route("/api/live", "GET")
    def api_live_get(self, m, p, b, q):
        out = dict(LIVE)
        if time.time() - out["t"] > 5:  # 后台 /traffic 流未就绪（核心刚启动或已停止）时退回连接快照
            c = conns_snapshot(timeout=3)
            out.update(up=0, down=0, upTotal=(c or {}).get("uploadTotal", 0), downTotal=(c or {}).get("downloadTotal", 0))
            out["stale"] = True
        if not out["inuse"]:
            out.update(memory_once())
        out["running"] = time.time() - LIVE["t"] < 5 or bool(out.get("inuse"))
        return self.send(200, out)

    @api_route("/api/devices", "GET")
    def api_devices_get(self, m, p, b, q):
        ips = set(arp_table()) | set(read_leases())
        if stats.STATS:
            with stats.STATS.lock:
                for b2 in stats.STATS.data["days"].values():
                    ips |= set(b2.get("dev", {}))
        c = conns_snapshot(timeout=5) or {}  # 与连接页 / 统计共用 2 秒快照，不再单独全量抓取
        ips |= {(x.get("metadata") or {}).get("sourceIP") for x in c.get("connections") or []}
        return self.send(200, {"map": device_map(sorted(i for i in ips if i)), "custom": load()["devices"]})

    @api_route("/api/devices", "PUT")
    def api_devices_put(self, m, p, b, q):
        devs, devs_in = {}, b.get("devices") or {}
        if not isinstance(devs_in, dict):
            return self.send(400, {"message": "devices 必须是对象"})
        for k, v in devs_in.items():
            k, v = str(k).strip().lower().replace("-", ":"), str(v).strip()[:40]
            if k and v and (is_ip(k) or re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", k)):
                devs[k] = v
        update(lambda d: d.update(devices=devs))
        return self.send(200, {"message": f"已保存 {len(devs)} 个设备备注"})

    @api_route("/api/ipinfo", "GET")
    def api_ipinfo_get(self, m, p, b, q):
        from .ipinfo import get_ipinfo
        return self.send(200, get_ipinfo())
'''
_SRC['shunt.webapi_subs'] = '''"""HTTP 接口：订阅增删、更新、订阅节点移除 / 恢复（类 H 的一部分，由 webapi.H 继承）"""
import re

from .base import DEFAULT_EXCLUDE
from .data import load, update
from .groups import NODE_LIST_MAX, node_name_ok, sub_orig_nodes, sub_removed
from .mihomo import drop_provider_cache
from .core import refresh_after_sub, refresh_later, refresh_regions, reload_core
from .sb_common import active_core
from .sb_subs import sb_update_subs
from .schedule import mh_update_subs
from .validate import sub_name_clash
from .webapi_base import api_route, mask_url, unmask, parse_node_items, sub_nodes_view, removed_only_left


class SubsAPI:
    """路由处理方法：@api_route 在类定义时登记到 ROUTES，方法里的 self 是 webapi.H 的实例"""

    @api_route("/api/subs/update", "POST")
    def api_subs_update_post(self, m, p, b, q):
        name = b.get("name") or ""
        if not isinstance(name, str):
            return self.send(400, {"message": "name 必须是订阅名称"})
        allsubs = [s["name"] for s in load()["subs"]]
        if name and name not in allsubs:  # 订阅已被删除（另一个页面 / 定时任务）：以前照样去更新，报一串看不懂的核心错误
            return self.send(404, {"message": f"订阅不存在：{name}"})
        names = allsubs if not name else [name]
        if active_core() == "singbox":
            okl, bad = sb_update_subs(names)
            ok2, msg2 = refresh_regions()
            msg = ("已更新：" + "；".join(okl) if not bad else "更新失败：" + "；".join(bad)) + ("；" + msg2 if msg2 and "无变化" not in msg2 else "")
            return self.reply(not bad, msg)
        okl, bad = mh_update_subs(names)
        ok2, msg2 = refresh_regions()
        msg = (("已更新：" + "；".join(okl) if okl else "已更新") if not bad else "更新失败：" + "；".join(bad)) \\
            + ("；" + msg2 if msg2 and "无变化" not in msg2 else "")
        return self.reply(not bad, msg)

    @api_route("/api/subs", "POST")
    def api_subs_post(self, m, p, b, q):
        name, url = str(b.get("name") or "").strip(), str(b.get("url") or "").strip()
        cur = next((s for s in load()["subs"] if isinstance(s, dict) and s.get("name") == name), None)
        if cur:  # 编辑订阅时表单里是打码后的 URL：没改就沿用原链接
            url = unmask(url, cur.get("url"), mask_url)
        if not name or not url.startswith("http"):
            return self.send(400, {"message": "名称或订阅链接无效"})
        other = sub_name_clash(name, [s.get("name") for s in load()["subs"] if isinstance(s, dict) and s.get("name")])
        if other:  # 「机场 A」和「机场_A」会共用 ./providers/<名>.yaml 和 sing-box 节点缓存，互相覆盖
            return self.send(400, {"message": f"与已有订阅「{other}」只差符号，会共用同一个缓存文件，请改个名字"})
        flt, exc = str(b.get("filter") or "").strip(), str(b["exclude"] if "exclude" in b else DEFAULT_EXCLUDE).strip()
        for pat in (flt, exc):
            try:
                re.compile(pat)
            except re.error as e:
                return self.send(400, {"message": f"正则表达式无效：{pat}（{e}）"})
        if (cur and cur.get("url") == url and active_core() != "singbox"
                and (flt, exc) != (cur.get("filter") or "", cur.get("exclude", DEFAULT_EXCLUDE) or "")
                and removed_only_left(name, flt, exc, sub_removed(cur))):
            return self.send(400, {"message": f"按新的筛选 / 排除规则，订阅「{name}」只剩已从订阅删除的节点，mihomo 会因为订阅里一个节点都没有而加载失败；"
                                              "请先在节点页恢复需要的节点，或调整筛选规则"})
        def save_sub(d):
            old = next((s for s in d["subs"] if s["name"] == name), None)
            new = {"name": name, "url": url, "filter": flt, "exclude": exc}
            if old and sub_removed(old):  # 编辑订阅时保留「从订阅删除」的节点名单
                new["removed"] = sub_removed(old)
            # 编辑时留在原位置：以前先删再追加到末尾，订阅顺序一变，sing-box 里不同订阅的同名节点去重后缀（「HK (2)」）
            # 会互换，已选中的节点 / 自定义组成员悄悄指向另一个订阅的节点；mihomo 的 use 顺序与节点排列也跟着变
            if old:
                d["subs"] = [new if s["name"] == name else s for s in d["subs"]]
            else:
                d["subs"].append(new)
        prev = update(save_sub)
        if not cur or cur.get("url") != url:  # 新订阅 / 换了链接：旧链接留下的 mihomo 缓存不能再用（sing-box 下载失败时也会拿它兜底）
            drop_provider_cache(name)
        if active_core() == "singbox":  # 先由面板下载节点，再生成配置
            okl, bad = sb_update_subs([name])
            ok, msg = reload_core(prev)
            if ok and bad:
                ok, msg = False, "订阅已保存，但下载失败：" + "；".join(bad)
            return self.reply(ok, msg if not ok else "已添加：" + "；".join(okl))
        ok, msg = reload_core(prev)
        if ok:
            refresh_after_sub(name)  # 订阅下载完成后按实际节点生成地区 / 均衡分组
        return self.reply(ok, msg)

    @api_route("/api/subs", "DELETE")
    def api_subs_delete(self, m, p, b, q):
        prev = update(lambda d: d.update(subs=[s for s in d["subs"] if s["name"] != b.get("name")]))
        ok, msg = reload_core(prev)
        if ok and isinstance(b.get("name"), str) and b["name"] and not any(s["name"] == b["name"] for s in load()["subs"]):
            drop_provider_cache(b["name"])  # 以后用同名重新添加（换了链接）时不会先加载这份旧节点
        return self.reply(ok, msg)

    @api_route("/api/subs/nodes", "GET")
    def api_subs_nodes_get(self, m, p, b, q):
        """订阅节点清单（节点页「从订阅删除」、策略组编辑器勾选订阅节点用）：
        每个节点给出显示名（sing-box 为去重改名后的名字）与原名（存进 data.json 的就是原名），以及引用它的自定义组"""
        return self.send(200, sub_nodes_view(load()))

    @api_route("/api/subs/nodes/remove", "POST")
    def api_subs_nodes_remove(self, m, p, b, q):
        items, err = parse_node_items(b)
        if err:
            return self.send(400, {"message": err})
        subs = {s["name"]: s for s in load()["subs"]}
        miss = sorted({x[0] for x in items if x[0] not in subs})
        if miss:
            return self.send(404, {"message": "订阅不存在：" + "、".join(miss[:3])})
        for sname in {x[0] for x in items}:
            if len(set(sub_removed(subs[sname])) | {n for s2, n in items if s2 == sname}) > NODE_LIST_MAX:
                return self.send(400, {"message": f"订阅「{sname}」删除的节点过多（最多 {NODE_LIST_MAX} 个）"})
        # 不能把一个订阅的节点全部删掉：mihomo 的 provider 过滤后一个节点都没有会解析失败（file doesn't have any proxy），
        # 订阅加载出错、之后每次更新也都失败，订阅新增的节点永远进不来，还会按退避间隔反复重新下载。不要这个订阅应直接删除订阅
        left = sub_orig_nodes(load())
        for sname in {x[0] for x in items}:
            if left.get(sname) and left[sname] <= {n for s2, n in items if s2 == sname}:
                return self.send(400, {"message": f"不能删除订阅「{sname}」的全部节点（至少保留一个）；不需要这个订阅可以在「订阅」页直接删除"})
        info = {"n": 0}

        def fn(d):
            for s in d["subs"]:
                add = [n for s2, n in items if s2 == s["name"]]
                if not add:
                    continue
                cur = sub_removed(s)
                new = [n for n in add if n not in cur]
                info["n"] += len(new)
                s["removed"] = cur + new
        return self.sub_nodes_apply(fn, info, lambda n: f"已从订阅删除 {n} 个节点")

    @api_route("/api/subs/nodes/restore", "POST")
    def api_subs_nodes_restore(self, m, p, b, q):
        sname = b.get("sub")
        if not isinstance(sname, str) or not sname:
            return self.send(400, {"message": "缺少订阅名"})
        if not any(s["name"] == sname for s in load()["subs"]):
            return self.send(404, {"message": f"订阅不存在：{sname}"})
        allx = bool(b.get("all"))
        names = b.get("names")
        if not allx:
            if not isinstance(names, list) or not names or len(names) > NODE_LIST_MAX or not all(node_name_ok(x) for x in names):
                return self.send(400, {"message": "请选择要恢复的节点"})
            names = set(names)
        info = {"n": 0}

        def fn(d):
            for s in d["subs"]:
                if s["name"] != sname:
                    continue
                cur = sub_removed(s)
                keep = [] if allx else [n for n in cur if n not in names]
                info["n"] = len(cur) - len(keep)
                if keep:
                    s["removed"] = keep
                else:
                    s.pop("removed", None)
        return self.sub_nodes_apply(fn, info, lambda n: f"已恢复 {n} 个节点")

    def sub_nodes_apply(self, fn, info, done):
        """删除 / 恢复订阅节点：锁内改 data.json → 与其他订阅修改一样重载核心（失败回滚；手动停止时只写配置）"""
        prev = update(fn)
        if not info["n"]:
            return self.send(200, {"message": done(0)})
        ok, msg = reload_core(prev)
        if ok and active_core() != "singbox":
            # mihomo 的订阅节点名单要等核心按新 exclude-filter 重新加载 provider 后才变：稍后按新名单再核对一次策略组
            # （刚恢复的节点被策略组勾选过时补进去）；核心没在运行时 refresh_regions 不会把它拉起来
            refresh_later(8)
        return self.reply(ok, done(info["n"]) + ("；" + msg if "手动停止" in msg else "") if ok else msg)
'''
_SRC['shunt.webapi_rules'] = '''"""HTTP 接口：规则、规则模式、规则测试、广告拦截、DNS、域名历史 / 查询（类 H 的一部分，由 webapi.H 继承）"""
import copy, json, os, threading, time
from urllib.parse import parse_qs, quote

from . import adblock
from .base import AB_DIR, AB_PRESETS, DNS_DEFAULT, DNS_LISTS, G_SEL, LOCK
from .data import HISTORY_FILE, HISTORY_SKIP, load, migrate_names, read_json, save, update
from .ruleutil import AB_SCOPES, complete_rule, norm_rule
from .mihomo import bad_rule_policies, dns_cfg, rule_policy_names, validate_dns
from .core import reload_core
from .corehttp import core, core_json
from .sb_common import active_core
from .sb_config import sb_ab_loaded, write_active_config
from .latency import fix_global
from .ruletest import clean_target, rule_test
from .adblock import AB_CACHE, AB_STATE, ab_check, ab_compose, ab_meta, adblock_update, list_id, parse_filter
from .dnsleak import antileak_dns, dns_route, leak_check
from .webapi_base import tp_apply, api_route, sub_caches_sync, sub_refetch


class RulesAPI:
    """路由处理方法：@api_route 在类定义时登记到 ROUTES，方法里的 self 是 webapi.H 的实例"""

    def save_adblock(self, b):
        cur = load()["adblock"]
        lists = []
        seen = set()
        # 形状校验：lists 里混了非对象、black/white 不是列表时以前回 500；black 是字符串时还会被逐字符拆成条目
        if not isinstance(b.get("lists", []), list) or not all(isinstance(x, dict) for x in b.get("lists", [])):
            return self.send(400, {"message": "列表格式不正确"})
        for key in ("black", "black_cn", "black_proxy", "white"):
            if not isinstance(b.get(key, []), list):
                return self.send(400, {"message": "黑名单 / 白名单格式不正确"})
        try:
            int(b.get("interval", cur["interval"]) or 0)
        except (TypeError, ValueError):
            return self.send(400, {"message": "更新间隔无效"})
        for l in b.get("lists", cur["lists"]):
            url = str(l.get("url") or "").strip()
            name = str(l.get("name") or "").strip()[:40] or (url.split("/")[2] if url.count("/") >= 2 else "")
            if not url.startswith(("http://", "https://")):
                return self.send(400, {"message": f"列表地址无效：{url or '(空)'}"})
            lid = list_id(url)
            if lid in seen:
                continue
            seen.add(lid)
            scope = l.get("scope") or "all"
            if scope not in AB_SCOPES:
                return self.send(400, {"message": f"列表「{name or lid}」的生效范围无效"})
            item = {"id": lid, "name": name or lid, "url": url, "enabled": bool(l.get("enabled", True))}
            if scope != "all":
                item["scope"] = scope  # 默认的「全部」不写入，旧配置 / 旧前端提交的列表保持原样
            lists.append(item)
        clean = lambda xs: [str(x).strip() for x in xs if str(x).strip()][:20000]
        new = {"enabled": bool(b.get("enabled", cur["enabled"])), "lists": lists,
               "black": clean(b.get("black", cur["black"])), "white": clean(b.get("white", cur["white"])),
               "black_cn": clean(b.get("black_cn", cur.get("black_cn") or [])),
               "black_proxy": clean(b.get("black_proxy", cur.get("black_proxy") or [])),
               "interval": int(b.get("interval", cur["interval"]) or 0), "dns": bool(b.get("dns", cur["dns"]))}
        if new["interval"] not in (0, 21600, 43200, 86400, 259200, 604800):
            return self.send(400, {"message": "更新间隔无效"})
        for key, label in (("black", "黑名单"), ("black_cn", "国内直连黑名单"), ("black_proxy", "国外代理黑名单"), ("white", "白名单")):
            bs, be, as_, ae, sk = parse_filter("\\n".join(new[key]), plain_suffix=True)
            if sk:
                bad = [x for x in new[key] if not any(parse_filter(x, plain_suffix=True)[:4])]
                return self.send(400, {"message": f"{label}中有无法识别的条目：" + "；".join(bad[:5])})
        prev = update(lambda d: d.update(adblock=new))
        need = [l["id"] for l in lists if l["enabled"] and not os.path.isfile(os.path.join(AB_DIR, l["id"] + ".txt"))]
        ab_compose(load())
        ok, msg = reload_core(prev)
        if not ok:
            ab_compose(load())
            return self.send(500, {"message": msg})
        AB_CACHE.clear()
        if new["enabled"] and need and not AB_STATE["updating"]:
            threading.Thread(target=adblock_update, args=(need,), daemon=True).start()
            return self.send(200, {"message": "已保存，正在下载新增的列表…"})
        return self.send(200, {"message": "已保存并生效"})

    def save_dns(self, b):
        d0 = load()
        cur = dns_cfg(d0)
        if b.get("reset"):
            dc = copy.deepcopy(DNS_DEFAULT)
        elif b.get("preset") == "antileak":
            dc = antileak_dns(cur)
        else:
            dc = dict(cur)
            for k in DNS_LISTS:
                if k in b:
                    if not isinstance(b[k], list):
                        return self.send(400, {"message": f"{k} 应为列表"})
                    dc[k] = [str(x).strip() for x in b[k] if str(x).strip()]
            for k in ("mode", "cache"):
                if k in b:
                    dc[k] = b[k]
            for k in ("policy", "respect_rules", "block_bypass"):
                if k in b:
                    dc[k] = bool(b[k])
            for k in ("hosts", "policies"):
                if k in b:
                    if not isinstance(b[k], list):
                        return self.send(400, {"message": f"{k} 应为列表"})
                    dc[k] = b[k]
        if dc["mode"] not in ("fake-ip", "redir-host"):
            return self.send(400, {"message": "增强模式只能是 fake-ip 或 redir-host"})
        if dc["cache"] not in ("arc", "lru"):
            return self.send(400, {"message": "缓存算法只能是 arc 或 lru"})
        dc = dns_cfg({"dns": dc})
        errs = validate_dns(dc, d0)
        if errs:
            return self.send(400, {"message": "；".join(errs[:5])})
        sniff = b.get("preset") == "antileak" and not d0.get("sniffer", True)

        def fn(d):
            d["dns"] = dc
            if sniff:
                d["sniffer"] = True
        prev = update(fn)
        ok, msg = reload_core(prev)
        if ok and dns_cfg(prev)["block_bypass"] != dc["block_bypass"] and prev.get("proxy_mode") != "off":
            tp_apply()  # 重新加载 nftables：是否劫持发往任意服务器的 53 端口
        if ok:
            core("POST", "/cache/dns/flush")
            core("POST", "/cache/fakeip/flush") if dc["mode"] == "fake-ip" else None
        done = "已应用防泄露设置" if b.get("preset") == "antileak" else "DNS 设置已生效"
        return self.reply(ok, done if ok else msg)

    @api_route("/api/ruletest", "POST")
    def api_ruletest_post(self, m, p, b, q):
        try:
            return self.send(200, rule_test(str(b.get("target") or "")))
        except ValueError as e:  # 空目标（如只填了 http://）：给出原因，不再 500「内部错误」
            return self.send(400, {"message": str(e)})

    @api_route("/api/rules", "PUT")
    def api_rules_put(self, m, p, b, q):
        rules_in = b.get("rules")
        # 必须是文本列表：以前缺省 / 传成数字时 500，[1] 这类非文本条目被跳过后当成空列表，把全部规则清空
        if not isinstance(rules_in, list) or not all(isinstance(r, str) for r in rules_in):
            return self.send(400, {"message": "rules 必须是规则文本列表"})
        d0 = load()
        pol = rule_policy_names(d0)  # 策略名本身带「 #」的规则不当成行内注释截掉
        rules = [complete_rule(norm_rule(r.strip(), pol), G_SEL) for r in rules_in if r.strip()]
        bad = bad_rule_policies([r for r in rules if not r.startswith("#")], d0, pol)
        if bad:
            return self.send(400, {"message": "以下规则的策略不存在：" + "；".join(bad[:5])})
        prev = update(lambda d: d.update(rules=rules))
        return self.reply(*reload_core(prev))

    @api_route("/api/mode", "PUT")
    def api_mode_put(self, m, p, b, q):
        mode = b.get("mode")
        if mode not in ("rule", "global", "direct"):
            return self.send(400, {"message": "模式无效"})
        with LOCK:
            d = load()
            d["mode"] = mode
            save(d)
            sb = active_core(d) == "singbox"
            write_active_config(d)
        core("PATCH", "/configs", {"mode": mode.capitalize() if sb else mode})
        if mode == "global":
            fix_global()
        return self.send(200, {"message": "ok"})

    @api_route("/api/adblock", "GET")
    def api_adblock_get(self, m, p, b, q):
        d = load()
        meta = ab_meta()
        rep = adblock.AD_STATS.report() if adblock.AD_STATS else {"today": 0, "top": [], "lists": {}, "days": []}
        lists = [dict(l, **{k: v for k, v in (meta.get(l["id"]) or {}).items()}, hits=rep["lists"].get(l["id"], 0)) for l in d["adblock"]["lists"]]
        if active_core(d) == "singbox":  # sing-box 的 clash_api 没有规则集信息：按已转换好的规则集文件统计
            loaded = sb_ab_loaded(d) if d["adblock"].get("enabled") else {}
        else:
            prov = (core_json("/providers/rules") or {}).get("providers") or {}
            loaded = {k: v.get("ruleCount") for k, v in prov.items() if k.startswith("ad-")}
        return self.send(200, {**{k: v for k, v in d["adblock"].items() if k != "lists"}, "lists": lists,
                               "presets": AB_PRESETS, "stats": rep, "updating": AB_STATE["updating"], "msg": AB_STATE["msg"],
                               "custom_hits": rep["lists"].get("custom", 0),
                               "custom_cn_hits": rep["lists"].get("customcn", 0), "custom_proxy_hits": rep["lists"].get("customproxy", 0),
                               "loaded": loaded})

    @api_route("/api/adblock", "PUT")
    def api_adblock_put(self, m, p, b, q):
        return self.save_adblock(b)

    @api_route("/api/adblock/update", "POST")
    def api_adblock_update_post(self, m, p, b, q):
        if AB_STATE["updating"]:
            return self.send(409, {"message": "正在更新中，请稍候"})
        ids = b.get("ids")
        if ids is not None and not (isinstance(ids, list) and all(isinstance(x, str) for x in ids)):
            return self.send(400, {"message": "ids 必须是字符串数组"})
        threading.Thread(target=adblock_update, args=(ids,), daemon=True).start()
        return self.send(200, {"message": "已开始下载，完成后自动生效"})

    @api_route("/api/adblock/check", "POST")
    def api_adblock_check_post(self, m, p, b, q):
        try:
            return self.send(200, ab_check(str(b.get("domain") or "")))
        except ValueError as e:  # 无效域名：给出原因，不再 500「内部错误」
            return self.send(400, {"message": str(e)})

    @api_route("/api/dns", "PUT")
    def api_dns_put(self, m, p, b, q):
        return self.save_dns(b)

    @api_route("/api/dnsleak", "GET")
    def api_dnsleak_get(self, m, p, b, q):
        return self.send(200, leak_check(live="nolive" not in parse_qs(q)))

    @api_route("/api/history", "GET")
    def api_history_get(self, m, p, b, q):
        h = read_json(HISTORY_FILE, [])
        return self.send(200, [{"id": x["id"], "t": x["t"], "changed": x["changed"]} for x in h])

    @api_route("/api/history", "POST")
    def api_history_post(self, m, p, b, q):
        h = {x["id"]: x for x in read_json(HISTORY_FILE, [])}
        snap = h.get(str(b.get("id") or ""))
        if not snap:
            return self.send(404, {"message": "快照不存在"})

        def apply_snap(d):
            d.update({k: v for k, v in snap["data"].items() if k not in HISTORY_SKIP})
            migrate_names(d)
        prev = update(apply_snap)
        note = sub_refetch(sub_caches_sync(prev, load()))
        ab_compose(load())
        ok, msg = reload_core(prev)
        if ok:
            tp_apply()
        return self.reply(ok, ("已恢复到 " + time.strftime("%m-%d %H:%M", time.localtime(snap["t"])) + " 的配置" + note) if ok else msg)

    @api_route("/api/dnsquery", "POST")
    def api_dnsquery_post(self, m, p, b, q):
        name = clean_target(str(b.get("name") or ""))
        qtype = str(b.get("type") or "A").upper()
        if not name or qtype not in ("A", "AAAA", "CNAME", "MX", "TXT", "NS", "HTTPS", "SRV", "PTR"):
            return self.send(400, {"message": "请输入域名并选择记录类型"})
        st = time.time()
        code, raw = core("GET", f"/dns/query?name={quote(name)}&type={qtype}", timeout=15)
        ms = int((time.time() - st) * 1000)
        try:
            j = json.loads(raw)
        except Exception:
            j = {"message": raw.decode(errors="ignore")}
        if code != 200:
            return self.send(502 if code == 502 else code, {"message": j.get("message") or "查询失败"})
        return self.send(200, {"name": name, "type": qtype, "ms": ms, "status": j.get("Status"),
                               "answer": j.get("Answer") or [], "policy": dns_route(name, load())})
'''
_SRC['shunt.webapi_sys'] = '''"""HTTP 接口：状态、面板密码、代理方式、核心启停 / 切换 / 更新、面板更新、设置、备份恢复、定时任务、健康度、日志、测速、背景图等（类 H 的一部分，由 webapi.H 继承）"""
import copy, os, re, ssl, threading, time
from urllib.parse import parse_qs

from .errlog import log_err
from . import stats
from .base import (
    CERT, DEFAULT_EXCLUDE, DNS_DEFAULT, KEY, LOCK, MIHOMO_BIN, PANEL_VERSION, PROXY_MODES, SMART_BIN, TUN_STACKS, UPD_STATE, upd_begin,
    upd_busy_msg
)
from .data import config_version, load, migrate_names, normalize, save, update
from .links import q1
from .groups import provider_rev
from .mihomo import dns_cfg, drop_ruleset_cache, safe_dev
from .core import reload_core
from .corehttp import core
from .sb_common import CORES, SB_STATE, active_core, core_conf_path
from .sb_config import build_singbox, rs_changed, sb_drop_ruleset_files, sb_has_gvisor, sb_refresh_rulesets, sb_is_fork, sb_tun_native, tun_stack_tip, sb_version
from .sb_switch import core_switch_job
from .panel_update import self_rollback, self_update
from .core_update import (
    CORE_CHANNELS, CORE_UPD, LGBM_FILE, bin_version, core_check, core_job_start, core_rollback_job, core_upd_status, core_update_job, lgbm_fetch
)
from .sysopt import nft_ok, opt_apply, opt_status, speed_test
from .notify import WD, WD_EVENTS_MAX, core_proc_alive, tg_send, wd_counts
from .latency import BG_FILE, bg_fetch, bg_meta, bg_meta_save, bg_save
from .logstream import ticket_new
from .adblock import ab_compose, fmt_size
from .logclean import LOG_STATE, log_clean, log_sizes
from .schedule import SCHED, TASK_NAMES, run_task, sched_event
from .devices import tun_runtime_error
from .diag import diagnose
from .https import gen_cert, restart_self
from .auth import auth_on, cookie_header, exposure_warning, login_attempt, request_ok as auth_request_ok, set_password, verify_password, auth_hash, session_new
from .validate import clean_fields, rename_sub_clashes, rename_sub_refs
from .webapi_base import (
    _SVC_LOCK, SVC_JOB, SVC_NAMES, tp_apply, _svc_job, _svc_status, api_route, mask_secret, unmask, state_subs, config_view, ad_today,
    state_groups, state_nodes, sub_caches_sync, sub_refetch, RESTORE_KEYS, renames_note, probe_data
)


class SysAPI:
    """路由处理方法：@api_route 在类定义时登记到 ROUTES，方法里的 self 是 webapi.H 的实例"""

    def set_proxy_mode(self, b):
        mode = b.get("mode")
        if mode not in PROXY_MODES:
            return self.send(400, {"message": "代理方式无效"})
        tun_in = b.get("tun") or {}
        if not isinstance(tun_in, dict):
            return self.send(400, {"message": "tun 参数格式不正确"})
        if tun_in.get("stack") is not None and tun_in["stack"] not in TUN_STACKS:
            return self.send(400, {"message": "TUN 协议栈只能是 auto / system / gvisor / mixed"})

        def fn(d):
            d["proxy_mode"], d["tproxy"] = mode, mode == "tproxy"
            t = d["tun"]
            for k in ("stack", "auto_redirect", "strict_route"):
                if k in tun_in:
                    t[k] = tun_in[k] if k == "stack" else bool(tun_in[k])
            if "device" in tun_in:
                t["device"] = safe_dev(tun_in["device"])
        prev = update(fn)
        ok, msg = reload_core(prev)  # TUN 开关在配置里，先校验并重载核心
        if not ok:
            return self.send(500, {"message": msg})
        WD["tp_off"] = False
        code, out = tp_apply()
        label = {"tproxy": "TProxy", "tun": "TUN", "off": "关闭"}[mode]
        note = ""
        if mode == "tun" and not WD["manual_stop"]:  # 核心手动停止时不检查（TUN 网卡要等核心启动才创建）；mihomo -t 只做语法校验；TUN 网卡创建失败时核心照常运行，需要单独确认
            err = tun_runtime_error(safe_dev(load()["tun"].get("device")))
            if active_core() == "singbox" and load()["tun"].get("auto_redirect", True) and not nft_ok():
                note = "（内核不支持 nftables，auto-redirect 未启用）"
            if err and "redirect" in err.lower() and load()["tun"].get("auto_redirect"):
                # auto-redirect 依赖 nftables / iptables，内核不支持时自动关闭后重试一次
                update(lambda d: d["tun"].update(auto_redirect=False))
                ok, msg = reload_core()
                err = tun_runtime_error(safe_dev(load()["tun"].get("device"))) if ok else msg
                note = "（auto-redirect 不可用，已自动关闭）" if not err else ""
            if err:  # TUN 起不来时局域网会断网：恢复为原来的代理方式
                with LOCK:  # 与 update() 一样在锁内写：data.json.tmp 是共用的临时文件名，并发写会互相截断 / 改名失败
                    save(prev)
                reload_core()
                tp_apply()
                return self.send(500, {"message": "TUN 启动失败，已恢复原来的代理方式：" + err +
                                       "。可尝试：关闭 auto-redirect、" + tun_stack_tip() + "、确认以 root 运行并已加载 tun 模块（modprobe tun）"})
        if WD["manual_stop"]:
            note += "（核心已手动停止，启动后生效）"
        if code != 0:
            return self.send(500, {"message": f"核心已切换为 {label}，但 nftables 规则处理失败：{out[-200:]}"})
        return self.send(200, {"message": f"代理方式：{label}{note}。" + (out.splitlines()[-1] if out else "")})

    @api_route("/api/state", "GET")
    def api_state_get(self, m, p, b, q):
        d = load()
        st, code, version = _svc_status()
        groups, gmeta = state_groups(d)
        out = {"running": "started" in st or code == 200, "version": version, "groups": groups, "group_meta": gmeta,
               "nodes": state_nodes(d), "default_exclude": DEFAULT_EXCLUDE, "sched_events": list(SCHED["events"])[:20],
               "panel_version": PANEL_VERSION, "core": active_core(d), "sb_fork": sb_is_fork(),
               "tun_gvisor": active_core(d) != "singbox" or sb_has_gvisor(),
               "tun_native": active_core(d) == "singbox" and sb_tun_native(),
               "wd": {"status": WD["status"], "last_check": WD["last_check"], "events": list(WD["events"]),
                      "counts": wd_counts(), "max": WD_EVENTS_MAX}}
        for k in ("mode", "subs", "rules", "rulesets", "bypass", "tests", "sub_interval", "ipv6", "https", "watchdog", "tg_token",
                  "tg_chat", "proxy_mode", "tun", "log_limit", "schedule", "devices", "sniffer", "gh_proxy"):
            out[k] = d.get(k)
        # 与 /api/config 一致：订阅 URL（常带 token）和 TG token 打码；前端原样提交回来时由 unmask() 换回原值
        out["subs"] = state_subs(d.get("subs"))
        out["tg_token"] = mask_secret(d.get("tg_token"))
        out["gh_token"] = mask_secret(d.get("gh_token"))  # GitHub Token 同样只回打码值
        try:
            out["dns"] = dns_cfg(d)
        except Exception as e:
            log_err("/api/state：DNS 设置", e)
            out["dns"] = copy.deepcopy(DNS_DEFAULT)
        out["adblock_on"] = bool((d.get("adblock") or {}).get("enabled")) if isinstance(d.get("adblock"), dict) else False
        out["auth"] = {"enabled": auth_on(), "warn": exposure_warning()}
        return self.send(200, out)

    @api_route("/api/auth/status", "GET")
    def api_auth_status_get(self, m, p, b, q):
        return self.send(200, {"enabled": auth_on(), "logged_in": auth_request_ok(self.headers), "warn": exposure_warning()})

    @api_route("/api/auth/login", "POST")
    def api_auth_login_post(self, m, p, b, q):
        if not auth_on():
            return self.send(200, {"message": "面板没有设置密码", "enabled": False})
        ok, tok = login_attempt(self.client_address[0], b.get("password") if isinstance(b.get("password"), str) else "")
        if not ok:
            return self.send(429 if "秒后" in tok else 401, {"message": tok, "auth": True})
        self._cookie = cookie_header(tok, secure=isinstance(self.connection, ssl.SSLSocket))
        return self.send(200, {"message": "已登录", "enabled": True})

    @api_route("/api/auth/logout", "POST")
    def api_auth_logout_post(self, m, p, b, q):
        self._cookie = cookie_header("", secure=isinstance(self.connection, ssl.SSLSocket), clear=True)
        return self.send(200, {"message": "已退出登录"})

    @api_route("/api/auth/password", "POST")
    def api_auth_password_post(self, m, p, b, q):
        """设置 / 修改 / 关闭面板密码。已设置密码时要先登录（route 已检查），并且要再输一次当前密码"""
        new = b.get("password", "")
        if not isinstance(new, str):
            return self.send(400, {"message": "密码格式不正确"})
        if auth_on() and not verify_password(b.get("current") if isinstance(b.get("current"), str) else "", auth_hash(fresh=True)):
            return self.send(403, {"message": "当前密码不正确"})
        try:
            h = set_password(new)
        except ValueError as e:
            return self.send(400, {"message": str(e)})
        secure = isinstance(self.connection, ssl.SSLSocket)
        self._cookie = cookie_header(session_new(h), secure=secure) if h else cookie_header("", secure=secure, clear=True)
        return self.send(200, {"message": "已设置面板密码" if h else "已关闭面板密码", "enabled": bool(h)})

    @api_route("/api/ping", "GET")
    def api_ping_get(self, m, p, b, q):
        """轻量心跳：前端 15 秒轮询，version 变化时才拉全量 /api/state。
        version = 配置版本（data.json 每次 save 自增）+ 订阅节点名单版本（新订阅下载完、节点增删时地区组会变）"""
        return self.send(200, {"ok": True, "core": active_core(), "version": f"{config_version()}.{provider_rev()}",
                               "ad_today": ad_today()})

    @api_route("/api/config", "GET")
    def api_config_get(self, m, p, b, q):
        txt = config_view(load(), masked=True)
        sbm = active_core() == "singbox"
        if "download" in parse_qs(q):
            return self.send(200, txt.encode(), "application/json" if sbm else "application/x-yaml")
        return self.send(200, {"text": txt, "path": core_conf_path(), "size": len(txt.encode()), "core": active_core()})

    @api_route("/api/sysopt", "GET")
    def api_sysopt_get(self, m, p, b, q):
        return self.send(200, opt_status())

    @api_route("/api/sysopt", "POST")
    def api_sysopt_post(self, m, p, b, q):
        ids = b.get("ids")
        if not isinstance(ids, list):
            return self.send(400, {"message": "参数错误"})
        ok, msg, errs = opt_apply([str(i) for i in ids])
        return self.send(200, {"ok": ok, "message": msg, "errors": errs} | opt_status())

    @api_route("/api/speedtest", "GET")
    def api_speedtest_get(self, m, p, b, q):
        via = q1(parse_qs(q), "via", default="proxy")
        return self.send(200, speed_test(via == "proxy"))

    @api_route("/api/speed", "GET")
    def api_speed_get(self, m, p, b, q):
        """「测速」页：当前进度 + 各项最近一次结果 + 历史"""
        from . import speedtest
        return self.send(200, speedtest.status())

    @api_route("/api/speed", "POST")
    def api_speed_post(self, m, p, b, q):
        from . import speedtest
        kind = str(b.get("kind") or "")
        if kind not in ("cn", "intl", "all"):
            return self.send(400, {"message": "参数错误"})
        ok = speedtest.start(kind)
        return self.send(200, {"ok": ok, "message": "" if ok else "正在测速，请稍候"} | speedtest.status())

    @api_route("/api/bg", "GET")
    def api_bg_get(self, m, p, b, q):
        bm = bg_meta()
        try:
            return self.send(200, {"exists": True, "v": int(os.path.getmtime(BG_FILE)), "size": os.path.getsize(BG_FILE),
                                   "url": bm.get("url", ""), "daily": bool(bm.get("daily"))})
        except OSError:
            return self.send(200, {"exists": False})

    @api_route("/api/bg", "POST")
    def api_bg_post(self, m, p, b, q):
        ok, msg = bg_save(b.get("data"))
        if not ok:
            return self.send(400, {"message": msg})
        bg_meta_save({})  # 改为上传的图片：不再按链接刷新
        return self.send(200, {"message": msg, "v": int(os.path.getmtime(BG_FILE))})

    @api_route("/api/bg/url", "POST")
    def api_bg_url_post(self, m, p, b, q):
        url = str(b.get("url") or "").strip() or bg_meta().get("url", "")
        ok, msg = bg_fetch(url)
        if not ok:
            return self.send(400, {"message": msg})
        bg_meta_save({"url": url, "daily": bool(b.get("daily")), "t": int(time.time())})
        return self.send(200, {"message": msg, "v": int(os.path.getmtime(BG_FILE))})

    @api_route("/api/bg/daily", "POST")
    def api_bg_daily_post(self, m, p, b, q):
        bm = bg_meta()
        if not bm.get("url"):
            return self.send(400, {"message": "当前背景图不是链接图片"})
        bm["daily"] = bool(b.get("daily"))
        bg_meta_save(bm)
        return self.send(200, {"message": "已开启每天自动更新" if bm["daily"] else "已关闭每天自动更新"})

    @api_route("/api/bg", "DELETE")
    def api_bg_delete(self, m, p, b, q):
        try:
            os.remove(BG_FILE)
        except OSError:
            pass
        bg_meta_save({})
        return self.send(200, {"message": "已移除背景图"})

    @api_route("/api/coreswitch", "GET")
    def api_coreswitch_get(self, m, p, b, q):
        d = load()
        if active_core(d) == "singbox" and not SB_STATE["warn"]:
            SB_STATE["warn"] = build_singbox(d)[1]
        from .core_cache import current_variant, status as cache_status
        return self.send(200, {"core": active_core(d), "variant": current_variant(), "cache": cache_status(),
                               "versions": {"mihomo": bin_version(MIHOMO_BIN), "smart": bin_version(SMART_BIN), "singbox": sb_version()}, "fork": sb_is_fork(),
                               "lgbm": os.path.isfile(LGBM_FILE),
                               "warn": SB_STATE["warn"] if active_core(d) == "singbox" else [], "busy": CORE_UPD["busy"]})

    @api_route("/api/coreswitch", "POST")
    def api_coreswitch_post(self, m, p, b, q):
        from .core_cache import SB_VARIANTS, current_variant, prefetch_start, set_enabled, variants
        gh = load().get("gh_proxy") or ""
        act = b.get("action")
        if any(not isinstance(b.get(k), (str, type(None))) for k in ("action", "core", "variant")):
            return self.send(400, {"message": "参数无效"})  # 以前传成列表、对象时 `in` / 字典查找抛 TypeError 回 500
        if act == "prefetch_auto":  # 自动更新已下载的内核 开 / 关
            set_enabled(bool(b.get("enabled")))
            return self.reply(True, "已开启自动更新已下载的内核" if b.get("enabled") else "已关闭自动下载（切换到没下载过的内核时仍会自动下载）")
        if act == "clean":  # 只保留正在用的内核：删缓存的其他版本、.bak 旧版本、不在使用的 Smart
            from .core_cache import clean
            return self.reply(*clean())
        if act in ("prefetch", "download"):  # prefetch：已下载过的内核有新版本就下载；download：只下载指定的一个（force 时即使已是最新也重新下载）
            only = None
            if act == "download":
                it = str(b.get("item") or "")
                if it not in ["sb_" + v for v in variants()] + ["mihomo", "smart"]:
                    return self.send(400, {"message": "未知的内核"})
                only = [it]
            ok, msg = prefetch_start(gh, only, bool(b.get("force")))
            return self.send(200 if ok else 409, {"message": msg})
        if act == "check":
            from .core_cache import check_all
            return self.send(200, {"remote": check_all(gh)})
        kind = b.get("core")
        if kind not in CORES:
            return self.send(400, {"message": "未知的内核"})
        var = b.get("variant") or None
        if kind == "singbox" and var is not None and var not in variants():
            return self.send(400, {"message": "当前 CPU 架构没有这个 sing-box 版本" if var in SB_VARIANTS else "未知的 sing-box 版本"})
        if kind == active_core() and (kind != "singbox" or not var or var == current_variant()):
            return self.send(400, {"message": f"当前已经是 {CORES[kind] if kind != 'singbox' else SB_VARIANTS.get(current_variant(), ('sing-box',))[0]}"})
        ok, msg = core_job_start(core_switch_job, kind, gh, var)
        return self.send(200 if ok else 409, {"message": msg})

    @api_route("/api/coreupdate", "GET")
    def api_coreupdate_get(self, m, p, b, q):
        ch = q1(parse_qs(q), "channel", default="stable")
        kind = "smart" if (q1(parse_qs(q), "core") or active_core()) == "smart" else "mihomo"
        if ch not in CORE_CHANNELS:
            return self.send(400, {"message": "未知的更新通道"})
        ok, info = core_check(ch, load().get("gh_proxy") or "", kind)
        info["busy"] = CORE_UPD["busy"]
        return self.send(200 if ok else 502, info)

    @api_route("/api/coreupdate/status", "GET")
    def api_coreupdate_status_get(self, m, p, b, q):
        return self.send(200, core_upd_status())

    @api_route("/api/coreupdate", "POST")
    def api_coreupdate_post(self, m, p, b, q):
        kind = "smart" if (b.get("core") or active_core()) == "smart" else "mihomo"
        if b.get("action") == "lgbm":
            try:
                n = lgbm_fetch(load().get("gh_proxy") or "")
            except Exception as e:
                return self.send(502, {"message": "模型下载失败：" + str(e)[:200]})
            if active_core() == "smart":
                reload_core()
            return self.reply(True, f"LightGBM 模型已更新（{n // 2**20} MB）")
        if b.get("action") == "rollback":
            ok, msg = core_job_start(core_rollback_job, kind)
        else:
            ch = "alpha" if kind == "smart" else (b.get("channel") or "stable")
            if ch not in CORE_CHANNELS:
                return self.send(400, {"message": "未知的更新通道"})
            ok, msg = core_job_start(core_update_job, ch, load().get("gh_proxy") or "", bool(b.get("force")), kind)
        return self.send(200 if ok else 409, {"message": msg})

    @api_route("/api/selfupdate", "GET")
    def api_selfupdate_get(self, m, p, b, q):
        ok, info = self_update(False, load().get("gh_proxy") or "")
        return self.send(200 if ok else 502, info)

    @api_route("/api/selfupdate", "POST")
    def api_selfupdate_post(self, m, p, b, q):
        if b.get("action") == "rollback":
            return self.reply(*self_rollback())
        ok, info = self_update(True, load().get("gh_proxy") or "")
        return self.send(200 if ok else 500, info)

    @api_route("/api/stats", "GET")
    def api_stats_get(self, m, p, b, q):
        return self.send(200, stats.STATS.report() if stats.STATS else {"today": {}, "month": {}, "days": [], "arp": {}})

    @api_route("/api/proxymode", "PUT")
    def api_proxymode_put(self, m, p, b, q):
        return self.set_proxy_mode(b)

    @api_route("/api/logs/info", "GET")
    def api_logs_info_get(self, m, p, b, q):
        return self.send(200, {"files": log_sizes(), "limit": load()["log_limit"], "last": LOG_STATE["last"]})

    @api_route("/api/logs/clean", "POST")
    def api_logs_clean_post(self, m, p, b, q):
        freed = log_clean(force=True)
        return self.send(200, {"message": f"已清理，释放 {fmt_size(freed)}", "files": log_sizes()})

    @api_route("/api/diag", "GET")
    def api_diag_get(self, m, p, b, q):
        return self.send(200, diagnose())

    @api_route("/api/schedule", "PUT")
    def api_schedule_put(self, m, p, b, q):
        sc = {}
        for k in ("sub_update", "core_restart", "geo_update"):
            v = str(b.get(k) or "").strip()
            if v and not re.fullmatch(r"([01]\\d|2[0-3]):[0-5]\\d", v):
                return self.send(400, {"message": f"时间格式应为 HH:MM：{v}"})
            sc[k] = v
        try:  # latency:"abc" / [1] 以前 int() 抛异常回 500
            lat = int(b.get("latency") or 0)
        except (TypeError, ValueError):
            return self.send(400, {"message": "定时测速间隔无效"})
        sc["latency"] = lat if lat in (0, 10, 30, 60, 180) else 0
        update(lambda d: d.update(schedule=sc))
        return self.send(200, {"message": "定时任务已保存"})

    @api_route("/api/schedule/run", "POST")
    def api_schedule_run_post(self, m, p, b, q):
        k = b.get("task")
        if k not in TASK_NAMES:
            return self.send(400, {"message": "未知任务"})
        ok, msg = run_task(k)
        sched_event(f"手动执行 {TASK_NAMES[k]}：{'✓' if ok else '✗'} {msg}")
        return self.reply(ok, msg)

    @api_route("/api/service", "GET")
    def api_service_get(self, m, p, b, q):
        return self.send(200, dict(SVC_JOB, alive=core_proc_alive()))

    @api_route("/api/service", "POST")
    def api_service_post(self, m, p, b, q):
        act = b.get("action")
        if act == "reload":
            return self.reply(*reload_core(force=True))
        if act in ("restart", "stop", "start"):
            if not _SVC_LOCK.acquire(blocking=False):  # 防连点 / 多标签页同时操作：两个 rc-service 并发会互相打断
                return self.send(409, {**SVC_JOB, "message": f"正在{SVC_NAMES.get(SVC_JOB['act'], '')}核心，请稍候"})
            SVC_JOB.update(busy=True, act=act, ok=None, message="", t=time.time())
            threading.Thread(target=_svc_job, args=(act,), daemon=True).start()  # 后台执行：请求立即返回，前端轮询结果
            return self.send(202, {**SVC_JOB, "message": f"正在{SVC_NAMES[act]}核心…"})
        if act == "upgrade" and active_core() == "singbox":
            return self.send(400, {"message": "请在 设置 → 核心 → 内核更新 里更新 sing-box"})
        if act == "upgrade":
            code, raw = core("POST", "/upgrade", {}, timeout=300)
            return self.send(code if code != 502 else 500, raw or b'{"message":"ok"}')
        if act == "geo" and active_core() == "singbox":
            return self.reply(*run_task("geo_update"))
        if act == "geo":
            code, raw = core("POST", "/configs/geo", {}, timeout=120)
            return self.send(code, raw or b'{"message":"ok"}')
        return self.send(400, {"message": "未知操作"})

    @api_route("/api/settings", "PUT")
    def api_settings_put(self, m, p, b, q):
        allowed = {"rulesets", "bypass", "tests", "sub_interval", "region_groups", "ipv6", "watchdog", "tg_token", "tg_chat",
                   "log_limit", "sniffer", "gh_proxy", "gh_token"}
        if "gh_proxy" in b:
            b["gh_proxy"] = str(b["gh_proxy"] or "").strip()
            if b["gh_proxy"] and not re.match(r"^https?://[\\w.\\-:]+/?$", b["gh_proxy"]):
                return self.send(400, {"message": "GitHub 加速地址格式应为 https://ghfast.top/"})
            if b["gh_proxy"] and not b["gh_proxy"].endswith("/"):
                b["gh_proxy"] += "/"
        if "gh_token" in b:  # 打码的 Token 原样提交回来 = 没改；ghp_… / github_pat_… 只含字母数字下划线
            b["gh_token"] = unmask(str(b["gh_token"] or "").strip(), load().get("gh_token"), mask_secret)
            if b["gh_token"] and not re.match(r"^[A-Za-z0-9_]{20,255}$", b["gh_token"]):
                return self.send(400, {"message": "GitHub Token 格式不对，应为 ghp_ 或 github_pat_ 开头的一串字母数字"})
        if "tg_token" in b:  # 打码的 token 原样提交回来 = 没改
            b["tg_token"] = unmask(b["tg_token"], load().get("tg_token"), mask_secret)
        if "sniffer" in b:
            b["sniffer"] = bool(b["sniffer"])
        if "log_limit" in b and b["log_limit"] not in (1, 2, 5, 10, 20):
            return self.send(400, {"message": "日志上限只能是 1 / 2 / 5 / 10 / 20 MB"})
        try:  # 形状校验：残缺的规则集 / 绕过设备 / 延迟站点直接 400，不写进 data.json
            new = clean_fields(b, allowed)
        except ValueError as e:
            return self.send(400, {"message": str(e)})
        test = load()
        test.update(new)
        err = probe_data(test)
        if err:
            return self.send(400, {"message": err})
        prev = update(lambda d: d.update(new))
        d = load()
        msg = "已保存"
        if "rulesets" in new:  # 新增 / 同名替换（换了地址、类型、格式）的规则集：不再沿用旧地址的缓存文件
            changed = rs_changed(prev, d)
            for n in changed:
                drop_ruleset_cache(n)
            if changed and active_core(d) == "singbox":
                sb_refresh_rulesets(d, changed)
            elif changed:
                sb_drop_ruleset_files(d, changed)
        if set(b) & {"rulesets", "sub_interval", "region_groups", "ipv6", "sniffer"} or ("bypass" in b and d["proxy_mode"] == "tun"):
            ok, msg = reload_core(prev)
            if not ok:
                return self.send(500, {"message": msg})
        if set(b) & {"bypass", "ipv6"} and d["proxy_mode"] != "off":
            tp_apply()
        if "watchdog" in b and not b["watchdog"]:
            WD["status"] = "off"
        return self.send(200, {"message": msg})

    @api_route("/api/https", "PUT")
    def api_https_put(self, m, p, b, q):
        on = bool(b.get("enable"))
        if on and not (os.path.isfile(CERT) and os.path.isfile(KEY)):
            ok, out = gen_cert()
            if not ok:
                return self.send(500, {"message": "生成证书失败（需要 openssl）：" + out[-200:]})
        update(lambda d: d.update(https=on))
        restart_self()
        return self.send(200, {"message": "面板将以 " + ("HTTPS" if on else "HTTP") + " 重启", "scheme": "https" if on else "http"})

    @api_route("/api/tg/test", "POST")
    def api_tg_test_post(self, m, p, b, q):
        d = load()
        if "tg_token" in b:
            d.update(tg_token=unmask(b.get("tg_token", ""), d.get("tg_token"), mask_secret), tg_chat=b.get("tg_chat", ""))
        return self.reply(*tg_send("✅ 测试消息：通知已配置成功", d))

    @api_route("/api/backup", "GET")
    def api_backup_get(self, m, p, b, q):
        d = load()
        return self.send(200, {k: v for k, v in d.items() if k not in ("secret", "panel_password", "gh_token")})  # 面板密码哈希、GitHub Token 不导出

    @api_route("/api/backup/list", "GET")
    def api_backup_list(self, m, p, b, q):
        from . import backup as backup_mod
        return self.send(200, {"backups": backup_mod.list()})

    @api_route("/api/backup/create", "POST")
    def api_backup_create(self, m, p, b, q):
        from . import backup as backup_mod
        ok, name = backup_mod.create()
        return self.send(200 if ok else 500, {"ok": ok, "name": name if ok else "",
                                              "message": name if ok else "备份失败：%s" % name})

    @api_route("/api/backup/restore", "POST")
    def api_backup_restore(self, m, p, b, q):
        # 与面板在线更新 / 内核更新互斥：以前恢复不占忙标志，与在线更新同时进行时两边交替覆盖 server.py / index.html，
        # 可能留下新旧混合的文件；内核替换到一半时恢复又会 restart_self 重启面板
        if not upd_begin("panel"):
            return self.send(409, {"message": upd_busy_msg()})
        try:
            return self._backup_restore(b)
        finally:
            UPD_STATE["busy"] = False

    def _backup_restore(self, b):
        from . import backup as backup_mod
        st = {}

        def check(raw):
            """与「恢复设置」相同的校验：CLEANERS 形状校验 → 订阅名冲突自动改名 → 补默认值 / 迁移 → 试生成配置。
            当前的 core / secret 保留（切换内核与控制密钥不随备份回退）"""
            if not isinstance(raw, dict):
                raise ValueError("data.json 不是 JSON 对象")
            vals = clean_fields(raw, RESTORE_KEYS)
            d = copy.deepcopy(raw)
            d.update(vals)
            if "subs" in vals:
                d["subs"], st["renames"] = rename_sub_clashes(vals["subs"])
                d["custom_groups"] = rename_sub_refs(d.get("custom_groups"), st["renames"])
            try:
                normalize(d)
            except Exception as e:
                raise ValueError(f"{type(e).__name__}: {e}"[:300])
            cur = load()
            for k in ("core", "secret"):
                d[k] = cur[k]
            d.pop("panel_password", None)  # 面板密码也保留当前的：快照里的旧密码 / 没有密码都不带回来
            if cur.get("panel_password"):
                d["panel_password"] = cur["panel_password"]
            err = probe_data(d)
            if err:
                raise ValueError(err)
            return d

        def apply(new):
            def fn(d):
                keep = {k: d[k] for k in ("core", "secret") if k in d}
                d.clear()
                d.update(copy.deepcopy(new))
                d.update(keep)
            st["prev"] = update(fn)

        ok, msg = backup_mod.restore(str(b.get("name") or "").strip(), check_data=check, apply_data=apply)
        restored = ok  # 程序文件（server.py / index.html）已换成备份里的版本
        if ok and "prev" in st:
            note = sub_refetch(sub_caches_sync(st["prev"], load()))
            if note:
                msg += note.lstrip("；") + "；"
            ab_compose(load())
            rok, rmsg = reload_core(st["prev"])
            if rok:
                tp_apply()
            msg += ("核心已重载：" if rok else "设置已撤销：") + rmsg
            ok = rok
        if restored:
            # index.html 按文件修改时间立即换成备份版本，正在运行的却还是当前的 server.py：不重启就是新旧前后端混跑。
            # 以前要用户自己重启面板，前端的「是否现在重启」又判断了后端从不返回的 j.ok，从来不会出现
            restart_self()
            msg += "；面板正在重启"
        # 程序文件已换（面板要重启）时也回 200，前端据 restart 刷新页面；ok=False 表示设置部分已撤销（见 message）
        return self.send(200 if ok or restored else 500, {"message": msg + renames_note(st.get("renames")), "ok": ok, "restart": restored})

    @api_route("/api/backup/delete", "POST")
    def api_backup_delete(self, m, p, b, q):
        from . import backup as backup_mod
        ok, msg = backup_mod.delete(str(b.get("name") or "").strip())
        if not ok and msg in ("非法文件名", "不是有效的备份文件", "备份不存在"):  # 参数问题返回 400 / 404，不是 500
            return self.send(404 if msg == "备份不存在" else 400, {"message": msg})
        return self.reply(ok, msg)

    @api_route("/api/health/status", "GET")
    def api_health_status(self, m, p, b, q):
        from . import health as health_mod
        return self.send(200, {"running": health_mod.HEALTH_STATE["running"],
                               "last_run": health_mod.HEALTH_STATE["last_run"],
                               "total": health_mod.HEALTH_STATE["total"],
                               "checked": health_mod.HEALTH_STATE["checked"],
                               "cfg": health_mod.health_cfg(),
                               "records": health_mod.get_records()})

    @api_route("/api/health/check", "POST")
    def api_health_check(self, m, p, b, q):
        from . import health as health_mod
        ok = health_mod.run_check_bg()
        return self.send(200, {"ok": ok, "message": "已开始检查" if ok else "检查正在进行中"})

    @api_route("/api/health/unhide", "POST")
    def api_health_unhide(self, m, p, b, q):
        from . import health as health_mod
        name = str(b.get("node_id") or "").strip()
        ok = health_mod.unhide(name) if name else False
        if not ok:  # 404 而不是 500：不是服务器内部错误
            return self.send(404, {"message": "节点不存在"})
        return self.reply(True, "已取消隐藏")

    @api_route("/api/health/cfg", "PUT")
    def api_health_cfg(self, m, p, b, q):
        from . import health as health_mod
        cfg = health_mod.health_cfg()
        if "enabled" in b:
            cfg["enabled"] = bool(b["enabled"])
        if "time" in b:
            t = str(b["time"]).strip()
            if not re.fullmatch(r"([01]\\d|2[0-3]):[0-5]\\d", t):
                return self.send(400, {"ok": False, "message": "时间格式应为 HH:MM"})
            cfg["time"] = t
        for k in ("warn_days", "hide_days"):
            if k in b:
                try:
                    v = int(b[k])
                    if v < 1 or v > 30:
                        raise ValueError
                    cfg[k] = v
                except (ValueError, TypeError):
                    return self.send(400, {"ok": False, "message": f"{k} 应为 1-30 的整数"})
        if cfg["warn_days"] >= cfg["hide_days"]:
            return self.send(400, {"ok": False, "message": "标红天数应小于隐藏天数"})
        health_mod.save_health_cfg(cfg)
        return self.send(200, {"ok": True, "cfg": cfg})

    @api_route("/api/restore", "POST")
    def api_restore_post(self, m, p, b, q):
        try:  # 恢复残缺 / 旧格式的备份：任何一项形状不对都整体拒绝，不写进 data.json
            vals = clean_fields(b, RESTORE_KEYS)
        except ValueError as e:
            return self.send(400, {"message": "备份内容无效：" + str(e)})
        if not vals:
            return self.send(400, {"message": "备份内容无效：没有可恢复的设置"})
        renames = []
        if "subs" in vals:  # 订阅名只差符号会共用同一个节点缓存文件：自动改名
            vals["subs"], renames = rename_sub_clashes(vals["subs"])
            if "custom_groups" in vals:  # 自定义组勾选的订阅节点跟着改指新订阅名
                vals["custom_groups"] = rename_sub_refs(vals["custom_groups"], renames)

        def apply_backup(d):
            d.update(copy.deepcopy(vals))
            if renames and "custom_groups" not in vals:  # 备份里没有自定义组：沿用的现有组里勾选的订阅节点也跟着改指新订阅名
                d["custom_groups"] = rename_sub_refs(d.get("custom_groups"), renames)
            if "proxy_mode" not in vals and "tproxy" in vals:
                d["proxy_mode"] = "tproxy" if vals["tproxy"] else "off"
            if d.get("proxy_mode") not in PROXY_MODES:
                d["proxy_mode"] = "tproxy" if d.get("tproxy", True) else "off"
            migrate_names(d)  # 旧版备份里的地区组名自动换成新名称
        test = load()
        apply_backup(test)
        err = probe_data(test)
        if err:
            return self.send(400, {"message": "备份内容无效：" + err})
        prev = update(apply_backup)
        note = sub_refetch(sub_caches_sync(prev, load()))
        ab_compose(load())
        ok, msg = reload_core(prev)
        if ok:
            tp_apply()
        return self.reply(ok, ("已恢复，" if ok else "") + msg + renames_note(renames) + (note if ok else ""))

    @api_route("/api/logticket", "POST")
    def api_logticket_post(self, m, p, b, q):
        return self.send(200, {"ticket": ticket_new()})
'''
_SRC['shunt.webapi'] = '''"""HTTP：请求处理类 H（连接 / 安全检查 / 路由分发）。各组接口在 webapi_nodes / webapi_subs / webapi_rules / webapi_sys，公共部分在 webapi_base"""
import gzip, json, os, re, shutil, socket, ssl, threading
from http.server import BaseHTTPRequestHandler
from collections import deque
from urllib.parse import parse_qs, unquote, urlparse

from .errlog import log_err
from .base import MAX_BODY, upd_begin, upd_busy_msg
from .data import load
from .links import q1
from .groups import sb_provider_nodes
from .core import refresh_regions, reload_core
from .corehttp import core
from .sb_common import CORES, SB_BIN, active_core
from .sb_subs import sb_update_subs
from .sb_config import sb_fake_providers, sb_fake_rule_providers, sb_is_fork, sb_rule_need, sb_sync_assets, sb_version, write_active_config
from .sb_switch import CHANNELS as SB_CHANNELS, sb_arch, sb_remote_version, sb_update_job
from .core_update import CORE_UPD, MANUAL_NOTE, core_job_start
from .sysopt import delay_test, favicon, svc_restart
from .notify import WD
from .latency import BG_FILE, bg_type
from .unlock import unlock_test
from .logstream import core_stream, ticket_use
from .nodeprobe import UI_BUDGET, fill_group, probe_nodes
from .access import acl_cfg, host_name, host_ok, ip_is_lan
from .auth import request_ok as auth_request_ok
from .static import MANIFEST, STATIC_TYPES, static_file
from .webapi_base import ROUTES, OPEN_GET, AUTH_OPEN
from .webapi_nodes import NodesAPI
from .webapi_subs import SubsAPI
from .webapi_rules import RulesAPI
from .webapi_sys import SysAPI


class H(NodesAPI, SubsAPI, RulesAPI, SysAPI, BaseHTTPRequestHandler):
    # HTTP/1.1 长连接：每秒的 /api/live 等轮询复用同一条 TCP / TLS 连接，不再每次握手。
    # 约定：每个响应都带准确的 Content-Length（204 / 304 无 body），流式响应发 Connection: close；
    # 请求体在路由前完整读掉，读不了的（超限、chunked）回应后关闭连接，避免残留字节被当成下一个请求
    protocol_version = "HTTP/1.1"
    timeout = 30  # 读请求/TLS 握手的超时：空闲的长连接 30 秒后关闭，卡住不发数据的连接不再永久占用线程（日志流只写不读，不受影响）
    _want_close = False  # 本次响应后关闭连接（end_headers 时补发 Connection: close）
    _started = False  # 本次响应的状态行是否已发出
    _raw = b""  # 本次请求已读入的请求体
    _cookie = ""  # 本次响应要下发的 Set-Cookie（登录 / 退出 / 改密码）

    def send_response(self, code, message=None):
        self._started = True
        super().send_response(code, message)

    def end_headers(self):
        if self._want_close and not self.close_connection:
            self.send_header("Connection", "close")  # send_header 会同时置 close_connection
        super().end_headers()

    def log_message(self, *a):
        pass

    def gzip_ok(self):
        return "gzip" in (self.headers.get("Accept-Encoding") or "")

    def sec_headers(self):
        self.send_header("X-Frame-Options", "SAMEORIGIN")  # 防点击劫持
        if self._cookie:
            self.send_header("Set-Cookie", self._cookie)
            self._cookie = ""
        if isinstance(self.connection, ssl.SSLSocket):  # 启用 HTTPS 后才有意义
            self.send_header("Strict-Transport-Security", "max-age=31536000")

    def send(self, code, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        if code < 200 or code in (204, 304):  # 这些状态码不允许带 body：写了会污染长连接上的下一个响应
            self.send_response(code)
            self.send_header("Cache-Control", "no-store")
            self.sec_headers()
            self.end_headers()
            return
        zipped = len(body) > 1400 and ctype in ("application/json", "text/html", "text/plain") and self.gzip_ok()
        if zipped:  # 连接列表、节点列表等较大的 JSON 压缩后通常只有 1/5～1/10
            body = gzip.compress(body, 5)
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        if zipped:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.sec_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, name):
        f = static_file(name)
        if not f:
            return self.send(404, {"message": "not found"})
        raw, gz, etag, ctype = f
        if self.headers.get("If-None-Match") == etag:  # 浏览器已有最新版本，不再传输
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.sec_headers()
            self.end_headers()
            return
        body = gz if gz is not None and self.gzip_ok() else raw
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
        if body is gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        # 页面每次打开都向服务器确认（未变化时只回 304），面板更新后立即生效；图标缓存 1 天
        self.send_header("Cache-Control", "no-cache" if name == "index.html" else "public, max-age=86400")
        self.sec_headers()
        self.end_headers()
        self.wfile.write(body)

    def pending_body(self):
        """请求声明的 body 长度；无法按长度读取（chunked 等）返回 -1，Content-Length 非法返回 -2"""
        te = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if te and te != "identity":
            return -1
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return -2
        return n if n >= 0 else -2

    def read_body(self):
        """把请求体完整读掉（长连接上不能留下未读字节）。返回错误响应已发出时为 False"""
        self._raw = b""
        n = self.pending_body()
        if n == 0:
            return True
        self._want_close = True  # 下面任何一条错误路径都不再复用这条连接
        if n == -1:
            self.send(411, {"message": "不支持分块传输的请求体，请带上 Content-Length"})
            return False
        if n == -2:
            self.send(400, {"message": "Content-Length 无效"})
            return False
        if n > MAX_BODY:  # 防止超大 body 耗尽路由器内存
            self.send(413, {"message": "请求体过大"})
            return False
        raw = self.rfile.read(n)
        if len(raw) < n:  # 客户端中途断开
            self.close_connection = True
            return False
        self._raw = raw
        self._want_close = False
        return True

    def body(self):
        if not self._raw:
            return {}
        try:
            return json.loads(self._raw)
        except Exception:
            return {}

    def do_GET(self): self.route("GET")
    def do_POST(self): self.route("POST")
    def do_PUT(self): self.route("PUT")
    def do_DELETE(self): self.route("DELETE")
    def do_PATCH(self): self.route("PATCH")

    def deny(self, why):
        if self.path.startswith("/api/"):
            return self.send(403, {"message": why})
        from html import escape as html_escape
        html = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                "<title>Shunt</title><div style='font:15px/1.7 system-ui;max-width:640px;margin:40px auto;padding:0 16px'>"
                f"<h3>访问被拒绝</h3><p>{html_escape(why)}</p></div>")
        return self.send(403, html.encode(), "text/html")

    def guard(self, m, p):
        """返回拒绝原因；空串表示放行"""
        a = acl_cfg()
        ip = self.client_address[0]
        if not a["public"] and not ip_is_lan(ip):
            return (f"面板默认只允许局域网访问，你的地址 {ip} 不是局域网地址。确实需要从外网访问时，"
                    "在路由器上执行：python3 /opt/mihomo-panel/server.py --allow-public on（请同时用 --password 设置面板密码）")
        host = self.headers.get("Host") or ""
        if not host_ok(host):
            h = host_name(host)
            return (f"域名 {h} 不在允许列表（防止 DNS 重绑定攻击）。用 IP 访问，或在路由器上执行："
                    f"python3 /opt/mihomo-panel/server.py --allow-host {h}")
        if m not in ("GET", "HEAD") and p.startswith("/api/"):
            origin = self.headers.get("Origin")
            if origin and origin != "null" and urlparse(origin).netloc.lower() != host.lower():
                return "跨站请求已拒绝"
            if "X-Shunt" not in self.headers and "X-Token" not in self.headers:  # X-Token：兼容旧版脚本
                return "缺少 X-Shunt 请求头，跨站请求已拒绝（脚本调用接口请加上 -H 'X-Shunt: 1'）"
        if m in ("GET", "HEAD") and p.startswith("/api/") and p not in OPEN_GET:
            # GET 也要求请求头：机密接口（备份、配置、历史）之外，测速（下载 50 MB）、批量延迟 / 解锁 / DNS 泄露检测等
            # 都很耗资源，不能让局域网里任意网页用 <img src> 跨站触发。前端 api() 总会带 X-Shunt
            if "X-Shunt" not in self.headers and "X-Token" not in self.headers:
                return "缺少 X-Shunt 请求头，跨站请求已拒绝（脚本调用请加上 -H 'X-Shunt: 1'）"
        return ""

    def route(self, m):
        self._want_close, self._started, self._raw, self._cookie = False, False, b"", ""
        u = urlparse(self.path)
        p = u.path
        why = self.guard(m, p)
        if why:
            if self.pending_body():  # 被拒绝的请求不读它的 body，回应后直接关闭连接
                self._want_close = True
            return self.deny(why)
        if p.startswith("/api/") and p not in AUTH_OPEN and not auth_request_ok(self.headers):  # 设置了面板密码：要先登录
            if self.pending_body():
                self._want_close = True
            return self.send(401, {"message": "未登录", "auth": True})
        if not self.read_body():
            return
        if m == "GET" and (p in ("/", "/index.html") or p.lstrip("/") in STATIC_TYPES):
            return self.send_static("index.html" if p == "/" else p.lstrip("/"))
        if m == "GET" and p == "/manifest.webmanifest":
            return self.send(200, json.dumps(MANIFEST, ensure_ascii=False).encode(), "application/manifest+json")
        if m == "GET" and p == "/bg":  # 背景图：CSS url() 无法带登录头，只读、不含敏感信息
            try:
                with open(BG_FILE, "rb") as f:
                    raw = f.read()
            except OSError:
                return self.send(404, {"message": "未设置背景图"})
            self.send_response(200)
            self.send_header("Content-Type", bg_type(raw[:16]) or "application/octet-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")  # 前端用 ?v=修改时间 刷新
            self.end_headers()
            self.wfile.write(raw)
            return
        if not p.startswith("/api/"):
            return self.send(404, {"message": "not found"})
        if p == "/api/logstream" and m == "GET":  # EventSource 无法带自定义头，用 /api/logticket 换来的一次性票据
            qs = parse_qs(u.query)
            if not ticket_use(q1(qs, "ticket")):
                return self.send(401, {"message": "未登录"})
            return self.logstream(q1(qs, "level", default="info"))
        try:
            return self.api(m, p, u.query)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:
            log_err(f"{m} {p}", e, tb=True)  # 堆栈只写服务端日志
            if self._started:  # 响应头已发出一半：无法再回 500，关闭连接让客户端知道响应不完整
                self.close_connection = True
                return
            return self.send(500, {"message": "内部错误"})

    def logstream(self, level):
        if level not in ("debug", "info", "warning", "error"):
            level = "info"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self._want_close = True  # 事件流没有长度，结束即关闭连接
        self.end_headers()
        q, stop = deque(), {}
        ev = threading.Event()

        def sink(line):
            q.append(line)
            ev.set()

        cname = CORES.get(active_core(load()), "mihomo")

        def reader():
            try:
                core_stream(f"/logs?level={level}", sink, stop)
                sink(json.dumps({"type": "warning", "payload": "日志流已结束（%s 可能已重启），正在重连…" % cname}))
            except ConnectionRefusedError:  # 核心进程不在（重启中 / 已停止 / 崩溃）：控制端口没人监听
                sink(json.dumps({"type": "warning", "payload": "%s 内核未运行或正在重启，等待恢复…" % cname, "retry": 1}))
            except Exception as e:
                sink(json.dumps({"type": "error", "payload": "无法连接 %s 日志流：%s" % (cname, e)}))
            stop["ended"] = True
            ev.set()

        threading.Thread(target=reader, daemon=True).start()
        try:
            while True:
                ev.wait(15)
                ev.clear()
                if not q and not stop.get("ended"):
                    self.wfile.write(b": ping\\n\\n")  # 心跳，同时探测浏览器是否断开
                while q:
                    self.wfile.write(b"data: " + q.popleft().encode() + b"\\n\\n")
                self.wfile.flush()
                if stop.get("ended"):
                    break
        except Exception:
            pass
        finally:
            stop["stop"] = True
            try:
                stop["sock"].shutdown(socket.SHUT_RDWR)
            except (KeyError, OSError):
                pass

    def reply(self, ok, msg):
        return self.send(200 if ok else 500, {"message": msg})

    def api(self, m, p, q):
        b = self.body() if m in ("POST", "PUT", "PATCH", "DELETE") else {}  # 请求体已在 route() 里完整读入
        if not isinstance(b, dict):  # [1] / "x" / 123 这类合法 JSON：以前各接口 b.get 抛 AttributeError，回 500 并写一条错误堆栈
            return self.send(400, {"message": "请求体必须是 JSON 对象"})
        if p == "/api/core/configs/geo" and m != "POST":
            # 重新下载全部 GEO / 规则集并重载核心：两个内核都只认 POST（与 mihomo 一致）。以前 mihomo 下原样透传，
            # 方法由核心判断；sing-box 下单独拦。统一在这里回 405，不经过核心
            return self.send(405, {"message": "该接口只支持 POST"})
        # 透传到 mihomo external-controller
        if p.startswith("/api/core/") and active_core() == "singbox":
            sub = p[len("/api/core"):]
            if sub == "/providers/proxies" and m == "GET":
                return self.send(200, sb_fake_providers(load()))
            if sub == "/providers/rules" and m == "GET":
                return self.send(200, sb_fake_rule_providers(load()))
            if sub.startswith("/providers/proxies/") and m == "PUT":
                okl, bad = sb_update_subs([unquote(sub.rsplit("/", 1)[1])])
                if not bad:
                    refresh_regions()
                return self.send(204 if not bad else 502, b"" if not bad else {"message": "；".join(bad)})
            if sub.startswith("/providers/proxies/") and sub.endswith("/healthcheck"):
                # 只测这个订阅的节点，逐个测（全局限并发）：以前测整个「手动选择」组，sing-box 整组测速同时只测 10 个、
                # 共用超时，失效节点多时这个订阅里的可用节点常常没轮到测，延迟一直不刷新
                sname = unquote(sub[len("/providers/proxies/"):-len("/healthcheck")])
                nodes = sb_provider_nodes(load()).get(sname)
                if nodes is None:
                    return self.send(404, {"message": "订阅不存在"})
                res = probe_nodes(nodes, UI_BUDGET)
                if nodes and all(v is None for v in res.values()):
                    return self.send(502, {"message": "核心无应答，测速没有结果"})
                return self.send(204, b"")
            m_gd = re.match(r"^/group/([^/?]+)/delay$", sub) if m == "GET" else None
            if m_gd:
                # sing-box 整组测速同时只测 10 个、共用超时，只列测通的节点：没轮到测的可用节点缺席、延迟也不刷新。
                # 缺席的逐个补测后再按同样格式返回（「全部测速」、组测速、概览测速都走这里）；mihomo 不经过这里，行为不变
                code, raw = core("GET", sub + ("?" + q if q else ""), timeout=60)
                code, raw = fill_group(unquote(m_gd.group(1)), code, raw, UI_BUDGET)
                return self.send(code, raw or b"{}")
            # 与 mihomo 一致只认 POST /configs/geo：以前任何方法都执行，GET 不要求 X-Shunt 头，
            # 局域网里打开的恶意网页用一个 <img src=…/api/core/configs/geo> 就能让面板强制重下全部规则集并重载核心
            if (sub.startswith("/providers/rules/") and m == "PUT") or (sub == "/configs/geo" and m == "POST"):
                d = load()
                need = sb_rule_need(d)
                if sub.startswith("/providers/rules/"):
                    nm = unquote(sub.rsplit("/", 1)[1])
                    need = {k: v for k, v in need.items() if k == nm}
                bad = sb_sync_assets(d, force=True, need=need)
                ok, msg = reload_core()
                return self.send(204 if not bad and ok else 502, b"" if not bad and ok else {"message": "；".join(bad) or msg})
            if sub.startswith("/configs") and m == "PUT":
                return self.reply(*reload_core(force=True))
            if sub == "/upgrade":
                return self.send(400, {"message": "请在 设置 → 核心 → 内核更新 里更新 sing-box"})
        if p.startswith("/api/core/"):
            slow = p.endswith("/delay") or p.endswith("/healthcheck")  # 测速可能超过默认 10 秒
            code, raw = core(m, p[len("/api/core"):] + ("?" + q if q else ""), b if b else None, timeout=60 if slow else 10)
            return self.send(code, raw or b"{}")
        if p == "/api/coreupdate" and m == "GET" and (q1(parse_qs(q), "core") or active_core()) == "singbox":
            ch = q1(parse_qs(q), "channel", default="stable")
            if ch not in SB_CHANNELS:
                return self.send(400, {"message": "未知的内核版本：" + ch})
            info = {"current": sb_version() or "未安装", "arch": sb_arch() or os.uname().machine, "channel": ch,
                    "backup": sb_version(SB_BIN + ".bak"), "remote": "", "busy": CORE_UPD["busy"], "core": "singbox",
                    "fork": sb_is_fork()}
            try:
                info["remote"] = sb_remote_version(ch, load().get("gh_proxy") or "")
            except Exception as e:
                info["message"] = "获取最新版本失败：" + str(e)
                return self.send(502, info)
            info["latest"] = info["remote"] == sb_version()
            return self.send(200, info)
        if p == "/api/coreupdate" and m == "POST" and (b.get("core") or active_core()) == "singbox":
            if b.get("action") == "rollback":
                if not sb_version(SB_BIN + ".bak"):
                    return self.send(400, {"message": "没有可回滚的 sing-box 备份"})
                if not upd_begin("core"):
                    return self.send(409, {"message": upd_busy_msg()})
                try:
                    tmp = SB_BIN + ".swap"
                    shutil.copy2(SB_BIN, tmp)
                    os.replace(SB_BIN + ".bak", SB_BIN)
                    os.replace(tmp, SB_BIN + ".bak")
                    if active_core() == "singbox":  # 官方版 ↔ 魔改版回滚：负载均衡组要按回滚后的内核重新生成
                        write_active_config()
                    # 手动停止了核心时只换程序，不替用户把核心拉起来
                    manual = active_core() == "singbox" and WD["manual_stop"]
                    if active_core() == "singbox" and not manual:
                        svc_restart(timeout=90)
                finally:
                    CORE_UPD["busy"] = False
                return self.reply(True, f"已回滚到 sing-box {sb_version()}" + (f"（{MANUAL_NOTE}）" if manual else ""))
            ch = b.get("channel") or "stable"
            if ch not in SB_CHANNELS:
                return self.send(400, {"message": "未知的内核版本：" + str(ch)})
            ok, msg = core_job_start(sb_update_job, ch, load().get("gh_proxy") or "", bool(b.get("force")))
            return self.send(200 if ok else 409, {"message": msg})
        if p == "/api/delay":
            return self.send(200, delay_test(force=q1(parse_qs(q), "force") == "1"))
        if p == "/api/favicon":
            b, ct = favicon(q1(parse_qs(q), "u", default=""))
            if not b:
                return self.send(404, {"message": "无图标"})
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")  # 远端内容：即使被当作文档打开也不能执行脚本
            self.sec_headers()
            self.end_headers()
            return self.wfile.write(b)
        if p == "/api/unlock":
            return self.send(200, unlock_test())
        if p == "/api/logs" and m == "GET":  # 只读接口只认 GET（以前任何方法都返回日志）
            from .logclean import core_log_text
            return self.send(200, {"log": core_log_text(300)})
        fn = ROUTES.get((m, p))
        if fn:
            return fn(self, m, p, b, q)
        return self.send(404, {"message": "not found"})
'''
_SRC['shunt.cli'] = '''""""""
import ipaddress, os, re, signal, ssl, sys, threading, time

from .svcmigrate import svc_migrate
from .errlog import log_err
from . import adblock, stats
from .base import CERT, KEY, PANEL_DIR, PORT
from . import base
from .data import load, update
from .mihomo import dns_cfg
from .sb_common import active_core, write_core_file
from .sb_config import write_active_config
from .sysopt import opt_fix_ulimit, opt_migrate_confd, rfile, sh
from .latency import monitor_loop
from .stats import Stats
from .adblock import AdStats
from .live import live_loop
from .logclean import log_loop
from .schedule import sched_loop
from .devices import rdns_loop
from .migrate import snapshot_selections, startup_migrate
from .https import Server, gen_cert, https_ready
from .update_guard import ROLLBACK_NOTE
from .auth import auth_on, exposure_warning, set_password
from .webapi import H
def tp_env():
    """供 tproxy.sh eval：绕过列表、IPv6 开关、本机 IPv6 网段（值均经过正则过滤）"""
    d = load()
    items = [x.strip() for x in d["bypass"] if isinstance(x, str) and x.strip()]
    macs = list(dict.fromkeys(x.lower().replace("-", ":") for x in items if re.fullmatch(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", x)))
    ips, ip6s = [], []
    for x in items:
        try:
            if "%" in x:  # 带网卡的地址（fe80::1%eth0）nftables 不认，以前写进集合导致整份规则加载失败；Go 后端同样跳过
                raise ValueError
            n = ipaddress.ip_network(x, strict=False)
            (ips if n.version == 4 else ip6s).append(n)
        except ValueError:
            pass
    # nftables 的 interval 集合不接受重叠 / 重复的元素（192.168.1.0/24 和 192.168.1.5 同时在列表里会报 conflicting intervals，
    # 整份透明代理规则加载失败），先合并
    ips, ip6s = [str(n) for n in ipaddress.collapse_addresses(ips)], [str(n) for n in ipaddress.collapse_addresses(ip6s)]
    local6 = []
    _, out = sh("ip -o -6 addr show scope global 2>/dev/null")
    for m in re.finditer(r"inet6 ([0-9a-fA-F:]+/\\d+)", out):
        try:
            local6.append(ipaddress.ip_interface(m.group(1)).network)
        except ValueError:
            pass
    local6 = [str(n) for n in ipaddress.collapse_addresses(local6)]
    return (f"B_IP='{','.join(ips)}'\\nB_MAC='{','.join(macs)}'\\nB_IP6='{','.join(ip6s)}'\\n"
            f"LOCAL6='{','.join(local6)}'\\nIPV6={1 if d.get('ipv6') else 0}\\nTPROXY={1 if d.get('tproxy') else 0}\\n"
            f"MODE={d['proxy_mode']}\\nDNS_ALL={1 if dns_cfg(d)['block_bypass'] else 0}")


def write_prep(path=None):
    """把 tp_env() 写到 CORE_PREP（0600、先写临时文件再改名），供 init.d / tproxy.sh 直接读。返回是否写成"""
    path = path or base.CORE_PREP
    if not path:
        return False
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tp_env() + "\\n")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def core_prep():
    """启动 / 重启核心前在面板进程里做 init.d start_pre 原来要起一个 Python 做的事：修正文件句柄设置、写内核选择、
    按当前设置生成配置（与 --gen 同一个函数、同一份 data.json），再写 CORE_PREP。
    以前 start_pre 跑 --gen、start_post 的 tproxy.sh 跑 --tpenv，每次都要重新编译导入整个面板（路由器上各约 0.3～1 秒）。
    生成失败就不写 CORE_PREP：init.d 照旧跑 --gen（失败沿用上一次的配置），行为与以前一致。返回是否写成"""
    path = base.CORE_PREP
    if not path:
        return False
    try:
        os.remove(path)  # 先删：本次生成失败时不能让 init.d 误用上一次的
    except OSError:
        pass
    try:
        opt_fix_ulimit()
    except Exception as e:
        log_err("文件句柄修正", e)
    dd = load()
    write_core_file(active_core(dd))
    write_active_config(dd)
    return write_prep(path)


def default_tz():
    """设备没设时区（无 /etc/localtime 也无 TZ，Alpine 默认即 UTC）时按北京时间算「今天」：
    广告拦截 / 流量的今日统计在本地 0 点清零，定时任务的时刻也按北京时间。设备自己设了时区则以设备为准"""
    if os.environ.get("TZ") or os.path.exists("/etc/localtime"):
        return False
    os.environ["TZ"] = "CST-8"
    time.tzset()
    return True


def main():
    """命令行入口：带参数时执行一次性命令，否则启动面板"""
    default_tz()
    if "--bypass" in sys.argv:  # 兼容旧版 tproxy.sh
        env = dict(l.split("=", 1) for l in tp_env().splitlines())
        print(env["B_IP"].strip("'") + "|" + env["B_MAC"].strip("'"))
        sys.exit(0)
    if "--tpenv" in sys.argv:
        print(tp_env())
        sys.exit(0)
    if "--gen" in sys.argv:  # init.d/shunt-core 启动前调用：按当前内核生成配置
        try:
            opt_fix_ulimit()
        except Exception as e:
            print("文件句柄修正失败：", e, file=sys.stderr)
        dd = load()
        write_core_file(active_core(dd))
        print(write_active_config(dd))
        try:  # init.d 的 start_post 由 tproxy.sh 直接读，不必再起一个 Python 跑 --tpenv
            write_prep()
        except Exception as e:
            print("写透明代理参数失败：", e, file=sys.stderr)
        sys.exit(0)
    if "--allow-public" in sys.argv:  # 允许 / 禁止从外网（非局域网地址）访问面板
        on = (sys.argv[sys.argv.index("--allow-public") + 1:] or ["on"])[0] in ("on", "1", "yes", "true")
        update(lambda x: x.update(allow_public=on))
        print(("已允许外网访问面板" + ("" if auth_on() else "。面板还没有设置密码，强烈建议执行 --password <新密码> 设置"))
              if on else "已恢复为只允许局域网访问")
        sys.exit(0)
    if "--password" in sys.argv:  # 设置面板密码（默认没有密码）；--password off 关闭
        args = sys.argv[sys.argv.index("--password") + 1:]
        if not args:
            print("面板密码：" + ("已设置" if auth_on() else "未设置") + "（--password <新密码> 设置，--password off 关闭）")
            sys.exit(0)
        pw = "" if args[0] in ("off", "-", "") else args[0]
        try:
            set_password(pw)
        except ValueError as e:
            print(e)
            sys.exit(1)
        print("已设置面板密码，打开面板时需要登录" if pw else "已关闭面板密码")
        sys.exit(0)
    if "--allow-host" in sys.argv:  # 允许用这个域名访问面板（DDNS、反向代理等）；--allow-host -x 删除
        args = sys.argv[sys.argv.index("--allow-host") + 1:]
        if not args:
            print("当前允许的域名：", ", ".join(load().get("allow_hosts") or []) or "（无，IP 和 .lan/.local 等局域网域名始终允许）")
            sys.exit(0)
        h = args[0].strip().lower()

        def _ah(x):
            cur = [y for y in x.get("allow_hosts") or [] if y != h.lstrip("-")]
            x["allow_hosts"] = cur if h.startswith("-") else cur + [h]
        update(_ah)
        print(("已删除 " if h.startswith("-") else "已允许 ") + h.lstrip("-"))
        sys.exit(0)
    if "--snapshot" in sys.argv:  # install.sh 升级前调用：记下各组当前选择，新配置生效后由面板恢复
        print("saved" if snapshot_selections() is not None else "skip")
        sys.exit(0)
    if "--gen-cert" in sys.argv:
        ok, out = gen_cert()
        print(out)
        sys.exit(0 if ok else 1)
    d = load()
    try:  # v6.8.2 起没有登录会话，删掉旧版留下的会话文件
        os.remove(os.path.join(PANEL_DIR, "sessions.json"))
    except FileNotFoundError:
        pass
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))  # 让 finally 有机会保存统计
    STATS = stats.STATS = Stats()
    AD_STATS = adblock.AD_STATS = AdStats()
    for target, args in ((STATS.loop, ()), (monitor_loop, ()), (AD_STATS.loop, ()), (AD_STATS.dns_loop, ()), (live_loop, ("/traffic",)),
                         (live_loop, ("/memory",)), (log_loop, ()), (sched_loop, ()), (rdns_loop, ()), (startup_migrate, ())):
        threading.Thread(target=target, args=args, daemon=True).start()
    try:
        opt_migrate_confd()
        opt_fix_ulimit()
    except Exception as e:
        log_err("文件句柄修正", e)
    svc_migrate()  # v7.7.6 服务改名：旧设备在线更新后自动迁移到 shunt / shunt-core
    if os.path.isfile(ROLLBACK_NOTE):
        log_err("面板在线更新", rfile(ROLLBACK_NOTE))
        os.remove(ROLLBACK_NOTE)
    try:
        warn = exposure_warning()
        if warn:  # 允许外网 / 额外域名访问却没有密码：启动时记一条，面板顶部也会一直提示
            print("警告：" + warn, flush=True)
            log_err("面板安全", warn)
    except Exception as e:
        log_err("面板密码", e)
    srv = Server(("0.0.0.0", PORT), H)
    scheme = "http"
    if https_ready(d):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2  # 禁用 TLS 1.0/1.1
        ctx.load_cert_chain(CERT, KEY)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
        scheme = "https"
    print(f"Shunt listening on {scheme}://0.0.0.0:{PORT}", flush=True)
    try:
        srv.serve_forever()
    finally:
        STATS.flush()
        AD_STATS.flush()
'''


class _Importer(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """从 _SRC 加载 shunt 包；把源码登记到 linecache，异常堆栈能显示出错的那一行"""

    @staticmethod
    def path(name):
        return name.replace(".", "/") + ("/__init__.py" if name == "shunt" else ".py")

    def find_spec(self, name, path=None, target=None):
        if name in _SRC:
            return importlib.util.spec_from_loader(name, self, origin=self.path(name), is_package=name == "shunt")
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, mod):
        fn, src = self.path(mod.__name__), _SRC[mod.__name__]
        mod.__file__ = fn
        linecache.cache[fn] = (len(src), None, src.splitlines(True), fn)
        code = _CODES.get(mod.__name__)
        exec(code if code is not None else compile(src, fn, "exec"), mod.__dict__)


def _codes():
    """内嵌模块的字节码缓存（__pycache__/server-embedded.<解释器>.bin）。exec 字符串没有 .pyc，
    每次启动面板、--gen、命令行都要把约 50 万字节的源码重新编译一遍，路由器上要一两秒；
    缓存按解释器版本 + 源码校验和失效（server.py 换了就重新编译），读写失败照旧现编译"""
    tag = sys.implementation.cache_tag
    if not tag or os.environ.get("PANEL_NO_CODE_CACHE"):
        return {}
    crc = n = 0
    for k, v in _SRC.items():
        b = (k + "=" + v).encode("utf-8", "surrogatepass")
        crc, n = zlib.crc32(b, crc), n + len(b)
    key = f"{tag} {n} {crc}".encode()
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__pycache__", f"server-embedded.{tag}.bin")
    try:
        with open(path, "rb") as f:
            if f.readline().rstrip(b"\n") == key:
                codes = marshal.loads(zlib.decompress(f.read()))
                if isinstance(codes, dict) and set(codes) == set(_SRC):
                    return codes
    except Exception:
        pass
    codes = {n: compile(s, _Importer.path(n), "exec") for n, s in _SRC.items()}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(key + b"\n")
            f.write(zlib.compress(marshal.dumps(codes)))  # 压缩后约 400KB，解压只要几毫秒
        os.replace(tmp, path)
    except Exception:  # 只读目录等：不缓存
        try:
            os.remove(tmp)
        except Exception:
            pass
    return codes


_CODES = _codes()
sys.meta_path.insert(0, _Importer())
import shunt  # noqa: E402

shunt.load(os.path.abspath(__file__))
for _m in shunt._loaded:  # 兼容 `import server` / runpy 的脚本（selftest.sh、离线包构建）：所有名字在这里都能拿到
    globals().update((k, v) for k, v in vars(_m).items() if not k.startswith("__") and k != "_link")


class _Facade(types.ModuleType):
    """`server.X = ...`（测试打桩）同时改掉各模块里的同名变量"""

    def __setattr__(self, k, v):
        super().__setattr__(k, v)
        shunt.setvar(k, v)


if __name__ != "__main__" and isinstance(sys.modules.get(__name__), types.ModuleType):
    sys.modules[__name__].__class__ = _Facade

if __name__ == "__main__":
    shunt.cli.main()
