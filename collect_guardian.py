#!/usr/bin/env python3
"""
collect_guardian.py — international news from the Guardian Open Platform (Content API).

A third source alongside the Dawn/Tribune scrapers, for a Western/international
view focused by default on US / Iran / Pakistan relations. Unlike the scrapers,
the Guardian API returns the FULL plain-text body plus date, byline, and section
in a single call, so there is no archive walking or HTML parsing: it runs in
minutes.

OUTPUT (same schema as collect_archive.py, so it merges into the same dataset)
  outlet, section, date, timestamp, author, headline, url, article_id, body
  outlet is always "guardian".

STRATEGY
  Exhaustive harvest by date-windowing (monthly), paging 1..pages within each
  window ordered oldest-first. This is stable and resumable; it avoids the quota
  burn and result-set drift of deep-paging one wide query. Per-window progress is
  checkpointed so a killed or quota-stopped run resumes losslessly.

API KEY
  --api-key, or the GUARDIAN_API_KEY environment variable. Never hardcoded.

USAGE
    export GUARDIAN_API_KEY=...
    # default: US/Iran/Pakistan relations in section=world, 2025-10-06 -> today
    python3 collect_guardian.py

    # all world news (no topic filter)
    python3 collect_guardian.py --q "" --out guardian_world.csv

    # smaller test
    python3 collect_guardian.py --start 2026-04-01 --end 2026-04-30 --out g_smoke.csv

SETUP
    pip install requests
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:
    sys.exit("Run:  pip install requests")

try:
    from urllib3.util.retry import Retry
except Exception:
    Retry = None

ENDPOINT = "https://content.guardianapis.com/search"
DEFAULT_START = "2025-10-06"
DEFAULT_Q = 'Pakistan AND (Iran OR "United States" OR US)'
MAX_PAGE_SIZE = 200
QUOTA_FLOOR = 5  # rotate to the next key when this few daily calls remain
# files searched for extra keys (one per line, '#' comments allowed); kept out of git
KEY_FILES = ["guardian_keys.txt", os.path.expanduser("~/.guardian_keys")]

_KEYS = []      # ordered, de-duplicated candidate API keys
_KEY_IDX = 0    # index of the key currently in use


def load_keys(cli_key, keys_file):
    keys = []

    def add(blob):
        for part in re.split(r"[,\s]+", (blob or "").strip()):
            if part and part not in keys:
                keys.append(part)

    add(cli_key)
    add(os.environ.get("GUARDIAN_API_KEY"))
    add(os.environ.get("GUARDIAN_API_KEYS"))
    for pth in ([keys_file] if keys_file else []) + KEY_FILES:
        if pth and os.path.exists(pth):
            with open(pth, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        add(line)
    return keys


def make_session(pool=8):
    s = requests.Session()
    if Retry is not None:
        retry = Retry(total=4, connect=4, read=4, backoff_factor=2,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset(["GET"]),
                      respect_retry_after_header=True)
        adapter = HTTPAdapter(max_retries=retry, pool_connections=pool, pool_maxsize=pool)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
    return s


SESSION = make_session()


def parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def load_ckpt(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            ck = json.load(f)
        ck.setdefault("done_windows", [])
        ck.setdefault("window_progress", {})
        return ck
    return {"done_windows": [], "window_progress": {}}


def save_ckpt(path, ck):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ck, f)
    os.replace(tmp, path)


def month_chunks(start, end):
    cur = start
    while cur <= end:
        first_of_next = date(cur.year + 1, 1, 1) if cur.month == 12 \
            else date(cur.year, cur.month + 1, 1)
        yield cur, min(end, first_of_next - timedelta(days=1))
        cur = first_of_next


def build_params(q, section, tag, from_date, to_date, page, page_size):
    p = {
        "from-date": from_date.isoformat(),
        "to-date": to_date.isoformat(),
        "order-by": "oldest",
        "order-date": "published",
        "page": page,
        "page-size": page_size,
        "show-fields": "bodyText,byline",
    }
    if q:
        p["q"] = q
    if section:
        p["section"] = section
    if tag:
        p["tag"] = tag
    return p


def remaining_calls(headers):
    for k, v in headers.items():
        if k.lower() in ("x-ratelimit-remaining-day", "x-ratelimit-remaining"):
            try:
                return int(v)
            except ValueError:
                return None
    return None


def fetch_page(params, tries=4):
    """Try the current key; on rejection/exhaustion rotate to the next stored key.
    Returns (response_dict, remaining_day) or (None, None) once all keys are spent."""
    global _KEY_IDX
    while _KEY_IDX < len(_KEYS):
        q = dict(params, **{"api-key": _KEYS[_KEY_IDX]})
        rotate = False
        for attempt in range(tries):
            try:
                r = SESSION.get(ENDPOINT, params=q, timeout=(10, 60))
                if r.status_code == 200:
                    body = r.json().get("response", {})
                    if body.get("status") == "ok":
                        return body, remaining_calls(r.headers)
                if r.status_code in (401, 403):       # bad/revoked key -> next key
                    print(f"  key #{_KEY_IDX + 1} rejected ({r.status_code}); rotating", flush=True)
                    rotate = True
                    break
                if r.status_code in (429, 503):       # rate/quota -> brief backoff, then rotate
                    time.sleep(5 * (attempt + 1))
                    continue
            except Exception:
                time.sleep(3 * (attempt + 1))
        if not rotate:
            print(f"  key #{_KEY_IDX + 1} exhausted/unreachable; rotating", flush=True)
        _KEY_IDX += 1
    return None, None


def row_from_result(res):
    fields = res.get("fields", {}) or {}
    ts = res.get("webPublicationDate", "") or ""
    return {
        "outlet": "guardian",
        "section": res.get("sectionId", ""),
        "date": ts[:10],
        "timestamp": ts,
        "author": fields.get("byline", "") or "",
        "headline": res.get("webTitle", ""),
        "url": res.get("webUrl", ""),
        "article_id": res.get("id", ""),
        "body": fields.get("bodyText", "") or "",
    }


def collect_window(q, section, tag, pillar, c_start, c_end, ck, ckpath, out, page_size, type_filter):
    global _KEY_IDX
    key = c_start.strftime("%Y-%m")
    if key in ck["done_windows"]:
        return 0
    page = ck["window_progress"].get(key, {}).get("next_page", 1)
    kept = 0
    while True:
        params = build_params(q, section, tag, c_start, c_end, page, page_size)
        resp, remaining = fetch_page(params)
        if resp is None:
            print(f"  {key}: page {page} failed on all keys; progress saved, rerun to resume",
                  flush=True)
            save_ckpt(ckpath, ck)
            out.flush()
            sys.exit(0)
        pages = resp.get("pages", 0)
        for res in resp.get("results", []):
            if type_filter == "article" and res.get("type") != "article":
                continue
            if pillar != "all" and res.get("pillarId") != pillar:
                continue
            row = row_from_result(res)
            if len(row["body"]) < 1:
                continue
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            kept += 1
        if page >= pages or pages == 0:
            ck["done_windows"].append(key)
            ck["window_progress"].pop(key, None)
            save_ckpt(ckpath, ck)
            print(f"  {key}: done ({pages} pages, {kept} kept)", flush=True)
            break
        ck["window_progress"][key] = {"next_page": page + 1, "pages": pages}
        save_ckpt(ckpath, ck)
        if remaining is not None and remaining < QUOTA_FLOOR:  # this key is nearly spent
            _KEY_IDX += 1
            if _KEY_IDX >= len(_KEYS):
                print(f"  {key}: all keys near daily quota; stopping, rerun to resume", flush=True)
                out.flush()
                sys.exit(0)
            print(f"  {key}: key near quota, switched to key #{_KEY_IDX + 1}", flush=True)
        page += 1
    return kept


def write_sorted_csv(jsonl_path, out_csv):
    rows = {}
    if os.path.exists(jsonl_path):
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    rows[r["article_id"]] = r          # de-dup by Guardian id
    items = sorted(rows.values(), key=lambda r: parse_ts(r.get("timestamp")) or datetime.max)
    cols = ["outlet", "section", "date", "timestamp", "author",
            "headline", "url", "article_id", "body"]
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in items:
            w.writerow({c: r.get(c, "") for c in cols})
    return len(items)


def main():
    global _KEYS
    p = argparse.ArgumentParser()
    p.add_argument("--api-key", help="one key, or comma-separated keys (also reads GUARDIAN_API_KEY[S])")
    p.add_argument("--keys-file", help="file of API keys, one per line (fallbacks)")
    p.add_argument("--q", default=DEFAULT_Q, help='free-text query; pass --q "" for none')
    p.add_argument("--section", default="world", help='section id; pass --section "" for none')
    p.add_argument("--tag", default=None, help="Guardian tag filter, e.g. world/iran (one run = one tag)")
    p.add_argument("--pillar", default="news",
                   help="keep only this pillar (news/opinion/sport/...); 'all' to disable")
    p.add_argument("--start", default=DEFAULT_START, help="YYYY-MM-DD (default 2025-10-06)")
    p.add_argument("--end", default=date.today().isoformat(), help="YYYY-MM-DD (default today)")
    p.add_argument("--page-size", type=int, default=MAX_PAGE_SIZE)
    p.add_argument("--type", default="article", choices=["article", "all"])
    p.add_argument("--out", default="guardian_news.csv")
    p.add_argument("--checkpoint", default="guardian_checkpoint.json")
    args = p.parse_args()

    _KEYS = load_keys(args.api_key, args.keys_file)
    if not _KEYS:
        sys.exit("No API key. Pass --api-key, set GUARDIAN_API_KEY[S], or add a guardian_keys.txt.")
    page_size = max(1, min(args.page_size, MAX_PAGE_SIZE))
    pillar = "all" if args.pillar == "all" else f"pillar/{args.pillar}"
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    jsonl_path = args.out + ".jsonl"
    ck = load_ckpt(args.checkpoint)

    print(f"Guardian harvest {start} -> {end} | section={args.section or 'ALL'} "
          f"| tag={args.tag or 'NONE'} | q={args.q or 'NONE'} | pillar={args.pillar} "
          f"| type={args.type} | {len(_KEYS)} key(s)")
    out = open(jsonl_path, "a", encoding="utf-8")
    total_kept = 0
    for c_start, c_end in month_chunks(start, end):
        total_kept += collect_window(args.q, args.section, args.tag, pillar, c_start, c_end,
                                     ck, args.checkpoint, out, page_size, args.type)
    out.close()

    total = write_sorted_csv(jsonl_path, args.out)
    print(f"\nDone -> {args.out} ({total} articles, sorted by timestamp; +{total_kept} this run)")


if __name__ == "__main__":
    main()
