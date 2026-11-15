# 更新记录与详细说明

## v11.6.36 新增：MiChongs 内核下地区竞技组改用 Smart
- 新增：内核换成 MiChongs 魔改版（支持 smart 出站组）时，地区「竞技」组自动由自动测速（urltest）改为智能竞技（smart）：按连接质量为每个服务挑节点、失败自动避开；健康检查地址 / 间隔 / 容差沿用分组设置，仍可在节点页点选手动指定（📌 取消固定恢复自动）；「哈希」负载均衡组不变。换回官方 / reF1nd 等不支持 smart 的内核或回滚时自动回退 urltest，配置自动重新生成（用 sing-box check 探测内核能力，按程序文件缓存）
- 新增：「策略组 → 地区分组」里的 LightGBM 模型开关（默认关闭，用内置场景公式）；开启后内核经「GitHub 加速」自动下载 Model.bin 到 /etc/sing-box，每 72 小时检查更新。学习数据存在 cache_file（cache.db），重启不丢
- 修复：没设「GitHub 加速」时，内核没起来（代理口拒绝连接）且 raw.githubusercontent.com 被 DNS 污染（直连被 SSRF 防护拦截）时面板无法在线更新；现在有 GitHub 校验值时最后自动改走内置镜像 ghfast.top / gh-proxy.com，下载后逐个比对校验值
- Python 与 Go 后端一致；已用真实 MiChongs 内核 1.15.0-alpha.13-xiaobaf14g.11 跑 sing-box check 与 Clash API（类型 Smart、手动指定 / 取消、组测速）验证

