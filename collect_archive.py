#!/usr/bin/env python3
"""
collect_archive.py — international (world) news dataset from newspaper archives.

Walks the DAILY DATE ARCHIVES of each outlet across a date range, collects every
article link, then scrapes the bodies and keeps ONLY the ones whose published
section is international/world. Output is one CSV sorted by publish timestamp,
with checkpointing so a multi-hour/day run can stop and resume losslessly.

WHY ARCHIVE WALK + SECTION META
  - A date archive is the full population for a day; section landing pages are
    infinite-scroll and only surface the latest ~40 stories, so they cannot
    reach back in time. The archive is the only way to get history.
  - The authoritative "is this international" signal is the article page's
    <meta property="article:section"> (Dawn -> "world", Tribune -> "World").
    Dawn additionally labels the section inline on the archive list, so we skip
    fetching the clearly-non-world stories. Tribune has no inline label, so we
    fetch and filter on the section meta.

OUTLETS
  Dawn    : date-aware daily archive (https://www.dawn.com/archive/YYYY-MM-DD),
            with an inline section link per story so non-world stories are
            skipped without fetching.
  Tribune : its /archive/<date> ignores the date, so history is discovered from
            the sitemaps (https://tribune.com.pk/sitemap.xml -> posts-N.xml),
            filtered by <lastmod>. No section signal there, so every in-window
            URL is fetched and article:section decides what is kept.
  (The News has no daily archive and no section meta, so it is not supported.)

OUTPUT COLUMNS
  outlet, section, date, timestamp, author, headline, url, article_id, body

WORKFLOW
  1. Phase 1 walks each day, collecting links (+ Dawn section hints) to a
     checkpoint.
  2. Phase 2 scrapes each link, keeps world/international, appends rows to a
     durable .jsonl.
  3. Phase 3 reads the .jsonl, de-duplicates, sorts by timestamp, writes the CSV.
  Stop any time and re-run the same command; it resumes from the checkpoint.

ROBUSTNESS / PERIODIC SCRAPING
  Built to run on a schedule against a persistent checkpoint:
    - --end defaults to today, so each run extends the window to the present.
    - --refresh-days (default 2) re-scans the most recent days, so stories
      published after an earlier run are still picked up.
    - retrying session (connect/read timeouts, backoff on 5xx and 403/429),
      per-URL failure cap, atomic checkpoint, and lossless resume mean a run
      killed by a timeout or rate-limit loses no work.
  e.g. cron daily:  cd <dir> && python3 collect_archive.py >> collect.log 2>&1

SETUP
    pip install requests beautifulsoup4 lxml

USAGE
    # default window: 2025-10-07 -> today, both outlets, world news
    python3 collect_archive.py

    # explicit range / single outlet
    python3 collect_archive.py --start 2025-10-07 --end 2026-06-15 --outlets dawn

    # smaller test first — always do this:
    python3 collect_archive.py --start 2026-06-10 --end 2026-06-12

ETHICS / TERMS
    Public journalism, light footprint: one request every few seconds, a real
    User-Agent, no parallel hammering. Store text + metadata for analysis, cite
    outlets in any publication. Keep it that way.
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin

try:
    import requests
    from bs4 import BeautifulSoup
    from requests.adapters import HTTPAdapter
except ImportError:
    sys.exit("Run:  pip install requests beautifulsoup4 lxml")

try:
    from urllib3.util.retry import Retry
except Exception:
    Retry = None

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
    "Connection": "keep-alive",
}

DEFAULT_START = "2025-10-07"
INTL_SECTIONS = ["world", "international", "foreign"]

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30

# per-request pacing (tunable from the CLI); concurrency spreads these across workers
DELAY_MIN = 1.0
DELAY_MAX = 2.5


def make_session(pool=16):
    s = requests.Session()
    s.headers.update(HEADERS)
    if Retry is not None:
        retry = Retry(total=3, connect=3, read=3, backoff_factor=2,
                      status_forcelist=(500, 502, 503, 504),
                      allowed_methods=frozenset(["GET"]))
        adapter = HTTPAdapter(max_retries=retry, pool_connections=pool, pool_maxsize=pool)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
    return s


SESSION = make_session()

OUTLETS = {
    "dawn": {
        # date-aware daily archive; list items carry an inline section link
        "mode": "archive",
        "archive": "https://www.dawn.com/archive/{d}",
        "base": "https://www.dawn.com",
        "story_sel": "article.story",
        "link_sel": "a[href*='/news/']",
        "body_sel": "div.story__content p, article p",
        "title_sel": "h1, h2.story__title",
        "author_sel": "a[href*='/authors/']",
    },
    "tribune": {
        # /archive/<date> ignores the date, so history comes from the sitemaps;
        # no section signal there, so every in-window URL is fetched and the
        # article:section meta decides whether it is kept.
        "mode": "sitemap",
        "sitemap_index": "https://tribune.com.pk/sitemap.xml",
        "url_marker": "/story/",
        "base": "https://tribune.com.pk",
        "body_sel": "span.story-text p, div.story-text p, div.tp-content p",
        "title_sel": "h1.title, h1",
        "author_sel": "a.author, span.author",
    },
}


def polite_get(url, tries=3):
    for attempt in range(tries):
        try:
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
            r = SESSION.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            if r.status_code == 200:
                return BeautifulSoup(r.text, "lxml")
            if r.status_code in (403, 429, 503):
                time.sleep(10 * (attempt + 1))  # back off harder on rate-limit
        except Exception:
            time.sleep(5 * (attempt + 1))
    return None


def fetch_text(url, tries=3):
    for attempt in range(tries):
        try:
            time.sleep(random.uniform(1.0, 2.0))
            r = SESSION.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            if r.status_code == 200:
                return r.text
            if r.status_code in (403, 429, 503):
                time.sleep(10 * (attempt + 1))
        except Exception:
            time.sleep(5 * (attempt + 1))
    return None


def daterange(start, end):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def meta_prop(soup, prop):
    m = soup.find("meta", {"property": prop}) or soup.find("meta", {"name": prop})
    c = m.get("content") if m else None
    return c.strip() if c else ""


def section_hint(story_tag):
    for a in story_tag.find_all("a"):
        h = a.get("href", "")
        if re.fullmatch(r"/[a-z][a-z0-9-]*", h):
            return h.strip("/").lower()
    return ""


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
        ck.setdefault("links", {})
        ck.setdefault("done_days", [])
        ck.setdefault("scraped", {})
        ck.setdefault("fails", {})
        return ck
    return {"links": {}, "done_days": [], "scraped": {}, "fails": {}}


def save_ckpt(path, ck):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ck, f)
    os.replace(tmp, path)


def collect_day(outlet, cfg, d, ck):
    """Collect every article link for one day. Returns (n_new, fetched_ok)."""
    soup = polite_get(cfg["archive"].format(d=d.isoformat()))
    if not soup:
        return 0, False
    n = 0

    def add(href, hint):
        nonlocal n
        href = href.split("#")[0]
        if href and href not in ck["links"]:
            ck["links"][href] = {"outlet": outlet, "hint": hint}
            n += 1

    if cfg["story_sel"]:
        # canonical daily list; section hint lets us skip non-world without fetching
        for story in soup.select(cfg["story_sel"]):
            a = story.select_one(cfg["link_sel"])
            if a:
                add(urljoin(cfg["base"], a.get("href", "")), section_hint(story))
    else:
        # no per-story container: take every article link, filter later on section meta
        for a in soup.select(cfg["link_sel"]):
            add(urljoin(cfg["base"], a.get("href", "")), "")
    return n, True


def collect_via_archive(outlet, cfg, start, end, ck, ckpath, refresh_days):
    refresh_cutoff = end - timedelta(days=refresh_days)  # re-scan recent days for late posts
    for d in daterange(start, end):
        key = f"{outlet}:{d.isoformat()}"
        recent = d >= refresh_cutoff
        if key in ck["done_days"] and not recent:
            continue
        n, ok = collect_day(outlet, cfg, d, ck)
        if not ok:
            if key not in ck["done_days"]:
                ck["fails"][key] = ck["fails"].get(key, 0) + 1
                if ck["fails"][key] >= 4:
                    ck["done_days"].append(key)
                    print(f"  {key}: archive unreachable x4, skipping")
                else:
                    print(f"  {key}: archive fetch failed, will retry next run")
            else:
                print(f"  {key}: refresh fetch failed (already collected)")
            save_ckpt(ckpath, ck)
            continue
        if key not in ck["done_days"]:
            ck["done_days"].append(key)
        print(f"  {key}: +{n} new links (total {len(ck['links'])})")
        save_ckpt(ckpath, ck)


def collect_via_sitemap(outlet, cfg, start, end, ck, ckpath):
    idx = fetch_text(cfg["sitemap_index"])
    if not idx:
        print(f"  {outlet}: sitemap index unreachable")
        return
    maps = [m for m in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", idx) if "posts-" in m]
    print(f"  {outlet}: scanning {len(maps)} post sitemaps for {start}..{end}")
    marker = cfg["url_marker"]
    for sm in maps:
        txt = fetch_text(sm)
        if not txt:
            print(f"    {sm.rsplit('/', 1)[-1]}: fetch failed, skipping")
            continue
        n = 0
        for block in re.findall(r"<url>(.*?)</url>", txt, re.S):
            loc = re.search(r"<loc>\s*([^<]+?)\s*</loc>", block)
            mod = re.search(r"<lastmod>\s*([^<]+?)\s*</lastmod>", block)
            if not loc or not mod:
                continue
            url = loc.group(1).split("#")[0]
            if marker not in url:
                continue
            try:
                d = date.fromisoformat(mod.group(1)[:10])
            except ValueError:
                continue
            if start <= d <= end and url not in ck["links"]:
                ck["links"][url] = {"outlet": outlet, "hint": ""}
                n += 1
        print(f"    {sm.rsplit('/', 1)[-1]}: +{n} in-window links (total {len(ck['links'])})")
        save_ckpt(ckpath, ck)


def scrape_one(url, outlet, sections):
    """Fetch + parse one article. Pure (no shared state). Returns (status, row)."""
    cfg = OUTLETS[outlet]
    soup = polite_get(url)
    if not soup:
        return "fail", None
    section = meta_prop(soup, "article:section").lower()
    if section not in sections:
        return "drop", None
    paras = [p.get_text(" ", strip=True) for p in soup.select(cfg["body_sel"])]
    body = " ".join(p for p in paras if len(p) > 30)
    if len(body) < 120:
        return "drop", None
    ts = meta_prop(soup, "article:published_time")
    title_el = soup.select_one(cfg["title_sel"])
    author_el = soup.select_one(cfg["author_sel"])
    return "ok", {
        "outlet": outlet,
        "section": section,
        "date": ts[:10] if ts else "",
        "timestamp": ts,
        "author": author_el.get_text(strip=True) if author_el else "",
        "headline": title_el.get_text(strip=True) if title_el else "",
        "url": url,
        "article_id": re.sub(r"\D", "", url)[-7:] or "",
        "body": body,
    }


def scrape_bodies(ck, ckpath, jsonl_path, sections, workers):
    todo = [u for u in ck["links"] if u not in ck["scraped"]]
    fetch_list = []
    for url in todo:
        meta = ck["links"][url]
        outlet = meta["outlet"] if isinstance(meta, dict) else meta
        hint = meta.get("hint", "") if isinstance(meta, dict) else ""
        if hint and hint not in sections:       # confident non-international -> no fetch
            ck["scraped"][url] = True
        else:
            fetch_list.append((url, outlet))
    save_ckpt(ckpath, ck)
    total = len(fetch_list)
    print(f"\nScraping {total} links with {workers} workers "
          f"({len(todo) - total} skipped by hint, {len(ck['scraped']) - (len(todo) - total)} done before)...")

    out = open(jsonl_path, "a", encoding="utf-8")
    kept = done = 0
    # workers only fetch/parse; this loop is the sole mutator of ck + the file (no locks needed)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(scrape_one, u, o, sections): u for u, o in fetch_list}
        for fut in as_completed(futs):
            url = futs[fut]
            done += 1
            try:
                status, row = fut.result()
            except Exception:
                status, row = "fail", None
            if status == "fail":
                ck["fails"][url] = ck["fails"].get(url, 0) + 1
                if ck["fails"][url] >= 4:
                    ck["scraped"][url] = True    # give up after repeated failures
            else:
                ck["scraped"][url] = True
                if status == "ok" and row:
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
                    out.flush()
                    kept += 1
            if done % 10 == 0:
                save_ckpt(ckpath, ck)
                print(f"    {done}/{total} processed, {kept} kept this run")
    save_ckpt(ckpath, ck)
    out.close()
    return kept


def write_sorted_csv(jsonl_path, out_csv):
    rows = {}
    if os.path.exists(jsonl_path):
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    rows[r["url"]] = r           # de-dup by url, last wins
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
    global DELAY_MIN, DELAY_MAX
    p = argparse.ArgumentParser()
    p.add_argument("--start", default=DEFAULT_START, help="YYYY-MM-DD (default 2025-10-07)")
    p.add_argument("--end", default=date.today().isoformat(), help="YYYY-MM-DD (default today)")
    p.add_argument("--outlets", nargs="+", default=list(OUTLETS), choices=list(OUTLETS))
    p.add_argument("--sections", nargs="+", default=INTL_SECTIONS,
                   help="article:section values to keep (default: world international foreign)")
    p.add_argument("--refresh-days", type=int, default=2,
                   help="re-scan this many most-recent days for late-published stories")
    p.add_argument("--workers", type=int, default=5,
                   help="concurrent fetch workers in the scrape phase (default 5)")
    p.add_argument("--min-delay", type=float, default=DELAY_MIN,
                   help="min per-request delay seconds (default 1.0)")
    p.add_argument("--max-delay", type=float, default=DELAY_MAX,
                   help="max per-request delay seconds (default 2.5)")
    p.add_argument("--out", default="intl_news.csv")
    p.add_argument("--checkpoint", default="intl_checkpoint.json")
    args = p.parse_args()

    DELAY_MIN, DELAY_MAX = args.min_delay, args.max_delay
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    sections = set(s.lower() for s in args.sections)
    jsonl_path = args.out + ".jsonl"
    ck = load_ckpt(args.checkpoint)

    print(f"Phase 1: discovering links {start} -> {end} for {args.outlets}")
    for outlet in args.outlets:
        cfg = OUTLETS[outlet]
        if cfg["mode"] == "sitemap":
            collect_via_sitemap(outlet, cfg, start, end, ck, args.checkpoint)
        else:
            collect_via_archive(outlet, cfg, start, end, ck, args.checkpoint, args.refresh_days)
    print(f"Phase 1 done. {len(ck['links'])} candidate links.")

    print(f"Phase 2: scraping bodies, keeping sections {sorted(sections)}")
    kept = scrape_bodies(ck, args.checkpoint, jsonl_path, sections, args.workers)
    print(f"Phase 2 done. {kept} international articles kept this run.")

    total = write_sorted_csv(jsonl_path, args.out)
    print(f"\nDone -> {args.out} ({total} articles, sorted by timestamp)")
    print(f"Next:  python3 extract_sources.py {args.out}")


if __name__ == "__main__":
    main()
