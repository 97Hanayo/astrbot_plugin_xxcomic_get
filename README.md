# astrbot_plugin_xxcomic_get

AstrBot 本子/漫画图片来源识别插件，使用 SoutuBot 搜图。

## 使用方法

在 QQ 中艾特机器人，发送指令并附带一张图片：

```text
/哈哈
```

插件会用 Playwright 打开 `https://soutubot.moe/`，通过页面上传图片并解析搜索结果。若搜索结果里有 `nhentai`，会先返回第一个 `nhentai` 结果的 ID 和标题。

也可以直接按文本搜索 nhentai，并返回第一个搜索结果：

```text
/嘻嘻 [Cuchuflin] Bug Bite: Chapter 8
```

确认需要下载时，再发送：

```text
/对的 123456
```

## 配置

在 AstrBot 插件配置中可调整：

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `soutubot.min_similarity` | 最低返回相似度 | `45` |
| `soutubot.max_results` | 最多返回结果数 | `3` |
| `soutubot.timeout_ms` | Playwright 超时毫秒 | `60000` |
| `soutubot.headless` | 是否使用无头浏览器 | `true` |
| `nhentai.download_enabled` | 是否允许通过 `/对的 <id>` 下载 nhentai 结果 | `true` |
| `nhentai.cookies` | nhentai Cookie，支持 `cookies.txt` 内容/路径或 `k=v;...` | 空 |
| `nhentai.api_key` | nhentai API Key，会作为 `Authorization: Key <api_key>` 请求头发送；为空时相关命令返回 `未配置api` | 空 |
| `nhentai.proxy` | nhentai API 和原图下载代理，例如 `http://127.0.0.1:7890`；出现 SSL EOF 时优先检查此项 | 空 |
| `nhentai.max_download_pages` | 最大下载页数 | `120` |
| `nhentai.block_risky_tags` | 是否阻止风险标签自动下载 | `true` |

SoutuBot 不需要 SauceNAO API Key。

## Cookie / 浏览器状态

插件会自动保存 Playwright 的浏览器状态，用于复用 Cloudflare Cookie：

```text
data/plugin_data/astrbot_plugin_xxcomic_get/cookies/soutubot_storage_state.json
```

如果部署环境被 Cloudflare 拦截，可以尝试：

- 确认 Playwright 浏览器已安装并可启动
- 临时把 `soutubot.headless` 设为 `false` 观察页面状态
- 删除上面的 `soutubot_storage_state.json` 后重试
- 适当调大 `soutubot.timeout_ms`

## 下载与发送

- `/哈哈` 会返回搜索结果中第一个 `nhentai` 结果的 ID 和标题，其他来源只返回搜索结果。
- `/嘻嘻` 会携带 API Key 调用 `https://nhentai.net/api/v2/search?query=...&sort=date&page=1`，返回第一个搜索结果的 ID 和标题。
- `/对的 <id>` 会通过 `https://nhentai.net/api/v2/galleries/{id}` 获取页列表，下载原图后合成为加密 PDF 发送。
- 下载图片保存在插件数据目录的 `downloads/{gallery_id}/originals/` 下。
- PDF 保存在插件数据目录的 `downloads/{gallery_id}/{gallery_id}.pdf`，文件名使用 nhentai ID。
- PDF 会随机生成 6 位小写字母加数字密码，并随发送消息一起给出。
- 原图地址通过 `https://nhentai.net/api/v2/cdn` 返回的 `image_servers` 拼接 API 返回的 `pages[].path`，不硬编码 CDN 子域名，也不通过连续探测猜页数。
- Cookie 和 API Key 只用于请求，不会主动输出到日志或回复。
- 若已经配置 API Key 但提示 `SSL: UNEXPECTED_EOF_WHILE_READING`，通常不是 key 错误，而是运行环境到 `nhentai.net` 的 HTTPS 连接被提前关闭；请给 AstrBot 所在环境配置可访问的 `nhentai.proxy`。

## 说明

- 命令入口：`/哈哈`、`/嘻嘻`、`/对的 <id>`
- 只处理同一条消息中的第一张图片
- 搜索服务：SoutuBot
- 下载来源：nhentai API v2
