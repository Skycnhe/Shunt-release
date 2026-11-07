# 更新记录与详细说明

## v8.10.27 修复装了旁路由后局域网共享（SMB / NAS）、打印机等按主机名打不开
- DNS：局域网主机名（`.lan`、`.local`、`.localdomain`、`.home`、`.home.arpa`、`.internal`、`.intranet`、`.router`）以前按 geosite:private 交给公共 DoH（doh.pub 等）解析，公共 DNS 根本不认识这些名字，一律解析失败；Fake-IP 过滤又只有「*.lan」一层，`nas.home.arpa`、`a.b.lan` 这类名字拿到 198.18.x 假地址被送进核心，`\\NAS`、`http://nas.lan:5000`、打印机都打不开。现在这些后缀始终不走 Fake-IP（不需要用户改过滤名单），并交给主路由（本机默认网关，只认内网 IPv4）的 DNS 解析；内网 IP 的反向解析（10.x / 172.16–31.x / 192.168.x / 169.254.x 的 PTR）也交给主路由，资源管理器、NAS 客户端显示主机名不再卡顿。mihomo / mihomo Smart / sing-box 一致
- 用户自定义的 DNS 策略里已有这些后缀时以用户的为准；data.json 的 `dns.lan` 可指定其他内网 DNS，写 `off` 关闭
- 透明代理本身（TProxy）对内网目标一直是直连放行（10/8、172.16/12、192.168/16、169.254/16、组播、广播），按 IP 访问不受影响，本次未改

