#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Shunt 分流 —— sing-box 旁路由面板一键安装 (Alpine Linux)
# Go 版：面板是单个程序 /opt/mihomo-panel/shunt（按本机架构下载、sha256 校验），不需要 python3
# 用法: sh install.sh [--cn] [--update-core] [--https] [--selftest]
# 离线安装包（面板 + sing-box 内核 + 依赖，GitHub Releases「offline-HK003」）:
#   一键: wget -qO- https://raw.githubusercontent.com/Skycnhe/Shunt-release/shunt-singbox/offline.sh | sh
#   本地: tar xzf shunt-offline-aarch64-alpine3.24.tar.gz && sh shunt-offline/install.sh
# 一键安装（无需先下载仓库）:
#   国外: wget -qO- https://raw.githubusercontent.com/Skycnhe/Shunt-release/shunt-singbox/install.sh | sh
#   国内: wget -qO- https://ghfast.top/https://raw.githubusercontent.com/Skycnhe/Shunt-release/shunt-singbox/install.sh | sh -s -- --cn
#   --cn           国内网络：apk 换国内镜像，GitHub 下载走加速代理（默认 https://ghfast.top/，可用 GH_PROXY 覆盖，
#                  如 GH_PROXY=https://gh-proxy.com/；首选失败时自动换内置的另一个镜像，下载的程序都按 sha256 校验）
#   --update-core  重新下载最新 sing-box 核心
#   --https        生成自签名证书并让面板使用 https://（也可之后在“设置”里开关）
#   --selftest     安装后用真实核心校验多种配置场景（selftest.sh）
set -e
[ "$(id -u)" = 0 ] || { echo "请用 root 运行"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"
GH="${GH_PROXY:-}"   # 国内可设置 GH_PROXY=https://ghfast.top/ 加速
REPO="${PANEL_REPO:-Skycnhe/Shunt-release}"; BRANCH="${PANEL_BRANCH:-shunt-singbox}"
CN=""; UPDATE_CORE=""; HTTPS=""; SELFTEST=""
for a in "$@"; do
  case "$a" in
    --cn) CN=1 ;;
    --update-core) UPDATE_CORE=1 ;;
    --https) HTTPS=1 ;;
    --selftest) SELFTEST=1 ;;
    *) echo "未知参数 $a"; exit 1 ;;
  esac
done

[ -f /etc/alpine-release ] || { echo "本脚本仅支持 Alpine Linux"; exit 1; }
REL=$(cut -d. -f1,2 /etc/alpine-release)
# 离线安装包：同目录有 offline/（内核、geo 数据、apk 依赖），全程不需要联网
OFF=""; [ -f "$SRC/index.html" ] && [ -f "$SRC/offline/VERSION" ] && OFF="$SRC/offline"
PKGS="curl wget ca-certificates nftables iptables ip6tables iproute2 gzip tar tzdata kmod coreutils"
[ -n "$HTTPS" ] && PKGS="$PKGS openssl"
ONLINE_DEPS=1
if [ -n "$OFF" ]; then
  echo ">> 离线安装包：$(tr '\n' ' ' < "$OFF/VERSION")"
  BREL=$(sed -n 's/^alpine=//p' "$OFF/VERSION"); BARCH=$(sed -n 's/^arch=//p' "$OFF/VERSION")
  [ "$BARCH" = "$(uname -m)" ] || { echo "!! 安装包是 $BARCH 的，本机是 $(uname -m)，请下载对应架构的包"; exit 1; }
  [ "$BREL" = "$REL" ] || echo "!! 安装包按 Alpine $BREL 打包，本机是 $REL：包内依赖装不上时会改为联网安装"
  echo ">> 安装依赖（包内离线源）"
  if apk add --no-network --allow-untrusted --repositories-file /dev/null --repository "$OFF/apk" $PKGS openssl >/tmp/shunt-apk.log 2>&1; then
    ONLINE_DEPS=""
  else
    echo "!! 包内依赖安装失败（详情 /tmp/shunt-apk.log），改为联网安装"
  fi
fi
if [ -n "$ONLINE_DEPS" ]; then
if [ -n "$CN" ]; then
  GH="${GH:-https://ghfast.top/}"
  MIRROR="${APK_MIRROR:-mirrors.ustc.edu.cn}"
  echo ">> 国内模式：apk 镜像 $MIRROR，GitHub 加速 $GH"
  sed -i -E "s#https?://[^/]+/alpine#https://$MIRROR/alpine#g" /etc/apk/repositories
fi

echo ">> 补齐软件源（main + community）"
BASE=$(sed -n -E 's#^(https?://[^ ]*/alpine)/.*#\1#p' /etc/apk/repositories | head -n1)
BASE="${BASE:-https://dl-cdn.alpinelinux.org/alpine}"
sed -i -E "s@^#[[:space:]]*(.*/v$REL/community)@\1@" /etc/apk/repositories
for r in main community; do
  grep -Eq "^[^#].*/(v$REL|edge|latest-stable)/$r/?[[:space:]]*$" /etc/apk/repositories || echo "$BASE/v$REL/$r" >> /etc/apk/repositories
done

echo ">> 安装依赖"
apk update >/dev/null || { echo "!! apk update 失败，请检查网络/软件源（国内请加 --cn）"; exit 1; }
for p in $PKGS; do apk add --no-cache "$p" >/dev/null 2>&1 || echo "!! 依赖 $p 安装失败"; done
fi
for c in curl nft ip gunzip sha256sum; do command -v $c >/dev/null || { echo "!! 缺少 $c，安装中止"; exit 1; }; done
update-ca-certificates >/dev/null 2>&1 || true

case "$(uname -m)" in
  x86_64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  armv7l|armv8l) ARCH=armv7 ;;
  i686|i386) ARCH=386 ;;
  *) echo "不支持的架构 $(uname -m)"; exit 1 ;;
esac
[ "$ARCH" = 386 ] && SBARCH=386 || SBARCH=$ARCH

# 下载：先走 GH_PROXY（--cn 默认 ghfast.top），失败再换内置镜像、最后直连。dl 网址 输出文件
MIRRORS="https://ghfast.top/ https://gh-proxy.com/"
dl() {
  for p in "$GH" $( [ -n "$CN$GH" ] && echo $MIRRORS ) ""; do
    case " $TRIED " in *" [$p] "*) continue ;; esac; TRIED="$TRIED [$p]"
    curl -fsSL --retry 2 --connect-timeout 15 -o "$2" "$p$1" 2>/dev/null && { TRIED=""; return 0; }
  done
  TRIED=""; return 1
}
TRIED=""
RAWB="https://raw.githubusercontent.com/$REPO"

# Go 版面板程序：本地没有 shunt 时按 bin/SHA256SUMS 下载本机架构的程序（gz 与解压后的程序两次 sha256 校验）
if ! { [ -f "$SRC/shunt" ] && [ -x "$SRC/shunt" ]; }; then
  [ -f "$SRC/index.html" ] || { SRC=$(mktemp -d); }
  mkdir -p "$SRC/bin"
  REF=""; SUMS_OK=""
  if [ ! -f "$SRC/bin/SHA256SUMS" ]; then
    # 能连上 GitHub API 时锁定到具体提交，SHA256SUMS 直接从 api.github.com 取（不经镜像，可信）；连不上时只能经镜像取
    REF=$(curl -fsSL --connect-timeout 10 "https://api.github.com/repos/$REPO/commits/$BRANCH" 2>/dev/null | sed -n 's/^  "sha": "\([0-9a-f]\{40\}\)".*/\1/p' | head -n 1)
    if [ -n "$REF" ] && curl -fsSL --connect-timeout 10 -H "Accept: application/vnd.github.raw" \
        "https://api.github.com/repos/$REPO/contents/bin/SHA256SUMS?ref=$REF" -o "$SRC/bin/SHA256SUMS" 2>/dev/null && grep -q '^version ' "$SRC/bin/SHA256SUMS"; then
      SUMS_OK=1
    else
      REF="$BRANCH"
      dl "$RAWB/$BRANCH/bin/SHA256SUMS" "$SRC/bin/SHA256SUMS" || { echo "!! 下载 bin/SHA256SUMS 失败（国内请加 --cn 或设置 GH_PROXY）"; exit 1; }
      echo "!! 无法访问 GitHub API，程序校验清单未经 GitHub 校验"
    fi
  fi
  BCOMMIT=$(sed -n 's/^commit[[:space:]]\{1,\}\([0-9a-f]\{40\}\).*/\1/p' "$SRC/bin/SHA256SUMS" | head -n 1)
  BVER=$(sed -n 's/^version[[:space:]]\{1,\}//p' "$SRC/bin/SHA256SUMS" | head -n 1)
  WGZ=$(awk -v f="shunt-linux-$ARCH.gz" '$2==f{print $1}' "$SRC/bin/SHA256SUMS")
  WBIN=$(awk -v f="shunt-linux-$ARCH" '$2==f{print $1}' "$SRC/bin/SHA256SUMS")
  [ -n "$BCOMMIT" ] && [ -n "$WGZ" ] && [ -n "$WBIN" ] || { echo "!! 发布里没有 $ARCH 架构的面板程序"; exit 1; }
  echo ">> 下载面板程序 $BVER（linux/$ARCH）"
  dl "$RAWB/$BCOMMIT/shunt-linux-$ARCH.gz" "$SRC/shunt.gz" || { echo "!! 下载面板程序失败（国内请加 --cn 或设置 GH_PROXY）"; exit 1; }
  [ "$(sha256sum "$SRC/shunt.gz" | cut -d' ' -f1)" = "$WGZ" ] || { echo "!! 面板程序压缩包校验失败（可能被篡改或下载不完整），安装中止"; exit 1; }
  gunzip -c "$SRC/shunt.gz" > "$SRC/shunt.new" && rm -f "$SRC/shunt.gz"
  [ "$(sha256sum "$SRC/shunt.new" | cut -d' ' -f1)" = "$WBIN" ] || { echo "!! 面板程序校验失败，安装中止"; exit 1; }
  chmod 755 "$SRC/shunt.new" && mv "$SRC/shunt.new" "$SRC/shunt"
  "$SRC/shunt" --version >/dev/null || { echo "!! 面板程序无法在本机运行"; exit 1; }
fi
# 其余面板文件（index.html、tproxy.sh、init.d 等，清单见发布分支的 update-files.txt）：本地没有时由程序下载，能连上 GitHub API 时逐个校验
if [ ! -f "$SRC/index.html" ]; then
  mkdir -p "$SRC/.fetch"
  echo ">> 下载面板文件 ($REPO@${REF:-$BRANCH})"
  PANEL_RAW="$RAWB/$BRANCH" GH_PROXY="$GH" PANEL_DATA="$SRC/.fetch/data.json" "$SRC/shunt" --fetch-panel "$SRC" || { echo "!! 下载面板文件失败（国内请加 --cn 或设置 GH_PROXY）"; exit 1; }
fi

mkdir -p /etc/mihomo-panel /opt/mihomo-panel /etc/sing-box/rules
if [ -n "$OFF" ]; then
  # 内核：没装过或带 --update-core 时用包里的（旧的留 .bak，面板里可回滚）
  if [ -f "$OFF/bin/sing-box" ] && { [ ! -x /usr/local/bin/sing-box ] || [ -n "$UPDATE_CORE" ]; }; then
    [ -x /usr/local/bin/sing-box ] && ! cmp -s "$OFF/bin/sing-box" /usr/local/bin/sing-box && cp /usr/local/bin/sing-box /usr/local/bin/sing-box.bak
    cp "$OFF/bin/sing-box" /usr/local/bin/sing-box.new && chmod 755 /usr/local/bin/sing-box.new && mv /usr/local/bin/sing-box.new /usr/local/bin/sing-box
    echo ">> 安装内核 sing-box"
  fi
  for f in "$OFF"/sb-rules/*; do [ -f "$f" ] && { [ -s "/etc/sing-box/rules/${f##*/}" ] || cp "$f" /etc/sing-box/rules/; }; done
elif [ ! -x /usr/local/bin/sing-box ] || [ -n "$UPDATE_CORE" ]; then
  ARCH=$SBARCH
  echo ">> 获取 sing-box 最新版本"
  # API 受限或走加速代理时从 releases/latest 的跳转地址里取版本号
  VER=$(curl -fsSL --retry 3 https://api.github.com/repos/SagerNet/sing-box/releases/latest | sed -n 's/.*"tag_name": *"v\{0,1\}\([^"]*\)".*/\1/p' | head -n1)
  [ -n "$VER" ] || VER=$(curl -fsSLI --retry 3 -o /dev/null -w '%{url_effective}' "${GH}https://github.com/SagerNet/sing-box/releases/latest" | sed -n 's#.*/tag/v\{0,1\}##p')
  [ -n "$VER" ] || { echo "获取版本失败，可设置 GH_PROXY 后重试"; exit 1; }
  echo ">> 下载 sing-box $VER ($ARCH)"
  # 只有完整解压成功才替换，不留半截二进制
  TMPD=$(mktemp -d)
  if curl -fL --retry 3 "${GH}https://github.com/SagerNet/sing-box/releases/download/v$VER/sing-box-$VER-linux-$ARCH-musl.tar.gz" | tar xz -C "$TMPD" \
      && [ -s "$TMPD/sing-box-$VER-linux-$ARCH-musl/sing-box" ]; then
    [ -x /usr/local/bin/sing-box ] && cp /usr/local/bin/sing-box /usr/local/bin/sing-box.bak
    install -m 755 "$TMPD/sing-box-$VER-linux-$ARCH-musl/sing-box" /usr/local/bin/sing-box.new && mv /usr/local/bin/sing-box.new /usr/local/bin/sing-box
    rm -rf "$TMPD"
  else
    rm -rf "$TMPD"; echo "!! 下载 sing-box 失败，可设置 GH_PROXY 后重试"; exit 1
  fi
fi
# GEO / 规则集（.srs）由面板按需下载到 /etc/sing-box/rules，不再需要 mihomo 的 geoip.dat / geosite.dat

echo ">> 安装面板"
# 程序先拷成 .new 再改名：覆盖安装时旧程序可能正在运行（直接覆盖会 Text file busy）
cp "$SRC/shunt" /opt/mihomo-panel/shunt.new && chmod 755 /opt/mihomo-panel/shunt.new && mv -f /opt/mihomo-panel/shunt.new /opt/mihomo-panel/shunt
cp "$SRC/index.html" "$SRC/tproxy.sh" "$SRC/selftest.sh" /opt/mihomo-panel/
for f in icon.svg icon-180.png icon-512.png server.py update-files.txt; do [ -f "$SRC/$f" ] && cp "$SRC/$f" /opt/mihomo-panel/; done
[ -f "$SRC/bin/SHA256SUMS" ] && mkdir -p /opt/mihomo-panel/bin && cp "$SRC/bin/SHA256SUMS" /opt/mihomo-panel/bin/
chmod +x /opt/mihomo-panel/tproxy.sh /opt/mihomo-panel/selftest.sh
# v7.7.6 起服务名为 shunt（面板）/ shunt-core（核心）：覆盖安装旧版时先停掉并删除旧服务
for s in mihomo-panel mihomo; do
  if [ -f /etc/init.d/$s ]; then
    rc-service $s stop >/dev/null 2>&1; rc-update del $s default >/dev/null 2>&1; rc-service $s zap >/dev/null 2>&1; rm -f /etc/init.d/$s
  fi
done
cp "$SRC/init.d/shunt" "$SRC/init.d/shunt-core" /etc/init.d/
chmod +x /etc/init.d/shunt /etc/init.d/shunt-core

if [ ! -f /etc/mihomo-panel/data.json ]; then
  printf '{"secret":"","mode":"rule","tproxy":true,"subs":[],"rules":[]}\n' > /etc/mihomo-panel/data.json
  NEWPW=1
fi
chmod 600 /etc/mihomo-panel/data.json

if [ -n "$HTTPS" ]; then
  echo ">> 生成自签名证书"
  /opt/mihomo-panel/shunt --gen-cert >/dev/null
  /opt/mihomo-panel/shunt --https on >/dev/null
fi

# 开启转发并持久化（IPv6 转发由 tproxy.sh 在开启 IPv6 透明代理时设置）
mkdir -p /etc/sysctl.d
printf 'net.ipv4.ip_forward=1\n' > /etc/sysctl.d/99-mihomo-panel.conf
grep -q '^net.ipv4.ip_forward' /etc/sysctl.conf 2>/dev/null || echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf
sysctl -qw net.ipv4.ip_forward=1
rc-update add sysctl boot >/dev/null 2>&1 || true

# 运行环境检测：容器（LXC / Docker）没有自己的内核，模块和设备要在宿主机上准备
CT=""
if [ -e /dev/.lxc-boot-id ] || grep -q ' lxcfs ' /proc/self/mountinfo 2>/dev/null || grep -q '/lxc' /proc/1/cgroup 2>/dev/null; then CT=lxc
elif [ -f /.dockerenv ] || [ -f /run/.containerenv ] || grep -Eq '/(docker|kubepods)' /proc/1/cgroup 2>/dev/null; then CT=docker; fi
[ -n "$CT" ] && echo ">> 检测到 $CT 容器：内核模块、TUN 设备由宿主机提供"
# TUN 模式需要 tun 内核模块；TProxy 需要 nft_tproxy。加载并写入 /etc/modules 开机自动加载（容器里由宿主机负责）
if [ -z "$CT" ]; then
  for m in tun nft_tproxy; do
    modprobe "$m" 2>/dev/null || echo "!! 无法加载内核模块 $m（精简内核可能不支持，对应的代理方式将不可用）"
    grep -qx "$m" /etc/modules 2>/dev/null || echo "$m" >> /etc/modules
  done
fi
[ -c /dev/net/tun ] || { mkdir -p /dev/net && mknod /dev/net/tun c 10 200 2>/dev/null && chmod 666 /dev/net/tun; }
TUN_OK=""; [ -c /dev/net/tun ] && /opt/mihomo-panel/shunt --tun-check 2>/dev/null && TUN_OK=1
TP_OK=""; nft -f - 2>/dev/null <<'NFT' && TP_OK=1
table inet shunt_probe { chain p { type filter hook prerouting priority mangle; meta l4proto tcp tproxy ip to 127.0.0.1:1 } }
NFT
nft delete table inet shunt_probe 2>/dev/null || true
[ -n "$TP_OK" ] && echo ">> TProxy 可用（nftables tproxy）" || echo "!! TProxy 不可用：内核缺少 nft_tproxy / nf_tables"
[ -n "$TUN_OK" ] && echo ">> TUN 可用（/dev/net/tun）" || echo "!! TUN 不可用：没有 /dev/net/tun 或无权创建网卡"
if [ "$CT" = lxc ] && { [ -z "$TP_OK" ] || [ -z "$TUN_OK" ]; }; then
  echo "   PVE 宿主机上这样设置后重启容器（<ID> 换成容器编号）："
  [ -z "$TP_OK" ] && echo "     modprobe nft_tproxy && echo nft_tproxy >> /etc/modules"
  [ -z "$TUN_OK" ] && { echo "     在 /etc/pve/lxc/<ID>.conf 末尾加两行："
    echo "       lxc.cgroup2.devices.allow: c 10:200 rwm"
    echo "       lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file"; }
  echo "     容器需在 选项 → 功能 里勾选 nesting；非特权容器不能用 TProxy 时可改用特权容器"
fi
[ -z "$TP_OK" ] && [ -z "$TUN_OK" ] && echo "!! TProxy 和 TUN 都不可用：只能把设备的 HTTP/SOCKS 代理指向本机 7890 端口使用"

# 升级安装：先记下各策略组当前选择（v6 地区组改名，如「🇺🇸 美国」→「🇺🇸 美国自动优选」），面板启动后按新名称恢复
[ -z "$NEWPW" ] && /opt/mihomo-panel/shunt --snapshot >/dev/null 2>&1 || true
/opt/mihomo-panel/shunt --gen >/dev/null
if [ -n "$SELFTEST" ]; then
  echo ">> 校验配置"
  sh /opt/mihomo-panel/selftest.sh || echo "!! 自检未全部通过，请把输出反馈给作者"
fi
rc-update add shunt-core default >/dev/null
rc-update add shunt default >/dev/null
rc-service shunt-core restart
rc-service shunt restart

PORT=$(sed -n 's/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}PANEL_PORT=["'"'"']\{0,1\}\([0-9]\{1,5\}\).*/\2/p' /etc/conf.d/shunt 2>/dev/null | tail -n 1)
PORT=${PORT:-8080}
IP=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.*src \([0-9.]*\).*/\1/p')
SCHEME=http; grep -q '"https": *true' /etc/mihomo-panel/data.json && [ -f /etc/mihomo-panel/cert.pem ] && SCHEME=https
echo
echo "========= Shunt 分流 · 安装完成 ========="
echo " 面板地址: ${SCHEME}://${IP:-本机IP}:${PORT}"
[ "$SCHEME" = https ] && echo " （自签名证书，浏览器提示不安全时选择继续访问）"
echo " 面板不需要密码，默认只允许局域网访问"
echo " 客户端把 网关 和 DNS 都设为 ${IP:-本机IP} 即可走透明代理"
echo " 代理方式（TProxy / TUN / 关闭）、DNS、广告拦截都在面板“设置 / 广告拦截”里配置"
echo " 配置自检: sh /opt/mihomo-panel/selftest.sh"
echo "==========================================="
