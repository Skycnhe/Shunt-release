# 更新记录与详细说明

## v11.6.8（同步 shunt v8.10.9） 发布地址改为公开仓库 Skycnhe/Shunt-release
- 一键安装、面板在线更新、离线安装包、文档站都改从公开仓库 [Skycnhe/Shunt-release](https://github.com/Skycnhe/Shunt-release)（分支 shunt-singbox）获取。升级到这一版后，以后的在线更新自动从新地址下载，不需要任何操作
- 新的一键安装：`wget -qO- https://raw.githubusercontent.com/Skycnhe/Shunt-release/shunt-singbox/install.sh | sh`（国内在前面加 `https://ghfast.top/`，后面加 `-s -- --cn`）
- 已经设置过「GitHub 加速地址」的照常生效；环境变量 PANEL_RAW 仍可覆盖更新来源

