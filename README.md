<p align="center"><img src="icon.svg" width="96" alt="Shunt"></p>

<h1 align="center">Shunt 分流 · shunt-singbox</h1>

## 截图

![概览](docs/overview.jpg)

| 节点 | 策略组 |
| --- | --- |
| ![节点](docs/nodes.jpg) | ![策略组](docs/groups.jpg) |
| **主题** | **手机** |
| ![主题](docs/themes.jpg) | <img src="docs/mobile.jpg" width="320" alt="手机"> |

## 一键安装

在 Alpine Linux 上用 root 执行，装完打开 `http://旁路由IP:8080`：

```sh
# 国内网络
wget -qO- https://ghfast.top/https://raw.githubusercontent.com/Skycnhe/Shunt-release/shunt-singbox/install.sh | sh -s -- --cn

# 国外网络
wget -qO- https://raw.githubusercontent.com/Skycnhe/Shunt-release/shunt-singbox/install.sh | sh
```
