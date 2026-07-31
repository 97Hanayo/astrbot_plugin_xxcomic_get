from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Node, Plain
from astrbot.api.star import Context, Star, StarTools, register


PLUGIN_NAME = "astrbot_plugin_xxcomic_get"
SOUTUBOT_HOME_URL = "https://soutubot.moe/"
NHENTAI_API_URL = "https://nhentai.net/api/v2/galleries/{gallery_id}"
NHENTAI_SEARCH_URL = "https://nhentai.net/api/v2/search"
NHENTAI_CDN_URL = "https://nhentai.net/api/v2/cdn"
DEFAULT_TIMEOUT_MS = 60000
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126 Safari/537.36"
)
BLOCKED_NHENTAI_TAGS = {
    "lolicon",
    "shotacon",
    "schoolgirl-uniform",
    "schoolboy-uniform",
}


@dataclass(slots=True)
class SearchResult:
    similarity: float
    title: str
    source: str
    language: str
    page: int | None
    subject_urls: list[str]
    page_urls: list[str]
    preview_url: str


@dataclass(slots=True)
class GalleryDownload:
    gallery_id: str
    media_id: str
    title: str
    image_paths: list[Path]
    metadata_path: Path


class EmptySearchResultError(RuntimeError):
    pass


def _get_data_dir() -> Path:
    try:
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
    except Exception:
        data_dir = Path(__file__).resolve().parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _get_browser_state_file() -> Path:
    state_dir = _get_data_dir() / "cookies"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "soutubot_storage_state.json"


def _get_download_dir(gallery_id: str) -> Path:
    download_dir = _get_data_dir() / "downloads" / gallery_id
    download_dir.mkdir(parents=True, exist_ok=True)
    return download_dir


def _get_config_value(config: AstrBotConfig | dict | None, key: str, default: Any) -> Any:
    if config is None:
        return default
    value: Any = config
    for part in key.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return default
        if value is None:
            return default
    return value


def _coerce_int(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(number, max_value))


def _coerce_float(value: Any, default: float, min_value: float, max_value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(number, max_value))


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "y"}:
            return True
        if lowered in {"0", "false", "no", "off", "n"}:
            return False
    if value is None:
        return default
    return bool(value)


def _normalize_language(value: str | None) -> str:
    if not value:
        return ""
    language_map = {
        "cn": "中文",
        "jp": "日文",
        "gb": "英文",
        "kr": "韩文",
        "Chinese": "中文",
        "Japanese": "日文",
        "English": "英文",
        "Korean": "韩文",
    }
    return language_map.get(value, value)


def _parse_nhentai_gallery_id(urls: list[str]) -> str | None:
    for url in urls:
        match = re.search(r"https?://(?:www\.)?nhentai\.(?:net|xxx)/g/(\d+)", url)
        if match:
            return match.group(1)
    return None


def _extract_command_text(message: str, command: str) -> str:
    text = (message or "").strip()
    if not text:
        return ""
    patterns = [
        rf"^/{re.escape(command)}(?:\s+|$)",
        rf"^{re.escape(command)}(?:\s+|$)",
    ]
    for pattern in patterns:
        text = re.sub(pattern, "", text, count=1).strip()
    return text


def _read_cookie_setting(value: Any) -> str:
    if not value:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        path = Path(text)
        if path.exists() and path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return path.read_text(encoding="utf-8-sig", errors="ignore")
    except (OSError, ValueError):
        pass
    return text


def _cookie_header_from_setting(value: Any) -> str:
    text = _read_cookie_setting(value)
    if not text:
        return ""
    if "\t" not in text and "=" in text:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "; ".join(lines)

    pairs: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        normalized = raw_line.replace("#HttpOnly_", "", 1)
        parts = normalized.split("\t")
        if len(parts) < 7:
            continue
        domain, _, _, _, _, name, cookie_value = parts[:7]
        if "nhentai.net" in domain:
            pairs.append(f"{name}={cookie_value}")
    return "; ".join(pairs)


def _add_nhentai_auth_header(headers: dict[str, str], api_key: Any) -> None:
    key = str(api_key or "").strip()
    if key:
        headers["Authorization"] = f"Key {key}"


def _join_url(base_url: str, path: str) -> str:
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def _request_bytes(url: str, headers: dict[str, str], timeout: float) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("content-type", "")
        return response.read(), content_type


def _load_json_url(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    body, content_type = _request_bytes(url, headers, timeout)
    if "json" not in content_type:
        raise RuntimeError(f"接口没有返回 JSON：{content_type}")
    return json.loads(body.decode("utf-8"))


def _format_result(result: SearchResult, order: int) -> str:
    lines = [f"{order}. 相似度 {result.similarity:.2f}% - {result.title}"]
    if result.source:
        lines.append(f"来源：{result.source}")
    if result.language:
        lines.append(f"语言：{result.language}")
    if result.page is not None:
        lines.append(f"页码：{result.page}")
    if result.subject_urls:
        lines.append(f"详情：{result.subject_urls[0]}")
    if result.page_urls:
        lines.append(f"图片页：{result.page_urls[0]}")
    return "\n".join(lines)


@register(PLUGIN_NAME, "hanayo", "用 SoutuBot 识别图片来源，或用文本搜索 nhentai", "1.0.1")
class XxComicGetPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or context.get_config()
        self._refresh_config()

    def _refresh_config(self) -> None:
        self.min_similarity = _coerce_float(
            _get_config_value(self.config, "soutubot.min_similarity", 45),
            default=45.0,
            min_value=0.0,
            max_value=100.0,
        )
        self.max_results = _coerce_int(
            _get_config_value(self.config, "soutubot.max_results", 3),
            default=3,
            min_value=1,
            max_value=10,
        )
        self.timeout_ms = _coerce_int(
            _get_config_value(self.config, "soutubot.timeout_ms", DEFAULT_TIMEOUT_MS),
            default=DEFAULT_TIMEOUT_MS,
            min_value=10000,
            max_value=180000,
        )
        self.headless = _coerce_bool(_get_config_value(self.config, "soutubot.headless", True), True)
        self.download_enabled = _coerce_bool(
            _get_config_value(self.config, "nhentai.download_enabled", True),
            True,
        )
        self.nhentai_cookies = _get_config_value(self.config, "nhentai.cookies", "")
        self.nhentai_api_key = _get_config_value(self.config, "nhentai.api_key", "")
        self.max_download_pages = _coerce_int(
            _get_config_value(self.config, "nhentai.max_download_pages", 120),
            default=120,
            min_value=1,
            max_value=300,
        )
        self.download_retries = _coerce_int(
            _get_config_value(self.config, "nhentai.download_retries", 2),
            default=2,
            min_value=0,
            max_value=5,
        )
        self.block_risky_tags = _coerce_bool(
            _get_config_value(self.config, "nhentai.block_risky_tags", True),
            True,
        )
        self.send_forward = _coerce_bool(
            _get_config_value(self.config, "nhentai.send_forward", True),
            True,
        )

    def _has_nhentai_api_key(self) -> bool:
        return bool(str(self.nhentai_api_key or "").strip())

    def _require_nhentai_api_key(self) -> str:
        api_key = str(self.nhentai_api_key or "").strip()
        if not api_key:
            raise RuntimeError("未配置api")
        return api_key

    @filter.command("哈哈")
    async def search_comic(self, event: AstrMessageEvent):
        """识别随命令发送的图片来源"""
        image = next(
            (component for component in event.get_messages() if isinstance(component, Image)),
            None,
        )
        if image is None:
            yield event.plain_result("？")
            return

        try:
            image_path = await image.convert_to_file_path()
            results = await self._search_soutubot(Path(image_path))
        except Exception as exc:
            logger.exception("SoutuBot 搜索失败")
            yield event.plain_result(f"识图失败：{exc}")
            return

        if not results:
            yield event.plain_result("空的。")
            return

        matched = [result for result in results if result.similarity >= self.min_similarity]
        if not matched:
            yield event.plain_result(
                f"没找到足够像的结果，最高相似度 {results[0].similarity:.2f}%。"
            )
            return

        best = matched[0]
        gallery_id = _parse_nhentai_gallery_id(best.subject_urls)
        if not self.download_enabled or not gallery_id:
            body = "\n\n".join(
                _format_result(result, index + 1)
                for index, result in enumerate(matched[: self.max_results])
            )
            yield event.plain_result(f"找到了这些可能来源：\n\n{body}")
            return
        if not self._has_nhentai_api_key():
            yield event.plain_result("未配置api")
            return

        try:
            download = await self._download_nhentai_gallery(gallery_id)
        except Exception as exc:
            logger.exception("nhentai 下载失败")
            yield event.plain_result(
                "找到了来源，但下载失败："
                f"{exc}\n\n{_format_result(best, 1)}"
            )
            return

        yield event.plain_result(download.title)
        if self.send_forward:
            try:
                yield self._build_forward_result(event, download)
                return
            except Exception as exc:
                logger.exception("合并转发构造失败，回退为逐张发送")
                yield event.plain_result(f"合并聊天记录发送失败，改为逐张发送：{exc}")

        for image_file in download.image_paths:
            yield event.chain_result([Image.fromFileSystem(str(image_file))])

    @filter.command("嘻嘻")
    async def search_nhentai_text(self, event: AstrMessageEvent):
        """按文本搜索 nhentai 并下载第一个结果"""
        query = _extract_command_text(getattr(event, "message_str", ""), "嘻嘻")
        if not query:
            yield event.plain_result("？")
            return
        if not self.download_enabled:
            yield event.plain_result("当前配置已关闭 nhentai 自动下载。")
            return
        if not self._has_nhentai_api_key():
            yield event.plain_result("未配置api")
            return

        try:
            gallery_id = await self._search_nhentai_first_gallery_id(query)
            download = await self._download_nhentai_gallery(gallery_id)
        except EmptySearchResultError:
            yield event.plain_result("空的。")
            return
        except Exception as exc:
            logger.exception("nhentai 文本搜索或下载失败")
            yield event.plain_result(f"搜索或下载失败：{exc}")
            return

        yield event.plain_result(download.title)
        if self.send_forward:
            try:
                yield self._build_forward_result(event, download)
                return
            except Exception as exc:
                logger.exception("合并转发构造失败，回退为逐张发送")
                yield event.plain_result(f"合并聊天记录发送失败，改为逐张发送：{exc}")

        for image_file in download.image_paths:
            yield event.chain_result([Image.fromFileSystem(str(image_file))])

    async def _search_soutubot(self, image_path: Path) -> list[SearchResult]:
        if not image_path.exists():
            raise FileNotFoundError(f"图片文件不存在: {image_path}")

        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            raise RuntimeError("当前环境没有可用的 Playwright Python 包") from exc

        state_file = _get_browser_state_file()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=self.headless)
            context_kwargs: dict[str, Any] = {}
            if state_file.exists():
                context_kwargs["storage_state"] = str(state_file)
            context = await browser.new_context(**context_kwargs)
            context.set_default_timeout(self.timeout_ms)
            page = await context.new_page()
            try:
                await page.goto(
                    SOUTUBOT_HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )
                await page.wait_for_timeout(3000)
                if await self._is_cloudflare_page(page):
                    raise RuntimeError("SoutuBot 正在进行 Cloudflare 人机验证，请先提供可复用浏览器状态或稍后重试")

                await page.wait_for_selector(
                    'input[type="file"]',
                    state="attached",
                    timeout=self.timeout_ms,
                )
                await page.locator('input[type="file"]').set_input_files(str(image_path))
                await page.wait_for_selector(
                    ".card-2, div.text-center > h3",
                    state="attached",
                    timeout=self.timeout_ms,
                )
                await context.storage_state(path=str(state_file))
                raw_results = await page.eval_on_selector_all(
                    ".card-2",
                    """
                    (cards, maxNeeded) => {
                        const langMap = { cn: "中文", jp: "日文", gb: "英文", kr: "韩文" };
                        return cards.slice(0, maxNeeded).map((card) => {
                            const spans = Array.from(card.querySelectorAll("span"));
                            const percentSpan = spans.find((el) => /\\d+(\\.\\d+)?%/.test(el.textContent || ""));
                            const similarityText = percentSpan?.textContent?.trim().replace("%", "") || "0";
                            const title = card.querySelector(".font-semibold span")?.innerText || "";
                            const preview = card.querySelector('a[target="_blank"] img')?.src || "";
                            const sourceImg = card.querySelector('img[src*="/images/icons/"]');
                            const source = sourceImg ? sourceImg.src.split("/").pop()?.replace(".png", "") : "";
                            const langFlag = card.querySelector('span.fi[class*="fi-"]');
                            const langCode = langFlag
                                ? Array.from(langFlag.classList).find((item) => item.startsWith("fi-"))?.replace("fi-", "")
                                : "";
                            const buttons = Array.from(card.querySelectorAll("a.el-button"));
                            const detail = buttons[0]?.href || "";
                            const image = buttons[1]?.href || "";
                            const pageText = buttons[1]?.innerText || "";
                            const pageMatch = pageText.match(/P(\\d+)/);
                            return {
                                similarity: Number.parseFloat(similarityText) || 0,
                                title,
                                source,
                                language: langCode ? (langMap[langCode] || langCode) : "",
                                page: pageMatch ? Number.parseInt(pageMatch[1], 10) : null,
                                subjectUrls: detail ? [detail] : [],
                                pageUrls: image ? [image] : [],
                                previewUrl: preview,
                            };
                        });
                    }
                    """,
                    self.max_results,
                )
            finally:
                await context.close()
                await browser.close()

        if not isinstance(raw_results, list):
            return []
        parsed = [
            SearchResult(
                similarity=float(item.get("similarity", 0) or 0),
                title=str(item.get("title") or "未知标题").strip(),
                source=str(item.get("source") or "").strip(),
                language=_normalize_language(str(item.get("language") or "").strip()),
                page=item.get("page") if isinstance(item.get("page"), int) else None,
                subject_urls=list(item.get("subjectUrls") or []),
                page_urls=list(item.get("pageUrls") or []),
                preview_url=str(item.get("previewUrl") or ""),
            )
            for item in raw_results
            if isinstance(item, dict)
        ]
        return sorted(
            (result for result in parsed if result is not None),
            key=lambda result: result.similarity,
            reverse=True,
        )

    async def _is_cloudflare_page(self, page: Any) -> bool:
        title = await page.title()
        body_text = ""
        try:
            body_text = await page.locator("body").inner_text(timeout=1000)
        except Exception:
            pass
        has_turnstile = await page.locator('input[name="cf-turnstile-response"]').count()
        combined = f"{title}\n{body_text}"
        return bool(
            has_turnstile
            or re.search(
                r"Just a moment|Checking your browser|Verifying you are human|Cloudflare|Ray ID|安全验证",
                combined,
                re.I,
            )
        )

    async def _download_nhentai_gallery(self, gallery_id: str) -> GalleryDownload:
        return await asyncio.to_thread(self._download_nhentai_gallery_sync, gallery_id)

    async def _search_nhentai_first_gallery_id(self, query: str) -> str:
        return await asyncio.to_thread(self._search_nhentai_first_gallery_id_sync, query)

    def _search_nhentai_first_gallery_id_sync(self, query: str) -> str:
        search_query = query.strip()
        if not search_query:
            raise RuntimeError("搜索文本为空")
        api_key = self._require_nhentai_api_key()

        url = (
            f"{NHENTAI_SEARCH_URL}?"
            f"{urllib.parse.urlencode({'query': search_query, 'sort': 'date', 'page': 1})}"
        )
        cookie_header = _cookie_header_from_setting(self.nhentai_cookies)
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://nhentai.net/search/",
        }
        if cookie_header:
            headers["Cookie"] = cookie_header
        _add_nhentai_auth_header(headers, api_key)

        payload = _load_json_url(url, headers, timeout=max(10.0, self.timeout_ms / 1000))
        raw_results = payload.get("result") or payload.get("results") or payload.get("data")
        if not isinstance(raw_results, list) or not raw_results:
            raise EmptySearchResultError(f"没有搜索到结果：{search_query}")

        first = raw_results[0]
        if not isinstance(first, dict):
            raise RuntimeError("第一个搜索结果格式不正确")
        gallery_id = str(first.get("id") or first.get("gallery_id") or "").strip()
        if not gallery_id:
            raise RuntimeError("第一个搜索结果没有 gallery id")
        return gallery_id

    def _download_nhentai_gallery_sync(self, gallery_id: str) -> GalleryDownload:
        api_key = self._require_nhentai_api_key()
        gallery_dir = _get_download_dir(gallery_id)
        images_dir = gallery_dir / "originals"
        if images_dir.exists():
            shutil.rmtree(images_dir)
        images_dir.mkdir(parents=True, exist_ok=True)

        cookie_header = _cookie_header_from_setting(self.nhentai_cookies)
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Referer": f"https://nhentai.net/g/{gallery_id}/",
        }
        if cookie_header:
            headers["Cookie"] = cookie_header
        _add_nhentai_auth_header(headers, api_key)

        metadata = _load_json_url(
            NHENTAI_API_URL.format(gallery_id=gallery_id),
            headers,
            timeout=max(10.0, self.timeout_ms / 1000),
        )
        cdn_config = _load_json_url(
            NHENTAI_CDN_URL,
            headers,
            timeout=max(10.0, self.timeout_ms / 1000),
        )
        image_servers = cdn_config.get("image_servers")
        if not isinstance(image_servers, list) or not image_servers:
            raise RuntimeError("nhentai API 没有返回可用图片 CDN")
        image_base_url = str(image_servers[0] or "").strip()
        if not image_base_url:
            raise RuntimeError("nhentai API 返回的图片 CDN 为空")

        pages = metadata.get("pages")
        if not isinstance(pages, list) or not pages:
            raise RuntimeError("nhentai API 没有返回可下载页列表")
        if len(pages) > self.max_download_pages:
            raise RuntimeError(
                f"页数 {len(pages)} 超过配置上限 {self.max_download_pages}，已停止下载"
            )

        tags = metadata.get("tags") if isinstance(metadata.get("tags"), list) else []
        tag_slugs = {
            str(tag.get("slug") or tag.get("name") or "").strip().lower()
            for tag in tags
            if isinstance(tag, dict)
        }
        if self.block_risky_tags and tag_slugs.intersection(BLOCKED_NHENTAI_TAGS):
            raise RuntimeError("命中受限标签，已停止自动下载")

        title_info = metadata.get("title") if isinstance(metadata.get("title"), dict) else {}
        title = (
            str(title_info.get("english") or "").strip()
            or str(title_info.get("pretty") or "").strip()
            or f"nhentai {gallery_id}"
        )
        media_id = str(metadata.get("media_id") or "")
        image_paths: list[Path] = []
        image_headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": f"https://nhentai.net/g/{gallery_id}/",
        }
        if cookie_header:
            image_headers["Cookie"] = cookie_header

        downloaded: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for index, item in enumerate(pages, 1):
            if not isinstance(item, dict):
                failures.append({"number": index, "error": "invalid page item"})
                continue
            page_path = str(item.get("path") or "").lstrip("/")
            if not page_path:
                failures.append({"number": index, "error": "empty path"})
                continue
            suffix = Path(urllib.parse.urlparse(page_path).path).suffix or ".webp"
            file_path = images_dir / f"{index:04d}{suffix}"
            image_url = _join_url(image_base_url, page_path)
            last_error: str | None = None
            for attempt in range(self.download_retries + 1):
                try:
                    body, content_type = _request_bytes(
                        image_url,
                        image_headers,
                        timeout=max(10.0, self.timeout_ms / 1000),
                    )
                    if not content_type.startswith("image/"):
                        raise RuntimeError(f"返回内容不是图片：{content_type}")
                    file_path.write_bytes(body)
                    image_paths.append(file_path)
                    downloaded.append(
                        {
                            "file": file_path.name,
                            "bytes": len(body),
                            "width": item.get("width"),
                            "height": item.get("height"),
                            "url": image_url,
                        }
                    )
                    last_error = None
                    break
                except (OSError, urllib.error.URLError, RuntimeError) as exc:
                    last_error = str(exc)
                    if attempt < self.download_retries:
                        time.sleep(0.8 * (attempt + 1))
            if last_error:
                failures.append({"number": index, "url": image_url, "error": last_error})

        if failures:
            raise RuntimeError(f"原图下载不完整：成功 {len(downloaded)} 张，失败 {len(failures)} 张")

        metadata_path = gallery_dir / "metadata.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "id": metadata.get("id"),
                    "media_id": media_id,
                    "title": title_info,
                    "num_pages": metadata.get("num_pages"),
                    "downloaded": downloaded,
                    "failures": failures,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return GalleryDownload(
            gallery_id=gallery_id,
            media_id=media_id,
            title=title,
            image_paths=image_paths,
            metadata_path=metadata_path,
        )

    def _build_forward_result(self, event: AstrMessageEvent, download: GalleryDownload):
        try:
            uin = int(event.get_sender_id())
        except Exception:
            uin = 0
        try:
            name = event.get_sender_name() or "SoutuBot"
        except Exception:
            name = "SoutuBot"
        nodes = [
            Node(
                uin=uin,
                name=name,
                content=[
                    Plain(
                        f"{download.title}\n"
                        f"ID: {download.gallery_id}\n"
                        f"页数: {len(download.image_paths)}"
                    )
                ],
            )
        ]
        nodes.extend(
            Node(
                uin=uin,
                name=name,
                content=[Image.fromFileSystem(str(image_path))],
            )
            for image_path in download.image_paths
        )
        return event.chain_result(nodes)

    async def terminate(self) -> None:
        pass
