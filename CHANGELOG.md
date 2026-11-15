# 更新记录与详细说明

## v8.10.34 修复与精简：日志票据并发、内核预发布提示、补 LICENSE
- 修复：设备名反查线程清理过期条目时，请求线程同时写入会抛「dictionary changed size during iteration」让线程退出，之后设备名一直不更新；日志流票据同理（偶发 500）
- 内核：正在用 sing-box 预发布版（alpha / beta / rc）时，版本表显示「建议正式版」提示（1.15.0-alpha.13 等预发布内核可能卡死）
- 许可：补上 GPL-3.0 LICENSE 文件（源码头一直声明 GPL-3.0-or-later），README 注明

