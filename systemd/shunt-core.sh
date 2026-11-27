#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# systemd 版核心服务的辅助脚本（shunt-core.service 调用），逻辑与 Alpine 的 init.d/shunt-core 相同：
#   pre       清空并留存上次的核心日志、加载 tun 模块、生成配置（面板刚写好 /run/shunt-core.prep 时跳过）
#   run       按 /etc/mihomo-panel/core 选内核，写 pid 文件后 exec（日志写 /var/log/mihomo.log）
#   post      等核心监听好端口后挂透明代理规则（tproxy.sh apply）
#   stop PID  先撤 nftables 表（局域网立即恢复直连），再 TERM 核心，15 秒后还在就 KILL
#   stoppost  撤掉残留的策略路由 / TUN 表，删 pid 文件
PANEL=/opt/mihomo-panel
PREP=/run/shunt-core.prep
PIDFILE=/run/shunt-core.pid
LOG=/var/log/mihomo.log
CORE=$(cat /etc/mihomo-panel/core 2>/dev/null)
if [ "$CORE" = singbox ] && [ -x /usr/local/bin/sing-box ]; then
  BIN=/usr/local/bin/sing-box; set -- "$1" "$2" run --disable-color -c /etc/sing-box/config.json -D /etc/sing-box
elif [ "$CORE" = smart ] && [ -x /usr/local/bin/mihomo-smart ]; then
  BIN=/usr/local/bin/mihomo-smart; set -- "$1" "$2" -d /etc/mihomo
else
  BIN=/usr/local/bin/mihomo; set -- "$1" "$2" -d /etc/mihomo
fi
ACT=$1; ARG=$2; shift 2
case "$ACT" in
  pre)
    if [ -s "$LOG" ]; then
      { echo "===== $(date '+%F %T') 启动前，上次运行的最后 200 行 ====="; tail -n 200 "$LOG"; } >> "$LOG.prev" 2>/dev/null
      tail -n 600 "$LOG.prev" > "$LOG.prev.t" 2>/dev/null && mv -f "$LOG.prev.t" "$LOG.prev"
    fi
    : > "$LOG"; modprobe tun 2>/dev/null; mkdir -p /etc/sing-box
    if [ -f "$PREP" ] && [ $(( $(date +%s) - $(stat -c %Y "$PREP" 2>/dev/null || echo 0) )) -lt 120 ]; then exit 0; fi
    rm -f "$PREP"
    python3 "$PANEL/server.py" --gen >/dev/null || echo "生成配置失败，沿用上一次的配置启动"
    exit 0 ;;
  run)
    echo $$ > "$PIDFILE"
    exec "$BIN" "$@" >> "$LOG" 2>&1 ;;
  post)
    _tpe=""; [ -f "$PREP" ] && _tpe="$PREP"
    TPENV_FILE="$_tpe" WAIT_PID="$PIDFILE" "$PANEL/tproxy.sh" apply || echo "透明代理规则未加载（核心已退出，或内核缺少 nftables / TProxy 模块？），可改用 TUN 模式"
    rm -f "$PREP"
    exit 0 ;;
  stop)
    nft delete table inet mihomo_panel 2>/dev/null
    P=${ARG:-$(cat "$PIDFILE" 2>/dev/null)}
    case "$P" in ''|*[!0-9]*) exit 0 ;; esac
    kill -TERM "$P" 2>/dev/null
    n=0; while [ $n -lt 150 ] && kill -0 "$P" 2>/dev/null; do sleep 0.1; n=$((n+1)); done
    kill -0 "$P" 2>/dev/null && kill -KILL "$P" 2>/dev/null
    exit 0 ;;
  stoppost)
    "$PANEL/tproxy.sh" stop leftovers
    rm -f "$PIDFILE"
    exit 0 ;;
  *) echo "usage: $0 pre|run|post|stop|stoppost"; exit 1 ;;
esac
