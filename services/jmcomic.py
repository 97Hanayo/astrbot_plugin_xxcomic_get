"""JMComic search and download implementation."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


def _apply_auth_cookies(option: Any, plugin: Any, core: Any) -> bool:
    configured_cookies = core._cookie_dict_from_setting(plugin.jmcomic_cookies)
    login_cookies = core._read_jmcomic_cookies()
    if configured_cookies:
        option.update_cookies(configured_cookies)
    if login_cookies:
        option.update_cookies(login_cookies)
    return bool(configured_cookies or login_cookies)


def _configured_credentials(plugin: Any) -> tuple[str, str]:
    return (
        str(getattr(plugin, "jmcomic_username", "") or "").strip(),
        str(getattr(plugin, "jmcomic_password", "") or "").strip(),
    )


def _login_from_config(plugin: Any, core: Any) -> None:
    account, password = _configured_credentials(plugin)
    if not account or not password:
        raise RuntimeError("请在插件配置中填写 jmcomic.username 和 jmcomic.password")
    login(plugin, account, password, core)


def _has_configured_credentials(plugin: Any) -> bool:
    account, password = _configured_credentials(plugin)
    return bool(account and password)


def _looks_like_authentication_error(exc: BaseException) -> bool:
    error_text = str(exc).lower()
    return any(
        marker in error_text
        for marker in (
            "未登录",
            "请先登录",
            "登录后",
            "login",
            "unauthorized",
            "forbidden",
            "401",
            "403",
        )
    )


def _is_domain_initialization_error(exc: BaseException) -> bool:
    error_text = str(exc)
    return "/setting" in error_text or "请求重试全部失败" in error_text


def _raise_domain_initialization_error(exc: BaseException, plugin: Any) -> None:
    domains = ", ".join(plugin.jmcomic_domains)
    proxy_hint = "；如果运行环境需要代理，请配置 jmcomic.proxy，例如 http://127.0.0.1:7890"
    raise RuntimeError(
        f"禁漫域名初始化失败，已尝试这些域名：{domains}{proxy_hint}"
    ) from exc


def _get_album_title(option: Any, numeric_id: str, fallback: str, core: Any) -> str:
    """Best-effort title lookup without making title retrieval block downloads."""
    for impl in ("html", "api"):
        try:
            detail = option.new_jm_client(impl=impl).get_album_detail(numeric_id)
            title = str(getattr(detail, "title", "") or "").strip()
            if title:
                return title
        except Exception as exc:
            core.logger.debug("获取禁漫标题失败（%s/%s）：%s", numeric_id, impl, exc)
    return fallback


def login(plugin: Any, account: str, password: str, core: Any) -> None:
    normalized_account = str(account or "").strip()
    normalized_password = str(password or "").strip()
    if not normalized_account or not normalized_password:
        raise RuntimeError("账号和密码不能为空")

    try:
        from jmcomic import create_option_by_file
    except Exception as exc:
        raise RuntimeError("当前环境缺少 jmcomic，请安装插件依赖后重试") from exc

    option_path = core._get_data_dir() / "jmcomic_login_option.yml"
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

    try:
        option = create_option_by_file(str(option_path))
        client = option.build_jm_client(impl="html")
        client.login(normalized_account, normalized_password)
        cookies = dict(client["cookies"])
    except Exception as exc:
        if _is_domain_initialization_error(exc):
            _raise_domain_initialization_error(exc, plugin)
        raise

    core._write_jmcomic_cookies(cookies)


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
    _apply_auth_cookies(option, plugin, core)

    try:
        page = option.new_jm_client().search_site(search_query=search_query, page=1)
    except Exception as exc:
        if _is_domain_initialization_error(exc):
            _raise_domain_initialization_error(exc, plugin)
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
            f"    image: {plugin.jmcomic_image_threads}",
            f"    photo: {plugin.jmcomic_photo_threads}",
        ]
    )
    option_path.write_text("\n".join(option_lines) + "\n", encoding="utf-8")

    pdf_password = core._generate_pdf_password()
    option = create_option_by_file(str(option_path))
    if not _apply_auth_cookies(option, plugin, core):
        _login_from_config(plugin, core)
        if not _apply_auth_cookies(option, plugin, core):
            raise RuntimeError("禁漫自动登录没有返回有效 token")

    title = _get_album_title(option, numeric_id, f"禁漫 {comic_id}", core)

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
        if _looks_like_authentication_error(exc) and _has_configured_credentials(plugin):
            try:
                _login_from_config(plugin, core)
                retry_option = create_option_by_file(str(option_path))
                if not _apply_auth_cookies(retry_option, plugin, core):
                    raise RuntimeError("禁漫自动登录没有返回有效 token")
                jm_download_album(
                    numeric_id,
                    retry_option,
                    extra=Feature.export_pdf(
                        pdf_dir=str(pdf_output_dir),
                        filename_rule="Aid",
                        encrypt={"password": pdf_password},
                        delete_original_file=False,
                    ),
                    check_exception=True,
                )
                option = retry_option
            except Exception as relogin_exc:
                if _is_domain_initialization_error(relogin_exc):
                    _raise_domain_initialization_error(relogin_exc, plugin)
                raise RuntimeError(
                    "禁漫登录状态已失效，自动登录失败，请检查 jmcomic.username 和 jmcomic.password"
                ) from relogin_exc
        elif _is_domain_initialization_error(exc):
            _raise_domain_initialization_error(exc, plugin)
        else:
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
    image_paths = core._collect_image_files(jm_work_dir)
    page_count = len(image_paths)
    if not generated_pdfs:
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
                "title": title,
                "page_count": page_count,
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
        title=title,
        page_count=page_count,
        pdf_path=pdf_path,
        pdf_password=pdf_password,
        metadata_path=metadata_path,
    )
