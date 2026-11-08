# 网易云点歌 · astrbot_plugin_netease_music

> 直接对接网易云音乐 Web API 的 AstrBot 点歌插件。支持 Cookie 播放 VIP 歌曲、音乐卡片、语音分片与音频文件。

![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.10.4-blue)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)
![License](https://img.shields.io/badge/License-MIT-green)

## 为什么重写

旧版依赖第三方聚合接口，网易云 VIP 歌曲经常拿不到播放链接，只能退回 QQ 音乐的几十秒试听。

这个版本不再经过聚合 API，而是直接调用网易云自己的接口：

- 搜索、歌曲详情、播放地址都直接请求 `music.163.com`
- 可选填写自己的网易云 Cookie（`MUSIC_U`）：账号本身有会员 / 播放权限时，**VIP 歌曲可播放完整版**
- 不填 Cookie 也能播放免费歌曲
- 不再自动切 QQ 音乐

## ✨ 特性

- 🔍 网易云搜索、候选选歌；cloudsearch / search-get 双接口自动切换
- 🍪 Cookie 播放 VIP 歌曲（完整版，不是试听）
- 🎵 音乐卡片：自定义卡片 / 网易云原生卡片可选
- 🎤 超长语音自动分片，分片失败先整段语音，再失败才发文件
- 📁 链接、语音、音频文件多种发送方式
- 🤖 指令结束自动 `stop_event`，不会再触发 AI 回复

## 📦 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/xZhongjie/astrbot_plugin_netease_music.git
```

重启 AstrBot，或在 WebUI 插件页重载。

依赖只有 `aiohttp`（AstrBot 已内置）。语音分片需要服务器安装 `ffmpeg` / `ffprobe`。

## 🍪 配置 Cookie（播放 VIP 必需）

1. 浏览器打开 <https://music.163.com> 并登录；
2. 按 `F12` → **Application / 存储** → **Cookies** → `https://music.163.com`；
3. 找到名为 `MUSIC_U` 的 Cookie，复制它的值；
4. 打开 AstrBot WebUI →「插件」→「网易云点歌」→「配置」；
5. 把值填到 `cookie` 项，保存并重载插件。

`cookie` 支持三种写法：

```text
MUSIC_U=xxxxx
```

```text
xxxxx
```

```text
MUSIC_U=xxxxx; __csrf=yyyyy
```

填好后发送 `/网易云状态`，会显示「Cookie：已配置（可播放 VIP）」。

> Cookie 等同账号登录凭据，只保存在本机配置文件里，不要发给别人。能否播放 VIP 取决于该账号本身是否有会员 / 播放权限。

## 🚀 使用

| 指令 | 说明 |
| --- | --- |
| `/点歌 歌名` | 搜索并列出候选 |
| `/点歌 序号` | 播放候选 |
| `/选歌 序号` | 同上（别名 `/选`） |
| `/直接点歌 歌名` | 直接播放第一首 |
| `/语音点歌 歌名` | 以语音条发送 |
| `/文件点歌 歌名` | 以音频文件发送 |
| `/网易云状态` | 查看 Cookie / 音质 / 分片配置 |
| `/点歌帮助` | 帮助 |

指令末尾可临时指定发送方式：

```text
/点歌 小情歌 语音
/点歌 小情歌 文件
/点歌 小情歌 卡片
/选歌 1 链接
```

### 示例

```text
用户：/点歌 小情歌
Bot：🎵 网易云点歌
     🔍 搜索：小情歌（共 5 首）

     1. 小情歌 (苏打绿版) - 苏打绿
        《小宇宙（苏打绿版）》 · 4:36 · VIP
     ...

用户：/点歌 1
Bot：🎵 小情歌 (苏打绿版)
     👤 歌手：苏打绿
     💿 专辑：小宇宙（苏打绿版）
     🎧 极高 320k · 4:36 · 9.8MB
     🔗 https://music.163.com/#/song?id=1952285469
     [音乐卡片]
```

## ⚙️ 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `cookie` | 空 | 网易云 `MUSIC_U`，填了才能播 VIP |
| `quality` | `exhigh` | `standard` / `higher` / `exhigh` / `lossless` |
| `card_type` | `custom` | `custom` 直接播放音频；`native` 网易云原生卡片 |
| `send_mode` | `auto` | `auto` / `card` / `link` / `record` / `file` |
| `auto_play_single` | `true` | 只命中一首时直接播放 |
| `search_limit` | `5` | 候选数量 1-10 |
| `voice_max_seconds` | `60` | 单段语音最长秒数，超出自动分片 |
| `voice_split` | `true` | 是否启用语音分片 |
| `selection_ttl` | `120` | 候选有效期（秒） |
| `request_timeout` | `15` | 接口超时 |
| `proxy` | 空 | 请求网易云时使用的 HTTP 代理 |
| `meting_api` | 空 | 可选，额外解析接口兜底 |
| `enable_public_meting` | `true` | 直连失败时尝试公共 Meting 实例（会向第三方发送歌曲 ID） |
| `show_cover` | `false` | 链接模式是否附带封面 |

## 🎤 语音分片

歌曲超过 `voice_max_seconds` 时：
1. 下载音频并用 ffmpeg 切成多段 16k 单声道 wav；
2. 依次发送「第 1/N 段」「第 2/N 段」……；
3. 分片失败会先尝试整段语音，再失败才改发音频文件。

需要 AstrBot 所在环境安装 `ffmpeg` 和 `ffprobe`。Docker 部署要装进容器。

## ❓ 常见问题

**Q：还是提示拿不到播放地址？**

A：先看 `/网易云状态` 是否显示 Cookie 已配置。VIP 歌曲必须用自己的 Cookie；Cookie 过期后重新复制即可。免费歌曲不填 Cookie 也能播放。v1.0.2 起，直连接口失败会依次尝试 `interface` 主机、老版 `br` 接口和公共 Meting 兜底；如果服务器 IP 被网易云风控，建议配置自己的 Meting 接口。

**Q：音乐卡片点不开？**

A：把 `card_type` 改为 `custom`（默认），它会把音频地址直接写进卡片；如果平台不支持自定义卡片，改用 `send_mode=link`。

**Q：语音发不出来？**

A：确认服务器有 `ffmpeg` / `ffprobe`，并检查日志里的错误。没有 ffmpeg 时插件会自动改发音频文件。

**Q：会触发 AI 回复吗？**

A：不会。所有点歌指令处理完后都会调用 `event.stop_event()`，阻止事件继续进入 LLM 链路。

## 📄 声明

- 本插件直接请求网易云公开 Web 接口，不存储、不分发音频文件。
- Cookie 由用户自行提供，仅用于本人账号的播放权限。
- 请遵守网易云音乐服务条款，不要用于商业用途或大规模抓取。

## License

[MIT](LICENSE)
