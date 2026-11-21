# 更新记录与详细说明

## v11.7.8 预览（Go 后端）：面板改为单个程序，设备上不再需要 python3
- 后端从 Python 改为 Go 单个程序 /opt/mihomo-panel/shunt（静态编译，amd64 / arm64 / armv7 / 386），页面和接口不变，功能与 v11.7.7 Python 版一致（同一批夹具对比生成的 sing-box 配置逐字节一致）
- 补齐 Go 版：节点探测、Google 专用组（gguard）、SOCKS5 落地、Cloudflare WARP、DNS 查询统计等预览功能，以及在线更新程序本身
- 安装：install.sh 不再安装 python3 / py3-yaml，按发布仓库的 bin/SHA256SUMS 下载本机架构的程序（gz 与解压后两次 sha256 校验，--cn 时 ghfast.top 失败自动换 gh-proxy.com）；--https、TUN 检测、升级前记录选择都改由程序完成
- 在线更新：程序随页面一起按 sha256 校验后替换，失败自动回滚。旧的 Python 版在线更新到本版时，先下载引导用的 server.py，重启时由它下载并校验程序后交给程序运行（只用这一次 python3）
- 修复：旧的 Python 版在线更新到本版后可能被自动回滚（国内常见）。引导程序以前每条线路超时 120 秒、先试 raw.githubusercontent.com 直连：raw 被 DNS 污染 / 丢包、核心代理 7890 不通时，直连一条就卡满 120 秒，超过旧版回滚守护的约 90 秒，来不及换镜像就被恢复成旧版。现在按「GitHub 加速 → 本机代理 7890 → 内置镜像 → 直连」顺序每隔 3 秒启动下一条并行下载（前一条失败立即换下一条），单次连接 / 读取 10 秒没进展就放弃，整个下载最多 60 秒；镜像下载的内容按 sha256 校验
- 引导程序：缺少 bin/SHA256SUMS（旧版拿不到 GitHub 校验值时只按内置列表更新）时先从发布分支下载；日志改写 stderr（tproxy.sh 会 eval「server.py --tpenv」的输出）；临时文件带进程号，init.d 与 tproxy.sh 同时运行时互不覆盖
- 修复：旧的 Python 版在线更新到本版后，OpenRC 停不掉面板（rc-status 显示 shunt crashed，之后在线更新 / 重启面板都不生效，直到重启设备）。引导程序以前作为服务进程运行、装好程序后原地变成 shunt，OpenRC 按记下的 python3 找不到它；现在服务进程始终是 shunt，引导程序只在启动前下载程序；已经处于这种状态的设备，更新到本版后第一次重启面板即恢复
- 修复：面板「回滚」在 Go 版上失败（覆盖正在运行的程序报 Text file busy，回滚到一半）
- 发布：程序分支（<分支>-bin）不再强推，新提交以上一次发布为父提交，设备手里旧 bin/SHA256SUMS 指向的程序在发布过程中也始终能下载
- selftest.sh 改为调用 shunt --selftest：33 个场景用真实 sing-box 校验，结果与 Python 版自测相同
- 离线包 / Docker 镜像：不再带 python3；规则集预下载改用 shunt --sync-rules，镜像启动时后台补下载缺少的规则集；局域网网段改由 ip route 读取
- 备份不再打包 server.py（程序不在备份里，跨版本恢复只恢复 data.json，与之前相同）；未设密码的提示改为 shunt --password
- 实测（arm64 沙箱，8 个节点）：启动到面板可访问约 0.05 秒（Python 版约 0.28 秒），常驻内存约 14 MB（Python 版约 23 MB）

