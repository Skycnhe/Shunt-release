# 更新记录与详细说明

## v11.6.31 预览版功能转正：🛡️ Google 专用组、SOCKS5 落地、DNS 查询结果统计、解锁检测 Google 定位、性能精简与多项修复
- 性能精简（功能与生成配置不变，全部夹具生成的 mihomo / sing-box 配置逐字一致）：server.py 启动引导不再导入 importlib.abc（连带的 importlib.resources / pathlib / inspect / typing 等约 20 个模块不再加载），内嵌源码改为出错要显示堆栈时才拆行登记到 linecache，模块加载完即释放顶层字节码；tarfile（备份 / 恢复）、concurrent.futures（落地检测 / 节点补测）改为用到时才导入；补上 mihomo 版早已有的内嵌模块字节码缓存（`__pycache__/server-embedded.<解释器>.bin`），以前每次启动面板、`--gen`、命令行都要现编译约 50 万字节源码。实测面板常驻内存 约 30.7 MB → 19.3 MB；新增 tests/test_lean_boot.py
- 手机端（≤720px）：概览「平均解析耗时」标签在 390px 宽被挤成一列竖字，改为标签不换行、副标题换下一行并省略；设置 → 核心的内核版本表不再横向滑动，名称 / 版本列可换行，「回滚 / 更新 / 下载」按钮另起一行完整显示；桌面不变
- 审查修复：mihomo 配置生成器（Python 与 Go 两份，供迁移 / 对照用）在 mihomo 内核没编译 gVisor（`mihomo -v` 的 Use tags 里没有 with_gvisor）时，TUN 协议栈「默认 / gvisor / mixed」改用 system，与 sing-box 已有的回退一致；Go 后端 /api/state 的 tun_gvisor 和 TUN 失败时的协议栈建议改为按当前内核判断（以前不论内核都读 sing-box 的检测结果），与 Python 一致
- 概览「域名过滤」：桌面宽度下「平均解析耗时」标签被右侧副标题挤成一列竖字，改为标签不换行、副标题放不下时换到下一行并省略（悬停看全文）；手机端不变
- 设置 → 核心：内核版本表在桌面卡片较窄时「回滚 / 更新 / 下载」按钮被裁到可视区外，改为名称、版本列可换行，操作列始终完整显示；手机端不变
- 规则：去掉排在 geolocation-!cn 之后的多余「DOMAIN-SUFFIX,dangbei.com,DIRECT」（当贝本身在 geosite-cn 里，这条从不单独生效）
- 新增「🛡️ Google 专用」策略组：🔍 Google / 📹 YouTube 的第一个选项（升级后自动切过去一次，失败 10 分钟后重试，之后不改用户自己的选择），Gemini / AI Studio / NotebookLM 等网页及其后端接口（geminiweb-pa.clients6.google.com、alkalimakersuite-pa、notebooklm.googleapis.com 等）改走 🔍 Google。组内按节点名排除 中国 / CN / 中转 / 回国 / 国内 等（不误伤「美国 CN2 GIA」和中国香港 / 台湾 / 澳门节点），默认 🛡️ Google 日本 → 🛡️ Google 美国 → 🛡️ Google 自动，也可手动选具体节点
- 审查修复：sing-box 关闭嗅探（redir-host）时 TUN 收到的 53 端口 DNS 不被劫持、绕过面板 DNS（广告拦截 / 分流 DNS 失效），改按目标端口 53 劫持，与 mihomo 的 dns-hijack 一致
- 手动节点 / 订阅里的 WireGuard 节点以前直接跳过，现在转换成 endpoints
- 新增「SOCKS5 落地」（顶栏「测速」和「设置」之间的「落地」页，Python 后端）：想要指定国家的出口 IP 时添加 SOCKS5 落地，可批量粘贴 socks5://用户:密码@地址:端口#名称 或 地址:端口:用户:密码；每个落地可选前置节点（默认直连，也可选手动节点、地区竞技组），mihomo 用 dialer-proxy、sing-box 用 detour 链式代理。落地节点自动加入「🚀 节点选择」「🖐️ 手动选择」和各分流组（不进自动测速组），订阅更新不受影响；「检测」显示落地的出口 IP、国家和延迟。前置节点被删除时自动改为直连
- 概览底部改为两栏：左边「故障自动直连（看门狗）」，长日志自动换行，默认只列最近 5 条（可展开全部）；右边新增「DNS 查询结果」环形图，统计最近 100 条查询里缓存命中 / 上游应答 / 查无此名（及查询失败）的数量和占比（sing-box 内核，按 debug 日志统计）。手机竖屏两张卡片上下排列
- 解锁检测新增「Google 定位」：显示 Google 判定的地区（YouTube Premium 页的地区，被跳到 google.cn 也算中国），以及「🔍 Google」组当前实际用的节点，被判为中国时直接标红
- 预览版里的 WARP 标签页（Cloudflare WARP 注册 / 导入 / 本地优选 IP）暂不进正式版，仍只在预览通道提供

