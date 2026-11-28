#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# 用真实 sing-box 校验面板生成的配置（多种场景），shunt-singbox 只用 sing-box。
# Go 版：场景生成与断言都在面板程序里（shunt --selftest），不需要 python3。
# 用法: sh selftest.sh            （设备上默认使用 /usr/local/bin/sing-box，规则集取自 /etc/sing-box/rules，缺少的会下载一次）
#       SB_BIN=/tmp/sing-box SB_RULES=/tmp/rules sh selftest.sh
#       KEEP=1 sh selftest.sh     保留生成的场景目录
#       PANEL_BIN=/path/to/shunt sh selftest.sh   指定面板程序（默认同目录或 /opt/mihomo-panel 下的 shunt）
SRC="$(cd "$(dirname "$0")" && pwd)"
BIN="${PANEL_BIN:-$SRC/shunt}"
[ -f "$BIN" ] && [ -x "$BIN" ] || BIN=/opt/mihomo-panel/shunt
[ -f "$BIN" ] && [ -x "$BIN" ] || { echo "找不到面板程序 shunt（$BIN）"; exit 1; }
exec "$BIN" --selftest
