"""Summarise an access log: lines look like
<ISO time> <level> <path> <status> <ms>
and print, per path, the request count and mean latency."""
import sys
from collections import defaultdict


def parse(lines):
    stats = defaultdict(lambda: [0, 0.0])
    for line in lines:
        line = line.rstrip("\n")
        if not line:
            continue
        ts, level, path, status, ms = line.split(" ")
        stats[path][0] += 1
        stats[path][1] += float(ms)
    return stats


def main(path):
    with open(path, encoding="utf-8") as f:
        stats = parse(f)
    for p, (n, total) in sorted(stats.items()):
        print("%-40s %5d  %7.1f ms" % (p, n, total / n))


if __name__ == "__main__":
    main(sys.argv[1])
