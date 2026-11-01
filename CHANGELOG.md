# 更新记录与详细说明

## v8.10.10 DNS 与分流规则修复
- sing-box 下 `DOMAIN-WILDCARD` 的 `*` 与 mihomo 一致可以跨多级：`*.google.com` 现在也命中 `a.b.google.com`（以前只匹配一级子域）；`?` 仍匹配一个字符
- sing-box 下引用 ipcidr / classical 规则集的规则（自定义 `RULE-SET,xxx,策略` 和规则集订阅，含逻辑规则里的子规则）没写 no-resolve 时先解析域名再匹配，规则集里的 IP 条目对域名访问也能命中（以前只有 IP-CIDR / GEOIP 会解析）
- 支持规则的 `src` 参数：`IP-CIDR,192.168.1.0/24,策略,src`、`GEOIP,…,src`、`RULE-SET,…,src` 在 sing-box 下按来源地址（设备）匹配，以前被当成目标地址，按设备分流的规则不生效
- 策略名带括号（如节点「HK(IPLC)」）的逻辑规则在 sing-box 下不再被当成「不支持」跳过
- 规则引用的节点 / 策略组已删除、且名字以「)」结尾（如「Gone(X)」）时，同样自动改用节点选择；以前原样写进配置，mihomo 整份配置加载失败。逻辑规则没写策略的也补上节点选择
- 逻辑规则（AND / OR / NOT）末尾多写的 `,no-resolve` 自动去掉：mihomo 会把它当成策略名导致配置加载失败（no-resolve 要写在子规则里）
- sing-box 支持「respect-rules」：开启时没写 #策略组 的代理 DNS（及国外的 DNS 策略上游）经节点选择发出，以前直接从 WAN 明文发出（可能泄露 / 被干扰），防泄露检测却显示正常；国内 / 内网 DNS 照旧直连，节点域名解析不受影响
- sing-box fake-ip 模式下，自定义 DNS 策略和广告白名单里的域名也返回 fake-ip（与 mihomo 一致，只有 Fake-IP 过滤名单和 hosts 拿真实 IP）；以前直接返回真实 IP，不能嗅探的连接按域名分流失效、白名单里的国外域名还可能拿到被污染的结果
- sing-box 转换 classical 规则集：逻辑规则（AND / OR / NOT）、`GEOIP,private` 现在会转换；GEOSITE / GEOIP 等 sing-box 规则集文件里不能引用的条目跳过后在「兼容提示」里列出（以前静默丢掉）
- `DOMAIN-REGEX` 等正则规则的值里带逗号（如 `^a{1,3}\.com$`）时不再被截断（以前 sing-box 下变成 `^a{1`，永远不命中）；sing-box 支持 `PROCESS-PATH-REGEX`

