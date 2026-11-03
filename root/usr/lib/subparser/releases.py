# -*- coding: utf-8 -*-
"""Работа с GitHub Releases SubParser."""
import json
import re
import urllib.request

from .config import get_ssl_ctx

REPO_USER = "ib0lit"
REPO_NAME = "SubParser"
API_ROOT = f"https://api.github.com/repos/{REPO_USER}/{REPO_NAME}"
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def normalize_tag(value):
    """Приводит выбор пользователя к безопасному значению.

    Возвращает ``branch``, ``latest`` или конкретный тег релиза.
    """
    tag = str(value or "").strip()
    if not tag:
        return "branch"
    low = tag.lower()
    if low in ("branch", "main"):
        return "branch"
    if low == "latest":
        return "latest"
    if not _TAG_RE.match(tag):
        raise ValueError("invalid release tag")
    return tag


def _get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "OpenWrt-SubParser"})
    with urllib.request.urlopen(req, timeout=timeout, context=get_ssl_ctx()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_release(item):
    tag = str(item.get("tag_name") or "").strip()
    if not tag or not _TAG_RE.match(tag):
        return None
    assets = item.get("assets") if isinstance(item.get("assets"), list) else []
    tar_url = ""
    zip_url = ""
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        # В репозитории есть релизы slim-сборок sing-box; для обновления
        # SubParser подходят только архивы самого сервиса.
        if not url or not name.startswith("subparser-"):
            continue
        if name.endswith(".tar.gz") and not tar_url:
            tar_url = url
        elif name.endswith(".zip") and not zip_url:
            zip_url = url
    if not tar_url and not zip_url:
        return None
    return {
        "tag": tag,
        "version": tag[1:] if tag.lower().startswith("v") else tag,
        "name": str(item.get("name") or tag),
        "prerelease": bool(item.get("prerelease")),
        "draft": bool(item.get("draft")),
        "published_at": str(item.get("published_at") or ""),
        "body": str(item.get("body") or ""),
        "asset_tar": tar_url,
        "asset_zip": zip_url,
    }


def get_releases(limit=20):
    """Возвращает опубликованные релизы, начиная с самого нового."""
    limit = max(1, min(int(limit), 100))
    data = _get_json(f"{API_ROOT}/releases?per_page={limit}")
    result = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict) or item.get("draft"):
                continue
            release = _parse_release(item)
            if release:
                result.append(release)
    return result


def get_release(tag):
    """Возвращает данные конкретного релиза или последнего (``latest``)."""
    tag = normalize_tag(tag)
    if tag in ("", "branch"):
        raise ValueError("release is not selected")
    if tag == "latest":
        releases = get_releases(30)
        if not releases:
            raise RuntimeError("no releases found")
        return releases[0]
    data = _get_json(f"{API_ROOT}/releases/tags/{tag}")
    release = _parse_release(data if isinstance(data, dict) else {})
    if not release:
        raise RuntimeError("release not found")
    return release
