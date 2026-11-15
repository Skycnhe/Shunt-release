# 更新记录与详细说明

## v11.6.32 修复：DNS 填裸 IPv6 时 mihomo 内核起不来
- 修复：设置里 DNS（直连 / 代理 / 默认 / 节点域名解析 / nameserver-policy）填不带方括号的 IPv6 地址（如 2400:3200::1、udp://2400:3200::1）时，切到 mihomo 内核生成的配置 mihomo -t 报「too many colons」拒绝启动；现在自动补方括号（Python 与 Go 后端一致，sing-box 配置不受影响）
- 落地：新落地的默认前置只在旧数据里的 WARP 节点仍存在时才用它，否则直连（以前 data.json 残留 warp 键而节点已删时会默认指向不存在的节点）

