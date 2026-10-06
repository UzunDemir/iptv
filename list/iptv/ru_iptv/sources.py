"""Загрузка M3U-источников: URL или локальные файлы."""

from __future__ import annotations

import os
from typing import List, Optional

import requests

DEFAULT_SOURCES = [
    "https://iptv-org.github.io/iptv/countries/ru.m3u",
    "https://iptv-org.github.io/iptv/languages/rus.m3u",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


def load_sources(path: Optional[str] = None) -> List[str]:
    """Читает список источников из файла (по одному на строку); # — комментарий."""
    path = path or "sources.txt"
    if os.path.isfile(path):
        items: List[str] = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                items.append(line)
        if items:
            return items
    return list(DEFAULT_SOURCES)


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def fetch_source(src: str, timeout: float = 30.0) -> str:
    """Возвращает текст плейлиста из URL или локального файла."""
    if os.path.isfile(src):
        with open(src, "rb") as f:
            return _decode(f.read())
    resp = requests.get(src, timeout=timeout, headers={"User-Agent": UA})
    resp.raise_for_status()
    return _decode(resp.content)
