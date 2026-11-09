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

## Docker 安装

Linux 宿主机，host 网络（镜像只含 sing-box 内核，与 shunt 分支同一仓库，标签 `singbox`）：

```sh
docker run -d --name shunt --restart unless-stopped --network host \
  --cap-add NET_ADMIN --cap-add NET_RAW --device /dev/net/tun \
  -v shunt-data:/data -e TZ=Asia/Shanghai --stop-timeout 30 \
  skycn2022/shunt:singbox
```

也可用 `ghcr.io/skycnhe/shunt-release:singbox`；固定版本用 `singbox-v<面板版本>`。宿主机需 `modprobe nft_tproxy tun` 并开启 `net.ipv4.ip_forward=1`；老内核（群晖等）TProxy 不可用时面板改用 TUN。
