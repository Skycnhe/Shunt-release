# 更新记录与详细说明

## v11.7.1 概览负载均衡小拓扑、sing-box 测速修复
- 概览：新增「🕸️ 当前负载均衡」小拓扑（🚀 节点选择当前走的轮询 / 哈希组 → 成员节点，连线粗细 = 连接数，显示每个节点连接数与下载速度，最多 8 个，其余点进拓扑页；只在概览可见时每 2 秒刷新）；IP 信息加「出口节点」；版面压紧：顶部四个小卡数值与说明同一行、实时流量图变矮、看门狗默认显示 3 条
- 修复：测速间隔设到 30 分钟以上时 sing-box 启动即退出（「interval must be less or equal than idle_timeout」，sing-box check 查不出）；竞技 / 哈希 / 全局自动 / 自定义自动测速组现在自动把 idle_timeout 放大到与间隔相同（官方、reF1nd、MiChongs 都实测能启动）
- 修复：MiChongs 内核开启 LightGBM 后启动约 1 秒即 FATAL 退出（下载 GeoLite2 国家 / ASN 库走了 1.14 起废弃的隐式默认 HTTP 客户端），看门狗会反复重启；现在显式经 DIRECT 下载，填了「GitHub 加速」时走加速
- 修复：sing-box 节点延迟比 mihomo 高 2–4 倍的问题。原因是 sing-box 默认用新连接测速，TCP、代理握手、TLS 全算进延迟；mihomo 开了 unified-delay，只计一次往返。现在内核支持时（reF1nd / MiChongs 魔改版）自动开启 urltest_unified_delay，测速数值与 mihomo 一致，竞技组也按真实延迟挑节点。官方内核不支持，保持原样。网速本身沙箱同节点实测两内核持平，无可靠差距
- 界面：「哈希」组的策略说明加提示：按主域名固定节点，google.com / gstatic.com、x.com / twimg.com 这类不同主域名可能分到不同节点，登录敏感的服务建议用竞技组

