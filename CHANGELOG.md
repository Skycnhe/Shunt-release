# 更新记录与详细说明

## v9.1.4 预览版：大广告列表误拦视频、内置视频白名单
- 修复：开了 HaGeZi Multi 等大列表后部分视频站打不开（如 jable）——HaGeZi Multi 整域名拦了视频 CDN mushroomtrack.com。新增内置视频白名单（mushroomtrack.com、googlevideo.com、ytimg.com、nflxvideo.net、bilivideo.com/.cn、hdslb.com、ttvnw.net、douyinvod.com、vimeocdn.com），与自定义白名单同级、始终放行
- anti-AD、AdGuard DNS filter、HaGeZi Multi 标为大列表（各约 10–19 万条）：预设按钮带 ⚠，添加前确认可能多占内存、误拦视频 / 接口域名。三者默认都不启用（与以前相同）
- 实测：三个列表合计约 47 万条域名，均已是纯域名后缀规则（没有正则 / 元素隐藏规则）；逐条匹配开销可忽略（200 次请求 1651 ms 对比 1650 ms），JSON 源与编译成 srs 的内存只差约 4 MB。变慢 / 打不开的原因是误拦，不是匹配开销

