"""JMComic search and download implementation."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


def search_albums(plugin: Any, query: str, limit: int, core: Any) -> list[Any]:
    search_query = query.strip()
    if not search_query:
        raise RuntimeError("搜索文本为空")
    max_results = core._coerce_int(
        limit,
        default=core.TEXT_SEARCH_RESULT_LIMIT,
        min_value=1,
        max_value=10,
    )

    try:
        from jmcomic import create_option_by_file
    except Exception as exc:
        raise RuntimeError("当前环境缺少 jmcomic，请安装插件依赖后重试") from exc

    option_path = core._get_data_dir() / "jmcomic_search_option.yml"
    option_lines = [
        "client:",
        "  impl: html",
        "  domain:",
        "    html:",
        *[f"      - {domain}" for domain in plugin.jmcomic_domains],
    ]
    if plugin.jmcomic_proxy:
        option_lines.extend(
            [
                "  postman:",
                "    meta_data:",
                f"      proxies: {plugin.jmcomic_proxy}",
            ]
        )
    option_path.write_text("\n".join(option_lines) + "\n", encoding="utf-8")

    option = create_option_by_file(str(option_path))
    cookies = core._cookie_dict_from_setting(plugin.jmcomic_cookies)
    if cookies:
        option.update_cookies(cookies)

    try:
        page = option.new_jm_client().search_site(search_query=search_query, page=1)
    except Exception as exc:
        error_text = str(exc)
        if "/setting" in error_text or "请求重试全部失败" in error_text:
            domains = ", ".join(plugin.jmcomic_domains)
            proxy_hint = "；如果运行环境需要代理，请配置 jmcomic.proxy，例如 http://127.0.0.1:7890"
            raise RuntimeError(
                f"禁漫域名初始化失败，已尝试这些域名：{domains}{proxy_hint}"
            ) from exc
        raise

    candidates: list[Any] = []
    for album_id, title in page:
        comic_id = str(album_id).strip()
        if not comic_id:
            continue
        normalized_id = f"jm{comic_id}" if comic_id.isdigit() else comic_id
        candidates.append(
            core.JmComicCandidate(
                comic_id=normalized_id,
                title=str(title or normalized_id).strip(),
            )
        )
        if len(candidates) >= max_results:
            break
    if candidates:
        return candidates
    raise core.EmptySearchResultError(f"没有搜索到结果：{search_query}")


def download_album(plugin: Any, comic_id: str, core: Any) -> Any:
    cached_download = core._load_cached_jmcomic_download(comic_id)
    if cached_download is not None:
        core.logger.info("复用 jmcomic 缓存：%s", comic_id)
        return cached_download

    try:
        from jmcomic import Feature, create_option_by_file, download_album as jm_download_album
    except Exception as exc:
        raise RuntimeError("当前环境缺少 jmcomic，请安装插件依赖后重试") from exc

    numeric_id = core._jmcomic_numeric_id(comic_id)
    download_dir = core._get_download_dir(comic_id)
    jm_work_dir = download_dir / "jmcomic"
    pdf_output_dir = download_dir / "pdf"
    metadata_path = download_dir / "metadata.json"
    pdf_path = download_dir / f"{comic_id}.pdf"
    core._clear_directory_contents(jm_work_dir)
    core._clear_directory_contents(pdf_output_dir)
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
        *[f"      - {domain}" for domain in plugin.jmcomic_domains],
    ]
    if plugin.jmcomic_proxy:
        option_lines.extend(
            [
                "  postman:",
                "    meta_data:",
                f"      proxies: {plugin.jmcomic_proxy}",
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

    pdf_password = core._generate_pdf_password()
    option = create_option_by_file(str(option_path))
    cookies = core._cookie_dict_from_setting(plugin.jmcomic_cookies)
    if cookies:
        option.update_cookies(cookies)

    try:
        jm_download_album(
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
            domains = ", ".join(plugin.jmcomic_domains)
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
        image_paths = core._collect_image_files(jm_work_dir)
        if not image_paths:
            raise RuntimeError("jmcomic 下载完成后没有找到导出的 PDF，也没有找到可用于合成 PDF 的图片")
        core._create_encrypted_pdf_from_images(image_paths, pdf_path, pdf_password)
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
    core._ensure_pdf_encrypted(pdf_path, pdf_password)

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
    return core.JmComicDownload(
        comic_id=comic_id,
        pdf_path=pdf_path,
        pdf_password=pdf_password,
        metadata_path=metadata_path,
    )
