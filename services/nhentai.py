"""nhentai search and gallery download implementation.

The AstrBot entrypoint owns command handling and configuration.  This module
only coordinates the nhentai API, image download, and PDF cache workflow.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any


def search_galleries(plugin: Any, query: str, limit: int, core: Any) -> list[Any]:
    search_query = query.strip()
    if not search_query:
        raise RuntimeError("搜索文本为空")
    max_results = core._coerce_int(
        limit,
        default=core.TEXT_SEARCH_RESULT_LIMIT,
        min_value=1,
        max_value=10,
    )
    api_key = plugin._require_nhentai_api_key()

    url = (
        f"{core.NHENTAI_SEARCH_URL}?"
        f"{urllib.parse.urlencode({'query': search_query, 'sort': 'date', 'page': 1})}"
    )
    cookie_header = core._cookie_header_from_setting(plugin.nhentai_cookies)
    headers = {
        "User-Agent": core.DEFAULT_USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://nhentai.net/search/",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    core._add_nhentai_auth_header(headers, api_key)

    payload = core._load_json_url(
        url,
        headers,
        timeout=max(10.0, plugin.timeout_ms / 1000),
        proxy=plugin.nhentai_proxy,
    )
    raw_results = payload.get("result") or payload.get("results") or payload.get("data")
    if not isinstance(raw_results, list) or not raw_results:
        raise core.EmptySearchResultError(f"没有搜索到结果：{search_query}")

    candidates: list[Any] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        gallery_id = str(item.get("id") or item.get("gallery_id") or "").strip()
        if not gallery_id:
            continue
        candidates.append(
            core.NhentaiCandidate(
                gallery_id=gallery_id,
                title=core._extract_nhentai_title(item, f"nhentai {gallery_id}"),
            )
        )
        if len(candidates) >= max_results:
            break
    if not candidates:
        raise core.EmptySearchResultError(f"没有搜索到结果：{search_query}")
    return candidates


def download_gallery(plugin: Any, gallery_id: str, core: Any) -> Any:
    cached_download = core._load_cached_gallery_download(gallery_id)
    if cached_download is not None:
        core.logger.info("复用 nhentai 缓存：%s", gallery_id)
        return cached_download

    api_key = plugin._require_nhentai_api_key()
    gallery_dir = core._get_download_dir(gallery_id)
    images_dir = gallery_dir / "originals"
    images_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = gallery_dir / f"{gallery_id}.pdf"
    metadata_path = gallery_dir / "metadata.json"

    cookie_header = core._cookie_header_from_setting(plugin.nhentai_cookies)
    headers = {
        "User-Agent": core.DEFAULT_USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Referer": f"https://nhentai.net/g/{gallery_id}/",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    core._add_nhentai_auth_header(headers, api_key)

    metadata = core._load_json_url(
        core.NHENTAI_API_URL.format(gallery_id=gallery_id),
        headers,
        timeout=max(10.0, plugin.timeout_ms / 1000),
        proxy=plugin.nhentai_proxy,
    )
    cdn_config = core._load_json_url(
        core.NHENTAI_CDN_URL,
        headers,
        timeout=max(10.0, plugin.timeout_ms / 1000),
        proxy=plugin.nhentai_proxy,
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
    if len(pages) > plugin.max_download_pages:
        raise RuntimeError(
            f"页数 {len(pages)} 超过配置上限 {plugin.max_download_pages}，已停止下载"
        )

    tags = metadata.get("tags") if isinstance(metadata.get("tags"), list) else []
    tag_slugs = {
        str(tag.get("slug") or tag.get("name") or "").strip().lower()
        for tag in tags
        if isinstance(tag, dict)
    }
    if plugin.block_risky_tags and tag_slugs.intersection(core.BLOCKED_NHENTAI_TAGS):
        raise RuntimeError("命中受限标签，已停止自动下载")

    title_info = metadata.get("title") if isinstance(metadata.get("title"), dict) else {}
    title = (
        str(title_info.get("english") or "").strip()
        or str(title_info.get("pretty") or "").strip()
        or f"nhentai {gallery_id}"
    )
    media_id = str(metadata.get("media_id") or "")
    image_headers = {
        "User-Agent": core.DEFAULT_USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": f"https://nhentai.net/g/{gallery_id}/",
    }
    if cookie_header:
        image_headers["Cookie"] = cookie_header

    failures: list[dict[str, Any]] = []
    image_specs: list[Any] = []
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
        image_url = core._join_url(image_base_url, page_path)
        image_specs.append(
            core.ImageDownloadSpec(
                path=file_path,
                url=image_url,
                metadata={
                    "number": index,
                    "width": item.get("width"),
                    "height": item.get("height"),
                },
            )
        )

    image_paths, downloaded, image_failures = core._download_image_specs(
        image_specs,
        image_headers,
        timeout=max(10.0, plugin.timeout_ms / 1000),
        proxy=plugin.nhentai_proxy,
        retries=plugin.download_retries,
        service_label="nhentai 原图",
        proxy_setting="nhentai.proxy",
    )
    failures.extend(image_failures)

    if failures:
        raise RuntimeError(f"原图下载不完整：成功 {len(downloaded)} 张，失败 {len(failures)} 张")

    pdf_password = core._generate_pdf_password()
    core._create_encrypted_pdf_from_images(image_paths, pdf_path, pdf_password)

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
    return core.GalleryDownload(
        gallery_id=gallery_id,
        media_id=media_id,
        title=title,
        image_paths=image_paths,
        pdf_path=pdf_path,
        pdf_password=pdf_password,
        metadata_path=metadata_path,
    )
