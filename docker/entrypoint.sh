#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Shunt Docker 镜像入口（shunt-singbox：只有 sing-box 内核）（由 tini 以 PID 1 拉起）。容器里没有真正的 init：
# - 用 OpenRC 的 rc-service 管 shunt（面板）和 shunt-core（核心），和 Alpine 实机共用同一份 init.d 脚本，
#   面板里的启动 / 停止 / 重启、看门狗、在线更新后重启面板等都照常走 rc-service
# - 本脚本负责：准备数据卷、清理上次异常退出留下的 nftables / 策略路由、按顺序启动、面板意外退出时拉起、
#   收到 SIGTERM（docker stop）时先撤透明代理规则（局域网立即恢复直连）再停面板和核心
# 数据全部在 /data（VOLUME）：/etc/mihomo-panel、/etc/mihomo、/etc/sing-box 是指向 /data 下同名目录的软链接
set -u
SEED=/opt/shunt-seed          # 镜像里预装的 sing-box 默认规则集，数据卷里没有时复制过去
PANEL=/opt/mihomo-panel
log() { echo "[shunt] $*"; }

# ---- 数据卷
mkdir -p /data/mihomo-panel /data/mihomo/providers /data/sing-box/rules /var/log
for f in "$SEED"/sb-rules/*; do
  [ -f "$f" ] && { [ -s "/data/sing-box/rules/${f##*/}" ] || cp "$f" /data/sing-box/rules/; }
done
NEW=""
if [ ! -f /data/mihomo-panel/data.json ]; then
  printf '{"secret":"","mode":"rule","tproxy":true,"subs":[],"rules":[]}\n' > /data/mihomo-panel/data.json
  NEW=1
fi
chmod 600 /data/mihomo-panel/data.json
if [ -n "${TZ:-}" ] && [ -f "/usr/share/zoneinfo/$TZ" ]; then cp "/usr/share/zoneinfo/$TZ" /etc/localtime; echo "$TZ" > /etc/timezone; fi

# ---- 面板端口：-e PANEL_PORT=8081（8080 被占用时）。容器用 host 网络，端口映射（-p / ports）不起作用；
# OpenRC 启动服务时会清掉容器的环境变量，所以写进 /etc/conf.d/shunt，由 init.d/shunt 传给面板
PORT=8080
if [ -n "${PANEL_PORT:-}" ]; then
  case "$PANEL_PORT" in
    *[!0-9]*) log "!! PANEL_PORT=$PANEL_PORT 不是有效端口，仍用 8080" ;;
    *) if [ "$PANEL_PORT" -ge 1 ] && [ "$PANEL_PORT" -le 65535 ]; then PORT=$PANEL_PORT
       else log "!! PANEL_PORT=$PANEL_PORT 超出 1-65535，仍用 8080"; fi ;;
  esac
  mkdir -p /etc/conf.d
  { echo "# 由 shunt-entrypoint 按容器环境变量 PANEL_PORT 生成"; echo "PANEL_PORT=$PORT"; } > /etc/conf.d/shunt 2>/dev/null \
    || log "!! 写 /etc/conf.d/shunt 失败（挂载成只读了？），面板端口以该文件为准"
fi
# 也可以直接挂载 /etc/conf.d/shunt（写 PANEL_PORT=8081），以文件里的为准
P=$(sed -n 's/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}PANEL_PORT=["'"'"']\{0,1\}\([0-9]\{1,5\}\).*/\2/p' /etc/conf.d/shunt 2>/dev/null | tail -n 1)
[ -n "$P" ] && [ "$P" -ge 1 ] && [ "$P" -le 65535 ] && PORT=$P
case " 7890 7893 1053 9090 " in *" $PORT "*) log "!! 面板端口 $PORT 和核心端口冲突（7890 / 7893 / 1053 / 9090），请换一个" ;; esac
echo "$PORT" > /run/shunt-panel.port   # 健康检查读这个文件

# ---- OpenRC：容器没有经过 OpenRC 开机，状态目录每次启动都重建（docker restart 会保留上次的 started / pid 记录）
rm -rf /run/openrc && mkdir -p /run/openrc/exclusive && touch /run/openrc/softlevel
rm -f /run/shunt.pid /run/shunt-core.pid /run/shunt-core.prep

# ---- 运行环境检查（network_mode: host 时规则写在宿主机的网络命名空间里）
# 探测语句与 tproxy.sh 实际下发的写法一致（inet 表里 tproxy ip 前要有 meta nfproto ipv4）
TP_OK=""; TP_ERR=$(nft -f - 2>&1 <<'NFT') && TP_OK=1
table inet shunt_probe { chain p { type filter hook prerouting priority mangle; meta nfproto ipv4 meta l4proto tcp tproxy ip to 127.0.0.1:1 } }
NFT
nft delete table inet shunt_probe 2>/dev/null
if [ -n "$TP_OK" ]; then log "TProxy 可用"
elif nft list tables >/dev/null 2>&1; then
  log "!! TProxy 不可用：宿主机内核没有加载 nft_tproxy（宿主机执行 modprobe nft_tproxy）"
  [ -n "$TP_ERR" ] && log "   nft: $(echo "$TP_ERR" | head -n 3 | tr '\n' ' ')"
else log "!! nftables 不可用：容器缺少 NET_ADMIN 权限（--cap-add NET_ADMIN）或宿主机内核不支持 nf_tables"; fi
if [ -c /dev/net/tun ]; then log "TUN 设备 /dev/net/tun 已映射"; else log "!! 没有 /dev/net/tun：TUN 模式不可用（--device /dev/net/tun）"; fi
[ "$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null)" = 1 ] || log "!! 宿主机未开启 IPv4 转发：请在宿主机执行 sysctl -w net.ipv4.ip_forward=1 并写入 /etc/sysctl.conf"
KERNEL=$(uname -r 2>/dev/null)   # 容器和宿主机共用内核
log "宿主机内核 $KERNEL"
if [ -z "$TP_OK" ]; then
  if [ -c /dev/net/tun ]; then
    log "   建议：面板「设置 → 代理方式」改用 TUN（群晖等 4.x 老内核通常没有 nf_tables / nft_tproxy，镜像里补不上）"
    # 全新安装且 TProxy 用不了：直接默认 TUN，免得第一次启动就是「规则加载失败」
    if [ -n "$NEW" ]; then
      printf '{"secret":"","mode":"rule","tproxy":false,"proxy_mode":"tun","subs":[],"rules":[]}\n' > /data/mihomo-panel/data.json
      log "   首次启动：已默认使用 TUN 模式"
    fi
  else
    log "   TProxy 和 TUN 都不可用，只能用 7890 端口的 HTTP / SOCKS 代理"
  fi
fi

# ---- 宿主机防火墙（Debian / Ubuntu / NAS）
# 局域网网卡：默认路由所在网卡，可用 SHUNT_LAN_IF 指定（群晖常见 ovs_eth0 / bond0，Unraid 常见 br0）
LAN_IF=${SHUNT_LAN_IF:-$(ip -4 route show default 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="dev"){print $(i+1); exit}}')}
LAN_NET=""
[ -n "$LAN_IF" ] && LAN_NET=$(ip -4 -o addr show dev "$LAN_IF" 2>/dev/null | awk '{print $4; exit}' \
  | python3 -c "import sys,ipaddress; v=sys.stdin.read().strip(); print(ipaddress.ip_interface(v).network if v else '')" 2>/dev/null)
# Docker 把 FORWARD 默认策略改成 DROP 时，局域网经旁路由转发的流量（TUN 模式全部流量、TProxy 下绕过 / 直连的）会被丢掉。
# 在 Docker 的 DOCKER-USER 链里放行局域网网卡，docker stop 时撤掉。宿主机可能用 iptables-nft（Debian 11+ / Ubuntu 20.04+）
# 或 iptables-legacy（群晖、老系统），两套都看。SHUNT_FORWARD_FIX=0 关闭
FWD_DONE=""
if [ "${SHUNT_FORWARD_FIX:-1}" != 0 ] && [ -n "$LAN_IF" ]; then
  for ipt in iptables iptables-legacy ip6tables ip6tables-legacy; do
    command -v $ipt >/dev/null 2>&1 || continue
    $ipt -w -S DOCKER-USER >/dev/null 2>&1 || continue
    $ipt -w -S FORWARD 2>/dev/null | grep -q '^-P FORWARD DROP' || continue
    $ipt -w -C DOCKER-USER -i "$LAN_IF" -j ACCEPT 2>/dev/null && continue   # 已有（用户自己加的）：不动，停止时也不删
    $ipt -w -I DOCKER-USER -i "$LAN_IF" -j ACCEPT 2>/dev/null && FWD_DONE="$FWD_DONE $ipt"
  done
  [ -n "$FWD_DONE" ] && log "宿主机 FORWARD 默认丢弃（Docker）：已在 DOCKER-USER 放行 $LAN_IF（$(echo $FWD_DONE | tr ' ' ',')），停止容器时撤掉"
fi
fwd_undo() {
  for ipt in $FWD_DONE; do $ipt -w -D DOCKER-USER -i "$LAN_IF" -j ACCEPT 2>/dev/null; done
}
# ufw（Ubuntu）/ firewalld（Debian、部分 NAS）默认拒绝入站：局域网连不上面板、DNS 1053 和 TProxy 7893，只能提示
NET=${LAN_NET:-192.168.1.0/24}
if iptables -w -S ufw-user-input >/dev/null 2>&1 || iptables-legacy -w -S ufw-user-input >/dev/null 2>&1; then
  log "!! 宿主机开着 ufw：请在宿主机放行局域网，否则面板 / DNS / 透明代理连不上："
  log "   ufw allow from $NET && ufw route allow in on ${LAN_IF:-eth0}"
fi
if nft list table inet firewalld >/dev/null 2>&1; then
  log "!! 宿主机开着 firewalld：请在宿主机把局域网加入 trusted 区域："
  log "   firewall-cmd --permanent --zone=trusted --add-source=$NET && firewall-cmd --reload"
fi

# 上次被 SIGKILL（docker stop 超时、宿主机断电）没来得及撤的规则：表 / 策略路由在宿主机命名空间里，留着局域网会断网
"$PANEL/tproxy.sh" stop leftovers >/dev/null 2>&1

# 镜像升级（面板版本变化）：和 install.sh 一样先记下各策略组的选择，新配置生效后面板按新组名恢复
VER=$(sed -n 's/^PANEL_VERSION = "\(.*\)"/\1/p' "$PANEL/server.py" | head -n 1)  # 内嵌的 base.py 里还有一行
if [ -z "$NEW" ] && [ "$(cat /data/mihomo-panel/.image-version 2>/dev/null)" != "$VER" ]; then
  python3 "$PANEL/server.py" --snapshot >/dev/null 2>&1 || true
fi
echo "$VER" > /data/mihomo-panel/.image-version
log "面板 v$VER"

STOPPING=""
shutdown() {
  [ -n "$STOPPING" ] && return
  STOPPING=1
  log "收到停止信号：撤透明代理规则，停止面板和核心"
  nft delete table inet mihomo_panel 2>/dev/null   # 先撤规则：局域网立即直连，不等核心退出（sing-box 最多 15 秒）
  rc-service shunt stop >/dev/null 2>&1
  rc-service shunt-core stop >/dev/null 2>&1        # init.d 的 stop_pre / stop_post 会再撤一次规则并清 TUN 残留
  kill_core
  "$PANEL/tproxy.sh" stop leftovers >/dev/null 2>&1
  fwd_undo
  [ -n "${TAIL_PID:-}" ] && kill "$TAIL_PID" 2>/dev/null
  log "已停止"
  exit 0
}
# rc-service 停不掉的核心（如面板正在重启核心时收到信号）：TERM 后最多等 15 秒，再 KILL
kill_core() {
  pidof mihomo mihomo-smart sing-box >/dev/null 2>&1 || return 0
  kill $(pidof mihomo mihomo-smart sing-box) 2>/dev/null
  i=0; while [ $i -lt 75 ] && pidof mihomo mihomo-smart sing-box >/dev/null 2>&1; do sleep 0.2; i=$((i+1)); done
  pidof mihomo mihomo-smart sing-box >/dev/null 2>&1 && kill -9 $(pidof mihomo mihomo-smart sing-box) 2>/dev/null
  return 0
}
trap shutdown TERM INT QUIT HUP

# ---- 启动：核心（用户在面板里手动停止过就不启动，与实机开机一致）→ 面板
if [ -f /data/mihomo-panel/core_stopped ]; then
  log "核心已在面板里手动停止，本次不自动启动"
else
  rc-service shunt-core start || log "!! 核心启动失败，详情见面板「日志」或 /var/log/mihomo.log"
fi
rc-service shunt start || log "!! 面板启动失败，详情见 /var/log/mihomo-panel.log"
touch /var/log/mihomo-panel.log
tail -n 20 -F /var/log/mihomo-panel.log 2>/dev/null &  # 面板日志同时输出到 docker logs
TAIL_PID=$!
grep -q '"https": *true' /data/mihomo-panel/data.json && [ -f /data/mihomo-panel/cert.pem ] && S=https || S=http
log "面板地址: $S://<本机IP>:$PORT"

# ---- 守护：面板进程意外退出时拉起。在线更新、切 HTTPS、恢复备份时面板会自己 rc-service shunt restart，
# 连续两次（20 秒）检查都不在才算退出，不和它抢
miss=0
while :; do
  sleep 10 &
  wait $! 2>/dev/null
  [ -n "$STOPPING" ] && break
  pid=$(cat /run/shunt.pid 2>/dev/null)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then miss=0; continue; fi
  miss=$((miss+1))
  [ $miss -lt 2 ] && continue
  log "面板未运行，重新启动"
  rc-service shunt zap >/dev/null 2>&1
  rc-service shunt start >/dev/null 2>&1
  miss=0
done
