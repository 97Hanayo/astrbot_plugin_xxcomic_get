# astrbot_plugin_xxcomic_get

AstrBot 本子/漫画图片来源识别插件，使用 SoutuBot 搜图。

## 使用方法

在 QQ 中艾特机器人，发送指令并附带一张图片：

```text
/哈哈
```

插件会用 Playwright 打开 `https://soutubot.moe/`，通过页面上传图片并解析搜索结果。若搜索结果里有 `nhentai`，会先返回第一个 `nhentai` 结果的 ID、标题和链接。

也可以直接按文本聚合搜索 nhentai、禁漫天堂和哔咔；每个来源最多返回前 5 条结果，并保留条目链接、ID 和标题：

```text
/嘻嘻 [Cuchuflin] Bug Bite: Chapter 8
```

兼容旧入口：也可以只搜索禁漫天堂，并返回第一个搜索结果的标题、ID 和条目链接，不下载：

```text
/JJS MANA 无修正
```

兼容旧入口：也可以只搜索哔咔漫画，并返回前几条结果的条目链接：

```text
/bk MANA 无修正
```

首次使用哔咔搜索前，需要管理员登录一次哔咔账号：

```text
/bklogin 用户名 密码
```

`/bklogin` 仅 AstrBot 管理员可调用，普通成员调用不会响应。插件只缓存哔咔返回的 token，不保存密码。

首次下载需要在插件配置中填写禁漫账号和密码；插件会自动登录并缓存 cookies/token，token 过期后会自动重新登录。

拿到任一来源的 ID 后，统一使用 `/对的` 下载整本并生成加密 PDF：

```text
/对的 123456
/对的 jm112233
/对的 64f1a2b3c4d5e6f789012345
```

`/JJ jm112233` 和 `/bkdl 64f1a2b3c4d5e6f789012345` 仍保留为兼容别名。

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
| `pica.proxy` | 哔咔 API 和图片下载代理，例如 `http://127.0.0.1:7890` | 空 |
| `pica.max_results` | `/bk` 最多返回结果数 | `5` |
| `pica.download_enabled` | 是否允许通过 `/对的 <24位ID>` 下载哔咔漫画 | `true` |
| `pica.max_download_pages` | 哔咔整本最大下载页数 | `300` |
| `pica.download_retries` | 哔咔单图下载重试次数 | `2` |

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

- `/哈哈` 会返回搜索结果中第一个 `nhentai` 结果的 ID、标题和链接，其他来源只返回搜索结果；成功搜索结果会以合并转发的聊天记录返回。
- `/嘻嘻` 会同时搜索 nhentai、禁漫天堂和哔咔，每个来源最多返回前 5 条结果的条目链接、ID 和标题，并以合并转发的聊天记录返回完整内容。
- nhentai 搜索会携带 API Key 调用 `https://nhentai.net/api/v2/search?query=...&sort=date&page=1`；未配置 API Key 时只会在 nhentai 分组提示 `未配置api`，其他来源仍会继续搜索。
- `/JJS <文本>` 会调用 `jmcomic` 的站内搜索，返回第一页第一条结果的标题、ID、条目链接，不下载；结果以合并转发的聊天记录返回。
- `/bk <文本>` 会调用哔咔 `POST /comics/advanced-search?page=1` 搜索漫画，返回条目链接、ID、标题、作者、分类、标签和页数等摘要，不下载；结果以合并转发的聊天记录返回。
- `/bklogin <用户名> <密码>` 会调用哔咔 `POST /auth/sign-in` 获取 token，仅管理员可调用；token 缓存在插件数据目录的 `accounts/pica_token.json`。
- `jmcomic.username` 和 `jmcomic.password` 会调用 JMComic 的 HTML 客户端自动登录；登录返回的 cookies/token 缓存在插件数据目录的 `accounts/jmcomic_token.json`，下载时自动携带。
- `/对的 <id>` 是统一下载入口：`jm` 加数字匹配禁漫，24 位十六进制字符串优先匹配哔咔，其他纯数字匹配 nhentai；下载前先复用完整缓存，最终始终校验为加密 PDF。三种来源的下载回复统一包含 ID、标题、页数、PDF 状态和密码。`/对的`、`/JJ`、`/bkdl` 没有 ID 时返回 `？`。
- `/JJ <jm id>` 和 `/bkdl <哔咔漫画ID>` 是兼容别名，分别转发到 `/对的` 的同一套下载流程。
- nhentai 通过 `https://nhentai.net/api/v2/galleries/{id}` 获取页列表；哔咔图片地址可能访问 `img.picacomic.com`、`storage-b.picacomic.com`、`storage1.picacomic.com`。
- 下载图片保存在插件数据目录的 `downloads/{gallery_id}/originals/` 下。
- PDF 保存在插件数据目录的 `downloads/{gallery_id}/{gallery_id}.pdf`，文件名使用 nhentai ID。
- PDF 会随机生成 6 位小写字母加数字密码，并随发送消息一起给出。
- 原图地址通过 `https://nhentai.net/api/v2/cdn` 返回的 `image_servers` 拼接 API 返回的 `pages[].path`，不硬编码 CDN 子域名，也不通过连续探测猜页数。
- Cookie 和 API Key 只用于请求，不会主动输出到日志或回复。
- 若已经配置 API Key 但提示 `SSL: UNEXPECTED_EOF_WHILE_READING`，通常不是 key 错误，而是运行环境到 `nhentai.net` 的 HTTPS 连接被提前关闭；请给 AstrBot 所在环境配置可访问的 `nhentai.proxy`。

## 说明

- 命令入口：`/哈哈`、`/嘻嘻 <文本>`、`/JJS <文本>`、`/bk <文本>`、`/bklogin <用户名> <密码>`、`/对的 <id>`；`/JJ`、`/bkdl` 为兼容别名
- 只处理同一条消息中的第一张图片
- 搜索服务：SoutuBot
- 下载来源：nhentai API v2、jmcomic、哔咔 API

## 禁漫天堂直接下载

发送统一命令：

```text
/对的 jm112233
```

兼容旧命令：`/JJ jm112233`

插件会调用 `jmcomic` 下载整本，并使用内置 PDF 导出能力生成加密 PDF。回复会包含禁漫专辑标题和页数；最终发送的文件名固定为输入 id，例如 `jm112233.pdf`；密码会随消息一起返回。id 仅接受 `jm+数字` 的形式。
如果运行环境的 jmcomic PDF 导出没有产物，插件会用已下载图片兜底合成加密 PDF。
下载前需要配置 `jmcomic.username` 和 `jmcomic.password`；登录 cookies/token 过期后插件会自动重新登录。也可以继续通过 `jmcomic.cookies` 提供手工 cookies。

相关配置：

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `jmcomic.download_enabled` | 是否允许通过 `/对的 <jm id>` 下载禁漫本子 | `true` |
| `jmcomic.download.threading.image` | 同时下载的图片数，范围 1-50；数值越小对禁漫压力越小 | `10` |
| `jmcomic.download.threading.photo` | 同时下载的章节数，范围 1-32；数值越小对禁漫压力越小 | `4` |
| `jmcomic.domains` | jmcomic 使用的禁漫域名列表，逗号或空格分隔，会按顺序重试 | `18comic.vip,18comic.org,jmcomic1.me,jmcomic.me,18comic-palworld.vip,18comic-c.art,18comic-palworld.club` |
| `jmcomic.domain` | 兼容旧配置；单个域名或逗号分隔的多个域名 | 空 |
| `jmcomic.proxy` | 禁漫下载代理，例如 `http://127.0.0.1:7890` | 空 |
| `jmcomic.cookies` | 禁漫 Cookie，支持 `cookies.txt` 内容/路径或 `k=v;...` | 空 |
| `jmcomic.username` | 禁漫登录账号；没有有效 cookies/token 时自动登录 | 空 |
| `jmcomic.password` | 禁漫登录密码；请勿提交含真实密码的配置文件 | 空 |

例如要进一步降低请求压力，可配置：

```yaml
jmcomic:
  download:
    threading:
      image: 5
      photo: 2
```

如果提示 `/setting` 或“请求重试全部失败”，通常是当前环境访问配置的禁漫域名失败。优先换 `jmcomic.domains`，或给 AstrBot 运行环境配置可访问的 `jmcomic.proxy`。

## 聚合文本搜索

发送：

```text
/嘻嘻 MANA 无修正
```

插件会同时搜索 nhentai、禁漫天堂和哔咔，并按来源分组返回前 5 条结果。每条结果都给出条目链接：

```text
nhentai：
1.
ID: 123456
标题: ...
链接：...

禁漫天堂：
1.
ID: jm112233
标题: ...
链接：...

哔咔：
1.
ID: ...
标题: ...
链接：...
```

如果某个来源没有配置、没有结果或搜索失败，只会影响该来源分组，其他来源会继续返回。

## 禁漫天堂搜索

发送：

```text
/JJS MANA 无修正
```

插件会调用 `jmcomic` 的 `search_site(search_query=..., page=1)` 搜索禁漫天堂站内结果，只返回第一条结果：

```text
找到第一个禁漫结果：
标题: ...
ID: jm112233
链接：...
```

这个命令不会下载图片，也不会生成 PDF。

## 哔咔搜索

发送：

```text
/bk MANA 无修正
```

插件会调用哔咔站内搜索，默认返回前 5 条结果：

```text
找到这些哔咔结果：
1. ...
ID: ...
链接：...
作者: ...
分类: ...
标签: ...
信息: ...
```

首次搜索前发送：

```text
/bklogin 用户名 密码
```

这个登录命令只有 AstrBot 管理员能调用，普通成员调用不会响应。登录成功后插件会缓存 token 到插件数据目录；如果 token 过期，再由管理员重新执行 `/bklogin` 即可。

下载搜索结果时使用返回的 24 位漫画 ID：

```text
/对的 64f1a2b3c4d5e6f789012345
```

兼容旧命令：`/bkdl 64f1a2b3c4d5e6f789012345`

插件会依次读取详情、所有分话和每个分话的图片页，下载后生成加密 PDF。PDF 保存在插件数据目录的 `downloads/bk_{id}/bk_{id}.pdf`，密码会随文件一起返回。

下载哔咔 PDF 时，代理需要能访问：

```text
picaapi.picacomic.com
img.picacomic.com
storage-b.picacomic.com
storage1.picacomic.com
```

相关配置：

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `pica.proxy` | 哔咔 API 和图片下载代理，例如 `http://127.0.0.1:7890` | 空 |
| `pica.max_results` | `/bk` 最多返回结果数 | `5` |
| `pica.download_enabled` | 是否允许通过 `/对的 <24位ID>` 下载哔咔漫画 | `true` |
| `pica.max_download_pages` | 哔咔整本最大下载页数 | `300` |
| `pica.download_retries` | 哔咔单图下载重试次数 | `2` |
