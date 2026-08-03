from __future__ import annotations

import asyncio
import json
import re
import secrets
import shutil
import ssl
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Plain
from astrbot.api.star import Context, Star, StarTools, register


PLUGIN_NAME = "astrbot_plugin_xxcomic_get"
SOUTUBOT_HOME_URL = "https://soutubot.moe/"
NHENTAI_API_URL = "https://nhentai.net/api/v2/galleries/{gallery_id}"
NHENTAI_SEARCH_URL = "https://nhentai.net/api/v2/search"
NHENTAI_CDN_URL = "https://nhentai.net/api/v2/cdn"
DEFAULT_TIMEOUT_MS = 60000
CACHE_CLEANUP_STATE_FILE = "cache_cleanup_state.json"
DEFAULT_CACHE_CLEANUP_HOUR = 2
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126 Safari/537.36"
)
DEFAULT_JMCOMIC_DOMAINS = [
    "18comic.vip",
    "18comic.org",
    "jmcomic1.me",
    "jmcomic.me",
    "18comic-palworld.vip",
    "18comic-c.art",
    "18comic-palworld.club",
]
JMCOMIC_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
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
    pdf_path: Path
    pdf_password: str
    metadata_path: Path


@dataclass(slots=True)
class JmComicDownload:
    comic_id: str
    pdf_path: Path
    pdf_password: str
    metadata_path: Path


@dataclass(slots=True)
class NhentaiCandidate:
    gallery_id: str
    title: str


@dataclass(slots=True)
class JmComicCandidate:
    comic_id: str
    title: str


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


def _get_cache_cleanup_state_file() -> Path:
    return _get_data_dir() / CACHE_CLEANUP_STATE_FILE


def _read_cache_cleanup_state() -> dict[str, Any]:
    state_file = _get_cache_cleanup_state_file()
    if not state_file.exists():
        return {}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache_cleanup_state(timestamp: float, cleanup_date: str) -> None:
    state_file = _get_cache_cleanup_state_file()
    state_file.write_text(
        json.dumps(
            {
                "last_cleanup_at": timestamp,
                "last_cleanup_date": cleanup_date,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _cleanup_date_from_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


def _seconds_until_next_cache_cleanup(cleanup_hour: int, now: float | None = None) -> float:
    current = datetime.fromtimestamp(now or time.time())
    target = current.replace(hour=cleanup_hour, minute=0, second=0, microsecond=0)
    if current >= target:
        target += timedelta(days=1)
    return max(60.0, (target - current).total_seconds())


def _clear_directory_contents(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except FileNotFoundError:
                pass


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


def _normalize_jmcomic_id(value: str) -> str:
    text = str(value or "").strip().lower()
    if not re.fullmatch(r"jm\d+", text):
        raise ValueError("禁漫 id 必须是 jm+数字，例如 jm112233")
    return text


def _jmcomic_numeric_id(comic_id: str) -> str:
    return comic_id[2:]


def _split_config_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw_items = [str(item or "") for item in value]
    else:
        raw_items = re.split(r"[,;\s]+", str(value or ""))
    return [item.strip().strip("/") for item in raw_items if item.strip().strip("/")]


def _natural_sort_key(path: Path) -> list[Any]:
    text = path.as_posix().lower()
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def _collect_image_files(base_dir: Path) -> list[Path]:
    if not base_dir.exists():
        return []
    return sorted(
        (
            path
            for path in base_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in JMCOMIC_IMAGE_SUFFIXES
        ),
        key=_natural_sort_key,
    )


def _extract_nhentai_title(payload: dict[str, Any], fallback: str) -> str:
    title_info = payload.get("title")
    if isinstance(title_info, dict):
        for key in ("english", "pretty", "japanese"):
            title = str(title_info.get(key) or "").strip()
            if title:
                return title
    for key in ("title", "name"):
        title = str(payload.get(key) or "").strip()
        if title:
            return title
    return fallback


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


def _cookie_dict_from_setting(value: Any, domain_keyword: str | None = None) -> dict[str, str]:
    text = _read_cookie_setting(value)
    if not text:
        return {}

    pairs: dict[str, str] = {}
    if "\t" not in text and "=" in text:
        for segment in re.split(r"[;\n]", text):
            segment = segment.strip()
            if not segment or "=" not in segment:
                continue
            name, cookie_value = segment.split("=", 1)
            name = name.strip()
            if name:
                pairs[name] = cookie_value.strip()
        return pairs

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
        if domain_keyword and domain_keyword not in domain:
            continue
        if name:
            pairs[name] = cookie_value
    return pairs


def _add_nhentai_auth_header(headers: dict[str, str], api_key: Any) -> None:
    key = str(api_key or "").strip()
    if key:
        headers["Authorization"] = f"Key {key}"


def _join_url(base_url: str, path: str) -> str:
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def _build_url_opener(proxy: str | None = None) -> urllib.request.OpenerDirector:
    proxy_url = str(proxy or "").strip()
    if not proxy_url:
        return urllib.request.build_opener()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(
            {
                "http": proxy_url,
                "https": proxy_url,
            }
        )
    )


def _is_ssl_unexpected_eof(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ssl.SSLError) and "UNEXPECTED_EOF_WHILE_READING" in str(current):
            return True
        current = current.__cause__ or current.__context__
    return "UNEXPECTED_EOF_WHILE_READING" in str(exc)


def _request_bytes(
    url: str,
    headers: dict[str, str],
    timeout: float,
    proxy: str | None = None,
) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with _build_url_opener(proxy).open(request, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "")
            return response.read(), content_type
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"nhentai HTTP {exc.code}: {exc.reason}") from exc
    except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
        if _is_ssl_unexpected_eof(exc):
            raise RuntimeError(
                "nhentai HTTPS 连接被提前关闭，通常是当前运行环境无法直连目标站点、"
                "代理没有生效或 TLS 被中间网络拦截；请配置 nhentai.proxy 后重试。"
            ) from exc
        raise


def _generate_pdf_password(length: int = 6) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _save_image_as_pdf_page(image_path: Path, pdf_path: Path) -> None:
    try:
        from PIL import Image as PilImage
    except Exception as exc:
        raise RuntimeError("当前环境缺少 Pillow，无法把图片合成为 PDF") from exc

    with PilImage.open(image_path) as image:
        image.load()
        if image.mode == "RGB":
            page = image.copy()
        elif image.mode in {"RGBA", "LA"} or (
            image.mode == "P" and "transparency" in image.info
        ):
            page = PilImage.new("RGB", image.size, "white")
            alpha = image.convert("RGBA")
            page.paste(alpha, mask=alpha.getchannel("A"))
        else:
            page = image.convert("RGB")
        page.save(pdf_path, "PDF", resolution=100.0)


def _encrypt_pdf_pages(page_paths: list[Path], output_path: Path, password: str) -> None:
    try:
        from pypdf import PdfReader, PdfWriter
    except Exception as exc:
        raise RuntimeError("当前环境缺少 pypdf，无法生成加密 PDF；请安装插件依赖后重试") from exc

    writer = PdfWriter()
    for page_path in page_paths:
        reader = PdfReader(str(page_path))
        writer.add_page(reader.pages[0])

    try:
        writer.encrypt(
            user_password=password,
            owner_password=password,
            algorithm="AES-256",
        )
    except TypeError:
        writer.encrypt(user_password=password, owner_password=password)

    with output_path.open("wb") as output:
        writer.write(output)


def _ensure_pdf_encrypted(pdf_path: Path, password: str) -> None:
    try:
        from pypdf import PdfReader, PdfWriter
    except Exception as exc:
        raise RuntimeError("当前环境缺少 pypdf，无法确认或补充 PDF 加密") from exc

    reader = PdfReader(str(pdf_path))
    if reader.is_encrypted:
        if reader.decrypt(password) == 0:
            raise RuntimeError("PDF 已加密，但不能用记录的密码打开")
        return

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    try:
        writer.encrypt(
            user_password=password,
            owner_password=password,
            algorithm="AES-256",
        )
    except TypeError:
        writer.encrypt(user_password=password, owner_password=password)

    temp_path = pdf_path.with_suffix(".encrypted.tmp.pdf")
    with temp_path.open("wb") as output:
        writer.write(output)
    temp_path.replace(pdf_path)


def _create_encrypted_pdf_from_images(
    image_paths: list[Path],
    output_path: Path,
    password: str,
) -> None:
    if not image_paths:
        raise RuntimeError("没有可用于生成 PDF 的图片")

    temp_dir = output_path.parent / ".pdf_pages"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        page_paths: list[Path] = []
        for index, image_path in enumerate(image_paths, 1):
            page_path = temp_dir / f"{index:04d}.pdf"
            _save_image_as_pdf_page(image_path, page_path)
            page_paths.append(page_path)
        _encrypt_pdf_pages(page_paths, output_path, password)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _read_gallery_metadata(metadata_path: Path) -> dict[str, Any]:
    if not metadata_path.exists():
        return {}
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _metadata_title(metadata: dict[str, Any], fallback: str) -> str:
    title_info = metadata.get("title")
    if isinstance(title_info, dict):
        for key in ("english", "pretty", "japanese"):
            title = str(title_info.get(key) or "").strip()
            if title:
                return title
    title = str(title_info or "").strip()
    return title or fallback


def _load_cached_gallery_download(gallery_id: str) -> GalleryDownload | None:
    gallery_dir = _get_download_dir(gallery_id)
    images_dir = gallery_dir / "originals"
    pdf_path = gallery_dir / f"{gallery_id}.pdf"
    metadata_path = gallery_dir / "metadata.json"
    cached_metadata = _read_gallery_metadata(metadata_path)
    cached_pdf_password = str(cached_metadata.get("pdf_password") or "").strip()
    cached_downloaded = cached_metadata.get("downloaded")
    if (
        not pdf_path.exists()
        or pdf_path.stat().st_size <= 0
        or not cached_pdf_password
        or not isinstance(cached_downloaded, list)
        or not cached_downloaded
    ):
        return None

    image_paths = [
        images_dir / str(item.get("file") or "")
        for item in cached_downloaded
        if isinstance(item, dict) and item.get("file")
    ]
    if not image_paths or not all(path.exists() and path.stat().st_size > 0 for path in image_paths):
        return None

    return GalleryDownload(
        gallery_id=gallery_id,
        media_id=str(cached_metadata.get("media_id") or ""),
        title=_metadata_title(cached_metadata, f"nhentai {gallery_id}"),
        image_paths=image_paths,
        pdf_path=pdf_path,
        pdf_password=cached_pdf_password,
        metadata_path=metadata_path,
    )


def _load_cached_jmcomic_download(comic_id: str) -> JmComicDownload | None:
    download_dir = _get_download_dir(comic_id)
    pdf_path = download_dir / f"{comic_id}.pdf"
    metadata_path = download_dir / "metadata.json"
    cached_metadata = _read_gallery_metadata(metadata_path)
    cached_pdf_password = str(cached_metadata.get("pdf_password") or "").strip()
    if not pdf_path.exists() or pdf_path.stat().st_size <= 0 or not cached_pdf_password:
        return None
    return JmComicDownload(
        comic_id=comic_id,
        pdf_path=pdf_path,
        pdf_password=cached_pdf_password,
        metadata_path=metadata_path,
    )


def _load_json_url(
    url: str,
    headers: dict[str, str],
    timeout: float,
    proxy: str | None = None,
) -> dict[str, Any]:
    body, content_type = _request_bytes(url, headers, timeout, proxy=proxy)
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


def _format_nhentai_candidate(candidate: NhentaiCandidate) -> str:
    return (
        "找到第一个 nhentai 结果：\n"
        f"ID: {candidate.gallery_id}\n"
        f"标题: {candidate.title}\n"
        f"链接: https://nhentai.net/g/{candidate.gallery_id}/"
    )


def _format_jmcomic_candidate(candidate: JmComicCandidate) -> str:
    return (
        "找到第一个禁漫结果：\n"
        f"标题: {candidate.title}\n"
        f"ID: {candidate.comic_id}"
    )


@register(PLUGIN_NAME, "hanayo", "用 SoutuBot 识别图片来源，或用文本搜索 nhentai/JMComic", "1.1.2")
class XxComicGetPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or context.get_config()
        self._cache_lock = threading.RLock()
        self._cache_cleanup_task: asyncio.Task[None] | None = None
        self._refresh_config()
        self._start_cache_cleanup_task()

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
        self.nhentai_proxy = str(_get_config_value(self.config, "nhentai.proxy", "") or "").strip()
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
        self.jmcomic_download_enabled = _coerce_bool(
            _get_config_value(self.config, "jmcomic.download_enabled", True),
            True,
        )
        configured_domains = _split_config_list(
            _get_config_value(
                self.config,
                "jmcomic.domains",
                _get_config_value(self.config, "jmcomic.domain", ""),
            )
        )
        self.jmcomic_domains = configured_domains or list(DEFAULT_JMCOMIC_DOMAINS)
        self.jmcomic_proxy = str(_get_config_value(self.config, "jmcomic.proxy", "") or "").strip()
        self.jmcomic_cookies = _get_config_value(self.config, "jmcomic.cookies", "")
        self.cache_cleanup_enabled = _coerce_bool(
            _get_config_value(self.config, "cache.cleanup_enabled", True),
            True,
        )
        self.cache_cleanup_hour = _coerce_int(
            _get_config_value(
                self.config,
                "cache.cleanup_hour",
                DEFAULT_CACHE_CLEANUP_HOUR,
            ),
            default=DEFAULT_CACHE_CLEANUP_HOUR,
            min_value=0,
            max_value=23,
        )

    def _start_cache_cleanup_task(self) -> None:
        if self._cache_cleanup_task is not None or not self.cache_cleanup_enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("缓存清理任务启动失败：当前没有运行中的事件循环")
            return
        self._cache_cleanup_task = loop.create_task(self._cache_cleanup_loop())

    async def _cache_cleanup_loop(self) -> None:
        while True:
            try:
                await self._cleanup_cache_if_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("插件缓存清理失败")
            await asyncio.sleep(_seconds_until_next_cache_cleanup(self.cache_cleanup_hour))

    async def _cleanup_cache_if_due(self) -> None:
        now = time.time()
        current = datetime.fromtimestamp(now)
        if current.hour < self.cache_cleanup_hour:
            return
        cleanup_date = _cleanup_date_from_timestamp(now)
        state = await asyncio.to_thread(_read_cache_cleanup_state)
        if str(state.get("last_cleanup_date") or "") == cleanup_date:
            return
        await asyncio.to_thread(self._clear_cache_sync)
        await asyncio.to_thread(_write_cache_cleanup_state, now, cleanup_date)

    def _clear_cache_sync(self) -> None:
        data_dir = _get_data_dir()
        with self._cache_lock:
            for name in ("downloads", "cookies"):
                _clear_directory_contents(data_dir / name)
        logger.info("已清理插件缓存：downloads, cookies")

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
        self._start_cache_cleanup_task()
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

        first_nhentai = next(
            (
                NhentaiCandidate(gallery_id, result.title)
                for result in matched
                for gallery_id in [_parse_nhentai_gallery_id(result.subject_urls)]
                if gallery_id
            ),
            None,
        )
        if not first_nhentai:
            body = "\n\n".join(
                _format_result(result, index + 1)
                for index, result in enumerate(matched[: self.max_results])
            )
            yield event.plain_result(f"找到了这些可能来源：\n\n{body}")
            return
        yield event.plain_result(_format_nhentai_candidate(first_nhentai))

    @filter.command("嘻嘻")
    async def search_nhentai_text(self, event: AstrMessageEvent):
        """按文本搜索 nhentai 并返回第一个结果，等待确认下载"""
        self._start_cache_cleanup_task()
        query = _extract_command_text(getattr(event, "message_str", ""), "嘻嘻")
        if not query:
            yield event.plain_result("？")
            return
        if not self._has_nhentai_api_key():
            yield event.plain_result("未配置api")
            return

        try:
            candidate = await self._search_nhentai_first_gallery(query)
        except EmptySearchResultError:
            yield event.plain_result("空的。")
            return
        except Exception as exc:
            logger.exception("nhentai 文本搜索失败")
            yield event.plain_result(f"搜索失败：{exc}")
            return

        yield event.plain_result(_format_nhentai_candidate(candidate))

    @filter.command("JJS")
    async def search_jmcomic_text(self, event: AstrMessageEvent):
        """按文本搜索禁漫并返回第一个结果，不下载"""
        self._start_cache_cleanup_task()
        query = _extract_command_text(getattr(event, "message_str", ""), "JJS")
        if not query:
            yield event.plain_result("请发送：/JJS <搜索文本>")
            return

        try:
            candidate = await self._search_jmcomic_first_album(query)
        except EmptySearchResultError:
            yield event.plain_result("空的。")
            return
        except Exception as exc:
            logger.exception("jmcomic 文本搜索失败")
            yield event.plain_result(f"搜索失败：{exc}")
            return

        yield event.plain_result(_format_jmcomic_candidate(candidate))

    @filter.command("对的")
    async def confirm_download_nhentai(self, event: AstrMessageEvent):
        """按给定 nhentai ID 下载原图并生成 PDF"""
        self._start_cache_cleanup_task()
        gallery_id = _extract_command_text(getattr(event, "message_str", ""), "对的")
        if not gallery_id:
            yield event.plain_result("请发送：/对的 <nhentai id>")
            return
        if not re.fullmatch(r"\d+", gallery_id):
            yield event.plain_result("nhentai id 只能是数字。")
            return
        cached_download = await asyncio.to_thread(
            self._load_cached_gallery_download_locked,
            gallery_id,
        )
        if cached_download is not None:
            logger.info("复用 nhentai 缓存：%s", gallery_id)
            yield self._build_pdf_result(event, cached_download)
            return
        if not self.download_enabled:
            yield event.plain_result("当前配置已关闭 nhentai 下载。")
            return
        if not self._has_nhentai_api_key():
            yield event.plain_result("未配置api")
            return

        yield event.plain_result("要点时间，等吧")

        try:
            download = await self._download_nhentai_gallery(gallery_id)
        except Exception as exc:
            logger.exception("nhentai 下载失败")
            yield event.plain_result(f"下载失败：{exc}")
            return
        yield self._build_pdf_result(event, download)

    @filter.command("JJ")
    async def download_jmcomic(self, event: AstrMessageEvent):
        """按给定禁漫 ID 下载整本并生成加密 PDF"""
        self._start_cache_cleanup_task()
        raw_id = _extract_command_text(getattr(event, "message_str", ""), "JJ")
        if not raw_id:
            yield event.plain_result("请发送：/JJ jm112233")
            return
        try:
            comic_id = _normalize_jmcomic_id(raw_id)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        cached_download = await asyncio.to_thread(
            self._load_cached_jmcomic_download_locked,
            comic_id,
        )
        if cached_download is not None:
            logger.info("复用 jmcomic 缓存：%s", comic_id)
            yield self._build_jmcomic_pdf_result(event, cached_download)
            return
        if not self.jmcomic_download_enabled:
            yield event.plain_result("当前配置已关闭禁漫下载。")
            return

        yield event.plain_result("开始下载禁漫本子并生成加密 PDF，可能需要一点时间。")

        try:
            download = await self._download_jmcomic_album(comic_id)
        except Exception as exc:
            logger.exception("jmcomic 下载失败")
            yield event.plain_result(f"下载失败：{exc}")
            return
        yield self._build_jmcomic_pdf_result(event, download)

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
        return await asyncio.to_thread(self._download_nhentai_gallery_sync_locked, gallery_id)

    async def _download_jmcomic_album(self, comic_id: str) -> JmComicDownload:
        return await asyncio.to_thread(self._download_jmcomic_album_sync_locked, comic_id)

    async def _search_nhentai_first_gallery(self, query: str) -> NhentaiCandidate:
        return await asyncio.to_thread(self._search_nhentai_first_gallery_sync, query)

    async def _search_jmcomic_first_album(self, query: str) -> JmComicCandidate:
        return await asyncio.to_thread(self._search_jmcomic_first_album_sync_locked, query)

    def _search_nhentai_first_gallery_sync(self, query: str) -> NhentaiCandidate:
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

        payload = _load_json_url(
            url,
            headers,
            timeout=max(10.0, self.timeout_ms / 1000),
            proxy=self.nhentai_proxy,
        )
        raw_results = payload.get("result") or payload.get("results") or payload.get("data")
        if not isinstance(raw_results, list) or not raw_results:
            raise EmptySearchResultError(f"没有搜索到结果：{search_query}")

        first = raw_results[0]
        if not isinstance(first, dict):
            raise RuntimeError("第一个搜索结果格式不正确")
        gallery_id = str(first.get("id") or first.get("gallery_id") or "").strip()
        if not gallery_id:
            raise RuntimeError("第一个搜索结果没有 gallery id")
        title = _extract_nhentai_title(first, f"nhentai {gallery_id}")
        return NhentaiCandidate(gallery_id=gallery_id, title=title)

    def _search_jmcomic_first_album_sync_locked(self, query: str) -> JmComicCandidate:
        with self._cache_lock:
            return self._search_jmcomic_first_album_sync(query)

    def _search_jmcomic_first_album_sync(self, query: str) -> JmComicCandidate:
        search_query = query.strip()
        if not search_query:
            raise RuntimeError("搜索文本为空")

        try:
            from jmcomic import create_option_by_file
        except Exception as exc:
            raise RuntimeError("当前环境缺少 jmcomic，请安装插件依赖后重试") from exc

        option_path = _get_data_dir() / "jmcomic_search_option.yml"
        option_lines = [
            "client:",
            "  impl: html",
            "  domain:",
            "    html:",
            *[f"      - {domain}" for domain in self.jmcomic_domains],
        ]
        if self.jmcomic_proxy:
            option_lines.extend(
                [
                    "  postman:",
                    "    meta_data:",
                    f"      proxies: {self.jmcomic_proxy}",
                ]
            )
        option_path.write_text("\n".join(option_lines) + "\n", encoding="utf-8")

        option = create_option_by_file(str(option_path))
        cookies = _cookie_dict_from_setting(self.jmcomic_cookies)
        if cookies:
            option.update_cookies(cookies)

        try:
            page = option.new_jm_client().search_site(search_query=search_query, page=1)
        except Exception as exc:
            error_text = str(exc)
            if "/setting" in error_text or "请求重试全部失败" in error_text:
                domains = ", ".join(self.jmcomic_domains)
                proxy_hint = "；如果运行环境需要代理，请配置 jmcomic.proxy，例如 http://127.0.0.1:7890"
                raise RuntimeError(
                    f"禁漫域名初始化失败，已尝试这些域名：{domains}{proxy_hint}"
                ) from exc
            raise

        for album_id, title in page:
            comic_id = str(album_id).strip()
            if not comic_id:
                continue
            return JmComicCandidate(
                comic_id=f"jm{comic_id}" if comic_id.isdigit() else comic_id,
                title=str(title or f"jm{comic_id}").strip(),
            )
        raise EmptySearchResultError(f"没有搜索到结果：{search_query}")

    def _download_nhentai_gallery_sync_locked(self, gallery_id: str) -> GalleryDownload:
        with self._cache_lock:
            return self._download_nhentai_gallery_sync(gallery_id)

    def _load_cached_gallery_download_locked(self, gallery_id: str) -> GalleryDownload | None:
        with self._cache_lock:
            return _load_cached_gallery_download(gallery_id)

    def _download_jmcomic_album_sync_locked(self, comic_id: str) -> JmComicDownload:
        with self._cache_lock:
            return self._download_jmcomic_album_sync(comic_id)

    def _load_cached_jmcomic_download_locked(self, comic_id: str) -> JmComicDownload | None:
        with self._cache_lock:
            return _load_cached_jmcomic_download(comic_id)

    def _download_jmcomic_album_sync(self, comic_id: str) -> JmComicDownload:
        cached_download = _load_cached_jmcomic_download(comic_id)
        if cached_download is not None:
            logger.info("复用 jmcomic 缓存：%s", comic_id)
            return cached_download

        try:
            from jmcomic import Feature, create_option_by_file, download_album
        except Exception as exc:
            raise RuntimeError("当前环境缺少 jmcomic，请安装插件依赖后重试") from exc

        numeric_id = _jmcomic_numeric_id(comic_id)
        download_dir = _get_download_dir(comic_id)
        jm_work_dir = download_dir / "jmcomic"
        pdf_output_dir = download_dir / "pdf"
        metadata_path = download_dir / "metadata.json"
        pdf_path = download_dir / f"{comic_id}.pdf"
        _clear_directory_contents(jm_work_dir)
        _clear_directory_contents(pdf_output_dir)
        jm_work_dir.mkdir(parents=True, exist_ok=True)
        pdf_output_dir.mkdir(parents=True, exist_ok=True)

        option_path = download_dir / "jmcomic_option.yml"
        option_lines = [
            "dir_rule:",
            f"  base_dir: {jm_work_dir.as_posix()}",
            "  rule: Bd / {Aid}",
            "client:",
            "  impl: html",
            "  domain:",
            "    html:",
            *[f"      - {domain}" for domain in self.jmcomic_domains],
        ]
        if self.jmcomic_proxy:
            option_lines.extend(
                [
                    "  postman:",
                    "    meta_data:",
                    f"      proxies: {self.jmcomic_proxy}",
                ]
            )
        option_lines.extend(
            [
                "download:",
                "  image:",
                "    decode: true",
                "  threading:",
                "    image: 10",
                "    photo: 4",
            ]
        )
        option_path.write_text("\n".join(option_lines) + "\n", encoding="utf-8")

        pdf_password = _generate_pdf_password()
        option = create_option_by_file(str(option_path))
        cookies = _cookie_dict_from_setting(self.jmcomic_cookies)
        if cookies:
            option.update_cookies(cookies)

        try:
            download_album(
                numeric_id,
                option,
                extra=Feature.export_pdf(
                    pdf_dir=str(pdf_output_dir),
                    filename_rule="Aid",
                    encrypt={"password": pdf_password},
                    delete_original_file=False,
                ),
                check_exception=True,
            )
        except Exception as exc:
            error_text = str(exc)
            if "/setting" in error_text or "请求重试全部失败" in error_text:
                domains = ", ".join(self.jmcomic_domains)
                proxy_hint = "；如果运行环境需要代理，请配置 jmcomic.proxy，例如 http://127.0.0.1:7890"
                raise RuntimeError(
                    f"禁漫域名初始化失败，已尝试这些域名：{domains}{proxy_hint}"
                ) from exc
            raise

        pdf_created_by = "jmcomic_export"
        generated_pdfs = sorted(
            (path for path in pdf_output_dir.glob("*.pdf") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not generated_pdfs:
            generated_pdfs = sorted(
                (path for path in download_dir.rglob("*.pdf") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        if not generated_pdfs:
            image_paths = _collect_image_files(jm_work_dir)
            if not image_paths:
                raise RuntimeError("jmcomic 下载完成后没有找到导出的 PDF，也没有找到可用于合成 PDF 的图片")
            _create_encrypted_pdf_from_images(image_paths, pdf_path, pdf_password)
            generated_pdf_name = pdf_path.name
            pdf_created_by = "plugin_fallback"
        else:
            generated_pdf = generated_pdfs[0]
            generated_pdf_name = generated_pdf.name
            if pdf_path.exists():
                pdf_path.unlink()
            shutil.move(str(generated_pdf), str(pdf_path))
        if not pdf_path.exists() or pdf_path.stat().st_size <= 0:
            raise RuntimeError("jmcomic PDF 重命名后文件不可用")
        _ensure_pdf_encrypted(pdf_path, pdf_password)

        metadata_path.write_text(
            json.dumps(
                {
                    "id": comic_id,
                    "jm_album_id": numeric_id,
                    "pdf": pdf_path.name,
                    "pdf_password": pdf_password,
                    "source_pdf": generated_pdf_name,
                    "pdf_created_by": pdf_created_by,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return JmComicDownload(
            comic_id=comic_id,
            pdf_path=pdf_path,
            pdf_password=pdf_password,
            metadata_path=metadata_path,
        )

    def _download_nhentai_gallery_sync(self, gallery_id: str) -> GalleryDownload:
        cached_download = _load_cached_gallery_download(gallery_id)
        if cached_download is not None:
            logger.info("复用 nhentai 缓存：%s", gallery_id)
            return cached_download

        api_key = self._require_nhentai_api_key()
        gallery_dir = _get_download_dir(gallery_id)
        images_dir = gallery_dir / "originals"
        images_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = gallery_dir / f"{gallery_id}.pdf"
        metadata_path = gallery_dir / "metadata.json"

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
            proxy=self.nhentai_proxy,
        )
        cdn_config = _load_json_url(
            NHENTAI_CDN_URL,
            headers,
            timeout=max(10.0, self.timeout_ms / 1000),
            proxy=self.nhentai_proxy,
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
            if file_path.exists() and file_path.stat().st_size > 0:
                image_paths.append(file_path)
                downloaded.append(
                    {
                        "file": file_path.name,
                        "bytes": file_path.stat().st_size,
                        "width": item.get("width"),
                        "height": item.get("height"),
                        "url": image_url,
                        "cached": True,
                    }
                )
                continue
            last_error: str | None = None
            for attempt in range(self.download_retries + 1):
                try:
                    body, content_type = _request_bytes(
                        image_url,
                        image_headers,
                        timeout=max(10.0, self.timeout_ms / 1000),
                        proxy=self.nhentai_proxy,
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
                            "cached": False,
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

        pdf_password = _generate_pdf_password()
        _create_encrypted_pdf_from_images(image_paths, pdf_path, pdf_password)

        metadata_path.write_text(
            json.dumps(
                {
                    "id": metadata.get("id"),
                    "media_id": media_id,
                    "title": title_info,
                    "num_pages": metadata.get("num_pages"),
                    "downloaded": downloaded,
                    "failures": failures,
                    "pdf": pdf_path.name,
                    "pdf_password": pdf_password,
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
            pdf_path=pdf_path,
            pdf_password=pdf_password,
            metadata_path=metadata_path,
        )

    def _build_pdf_result(self, event: AstrMessageEvent, download: GalleryDownload):
        return event.chain_result(
            [
                Plain(
                    f"ID: {download.gallery_id}\n"
                    f"页数: {len(download.image_paths)}\n"
                    f"密码: {download.pdf_password}"
                ),
                File(name=f"{download.gallery_id}.pdf", file=str(download.pdf_path)),
            ]
        )

    def _build_jmcomic_pdf_result(self, event: AstrMessageEvent, download: JmComicDownload):
        return event.chain_result(
            [
                Plain(
                    f"ID: {download.comic_id}\n"
                    f"密码: {download.pdf_password}"
                ),
                File(name=f"{download.comic_id}.pdf", file=str(download.pdf_path)),
            ]
        )

    async def terminate(self) -> None:
        if self._cache_cleanup_task is not None:
            self._cache_cleanup_task.cancel()
            try:
                await self._cache_cleanup_task
            except asyncio.CancelledError:
                pass
            self._cache_cleanup_task = None
