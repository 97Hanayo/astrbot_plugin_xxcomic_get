# astrbot_plugin_xxcomic_get

AstrBot 本子/漫画图片来源识别插件，使用 SoutuBot 搜图。

## 使用方法

在 QQ 中艾特机器人，发送指令并附带一张图片：

```text
/哈哈
```

插件会用 Playwright 打开 `https://soutubot.moe/`，通过页面上传图片并解析搜索结果。若最高匹配结果来自 `nhentai`，会通过 `https://nhentai.net/api/v2/galleries/{id}` 获取页列表，下载原图后先发送标题，再按顺序用合并聊天记录发送图片。

也可以直接按文本搜索 nhentai，并下载第一个搜索结果：

```text
/嘻嘻 [Cuchuflin] Bug Bite: Chapter 8
```

## 配置

在 AstrBot 插件配置中可调整：

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `soutubot.min_similarity` | 最低返回相似度 | `45` |
| `soutubot.max_results` | 最多返回结果数 | `3` |
| `soutubot.timeout_ms` | Playwright 超时毫秒 | `60000` |
| `soutubot.headless` | 是否使用无头浏览器 | `true` |
| `nhentai.download_enabled` | 是否自动下载最高匹配的 nhentai 结果 | `true` |
| `nhentai.cookies` | nhentai Cookie，支持 `cookies.txt` 内容/路径或 `k=v;...` | 空 |
| `nhentai.api_key` | nhentai API Key，会作为 `Authorization: Key <api_key>` 请求头发送；为空时相关命令返回 `未配置api` | 空 |
| `nhentai.max_download_pages` | 最大下载页数 | `120` |
| `nhentai.send_forward` | 是否优先合并聊天记录发送 | `true` |
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

- `/哈哈` 自动下载只处理最高匹配的 `nhentai` 结果，其他来源只返回搜索结果。
- `/嘻嘻` 会携带 API Key 调用 `https://nhentai.net/api/v2/search?query=...&sort=date&page=1`，直接使用第一个搜索结果。
- 下载文件保存在插件数据目录的 `downloads/{gallery_id}/originals/` 下。
- 原图地址通过 `https://nhentai.net/api/v2/cdn` 返回的 `image_servers` 拼接 API 返回的 `pages[].path`，不硬编码 CDN 子域名，也不通过连续探测猜页数。
- 合并聊天记录主要适配 OneBot v11；平台不支持时会回退为逐张图片。
- Cookie 和 API Key 只用于请求，不会主动输出到日志或回复。

## 说明

- 命令入口：`/哈哈`、`/嘻嘻`
- 只处理同一条消息中的第一张图片
- 搜索服务：SoutuBot
- 下载来源：nhentai API v2
