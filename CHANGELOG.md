# 更新记录与详细说明

## v11.6.37 修复：订阅节点前置代理缺失 / 成环导致 sing-box 起不来
- 修复：订阅节点带前置代理（detour / dialer-proxy）而前置节点不存在、被筛掉或互相成环时，sing-box check 能过、启动即 FATAL（dependency not found），一个坏节点让整个核心起不来；现在这些节点自动跳过并在保存时提示（同步 shunt 分支已有的处理）
- 修复：Clash 订阅里读不出来的节点以前静默丢弃，现在计入「无法识别已跳过」
- 校验：corecheck 引用检查增加出站成环检查

