"""Печатает число строк файла и последние N строк: python tools/tail.py FILE [N]."""

import sys

path = sys.argv[1]
n = int(sys.argv[2]) if len(sys.argv) > 2 else 10

with open(path, "r", encoding="utf-8", errors="replace") as f:
    lines = f.readlines()

print(f"lines: {len(lines)}")
for line in lines[-n:]:
    print(line.rstrip())
