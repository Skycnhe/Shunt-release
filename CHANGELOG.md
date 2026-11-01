# 更新记录与详细说明

## v11.6.9（同步 shunt v8.10.10 / v8.10.11） DNS / 分流 / 规则 / 直连修复
- 同步 v8.10.10：respect-rules、fake-ip 下 DNS 策略与白名单返回 fake-ip、DOMAIN-WILDCARD 跨多级、RULE-SET 引用 ipcidr / classical 规则集先解析、`src` 按来源地址匹配、括号策略名、已删除的 `Gone(X)` 策略改用节点选择、正则值含逗号、PROCESS-PATH-REGEX、classical 规则集逻辑规则转换与兼容提示
- DNS：CGNAT（100.64.0.0/10）DNS 不再经节点发出；裸 IPv6 DNS 地址不再让配置生成失败（Go 端以前生成非法地址）
- DNS / 直连：IPv6 Fake-IP 段（fc00::/18、fdfe:dcba:9876::/64）不再被保留地址规则提前放行，AAAA 假地址的连接恢复正常
- 分流：自定义 `MATCH` 真正兜底并停止后面的规则；订阅节点字段类型异常时跳过该节点而不是整份配置失败；Go 端多订阅时 outbounds 顺序稳定、与 Python 一致
- 规则：逻辑规则里的 `GEOSITE,xxx` 会触发规则集下载；Go 端逻辑规则中某个子条件永不命中时按 AND/OR/NOT 化简而不是整条丢掉；规则测试修复日志连接泄漏、`80-x` 端口 500、`GEOIP,private` 测不出命中；删除规则 / 规则集改按内容而非序号
- 规则：classical 规则集里 Go 不支持的正则跳过并提示，以前内核起不来
- 直连：补上核心被 KILL 后的 TUN / auto-redirect 残留清理（stop leftovers）、停止前先撤透明代理规则、启动时等核心端口就绪再挂规则（以前核心起不来也挂规则导致断网）
- 直连：看门狗直连期间保存设置不再把规则挂回卡死的核心，核心恢复后自动恢复透明代理；全局代理模式下局域网 / NTP / TUN 绕过设备仍直连；绕过地址拒收带网卡名的 IPv6

