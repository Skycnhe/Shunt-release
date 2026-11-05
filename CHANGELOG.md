# 更新记录与详细说明

## v11.6.19 Google Play 下载卡 99% 修复（同步 shunt v8.10.21）
- 分流：国行手机 Google Play 下载卡在 99% 修复。国行机的 Play 下载入口是 services.googleapis.cn，以前按国内域名直连，被指到 Google 国内 CDN（*.xn--ngstr-lra8j.com）后最后校验卡住；现在 googleapis.cn 与 xn--ngstr-lra8j.com 走「🔍 Google」。更新后在手机上清一下 Play 商店缓存再下载

