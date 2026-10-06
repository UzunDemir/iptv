"""Разбор и запись M3U/M3U8-плейлистов (формат IPTV)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

ATTR_RE = re.compile(r'([A-Za-z0-9\-_]+)="([^"]*)"')


@dataclass
class Channel:
    name: str
    url: str = ""
    attrs: Dict[str, str] = field(default_factory=dict)
    user_agent: Optional[str] = None
    source: str = ""

    @property
    def tvg_id(self) -> str:
        return self.attrs.get("tvg-id", "")

    def dedupe_key(self) -> str:
        """Ключ для схлопывания дубликатов: tvg-id, иначе имя канала."""
        if self.tvg_id:
            return self.tvg_id.strip().lower()
        return self.name.strip().lower()


def parse_m3u(text: str, source: str = "") -> List[Channel]:
    """Парсит текст плейлиста в список Channel."""
    channels: List[Channel] = []
    pending: Optional[Channel] = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#EXTINF"):
            name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            attrs = dict(ATTR_RE.findall(line))
            pending = Channel(name=name, attrs=attrs, source=source)

        elif line.upper().startswith("#EXTVLCOPT") and pending is not None:
            if "http-user-agent=" in line.lower():
                pending.user_agent = line.split("=", 1)[1].strip()

        elif line.startswith("#"):
            continue  # прочие служебные теги

        else:
            if pending is not None:
                pending.url = line
                channels.append(pending)
                pending = None

    return channels


def write_m3u(channels: List[Channel], path: str) -> None:
    """Пишет список каналов в M3U-файл (UTF-8, LF)."""
    lines = ["#EXTM3U"]
    for ch in channels:
        attrs = " ".join(f'{k}="{v}"' for k, v in ch.attrs.items() if v)
        head = "#EXTINF:-1"
        if attrs:
            head += " " + attrs
        head += "," + (ch.name or ch.url)
        lines.append(head)
        if ch.user_agent:
            lines.append(f"#EXTVLCOPT:http-user-agent={ch.user_agent}")
        lines.append(ch.url)

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
