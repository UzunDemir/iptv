"""CLI: python -m ru_iptv — сбор рабочих российских каналов в один плейлист."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Dict, List, Optional, Tuple

from .checker import CheckResult, check_many
from .m3u import Channel, parse_m3u, write_m3u
from .sources import fetch_source, load_sources


def _setup_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _progress(res: CheckResult) -> None:
    if res.ok:
        extra = res.resolution or res.kind
        print(f"[ok]   {res.channel.name}  [{extra}, {res.latency_ms} ms]", flush=True)
    else:
        print(f"[fail] {res.channel.name}  ({res.error})", flush=True)


def _collect_candidates(sources: List[str], limit: int = 0
                        ) -> Tuple[List[Channel], List[Tuple[str, int]]]:
    """Скачивает источники, парсит каналы, убирает дубликаты по URL."""
    channels: List[Channel] = []
    seen = set()
    stats: List[Tuple[str, int]] = []
    for src in sources:
        try:
            text = fetch_source(src)
        except Exception as e:
            print(f"[warn] источник недоступен: {src} ({type(e).__name__}: {e})")
            stats.append((src, 0))
            continue
        added = 0
        for ch in parse_m3u(text, source=src):
            url = (ch.url or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            ch.url = url
            channels.append(ch)
            added += 1
        stats.append((src, added))
        print(f"[src]  {src}: {added} каналов")
    if limit > 0:
        channels = channels[:limit]
    return channels, stats


def _build_playlist(results: List[CheckResult], dedupe: bool = True,
                    min_height: int = 0, hls_only: bool = False
                    ) -> Tuple[List[Channel], int]:
    """Отбирает рабочие потоки, сортирует и схлопывает дубликаты каналов."""
    ok_results = [r for r in results if r.ok]
    usable = [
        r for r in ok_results
        if r.height >= min_height and (not hls_only or r.kind == "hls")
    ]

    groups: Dict[str, List[CheckResult]] = {}
    order: List[str] = []
    for r in usable:
        key = r.channel.dedupe_key()
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)

    chosen: List[Channel] = []
    for key in order:
        items = groups[key]
        items.sort(key=lambda r: (
            -r.height,
            r.latency_ms if r.latency_ms is not None else 10 ** 9,
            -(r.bandwidth or 0),
        ))
        if dedupe:
            chosen.append(items[0].channel)
        else:
            chosen.extend(i.channel for i in items)

    chosen.sort(key=lambda c: ((c.attrs.get("group-title") or "").lower(), c.name.lower()))
    return chosen, len(ok_results)


def _write_report(path: str, src_stats: List[Tuple[str, int]],
                  results: List[CheckResult], playlist_count: int) -> None:
    categories: Dict[str, int] = {}
    for r in results:
        cat = r.error_category()
        categories[cat] = categories.get(cat, 0) + 1

    doc = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sources": [{"url": s, "added": n} for s, n in src_stats],
        "summary": {
            "candidates": len(results),
            "ok": sum(1 for r in results if r.ok),
            "playlist_channels": playlist_count,
            "categories": categories,
        },
        "results": [
            {
                "name": r.channel.name,
                "tvg_id": r.channel.tvg_id,
                "group": r.channel.attrs.get("group-title", ""),
                "url": r.channel.url,
                "final_url": r.final_url,
                "ok": r.ok,
                "kind": r.kind,
                "status": r.status,
                "latency_ms": r.latency_ms,
                "resolution": r.resolution,
                "width": r.width,
                "height": r.height,
                "bandwidth": r.bandwidth,
                "error": r.error,
                "category": r.error_category(),
                "source": r.channel.source,
            }
            for r in sorted(results,
                            key=lambda x: (not x.ok, -(x.height or 0),
                                           x.latency_ms or 10 ** 9))
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)


def main(argv: Optional[List[str]] = None) -> int:
    _setup_stdout()

    ap = argparse.ArgumentParser(
        prog="ru_iptv",
        description="Собирает рабочие потоки российских каналов "
                    "из публичных M3U-источников в один плейлист.",
    )
    ap.add_argument("--sources", default=None,
                    help="файл со списком источников (по умолчанию sources.txt)")
    ap.add_argument("--out", default="playlist.m3u",
                    help="итоговый плейлист (по умолчанию playlist.m3u)")
    ap.add_argument("--report", default="report.json",
                    help="отчёт о проверке (по умолчанию report.json)")
    ap.add_argument("--workers", type=int, default=64,
                    help="параллельных проверок (64)")
    ap.add_argument("--timeout", type=float, default=12.0,
                    help="таймаут на поток, сек (12)")
    ap.add_argument("--retries", type=int, default=1,
                    help="повторы при сетевых ошибках (1)")
    ap.add_argument("--min-height", type=int, default=0,
                    help="минимальная высота кадра, 0 = без фильтра")
    ap.add_argument("--hls-only", action="store_true",
                    help="оставить только HLS-потоки")
    ap.add_argument("--all-variants", action="store_true",
                    help="не схлопывать дубликаты каналов")
    ap.add_argument("--limit", type=int, default=0,
                    help="ограничить число кандидатов (для тестов)")
    ap.add_argument("--no-report", action="store_true", help="не писать отчёт")
    ap.add_argument("--probe", nargs="*", default=None,
                    help="проверить отдельные ссылки и выйти")
    args = ap.parse_args(argv)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    if args.probe:
        chans = [Channel(name=u, url=u, source="probe") for u in args.probe]
        results = asyncio.run(check_many(chans, workers=min(8, len(chans)),
                                         timeout=args.timeout, retries=0))
        for r in results:
            verdict = "OK  " if r.ok else "FAIL"
            detail = r.resolution or r.kind
            print(f"{verdict} {r.channel.url} -> {detail}, "
                  f"{r.latency_ms} ms, {r.error or ''}")
        return 0 if any(r.ok for r in results) else 1

    sources = load_sources(args.sources)
    print(f"Источников: {len(sources)}")

    channels, src_stats = _collect_candidates(sources, args.limit)
    if not channels:
        print("Кандидаты не найдены — проверьте сеть и список источников.")
        return 1
    print(f"Кандидатов к проверке: {len(channels)} "
          f"(воркеров: {args.workers}, таймаут: {args.timeout:g} с)")

    t0 = time.monotonic()
    try:
        results = asyncio.run(check_many(channels, workers=args.workers,
                                         timeout=args.timeout, retries=args.retries,
                                         on_result=_progress))
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130
    elapsed = time.monotonic() - t0

    chosen, ok_total = _build_playlist(results, dedupe=not args.all_variants,
                                       min_height=args.min_height,
                                       hls_only=args.hls_only)
    write_m3u(chosen, args.out)
    if not args.no_report:
        _write_report(args.report, src_stats, results, len(chosen))

    categories: Dict[str, int] = {}
    for r in results:
        cat = r.error_category()
        categories[cat] = categories.get(cat, 0) + 1

    print()
    print(f"Проверено:    {len(results)} за {elapsed:.0f} с")
    print(f"Рабочих:      {ok_total}")
    print(f"Не работают:  {len(results) - ok_total}")
    parts = ", ".join(f"{k}: {v}" for k, v in
                      sorted(categories.items(), key=lambda x: -x[1]))
    print(f"Итоги:        {parts}")
    print(f"Плейлист:     {args.out} ({len(chosen)} каналов)")
    if not args.no_report:
        print(f"Отчёт:        {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
