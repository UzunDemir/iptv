"""Асинхронная проверка живости IPTV-потоков (HLS, MPEG-TS, DASH).

Поток считается рабочим, только если сервер не просто ответил 200 OK,
а реально отдал данные: для HLS — скачан первый сегмент (и init-сегмент
для fMP4), для MPEG-TS — получены байты потока, для DASH — валидный MPD.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import aiohttp

from .m3u import Channel

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

_RES_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)")
_BW_RE = re.compile(r"BANDWIDTH=(\d+)")
_MAP_RE = re.compile(r'URI="([^"]+)"')

READ_CAP = 512 * 1024   # максимум читаемых байт плейлиста
RAW_MIN_BYTES = 4096    # минимум байт для raw-потока
SEG_MIN_BYTES = 188     # один пакет MPEG-TS


@dataclass
class CheckResult:
    channel: Channel
    ok: bool = False
    status: Optional[int] = None
    error: Optional[str] = None
    latency_ms: Optional[int] = None
    kind: str = "?"
    resolution: Optional[str] = None
    width: int = 0
    height: int = 0
    bandwidth: Optional[int] = None
    final_url: str = ""

    def error_category(self) -> str:
        if self.ok:
            return "ok"
        err = (self.error or "unknown").lower()
        if "timeout" in err:
            return "timeout"
        if err.startswith("http 4"):
            return "http_4xx"
        if err.startswith("http 5"):
            return "http_5xx"
        if err.startswith("http"):
            return "http_other"
        if "dns" in err or "getaddrinfo" in err:
            return "dns"
        if "connect" in err or "refused" in err:
            return "connect"
        if "disconnect" in err:
            return "disconnect"
        if "no data" in err or "empty" in err or "too little" in err:
            return "empty"
        if "html" in err or "json" in err:
            return "not_a_stream"
        return "other"


def _timeout(total: float) -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(
        total=total,
        connect=min(6.0, total),
        sock_read=min(6.0, total),
    )


def _origin(url: str) -> Optional[str]:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}/"
    return None


def _err_name(e: Exception) -> str:
    if isinstance(e, asyncio.TimeoutError):
        return "timeout"
    text = str(e).lower()
    if "getaddrinfo" in text or "name resolution" in text:
        return "dns error"
    if "cannot connect" in text or "connect call failed" in text or "connection refused" in text:
        return "connect error"
    if isinstance(e, aiohttp.ServerDisconnectedError):
        return "disconnected"
    return type(e).__name__


async def _read_text(resp, first: bytes, cap: int = READ_CAP) -> str:
    """Дочитывает тело ответа до конца (или cap) и декодирует как текст."""
    buf = bytearray(first)
    while len(buf) < cap:
        try:
            chunk = await resp.content.readany()
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    return bytes(buf).decode("utf-8", "replace")


def parse_master(text: str) -> List[Tuple[int, int, int, str]]:
    """Разбирает master-плейлист: список (bandwidth, width, height, url)."""
    lines = [ln.strip() for ln in text.splitlines()]
    out: List[Tuple[int, int, int, str]] = []
    for i, ln in enumerate(lines):
        if not ln.startswith("#EXT-X-STREAM-INF"):
            continue
        bandwidth = 0
        width = 0
        height = 0
        m = _BW_RE.search(ln)
        if m:
            bandwidth = int(m.group(1))
        m = _RES_RE.search(ln)
        if m:
            width, height = int(m.group(1)), int(m.group(2))
        for nxt in lines[i + 1:]:
            if nxt and not nxt.startswith("#"):
                out.append((bandwidth, width, height, nxt))
                break
    return out


def first_segment(text: str, base: str) -> Tuple[Optional[str], Optional[str]]:
    """Первый сегмент медиа-плейлиста и опциональный init-сегмент (EXT-X-MAP)."""
    init = None
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if ln.startswith("#EXT-X-MAP"):
            m = _MAP_RE.search(ln)
            if m:
                init = urljoin(base, m.group(1))
        elif not ln.startswith("#"):
            return urljoin(base, ln), init
    return None, init


async def _poke(session, url, headers, timeout, min_bytes: int = SEG_MIN_BYTES):
    """Качает начало сегмента. Возвращает (успех, описание ошибки)."""
    try:
        async with session.get(url, headers=headers, timeout=_timeout(timeout),
                               allow_redirects=True) as resp:
            if resp.status != 200:
                return False, f"segment HTTP {resp.status}"
            got = 0
            timed_out = False
            while got < 32768:
                try:
                    chunk = await resp.content.readany()
                except asyncio.TimeoutError:
                    timed_out = True
                    break
                if not chunk:
                    break
                got += len(chunk)
            if got >= min_bytes:
                return True, None
            if got > 0:
                return False, "segment stalled"
            if timed_out:
                return False, "segment timeout"
            return False, "empty segment"
    except asyncio.TimeoutError:
        return False, "segment timeout"
    except (aiohttp.ClientError, OSError) as e:
        return False, f"segment {_err_name(e)}"


async def _get_text(session, url, headers, timeout):
    """Загружает текст плейлиста. Возвращает (текст, ошибка)."""
    try:
        async with session.get(url, headers=headers, timeout=_timeout(timeout),
                               allow_redirects=True) as resp:
            if resp.status != 200:
                return None, f"HTTP {resp.status}"
            try:
                first = await resp.content.readany()
            except asyncio.TimeoutError:
                return None, "timeout"
            if not first:
                return None, "empty playlist"
            return await _read_text(resp, first), None
    except asyncio.TimeoutError:
        return None, "timeout"
    except (aiohttp.ClientError, OSError) as e:
        return None, _err_name(e)


async def _check_hls(res: CheckResult, session, text: str, base: str,
                     headers, timeout) -> CheckResult:
    res.kind = "hls"

    if "#EXT-X-STREAM-INF" in text:
        variants = parse_master(text)
        if not variants:
            res.error = "empty master playlist"
            return res
        variants.sort(key=lambda v: (v[2], v[0]), reverse=True)  # высота, затем битрейт
        last_err = "no variant worked"
        for bandwidth, width, height, vurl in variants[:3]:
            vfull = urljoin(base, vurl)
            vtext, verr = await _get_text(session, vfull, headers, timeout)
            if vtext is None:
                last_err = verr or "variant unavailable"
                continue
            seg, init = first_segment(vtext, vfull)
            if not seg:
                last_err = "no segments in variant"
                continue
            if init:
                await _poke(session, init, headers, timeout)  # best-effort
            ok, serr = await _poke(session, seg, headers, timeout)
            if ok:
                res.ok = True
                res.bandwidth = bandwidth or None
                res.width = width
                res.height = height
                if width and height:
                    res.resolution = f"{width}x{height}"
                return res
            last_err = serr or "segment unavailable"
        res.error = last_err
        return res

    # Медиа-плейлист: проверяем первый сегмент
    seg, init = first_segment(text, base)
    if not seg:
        res.error = "no segments in playlist"
        return res
    if init:
        await _poke(session, init, headers, timeout)  # best-effort
    ok, serr = await _poke(session, seg, headers, timeout)
    if ok:
        res.ok = True
    else:
        res.error = serr or "segment unavailable"
    return res


async def check_channel(session, ch: Channel, timeout: float) -> CheckResult:
    """Проверяет один поток. Возвращает CheckResult (без исключений)."""
    t0 = time.monotonic()
    headers = {"User-Agent": ch.user_agent or DEFAULT_UA, "Accept": "*/*"}
    ref = _origin(ch.url)
    if ref:
        headers["Referer"] = ref

    res = CheckResult(channel=ch)
    try:
        async with session.get(ch.url, headers=headers, timeout=_timeout(timeout),
                               allow_redirects=True, max_redirects=10) as resp:
            res.status = resp.status
            res.final_url = str(resp.url)
            try:
                first = await resp.content.readany()
            except asyncio.TimeoutError:
                res.latency_ms = int((time.monotonic() - t0) * 1000)
                res.error = "timeout"
                return res
            res.latency_ms = int((time.monotonic() - t0) * 1000)

            if res.status != 200:
                res.error = f"HTTP {res.status}"
                return res
            if not first:
                res.error = "no data"
                return res

            head = first.lstrip()

            if head[:7].upper() == b"#EXTM3U":
                text = await _read_text(resp, first)
                return await _check_hls(res, session, text, res.final_url, headers, timeout)

            if b"<MPD" in first[:4096]:
                text = await _read_text(resp, first, 256 * 1024)
                res.kind = "dash"
                if "<Period" in text:
                    res.ok = True
                else:
                    res.error = "invalid MPD"
                return res

            if head[:1] in (b"<", b"{"):
                res.kind = "html"
                res.error = "not a stream (html/json)"
                return res

            # Сырой поток (MPEG-TS или live-контейнер): нужны реальные байты
            data = bytearray(first)
            while len(data) < 32768:
                try:
                    chunk = await resp.content.readany()
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    break
                data += chunk
            res.kind = "ts" if data[:1] == b"\x47" else "raw"
            if len(data) >= RAW_MIN_BYTES:
                res.ok = True
            else:
                res.error = "too little data"
            return res

    except asyncio.TimeoutError:
        res.error = "timeout"
    except (aiohttp.ClientError, OSError) as e:
        res.error = _err_name(e)
    except Exception as e:  # страховка от неожиданных исключений
        res.error = f"{type(e).__name__}: {e}"
    return res


async def check_with_retry(session, ch: Channel, timeout: float,
                           retries: int = 1) -> CheckResult:
    """Проверка с повторами при сетевых сбоях.

    Повторяем сетевые ошибки и таймауты (они бывают транзиентными),
    но не повторяем явные ответы 4xx — это осмысленный отказ.
    """
    last: Optional[CheckResult] = None
    for _ in range(retries + 1):
        try:
            res = await asyncio.wait_for(check_channel(session, ch, timeout),
                                         timeout=timeout + 4)
        except asyncio.TimeoutError:
            res = CheckResult(channel=ch, error="timeout")
        except Exception as e:
            res = CheckResult(channel=ch, error=f"{type(e).__name__}: {e}")

        if res.ok:
            return res
        last = res
        if res.status is not None and 400 <= res.status < 500:
            break
    return last  # type: ignore[return-value]


async def check_many(channels: List[Channel], workers: int = 64, timeout: float = 12.0,
                     retries: int = 1,
                     on_result: Optional[Callable[[CheckResult], None]] = None,
                     ) -> List[CheckResult]:
    """Проверяет список каналов параллельно; on_result вызывается по мере готовности."""
    sem = asyncio.Semaphore(workers)
    connector = aiohttp.TCPConnector(limit=workers, limit_per_host=16, ssl=False)
    results: List[CheckResult] = []

    async with aiohttp.ClientSession(connector=connector) as session:
        async def one(ch: Channel) -> CheckResult:
            async with sem:
                return await check_with_retry(session, ch, timeout, retries)

        tasks = [asyncio.ensure_future(one(c)) for c in channels]
        for fut in asyncio.as_completed(tasks):
            res = await fut
            results.append(res)
            if on_result:
                on_result(res)

    return results
