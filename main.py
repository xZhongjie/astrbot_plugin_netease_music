"""astrbot_plugin_netease_music —— 直接对接网易云音乐的点歌插件。

特点：
* 不依赖落月等第三方聚合接口，搜索 / 详情 / 取链直接调用网易云 Web API；
* 可选填写网易云 Cookie（MUSIC_U），填写后可播放 VIP 歌曲；
* 支持音乐卡片、链接、语音（超长自动分片）、音频文件；
* 指令处理结束后停止事件，不会再触发 AI 回复。

使用前请确认你有权播放相应歌曲。
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_netease_music"
PLUGIN_VERSION = "v1.0.0"
API_BASE = "https://music.163.com"

QUALITY_LEVELS = {
    "standard": ("standard", "mp3", "128k"),
    "higher": ("higher", "mp3", "192k"),
    "exhigh": ("exhigh", "mp3", "320k"),
    "lossless": ("lossless", "flac", "无损"),
}

QUALITY_LABELS = {
    "standard": "标准 128k",
    "higher": "较高 192k",
    "exhigh": "极高 320k",
    "lossless": "无损",
}

CARD_LABELS = {
    "custom": "自定义音乐卡片",
    "native": "网易云原生卡片",
}

MODE_LABELS = {
    "card": "音乐卡片",
    "link": "播放链接",
    "record": "语音",
    "file": "文件",
}

HELP_TEXT = """🎵 网易云点歌 使用说明

/点歌 歌名 —— 搜索歌曲并列出候选
/点歌 序号 —— 播放候选中对应序号的歌曲
/选歌 序号 —— 同上
/直接点歌 歌名 —— 直接播放搜索结果第一首
/语音点歌 歌名 —— 以语音条发送（超长自动分片）
/文件点歌 歌名 —— 以音频文件发送
/网易云状态 —— 查看 Cookie / 音质配置
/点歌帮助 —— 显示本帮助

在指令末尾加发送方式可临时覆盖：
/点歌 晴天 语音、/选歌 1 文件、/点歌 晴天 卡片、/点歌 晴天 链接

示例：/点歌 小情歌
VIP 歌曲需要在插件配置中填写网易云 Cookie（MUSIC_U）。"""


class NeteaseError(Exception):
    """网易云接口返回的业务错误。"""


def _cfg(config: AstrBotConfig | dict | None, key: str, default):
    if not config:
        return default
    try:
        value = config.get(key, default)
    except Exception:  # noqa: BLE001 - 防御异常配置对象
        return default
    return default if value is None else value


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _stop_ai_reply(handler):
    """命令处理结束后阻止事件继续进入默认 LLM 链路。"""

    @functools.wraps(handler)
    async def wrapper(self, event):
        try:
            async for result in handler(self, event):
                yield result
        finally:
            event.stop_event()

    return wrapper


def _format_duration(ms: int) -> str:
    seconds = max(0, int(ms / 1000))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _format_size(num: int) -> str:
    if not num:
        return ""
    if num >= 1024 * 1024:
        return f"{num / 1024 / 1024:.1f}MB"
    return f"{num / 1024:.0f}KB"


def _parse_duration_text(value: str) -> int | None:
    """解析 ffprobe 返回的秒数或 4分29秒 形式。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        pass
    match = re.fullmatch(r"(?:(\d+)分)?(?:(\d+)秒)?", text)
    if match and any(match.groups()):
        return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)
    return None


@dataclass
class Song:
    song_id: str
    title: str
    singer: str = ""
    album: str = ""
    duration_ms: int = 0
    fee: int = 0
    cover: str = ""
    alias: str = ""

    @property
    def is_vip(self) -> bool:
        return self.fee == 1

    @classmethod
    def from_search(cls, data: dict) -> Song:
        artists = data.get("artists") or []
        album = data.get("album") or {}
        alias = data.get("alias") or []
        return cls(
            song_id=str(data.get("id") or ""),
            title=str(data.get("name") or "").strip(),
            singer="/".join(str(a.get("name") or "") for a in artists if a.get("name")),
            album=str(album.get("name") or "").strip(),
            duration_ms=_as_int(data.get("duration"), 0),
            fee=_as_int(data.get("fee"), 0),
            cover=str(album.get("picUrl") or album.get("blurPicUrl") or "").strip(),
            alias=" / ".join(str(a) for a in alias if a),
        )


@dataclass
class SearchSession:
    keyword: str
    songs: list[Song]
    created_at: float
    mode: str | None = None


class NeteaseClient:
    """网易云 Web API 客户端。"""

    def __init__(self, config: AstrBotConfig | dict | None):
        self.config = config
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    @property
    def timeout(self) -> int:
        return max(5, _as_int(_cfg(self.config, "request_timeout", 15), 15))

    @property
    def proxy(self) -> str:
        return str(_cfg(self.config, "proxy", "") or "").strip()

    @property
    def cookie(self) -> str:
        raw = str(_cfg(self.config, "cookie", "") or "").strip()
        if not raw:
            return ""
        # 只粘贴 MUSIC_U 的值时自动补全
        if "=" not in raw:
            return f"MUSIC_U={raw}"
        return raw

    @property
    def quality(self) -> str:
        value = str(_cfg(self.config, "quality", "exhigh")).lower()
        return value if value in QUALITY_LEVELS else "exhigh"

    @property
    def card_type(self) -> str:
        value = str(_cfg(self.config, "card_type", "custom")).lower()
        return value if value in CARD_LABELS else "custom"

    def _headers(self) -> dict:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Referer": "https://music.163.com/",
            "Accept": "application/json, text/plain, */*",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _request(self, path: str, params: dict) -> dict:
        url = f"{API_BASE}{path}"
        try:
            session = await self._get_session()
            async with session.get(
                url,
                params=params,
                headers=self._headers(),
                proxy=self.proxy or None,
            ) as response:
                text = await response.text()
                if response.status != 200:
                    raise NeteaseError(f"网易云接口返回 HTTP {response.status}")
                try:
                    data = json.loads(text)
                except ValueError:
                    raise NeteaseError("网易云接口返回了无法解析的数据") from None
                if not isinstance(data, dict):
                    raise NeteaseError("网易云接口返回格式异常")
                code = data.get("code")
                if code is not None and code not in (0, 200):
                    raise NeteaseError(str(data.get("message") or f"接口错误码 {code}"))
                return data
        except NeteaseError:
            raise
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise NeteaseError(f"无法连接网易云：{exc}") from exc

    async def search(self, keyword: str, limit: int) -> list[Song]:
        data = await self._request(
            "/api/search/get/web",
            {
                "s": keyword,
                "type": 1,
                "offset": 0,
                "limit": limit,
                "total": "true",
            },
        )
        result = data.get("result") or {}
        songs = result.get("songs") or []
        return [
            Song.from_search(item)
            for item in songs
            if isinstance(item, dict) and item.get("id")
        ]

    async def detail(self, song: Song) -> Song:
        """补全专辑、封面、时长等信息。"""
        try:
            data = await self._request("/api/song/detail", {"ids": f"[{song.song_id}]"})
        except NeteaseError:
            return song
        songs = data.get("songs") or []
        if not songs:
            return song
        item = songs[0]
        album = item.get("album") or {}
        artists = item.get("artists") or []
        return Song(
            song_id=song.song_id,
            title=str(item.get("name") or song.title),
            singer="/".join(str(a.get("name") or "") for a in artists if a.get("name"))
            or song.singer,
            album=str(album.get("name") or song.album),
            duration_ms=_as_int(item.get("duration"), song.duration_ms),
            fee=_as_int(item.get("fee"), song.fee),
            cover=str(album.get("picUrl") or album.get("blurPicUrl") or song.cover),
            alias=" / ".join(str(a) for a in (item.get("alias") or [])),
        )

    async def _song_url(self, song_id: str, level: str) -> dict | None:
        if level not in QUALITY_LEVELS:
            level = "exhigh"
        api_level, encode_type, _ = QUALITY_LEVELS[level]
        data = await self._request(
            "/api/song/enhance/player/url/v1",
            {
                "ids": f"[{song_id}]",
                "level": api_level,
                "encodeType": encode_type,
            },
        )
        items = data.get("data") or []
        if not items or not isinstance(items[0], dict):
            return None
        item = items[0]
        url = str(item.get("url") or "").strip()
        if not url:
            return None
        trial = item.get("freeTrialInfo")
        return {
            "url": url,
            "quality": QUALITY_LABELS.get(level, level),
            "size": _as_int(item.get("size"), 0),
            "trial": bool(trial),
            "level": level,
        }

    async def _probe_audio(self, url: str) -> bool:
        try:
            session = await self._get_session()
            timeout = aiohttp.ClientTimeout(total=min(self.timeout, 8))
            async with session.get(
                url,
                headers={
                    "Range": "bytes=0-1",
                    "User-Agent": "Mozilla/5.0 (compatible; AstrBot)",
                    "Referer": "https://music.163.com/",
                },
                allow_redirects=True,
                timeout=timeout,
            ) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                return response.status in (200, 206) and (
                    "audio/" in content_type or "octet-stream" in content_type
                )
        except (TimeoutError, aiohttp.ClientError):
            return False

    async def _outer_url(self, song: Song) -> dict | None:
        url = f"https://music.163.com/song/media/outer/url?id={song.song_id}.mp3"
        if not await self._probe_audio(url):
            return None
        return {
            "url": url,
            "quality": "标准 128k（免费线路）",
            "size": 0,
            "trial": False,
            "level": "standard",
        }

    async def _meting_url(self, song: Song) -> dict | None:
        template = str(_cfg(self.config, "meting_api", "") or "").strip()
        if not template:
            return None
        try:
            url = template.format(
                id=song.song_id,
                source="netease",
                quality=self.quality,
            )
        except (KeyError, ValueError):
            logger.warning("meting_api 模板占位符不正确")
            return None

        if await self._probe_audio(url):
            return {
                "url": url,
                "quality": "Meting 解析",
                "size": 0,
                "trial": False,
                "level": "custom",
            }

        try:
            session = await self._get_session()
            async with session.get(
                url,
                headers=self._headers(),
                proxy=self.proxy or None,
            ) as response:
                text = (await response.text()).strip()
        except (TimeoutError, aiohttp.ClientError) as exc:
            logger.warning("Meting 请求失败：%s", exc)
            return None

        audio_url = ""
        try:
            data = json.loads(text)
        except ValueError:
            if text.startswith(("http://", "https://")):
                audio_url = text
        else:
            if isinstance(data, dict):
                value = data.get("url")
                if not value:
                    value = data.get("data")
                    if isinstance(value, dict):
                        value = value.get("url")
                if isinstance(value, str):
                    audio_url = value.strip()
        if not audio_url or not await self._probe_audio(audio_url):
            return None
        return {
            "url": audio_url,
            "quality": "Meting 解析",
            "size": 0,
            "trial": False,
            "level": "custom",
        }

    async def play_info(self, song: Song) -> dict:
        """获取可播放链接，优先使用 Cookie 对应音质，失败时依次降级。"""
        order = ["lossless", "exhigh", "higher", "standard"]
        start = order.index(self.quality) if self.quality in order else 1
        levels = order[start:]

        for level in levels:
            info = await self._song_url(song.song_id, level)
            if info:
                return info

        meting = await self._meting_url(song)
        if meting:
            return meting

        outer = await self._outer_url(song)
        if outer:
            return outer

        if song.is_vip and not self.cookie:
            raise NeteaseError(
                "这首歌是 VIP 歌曲，当前没有配置网易云 Cookie，免费接口拿不到播放地址"
            )
        raise NeteaseError("网易云没有返回可用的播放地址（可能受版权或账号限制）")

    async def download(self, url: str, dest: Path) -> None:
        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60)
        try:
            async with session.get(
                url,
                headers=self._headers(),
                proxy=self.proxy or None,
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    raise NeteaseError(f"音频下载失败（HTTP {response.status}）")
                with dest.open("wb") as file:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        file.write(chunk)
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise NeteaseError(f"音频下载失败：{exc}") from exc


def _build_music_card(song: Song, audio: str, native: bool = False):
    """构造 OneBot 音乐卡片。

    自定义卡片直接用音频地址，试听 / VIP Cookie 链接都能播放；
    原生 163 卡片只带歌曲 id，交给 QQ 客户端处理。
    """
    card = Comp.Music()
    if native:
        object.__setattr__(card, "id", int(song.song_id))
        object.__setattr__(card, "_type", "163")
        return card
    if not audio:
        return None
    object.__setattr__(card, "_type", "custom")
    object.__setattr__(card, "url", f"https://music.163.com/#/song?id={song.song_id}")
    object.__setattr__(card, "audio", audio)
    object.__setattr__(card, "title", f"{song.title} - {song.singer}".strip(" -"))
    object.__setattr__(card, "image", song.cover or "")
    return card


def _safe_filename(song: Song, url: str) -> str:
    suffix = PurePosixPath(urlparse(url).path).suffix
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,5}", suffix or ""):
        suffix = ".mp3"
    stem = f"{song.title} - {song.singer}".strip(" -").replace("/", "_")
    stem = re.sub(r'[\\:*?"<>|\r\n]+', "_", stem) or "song"
    return f"{stem[:80]}{suffix}"


def format_search_result(
    keyword: str,
    songs: list[Song],
    ttl: int,
    has_cookie: bool,
    mode: str | None = None,
) -> str:
    lines = [
        "🎵 网易云点歌",
        f"🔍 搜索：{keyword}（共 {len(songs)} 首）",
    ]
    if not has_cookie:
        lines.append("⚠️ 未配置 Cookie，VIP 歌曲可能无法播放")
    if mode:
        lines.append(f"本次发送方式：{MODE_LABELS.get(mode, mode)}")
    lines.append("")

    for index, song in enumerate(songs, 1):
        head = f"{index}. {song.title}"
        if song.singer:
            head += f" - {song.singer}"
        lines.append(head)

        meta: list[str] = []
        if song.album:
            meta.append(f"《{song.album}》")
        if song.duration_ms:
            meta.append(_format_duration(song.duration_ms))
        if song.is_vip:
            meta.append("VIP")
        if meta:
            lines.append("    " + " · ".join(meta))

    lines.extend(["", f"回复「/点歌 序号」或「/选歌 序号」播放，{ttl} 秒内有效。"])
    return "\n".join(lines)


def format_play_info(song: Song, play: dict, extra_note: str = "") -> str:
    lines = [f"{extra_note}🎵 {song.title}"]
    if song.singer:
        lines.append(f"👤 歌手：{song.singer}")
    if song.album:
        lines.append(f"💿 专辑：{song.album}")

    detail: list[str] = []
    if play.get("quality"):
        detail.append(str(play["quality"]))
    if song.duration_ms:
        detail.append(_format_duration(song.duration_ms))
    if play.get("size"):
        detail.append(_format_size(_as_int(play["size"], 0)))
    if play.get("trial"):
        detail.append("试听")
    if detail:
        lines.append("🎧 " + " · ".join(detail))

    lines.append(f"🔗 https://music.163.com/#/song?id={song.song_id}")
    return "\n".join(lines)


class NeteaseMusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.client = NeteaseClient(self.config)
        self._sessions: dict[str, SearchSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        logger.info(
            "网易云点歌插件 %s 已加载，Cookie：%s",
            PLUGIN_VERSION,
            "已配置" if self.client.cookie else "未配置",
        )

    async def terminate(self):
        await self.client.close()

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _limit(self) -> int:
        return min(10, max(1, _as_int(_cfg(self.config, "search_limit", 5), 5)))

    def _ttl(self) -> int:
        return min(600, max(15, _as_int(_cfg(self.config, "selection_ttl", 120), 120)))

    def _auto_play_single(self) -> bool:
        return bool(_cfg(self.config, "auto_play_single", True))

    def _voice_max_seconds(self) -> int:
        return max(
            0,
            _as_int(_cfg(self.config, "voice_max_seconds", 60), 60),
        )

    def _voice_split(self) -> bool:
        return bool(_cfg(self.config, "voice_split", True))

    def _show_cover(self) -> bool:
        return bool(_cfg(self.config, "show_cover", False))

    def _send_mode(self, event: AstrMessageEvent, explicit: str | None = None) -> str:
        mode = (explicit or "").lower() or str(
            _cfg(self.config, "send_mode", "auto")
        ).lower()
        platform = event.get_platform_name()
        if mode == "auto":
            return "card" if platform == "aiocqhttp" else "link"
        if mode == "card" and platform != "aiocqhttp":
            return "link"
        if mode not in {"card", "link", "record", "file"}:
            return "link"
        return mode

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}::{event.get_sender_id()}"

    def _prune_sessions(self) -> None:
        now = time.time()
        ttl = self._ttl()
        expired = [
            key
            for key, session in self._sessions.items()
            if now - session.created_at > ttl
        ]
        for key in expired:
            self._sessions.pop(key, None)
        if len(self._sessions) > 300:
            oldest = sorted(
                self._sessions.items(), key=lambda item: item[1].created_at
            )[: len(self._sessions) - 300]
            for key, _ in oldest:
                self._sessions.pop(key, None)

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    @filter.command("点歌", alias={"网易云点歌"})
    @_stop_ai_reply
    async def cmd_request(self, event: AstrMessageEvent):
        """点歌：/点歌 歌名 或 /点歌 序号。"""
        keyword, mode = self._split_mode(
            self._extract_args(event, ("点歌", "网易云点歌"))
        )
        if not keyword:
            yield event.plain_result(HELP_TEXT)
            return
        if keyword.isdigit():
            components = await self._select_song(event, int(keyword), mode)
            if components:
                yield event.chain_result(components)
            return
        components = await self._search(event, keyword, mode=mode)
        if components:
            yield event.chain_result(components)

    @filter.command("选歌", alias={"选"})
    @_stop_ai_reply
    async def cmd_select(self, event: AstrMessageEvent):
        """选择候选：/选歌 序号 [语音|文件|卡片|链接]。"""
        args, mode = self._split_mode(self._extract_args(event, ("选歌", "选")))
        if not args.isdigit():
            yield event.plain_result(
                "用法：/选歌 序号 [语音|文件|卡片|链接]\n"
                "请先发送「/点歌 歌名」获取候选列表。"
            )
            return
        components = await self._select_song(event, int(args), mode)
        if components:
            yield event.chain_result(components)

    @filter.command("直接点歌", alias={"点播", "来一首"})
    @_stop_ai_reply
    async def cmd_direct(self, event: AstrMessageEvent):
        """直接播放第一首：/直接点歌 歌名 [语音|文件]。"""
        keyword, mode = self._split_mode(
            self._extract_args(event, ("直接点歌", "点播", "来一首"))
        )
        if not keyword:
            yield event.plain_result(
                "用法：/直接点歌 歌名 [语音|文件]\n示例：/直接点歌 小情歌"
            )
            return
        components = await self._search(event, keyword, direct=True, mode=mode)
        if components:
            yield event.chain_result(components)

    @filter.command("语音点歌", alias={"语音点播"})
    @_stop_ai_reply
    async def cmd_voice(self, event: AstrMessageEvent):
        """以语音条发送：/语音点歌 歌名。"""
        keyword = self._extract_args(event, ("语音点歌", "语音点播"))
        if not keyword:
            yield event.plain_result(
                "用法：/语音点歌 歌名\n示例：/语音点歌 小情歌\n"
                "注意：语音模式需要服务器安装 ffmpeg。"
            )
            return
        components = await self._search(event, keyword, direct=True, mode="record")
        if components:
            yield event.chain_result(components)

    @filter.command("文件点歌", alias={"文件点播"})
    @_stop_ai_reply
    async def cmd_file(self, event: AstrMessageEvent):
        """以音频文件发送：/文件点歌 歌名。"""
        keyword = self._extract_args(event, ("文件点歌", "文件点播"))
        if not keyword:
            yield event.plain_result("用法：/文件点歌 歌名\n示例：/文件点歌 小情歌")
            return
        components = await self._search(event, keyword, direct=True, mode="file")
        if components:
            yield event.chain_result(components)

    @filter.command("网易云状态", alias={"点歌状态"})
    @_stop_ai_reply
    async def cmd_status(self, event: AstrMessageEvent):
        """查看 Cookie 与音质配置。"""
        cookie = self.client.cookie
        quality = QUALITY_LABELS.get(self.client.quality, self.client.quality)
        card = CARD_LABELS.get(self.client.card_type, self.client.card_type)
        lines = [
            f"🎧 网易云点歌 {PLUGIN_VERSION}",
            f"Cookie：{'已配置（可播放 VIP）' if cookie else '未配置（仅免费歌曲）'}",
            f"音质：{quality}",
            f"卡片：{card}",
            (
                f"语音分片：{'开启' if self._voice_split() else '关闭'}"
                f"（单段 {self._voice_max_seconds()} 秒）"
            ),
        ]
        if not cookie:
            lines.append(
                "💡 在 WebUI 插件配置的 cookie 项填入 MUSIC_U 即可播放 VIP 歌曲。"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("点歌帮助", alias={"点歌菜单", "点歌说明"})
    @_stop_ai_reply
    async def cmd_help(self, event: AstrMessageEvent):
        """显示帮助。"""
        yield event.plain_result(HELP_TEXT)

    # ------------------------------------------------------------------
    # 参数
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_args(event: AstrMessageEvent, names: tuple[str, ...]) -> str:
        text = (event.message_str or "").strip()
        if not text:
            return ""
        alternatives = "|".join(
            re.escape(name) for name in sorted(names, key=len, reverse=True)
        )
        match = re.search(rf"(?:^|\s)(?:{alternatives})(?:\s+|$)", text)
        if not match:
            return ""
        return text[match.end() :].strip()

    @staticmethod
    def _split_mode(text: str) -> tuple[str, str | None]:
        parts = text.rsplit(maxsplit=1)
        if len(parts) == 2:
            mode = {
                "语音": "record",
                "voice": "record",
                "record": "record",
                "文件": "file",
                "file": "file",
                "卡片": "card",
                "card": "card",
                "链接": "link",
                "link": "link",
            }.get(parts[1].strip().lower())
            if mode:
                return parts[0].strip(), mode
        return text.strip(), None

    # ------------------------------------------------------------------
    # 业务
    # ------------------------------------------------------------------
    async def _search(
        self,
        event: AstrMessageEvent,
        keyword: str,
        direct: bool = False,
        mode: str | None = None,
    ) -> list:
        self._prune_sessions()
        try:
            songs = await self.client.search(keyword, self._limit())
        except NeteaseError as exc:
            return [Comp.Plain(f"❌ 搜索失败：{exc}")]
        except Exception:  # noqa: BLE001
            logger.exception("搜索网易云歌曲失败")
            return [Comp.Plain("❌ 搜索失败：插件内部错误，请查看日志。")]

        if not songs:
            return [Comp.Plain(f"🔍 没有找到「{keyword}」相关的歌曲。")]

        if direct or (self._auto_play_single() and len(songs) == 1):
            return await self._play(event, songs[0], mode)

        self._sessions[self._session_key(event)] = SearchSession(
            keyword=keyword,
            songs=songs,
            created_at=time.time(),
            mode=mode,
        )
        return [
            Comp.Plain(
                format_search_result(
                    keyword,
                    songs,
                    self._ttl(),
                    bool(self.client.cookie),
                    mode,
                )
            )
        ]

    async def _select_song(
        self,
        event: AstrMessageEvent,
        index: int,
        mode: str | None = None,
    ) -> list:
        self._prune_sessions()
        session = self._sessions.get(self._session_key(event))
        if session is None:
            return [Comp.Plain("⚠️ 当前没有待选择的歌曲，请先发送「/点歌 歌名」。")]
        if not 1 <= index <= len(session.songs):
            return [
                Comp.Plain(
                    f"⚠️ 序号超出范围，请输入 1 - {len(session.songs)} 之间的数字。"
                )
            ]
        return await self._play(event, session.songs[index - 1], mode or session.mode)

    async def _play(
        self,
        event: AstrMessageEvent,
        song: Song,
        mode: str | None = None,
    ) -> list:
        key = self._session_key(event)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                song = await self.client.detail(song)
            except NeteaseError:
                pass

            try:
                play = await self.client.play_info(song)
            except NeteaseError as exc:
                return self._error_chain(song, exc)
            except Exception:  # noqa: BLE001
                logger.exception("获取网易云播放地址失败")
                return [Comp.Plain("❌ 播放失败：插件内部错误，请查看日志。")]

            self._sessions.pop(key, None)
            return await self._finalize_play(event, song, play, mode)

    def _error_chain(self, song: Song, error: Exception) -> list:
        lines = [f"❌ 播放失败：{error}"]
        lines.append(f"🔗 https://music.163.com/#/song?id={song.song_id}")
        if song.is_vip and not self.client.cookie:
            lines.append(
                "💡 这是 VIP 歌曲：请在 WebUI 插件配置的 cookie 项填入网易云 "
                "MUSIC_U，保存后重载插件。"
            )
        else:
            lines.append("💡 可以换一首歌，或检查 Cookie 是否已过期。")
        return [Comp.Plain("\n".join(lines))]

    async def _finalize_play(
        self,
        event: AstrMessageEvent,
        song: Song,
        play: dict,
        mode: str | None = None,
    ) -> list:
        if self._send_mode(event, mode) == "record":
            components = await self._maybe_split_voice(event, song, play, mode)
            if components is not None:
                return components
            if await self._try_single_voice(event, song, play):
                return []
        return self._build_chain(event, song, play, mode)

    def _build_chain(
        self,
        event: AstrMessageEvent,
        song: Song,
        play: dict,
        mode: str | None = None,
    ) -> list:
        audio = str(play.get("url") or "").strip()
        if not audio:
            return [Comp.Plain("❌ 播放地址为空，请稍后再试。")]

        info = format_play_info(song, play)
        mode = self._send_mode(event, mode)
        note = ""
        if mode == "record" and shutil.which("ffmpeg") is None:
            mode = "file"
            note = "\n⚠️ 未检测到 ffmpeg，语音已改为音频文件发送。"
        components: list = [Comp.Plain(info + note)]

        if mode == "card":
            card = _build_music_card(
                song, audio, native=self.client.card_type == "native"
            )
            if card is not None:
                components.append(card)
            else:
                mode = "link"
        elif mode == "record":
            components.append(Comp.Record(file=audio, url=audio))
        elif mode == "file":
            components.append(Comp.File(name=_safe_filename(song, audio), url=audio))

        if mode == "link" and self._show_cover() and song.cover:
            components.insert(0, Comp.Image.fromURL(song.cover))
        return components

    # ------------------------------------------------------------------
    # 语音分片
    # ------------------------------------------------------------------
    async def _maybe_split_voice(
        self,
        event: AstrMessageEvent,
        song: Song,
        play: dict,
        mode: str | None = None,
    ) -> list | None:
        limit = self._voice_max_seconds()
        if limit <= 0 or not self._voice_split():
            return None
        if shutil.which("ffmpeg") is None:
            return None

        audio = str(play.get("url") or "").strip()
        if not audio:
            return None

        seconds = int(song.duration_ms / 1000) if song.duration_ms else None
        if not seconds:
            seconds = await self._probe_duration(audio)
        if not seconds or seconds <= limit:
            return None

        try:
            return await self._send_split_voice(event, song, play, limit, mode)
        except Exception:  # noqa: BLE001
            logger.exception("语音分片失败，尝试整段语音")
            play["_split_failed"] = True
            return None

    async def _try_single_voice(
        self,
        event: AstrMessageEvent,
        song: Song,
        play: dict,
    ) -> bool:
        if shutil.which("ffmpeg") is None:
            return False
        limit = self._voice_max_seconds()
        seconds = int(song.duration_ms / 1000) if song.duration_ms else None
        if not play.get("_split_failed") and (
            limit <= 0 or not seconds or seconds <= limit
        ):
            return False

        audio = str(play.get("url") or "").strip()
        if not audio:
            return False
        info = format_play_info(song, play) + "\n🎤 已按整段语音发送。"
        try:
            await event.send(
                event.chain_result(
                    [Comp.Plain(info), Comp.Record(file=audio, url=audio)]
                )
            )
            return True
        except Exception:  # noqa: BLE001
            logger.exception("整段语音发送失败，改用音频文件")
            return False

    async def _send_split_voice(
        self,
        event: AstrMessageEvent,
        song: Song,
        play: dict,
        limit: int,
        mode: str | None = None,
    ) -> list:
        work_dir = Path(tempfile.mkdtemp(prefix="astrbot_netease_voice_"))
        try:
            source = work_dir / "source"
            await self.client.download(str(play["url"]), source)
            chunks = await self._split_audio(source, limit, work_dir)
            if not chunks:
                raise RuntimeError("没有生成语音分片")

            total = len(chunks)
            header = (
                format_play_info(song, play)
                + f"\n🎤 已按 {limit} 秒分片，共 {total} 段。"
            )
            for index, chunk in enumerate(chunks, 1):
                components = [
                    Comp.Plain(header if index == 1 else f"第 {index}/{total} 段"),
                    Comp.Record(file=str(chunk), url=str(chunk)),
                ]
                await event.send(event.chain_result(components))
                await asyncio.sleep(0.5)
            return []
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _split_audio(
        self, source: Path, seconds: int, work_dir: Path
    ) -> list[Path]:
        out_dir = work_dir / "chunks"
        out_dir.mkdir()
        ffmpeg = shutil.which("ffmpeg")
        pattern = str(out_dir / "part_%03d.wav")
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "segment",
            "-segment_time",
            str(seconds),
            "-reset_timestamps",
            "1",
            pattern,
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", "replace")[-400:])
        return sorted(out_dir.glob("part_*.wav"))

    async def _probe_duration(self, url: str) -> int | None:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return None
        command = [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            url,
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
            return _parse_duration_text(stdout.decode("utf-8", "replace").strip())
        except (OSError, ValueError):
            return None
