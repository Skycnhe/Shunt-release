# 更新记录与详细说明

## v11.7.8 预览（Go 后端）：面板改为单个程序，设备上不再需要 python3
- 后端从 Python 改为 Go 单个程序 /opt/mihomo-panel/shunt（静态编译，amd64 / arm64 / armv7 / 386），页面和接口不变，功能与 v11.7.7 Python 版一致（同一批夹具对比生成的 sing-box 配置逐字节一致）
- 补齐 Go 版：节点探测、Google 专用组（gguard）、SOCKS5 落地、Cloudflare WARP、DNS 查询统计等预览功能，以及在线更新程序本身
- 安装：install.sh 不再安装 python3 / py3-yaml，按发布仓库的 bin/SHA256SUMS 下载本机架构的程序（gz 与解压后两次 sha256 校验，--cn 时 ghfast.top 失败自动换 gh-proxy.com）；--https、TUN 检测、升级前记录选择都改由程序完成
- 在线更新：程序随页面一起按 sha256 校验后替换，失败自动回滚。旧的 Python 版在线更新到本版时，先下载引导用的 server.py，重启时由它下载并校验程序后交给程序运行（只用这一次 python3）
- selftest.sh 改为调用 shunt --selftest：33 个场景用真实 sing-box 校验，结果与 Python 版自测相同
- 离线包 / Docker 镜像：不再带 python3；规则集预下载改用 shunt --sync-rules，镜像启动时后台补下载缺少的规则集；局域网网段改由 ip route 读取
- 备份不再打包 server.py（程序不在备份里，跨版本恢复只恢复 data.json，与之前相同）；未设密码的提示改为 shunt --password
- 实测（arm64 沙箱，8 个节点）：启动到面板可访问约 0.05 秒（Python 版约 0.28 秒），常驻内存约 14 MB（Python 版约 23 MB）

