"""Сводка качества результата: python tools/stats.py playlist.m3u report.json"""

import json
import re
import sys
from collections import Counter

pl_path, rep_path = sys.argv[1], sys.argv[2]

text = open(pl_path, encoding="utf-8").read()
lines = text.splitlines()
infos = [ln for ln in lines if ln.startswith("#EXTINF")]
urls = [ln for ln in lines if ln and not ln.startswith("#")]

groups = Counter(
    re.search(r'group-title="([^"]*)"', ln).group(1)
    for ln in infos if 'group-title="' in ln
)

print("playlist entries:", len(infos))
print("playlist urls:   ", len(urls))
print("duplicate urls:  ", len(urls) - len(set(urls)))
print("unique names:    ", len(set(ln.rsplit(",", 1)[-1] for ln in infos)))
print("groups:")
for g, n in groups.most_common(12):
    print(f"  {g or '(empty)'}: {n}")

doc = json.load(open(rep_path, encoding="utf-8"))
print("report summary:", json.dumps(doc["summary"], ensure_ascii=True))
results = doc["results"]
ok = [r for r in results if r["ok"]]


def height_of(r):
    if r.get("height"):
        return r["height"]
    m = re.match(r"(\d+)x(\d+)", r.get("resolution") or "")
    return int(m.group(2)) if m else 0


hd = [r for r in ok if height_of(r) >= 720]
fast = sorted((r for r in ok if r["latency_ms"] is not None),
              key=lambda r: r["latency_ms"])[:5]

print("ok:", len(ok), "of", len(results), "| >=720p:", len(hd))
print("kinds(ok):", dict(Counter(r["kind"] for r in ok)))
for r in fast:
    print(f"  fast: {r['name']}  {r['latency_ms']}ms  {r['resolution']}")

print("first entry sample:")
for ln in lines[:3]:
    print(" ", ln)
