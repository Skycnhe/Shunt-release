# 更新记录与详细说明

## v11.6.28 新增 Docker 镜像（只含 sing-box 内核）
- Docker：镜像 `skycn2022/shunt:singbox`（Docker Hub，与 shunt 分支同一仓库）/ `ghcr.io/skycnhe/shunt-release:singbox`，另有固定版本标签 `singbox-v<面板版本>`；`latest` / `hk001` / `v<版本>` 仍是 shunt 分支的。支持 linux/amd64、linux/arm64，内置面板、sing-box（官方 musl 版）和默认 .srs 规则集，构建时用 selftest.sh 校验全部场景配置
- 发布：公开仓库 shunt-singbox 分支推送且面板版本或 Docker 相关文件变化时自动构建，amd64 / arm64 原生 runner 上分别用 host 网络实际跑一遍（面板可访问、核心运行、nftables 规则挂上、面板重启核心、docker stop 后规则撤干净、再次启动）后才发布
- 用法见 README「Docker 安装」；入口脚本、数据卷（/data）、PANEL_PORT、宿主机防火墙处理与 shunt 分支镜像一致

