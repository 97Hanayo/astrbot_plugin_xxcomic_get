"""PicaComic authentication, search, and download implementation."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any


def login(plugin: Any, email: str, password: str, core: Any) -> None:
    normalized_email = str(email or "").strip()
    normalized_password = str(password or "").strip()
    if not normalized_email or not normalized_password:
        raise RuntimeError("用户名和密码不能为空")
    endpoint = "/auth/sign-in"
    method = "POST"
    payload = core._request_json(
        method,
        core._pica_api_url(endpoint),
        core._build_pica_headers(method, endpoint),
        timeout=max(10.0, plugin.timeout_ms / 1000),
        proxy=plugin.pica_proxy,
        json_body={
            "email": normalized_email,
            "password": normalized_password,
        },
    )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("哔咔登录返回格式不正确")
    token = str(data.get("token") or "").strip()
    if not token:
        raise RuntimeError("哔咔登录没有返回 token")
    core._write_pica_token(token)


def search_comics(
    plugin: Any,
    query: str,
    limit: int | None,
    core: Any,
) -> list[Any]:
    search_query = query.strip()
    if not search_query:
        raise RuntimeError("搜索文本为空")
    max_results = core._coerce_int(
        limit if limit is not None else plugin.pica_max_results,
        default=plugin.pica_max_results,
        min_value=1,
        max_value=10,
    )
    token = plugin._require_pica_token()
    endpoint = "/comics/advanced-search"
    method = "POST"
    query_params = {"page": 1}
    payload = core._request_json(
        method,
        f"{core._pica_api_url(endpoint)}?{urllib.parse.urlencode(query_params)}",
        core._build_pica_headers(method, endpoint, query_params, token=token),
        timeout=max(10.0, plugin.timeout_ms / 1000),
        proxy=plugin.pica_proxy,
        json_body={
            "keyword": search_query,
            "categories": [],
            "s": "ua",
        },
    )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("哔咔搜索返回格式不正确")
    comics = data.get("comics")
    if not isinstance(comics, dict):
        raise RuntimeError("哔咔搜索没有返回漫画列表")
    docs = comics.get("docs")
    if not isinstance(docs, list) or not docs:
        raise core.EmptySearchResultError(f"没有搜索到结果：{search_query}")

    candidates: list[Any] = []
    for item in docs:
        if not isinstance(item, dict):
            continue
        comic_id = str(item.get("_id") or item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not comic_id or not title:
            continue
        raw_categories = item.get("categories")
        categories = (
            [str(value).strip() for value in raw_categories if str(value or "").strip()]
            if isinstance(raw_categories, list)
            else []
        )
        raw_tags = item.get("tags")
        tags = (
            [str(value).strip() for value in raw_tags if str(value or "").strip()]
            if isinstance(raw_tags, list)
            else []
        )
        pages_count = item.get("pagesCount")
        likes_count = item.get("likesCount")
        candidates.append(
            core.PicaComicCandidate(
                comic_id=comic_id,
                title=title,
                author=str(item.get("author") or "").strip(),
                categories=categories,
                tags=tags,
                pages_count=pages_count if isinstance(pages_count, int) else None,
                likes_count=likes_count if isinstance(likes_count, int) else None,
                finished=item.get("finished") if isinstance(item.get("finished"), bool) else None,
            )
        )
        if len(candidates) >= max_results:
            break
    if not candidates:
        raise core.EmptySearchResultError(f"没有搜索到结果：{search_query}")
    return candidates


def api_request(
    plugin: Any,
    method: str,
    endpoint: str,
    token: str,
    query: dict[str, Any] | None,
    json_body: dict[str, Any] | None,
    core: Any,
) -> dict[str, Any]:
    url = core._pica_api_url(endpoint)
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    return core._request_json(
        method,
        url,
        core._build_pica_headers(method, endpoint, query, token=token),
        timeout=max(10.0, plugin.timeout_ms / 1000),
        proxy=plugin.pica_proxy,
        json_body=json_body,
    )


def fetch_paged_docs(
    plugin: Any,
    endpoint: str,
    token: str,
    data_key: str,
    core: Any,
) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        payload = api_request(plugin, "GET", endpoint, token, {"page": page}, None, core)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("哔咔分页接口返回格式不正确")
        page_data = data.get(data_key)
        if not isinstance(page_data, dict):
            raise RuntimeError(f"哔咔分页接口没有返回 {data_key}")
        raw_docs = page_data.get("docs")
        if not isinstance(raw_docs, list):
            raise RuntimeError("哔咔分页接口没有返回 docs")
        docs.extend(item for item in raw_docs if isinstance(item, dict))
        pages_value = page_data.get("pages")
        total_pages = pages_value if isinstance(pages_value, int) and pages_value > 0 else page
        if not raw_docs:
            break
        page += 1
    return docs


def fetch_comic_detail(
    plugin: Any,
    comic_id: str,
    token: str,
    core: Any,
) -> dict[str, Any]:
    payload = api_request(plugin, "GET", f"/comics/{comic_id}", token, None, None, core)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("哔咔详情返回格式不正确")
    comic = data.get("comic")
    if not isinstance(comic, dict):
        raise RuntimeError("哔咔详情没有返回 comic")
    return comic


def download_comic(plugin: Any, comic_id: str, core: Any) -> Any:
    cached_download = core._load_cached_pica_download(comic_id)
    if cached_download is not None:
        core.logger.info("复用哔咔缓存：%s", comic_id)
        return cached_download

    token = plugin._require_pica_token()
    comic = fetch_comic_detail(plugin, comic_id, token, core)
    title = str(comic.get("title") or f"哔咔 {comic_id}").strip()
    episodes = fetch_paged_docs(plugin, f"/comics/{comic_id}/eps", token, "eps", core)
    if not episodes:
        raise RuntimeError("哔咔漫画没有返回分话列表")

    download_dir = core._get_pica_download_dir(comic_id)
    images_dir = download_dir / "originals"
    images_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = download_dir / f"bk_{comic_id}.pdf"
    metadata_path = download_dir / "metadata.json"

    image_headers = {
        "User-Agent": core.PICA_USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    failures: list[dict[str, Any]] = []
    image_specs: list[Any] = []

    sorted_episodes = sorted(
        episodes,
        key=lambda item: item.get("order") if isinstance(item.get("order"), int) else 0,
    )
    for episode in sorted_episodes:
        order = episode.get("order")
        if not isinstance(order, int):
            continue
        episode_title = str(episode.get("title") or f"EP {order}").strip()
        pages = fetch_paged_docs(
            plugin,
            f"/comics/{comic_id}/order/{order}/pages",
            token,
            "pages",
            core,
        )
        for page_index, page_item in enumerate(pages, 1):
            media = page_item.get("media")
            if not isinstance(media, dict):
                failures.append({"episode": order, "page": page_index, "error": "missing media"})
                continue
            if len(image_specs) >= plugin.pica_max_download_pages:
                raise RuntimeError(
                    f"页数超过配置上限 {plugin.pica_max_download_pages}，已停止下载"
                )
            try:
                image_url = core._stringify_pica_image_url(media)
            except Exception as exc:
                failures.append({"episode": order, "page": page_index, "error": str(exc)})
                continue
            suffix = core._pica_image_suffix(media, image_url)
            file_path = images_dir / f"{order:03d}_{page_index:04d}{suffix}"
            image_specs.append(
                core.ImageDownloadSpec(
                    path=file_path,
                    url=image_url,
                    metadata={
                        "episode_order": order,
                        "episode_title": episode_title,
                        "page": page_index,
                    },
                )
            )

    image_paths, downloaded, image_failures = core._download_image_specs(
        image_specs,
        image_headers,
        timeout=max(10.0, plugin.timeout_ms / 1000),
        proxy=plugin.pica_proxy,
        retries=plugin.pica_download_retries,
        service_label="哔咔图片",
        proxy_setting="pica.proxy",
    )
    failures.extend(image_failures)

    if failures:
        raise RuntimeError(f"哔咔图片下载不完整：成功 {len(downloaded)} 张，失败 {len(failures)} 张")
    if not image_paths:
        raise RuntimeError("哔咔漫画没有可用于生成 PDF 的图片")

    pdf_password = core._generate_pdf_password()
    core._create_encrypted_pdf_from_images(image_paths, pdf_path, pdf_password)
    metadata_path.write_text(
        json.dumps(
            {
                "id": comic_id,
                "title": title,
                "episodes": [
                    {
                        "order": item.get("order"),
                        "title": item.get("title"),
                    }
                    for item in sorted_episodes
                ],
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
    return core.PicaComicDownload(
        comic_id=comic_id,
        title=title,
        image_paths=image_paths,
        pdf_path=pdf_path,
        pdf_password=pdf_password,
        metadata_path=metadata_path,
    )
