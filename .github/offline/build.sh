#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# 在 Alpine 容器里运行（容器架构 = 目标架构）：准备 shunt-singbox 的面板 + sing-box 内核 + 默认 .srs 规则集
# 用法: sh .github/offline/build.sh <仓库目录> <输出目录>
# Dockerfile 用它准备镜像内容：NO_APK=1 不下载 apk 离线源，NO_TAR=1 只留下 /tmp/shunt-offline 不打 tar 包
# shunt-singbox 只用 sing-box：不下载 mihomo / mihomo Smart / geo 数据（GEO / 规则集由面板按需下载 .srs）
# Go 版面板程序 shunt（不需要 python3）的来源，按顺序：SHUNT_BIN=程序路径；仓库里的 dist/shunt-linux-<架构>（tools/build-bin.sh 的输出）；
#   否则按 bin/SHA256SUMS 从发布仓库（PANEL_REPO，默认 GITHUB_REPOSITORY 或 Skycnhe/Shunt-release）下载并校验 sha256
set -eu
SRC=$(cd "$1" && pwd); OUT=$(mkdir -p "$2" && cd "$2" && pwd)
REL=$(cut -d. -f1,2 /etc/alpine-release); M=$(uname -m)
case "$M" in
  aarch64) SA=arm64 ;;
  x86_64) SA=amd64 ;;
  *) echo "不支持的架构 $M"; exit 1 ;;
esac
apk add --no-cache curl tar gzip coreutils >/dev/null
C="curl -fsSL --retry 5 --retry-delay 3"
latest_tag() { curl -fsSI "https://github.com/$1/releases/latest" | sed -n 's#^[Ll]ocation: .*/tag/\([^[:space:]]*\).*#\1#p' | tr -d '\r'; }

W=/tmp/shunt-offline; rm -rf "$W"
mkdir -p "$W/offline/apk/$M" "$W/offline/bin" "$W/offline/sb-rules"
cd "$SRC"
cp index.html tproxy.sh selftest.sh install.sh "$W/"
# server.py：发布仓库里是引导程序（旧 Python 版在线更新到 Go 版时用），带上它和 update-files.txt、bin/SHA256SUMS
for f in README.md CHANGELOG.md LICENSE icon.svg icon-180.png icon-512.png offline.sh server.py update-files.txt; do [ -f "$f" ] && cp "$f" "$W/"; done
cp -r init.d "$W/"
[ -f bin/SHA256SUMS ] && { mkdir -p "$W/bin"; cp bin/SHA256SUMS "$W/bin/"; }

echo ">> 面板程序 shunt (linux/$SA)"
if [ -n "${SHUNT_BIN:-}" ]; then
  cp "$SHUNT_BIN" "$W/shunt"
elif [ -f "$SRC/dist/shunt-linux-$SA" ]; then
  cp "$SRC/dist/shunt-linux-$SA" "$W/shunt"
else
  [ -f "$SRC/bin/SHA256SUMS" ] || { echo "没有 bin/SHA256SUMS，也没有 dist/shunt-linux-$SA（先运行 sh tools/build-bin.sh）"; exit 1; }
  REPO="${PANEL_REPO:-${GITHUB_REPOSITORY:-Skycnhe/Shunt-release}}"
  BC=$(sed -n 's/^commit[[:space:]]\{1,\}\([0-9a-f]\{40\}\).*/\1/p' "$SRC/bin/SHA256SUMS" | head -n 1)
  WGZ=$(awk -v f="shunt-linux-$SA.gz" '$2==f{print $1}' "$SRC/bin/SHA256SUMS")
  WBIN=$(awk -v f="shunt-linux-$SA" '$2==f{print $1}' "$SRC/bin/SHA256SUMS")
  [ -n "$BC" ] && [ -n "$WGZ" ] && [ -n "$WBIN" ] || { echo "bin/SHA256SUMS 里没有 linux/$SA 的程序"; exit 1; }
  $C -o /tmp/shunt.gz "https://raw.githubusercontent.com/$REPO/$BC/shunt-linux-$SA.gz"
  [ "$(sha256sum /tmp/shunt.gz | cut -d' ' -f1)" = "$WGZ" ] || { echo "面板程序压缩包 sha256 不符"; exit 1; }
  gunzip -c /tmp/shunt.gz > "$W/shunt" && rm -f /tmp/shunt.gz
  [ "$(sha256sum "$W/shunt" | cut -d' ' -f1)" = "$WBIN" ] || { echo "面板程序 sha256 不符"; exit 1; }
fi
chmod 755 "$W/shunt"
"$W/shunt" --version

PKGS="curl wget ca-certificates nftables iptables ip6tables iproute2 gzip tar tzdata kmod coreutils openssl chrony"
if [ -n "${NO_APK:-}" ]; then
  echo ">> 跳过 apk 依赖（NO_APK）"
else
echo ">> apk 依赖 (Alpine $REL $M)"
apk update >/dev/null
apk fetch -R -o "$W/offline/apk/$M" $PKGS >/dev/null
apk index --allow-untrusted -o "$W/offline/apk/$M/APKINDEX.tar.gz" "$W/offline/apk/$M"/*.apk
ln -s "$M" "$W/offline/apk/noarch"
echo "$PKGS" > "$W/offline/apk/PKGS"
fi

echo ">> sing-box (musl)"
BT=$(latest_tag SagerNet/sing-box); BV=${BT#v}
[ -n "$BV" ] || { echo "取不到 sing-box 最新版本"; exit 1; }
$C "https://github.com/SagerNet/sing-box/releases/download/$BT/sing-box-$BV-linux-$SA-musl.tar.gz" | tar xz -C /tmp
cp "/tmp/sing-box-$BV-linux-$SA-musl/sing-box" "$W/offline/bin/sing-box"
chmod 755 "$W"/offline/bin/*
"$W/offline/bin/sing-box" version | head -n1

echo ">> sing-box 默认规则集"
T=$(mktemp -d); echo '{}' > "$T/data.json"
PANEL_OFFLINE=1 PANEL_DATA="$T/data.json" MIHOMO_DIR="$T" SB_DIR="$T/sb" SB_RULES="$W/offline/sb-rules" SB_BIN="$W/offline/bin/sing-box" \
  "$W/shunt" --sync-rules || echo "!! 规则集预下载失败（不影响使用，面板启动后会自动下载）"
rm -rf "$T"

PV=$("$W/shunt" --version)
cat > "$W/offline/VERSION" <<V
panel=$PV
alpine=$REL
arch=$M
sing-box=$BV
built=$(date -u +%F)
V
cat "$W/offline/VERSION"

# 发布前自测：sing-box 校验面板生成的全部场景配置，任何一项失败就不发布
echo ">> 自测 sing-box"
PANEL_BIN="$W/shunt" SB_BIN="$W/offline/bin/sing-box" SB_RULES="$W/offline/sb-rules" \
  sh "$W/selftest.sh" || { echo "!! 自测失败，停止打包"; exit 1; }
[ -n "${NO_TAR:-}" ] && { echo ">> 已准备好 $W（NO_TAR，不打包）"; exit 0; }
NAME="shunt-offline-$M-alpine$REL.tar.gz"
tar -C /tmp -czf "$OUT/$NAME" shunt-offline
ls -lh "$OUT/$NAME"
