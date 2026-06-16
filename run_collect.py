#!/usr/bin/env python3
"""
run_collect.py — orchestrate collect_archive.py over a long window, month by month.

Each calendar month is run as an isolated subprocess with its own checkpoint and
output, so a crash or kill in one month never corrupts another and the whole
sweep resumes where it left off. After all chunks finish it merges every monthly
CSV into one de-duplicated, timestamp-sorted file.

USAGE
    # full default window (2025-10-07 -> today), both outlets
    python3 run_collect.py

    # explicit range / outlet / concurrency
    python3 run_collect.py --start 2025-10-07 --end 2026-06-15 --outlets dawn --workers 6

    # background, logged:
    nohup python3 run_collect.py > run_collect.log 2>&1 &
    tail -f run_collect.log

Re-running is safe: completed months skip instantly via their checkpoints; the
current month re-scans its recent days for late-published stories.
"""

import argparse
import csv
import os
import subprocess
import sys
from datetime import date, timedelta

DATA_DIR = "data"
LOG_DIR = "logs"
DEFAULT_START = "2025-10-07"
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collect_archive.py")


def month_chunks(start, end):
    """Yield (chunk_start, chunk_end) clamped to [start, end], one per calendar month."""
    cur = start
    while cur <= end:
        first_of_next = date(cur.year + 1, 1, 1) if cur.month == 12 \
            else date(cur.year, cur.month + 1, 1)
        chunk_end = min(end, first_of_next - timedelta(days=1))
        yield cur, chunk_end
        cur = first_of_next


def run_chunk(c_start, c_end, outlets, workers, sections, retries=2):
    tag = c_start.strftime("%Y_%m")
    out = os.path.join(DATA_DIR, f"intl_{tag}.csv")
    ckpt = os.path.join(DATA_DIR, f"ckpt_{tag}.json")
    log = os.path.join(LOG_DIR, f"{tag}.log")
    cmd = [sys.executable, SCRIPT,
           "--start", c_start.isoformat(), "--end", c_end.isoformat(),
           "--outlets", *outlets, "--sections", *sections,
           "--workers", str(workers), "--out", out, "--checkpoint", ckpt]
    for attempt in range(1, retries + 2):
        print(f"[{tag}] {c_start} -> {c_end}  (attempt {attempt})", flush=True)
        with open(log, "a", encoding="utf-8") as lf:
            rc = subprocess.call(cmd, stdout=lf, stderr=subprocess.STDOUT)
        if rc == 0:
            print(f"[{tag}] done -> {out}", flush=True)
            return out
        print(f"[{tag}] exit {rc}; see {log}", flush=True)
    print(f"[{tag}] FAILED after retries; continuing", flush=True)
    return out if os.path.exists(out) else None


def merge(paths, merged):
    rows = {}
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[r["url"]] = r
    items = sorted(rows.values(), key=lambda r: r.get("timestamp") or "9999")
    cols = ["outlet", "section", "date", "timestamp", "author",
            "headline", "url", "article_id", "body"]
    with open(merged, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in items:
            w.writerow({c: r.get(c, "") for c in cols})
    return len(items)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", default=DEFAULT_START)
    p.add_argument("--end", default=date.today().isoformat())
    p.add_argument("--outlets", nargs="+", default=["dawn", "tribune"])
    p.add_argument("--sections", nargs="+", default=["world", "international", "foreign"])
    p.add_argument("--workers", type=int, default=5)
    p.add_argument("--merged", default="intl_news.csv")
    args = p.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    outputs = []
    for c_start, c_end in month_chunks(start, end):
        outputs.append(run_chunk(c_start, c_end, args.outlets, args.workers, args.sections))

    total = merge(outputs, args.merged)
    print(f"\nMerged {total} articles -> {args.merged}")
    print(f"Next:  python3 extract_sources.py {args.merged}")


if __name__ == "__main__":
    main()
