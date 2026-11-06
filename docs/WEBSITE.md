# 软件官网

官网：https://ai-session-migration.pages.dev/

源码位于 `site/`，部署配置为仓库根目录 `wrangler.jsonc`。纯静态 HTML、CSS、少量渐进增强 JavaScript，无框架构建、会话上传接口、账号、数据库或分析脚本。沿用桌面客户端的石墨色与薄荷绿视觉；界面示意全部使用合成内容，明确标注，不扫描访客资料。

Cloudflare Pages 项目为 `ai-session-migration`，生产分支 `main`。仅上传 `site/`；不上传仓库根目录、客户端私有状态、真实会话、备份、密钥、机器报告或维护脚本。安装包使用公开 GitHub Release 的固定版本链接，页面展示预览版、签名状态和真实设备未验证范围。安装包不放入 Pages 静态资源目录，遵循 [Pages 单文件限制](https://developers.cloudflare.com/pages/platform/limits/)。

当前下载更新为 `v0.3.0`，保持两个已验收客户端的原始 SHA-256，配套 `GUI-0.3.0-Guide.txt` 和 `SHA256SUMS.txt`。发布内容仅为通用安装包与公开操作说明，不包含用户资料。桌面应用版本未因官网变更而升级。

静态检查已检查锚点唯一性、内部链接、图片 alt、离线资源存在、外链域名、XML、JavaScript 语法、部署文件大小，以及部署目录不含私密资料类型。CSS 含桌面、平板、手机布局和减少动态效果规则。浏览器未验证，不将静态检查当作视觉或交互验收。

历史0.2.0上线及独立回读记录：生产地址为 https://ai-session-migration.pages.dev/ ，部署 ID `24a903d4-367f-4648-83a1-f9e675d133bf`，部署版本地址为 https://24a903d4.ai-session-migration.pages.dev 。Cloudflare 返回 production / deploy success，网页资源对应提交 `3c4b0f7fdae52e32168cc00b7abf29224341aa58`；后续仅更新本发布记录，不改动上线资源。

7 个公开资源（首页、CSS、JS、图标、robots、sitemap）已从生产地址实际获取，与本地字节和 SHA-256 一致。安全响应头实际生效。4 个公开下载入口已核验：两个安装包 HTTP 200 且 GitHub 服务端摘要/大小与本地一致；操作说明和 SHA-256 清单实际下载并逐字节核对。所有安装包摘要保持已验收版本不变。浏览器未验证。

2026-11-06：本轮同步0.3.0主分支和预览版下载，官网新增恢复中心、选择文件/目录/整组、目标备份、分页、缓存和副本/原生恢复边界。保留合成示意标签及真实Windows待验证说明。仅部署site目录，浏览器未验证。部署完成后独立核对生产资源字节、响应头和四个版本下载入口。

0.3.0 本轮已上线并独立回读：生产部署 `f5afca75-848c-4536-81a7-8c4bef0db919`，部署地址 https://f5afca75.ai-session-migration.pages.dev ，生产分支main，资源提交 `f0b42acc61ba44f7263a5ac7e9dfc970ea7efedd`。7个生产资源与本地字节一致，安全响应头通过，4个公开下载入口HTTP可用且资产服务端摘要与已验收文件一致；通用说明及校验清单已实际下载逐字节比对。主分支[集成CI](https://github.com/ForceMind/ai-session-migration/actions/runs/42216621333)通过；应用源码与已验收构建8aecbd0一致，未重新编译或替换发布安装包。浏览器未验证。
