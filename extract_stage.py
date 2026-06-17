"""
extract_stage.py — STAGE 1: the LLM extraction step, run ONCE and cached.

This is the only step that spends API tokens. For every chunk it saves, to
out/extractions.jsonl, a full record so you never have to re-call the model to
re-run the analysis or to debug a result:

  result  -> raw_response (verbatim model output), parsed candidate edges,
             validated edges that survived the gates
  rationale -> a per-edge drop log saying WHICH gate rejected each candidate
             (unresolved actor / hallucinated quote / commentary speaker / ...)

It is RESUMABLE: chunks whose doc_id is already in the JSONL are skipped, so a
re-run only extracts what's new (or pass --fresh to start over).

Then run analyze_stage.py — no tokens — over the JSONL as many times as you like.

Usage:
  export OPENAI_API_KEY=sk-...        # or: direnv allow  (.envrc holds it)
  # tiny debug run first — eyeball out/extractions.jsonl to see why edges drop:
  python extract_stage.py --csv articles.csv --provider openai --sample 3
  # then the real sample / full corpus:
  python extract_stage.py --csv articles.csv --provider openai --sample 150
  python extract_stage.py --csv articles.csv --provider openai --sample 0
"""

import argparse
import json
import os
import re
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

import extraction as ex
from vocab import actors_mentioned

# Articles must touch the US-Iran-Pakistan axis to be worth extracting. An article
# is on-axis if it names >=2 of these AND at least one of the US/Iran dyad — so
# bridge-halves (Pakistan+Iran, US+Pakistan) survive but pure off-axis is skipped.
FOCAL = ["US", "Iran", "Pakistan", "Oman", "Qatar"]
DYAD = {"US", "Iran"}


def is_on_axis(text, focal=FOCAL, dyad=DYAD):
    hits = actors_mentioned(text, focal)
    return len(hits) >= 2 and bool(hits & dyad)


def chunk_paragraphs(text, min_len=40):
    parts = re.split(r"\n{2,}|(?<=[.?!])\s{2,}", text or "")
    out = [p.strip() for p in parts if len(p.strip()) >= min_len]
    return out or ([text.strip()] if text and text.strip() else [])


def get_caller(provider):
    if provider == "openai":
        return ex.call_llm_openai
    if provider == "gemini":
        return ex.call_llm_gemini
    if provider == "mock":
        return lambda prompt: "[]"
    sys.exit(f"unknown provider: {provider}")


def load_done(path):
    """doc_ids already extracted, so a re-run skips them."""
    done = set()
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        done.add(json.loads(line)["doc_id"])
                    except (json.JSONDecodeError, KeyError):
                        pass
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--text-col", default="body")
    ap.add_argument("--date-col", default="date")
    ap.add_argument("--outlet-col", default="outlet")
    ap.add_argument("--outlet-default", default="dawn")
    ap.add_argument("--provider", default="openai", choices=["openai", "gemini", "mock"])
    ap.add_argument("--sample", type=int, default=150, help="0 = whole corpus")
    ap.add_argument("--no-chunk", action="store_true", help="send whole article")
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--out-file", default="extractions.jsonl")
    ap.add_argument("--fresh", action="store_true", help="ignore + overwrite existing JSONL")
    ap.add_argument("--workers", type=int, default=8, help="concurrent API calls (1 = sequential)")
    ap.add_argument("--no-filter", action="store_true",
                    help="extract every article (default: skip off-axis ones, no tokens spent on them)")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, args.out_file)

    df = pd.read_csv(args.csv)

    # relevance pre-filter (free) — drop off-axis articles before sampling so the
    # token budget is spent on US-Iran-Pakistan content, not Sudan/Gaza pieces.
    n_all = len(df)
    if not args.no_filter:
        on_axis = df[args.text_col].fillna("").map(is_on_axis)
        df = df[on_axis]
        print(f"Relevance filter: {len(df)}/{n_all} articles on the US-Iran-Pakistan axis "
              f"(skipped {n_all - len(df)} off-axis, 0 tokens). Use --no-filter to keep all.")

    if args.sample and len(df) > args.sample:
        df = df.sample(args.sample, random_state=0)

    # build the chunk corpus
    corpus = []
    for i, row in df.iterrows():
        text = str(row[args.text_col]) if pd.notna(row.get(args.text_col)) else ""
        date = str(row[args.date_col])[:10]
        outlet = str(row[args.outlet_col]).lower() if args.outlet_col in df.columns else args.outlet_default
        chunks = [text] if args.no_chunk else chunk_paragraphs(text)
        for j, ch in enumerate(chunks):
            corpus.append({"text": ch, "doc_id": f"{i}_{j}", "date": date, "outlet": outlet})

    if args.fresh and os.path.exists(out_path):
        os.remove(out_path)
    done = load_done(out_path)
    todo = [c for c in corpus if c["doc_id"] not in done]

    print(f"Loaded {len(df)} articles -> {len(corpus)} chunks. Provider={args.provider}. "
          f"Phase boundary={ex.PHASE_BOUNDARY}.")
    print(f"{len(done)} already extracted; {len(todo)} to do. -> {out_path}")
    if not todo:
        print("Nothing to extract. Run analyze_stage.py.")
        return

    call = get_caller(args.provider)
    tally = Counter()
    kept_total = 0

    def extract_one(c):
        """Pure per-chunk work (runs in a worker thread): call LLM, parse, validate."""
        ph = ex.phase_of(c["date"])
        try:
            raw = call(ex.build_prompt(c["text"]))
            err = None
        except Exception as exc:                         # don't lose the batch on one bad call
            raw, err = "", f"{type(exc).__name__}: {exc}"
        parsed = ex._loads(raw) if raw else []
        clean, drops = ex.validate_edges_verbose(parsed, c["text"], c["doc_id"], ph, c["outlet"])
        return {
            "doc_id": c["doc_id"], "date": c["date"], "phase": ph, "outlet": c["outlet"],
            "text": c["text"],
            "error": err,
            "raw_response": raw,
            "n_parsed": len(parsed),
            "edges": clean,                              # result
            "drops": drops,                              # rationale: why each candidate died
        }

    write_lock = threading.Lock()                        # serialize JSONL appends + counters
    workers = max(1, args.workers)
    print(f"Extracting with {workers} concurrent worker(s)...")
    with open(out_path, "a") as f, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(extract_one, c) for c in todo]
        for n, fut in enumerate(as_completed(futures), 1):
            rec = fut.result()
            with write_lock:
                for d in rec["drops"]:
                    tally[d["reason"].split(":")[0].split(" (")[0]] += 1
                kept_total += len(rec["edges"])
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                if n % 10 == 0 or n == len(todo):
                    print(f"  [{n}/{len(todo)}] kept={kept_total} edges so far")

    print(f"\nDone. {kept_total} validated edges across {len(todo)} new chunks.")
    if tally:
        print("Drop reasons (why candidate edges were rejected):")
        for reason, k in tally.most_common():
            print(f"  {k:5d}  {reason}")
    print(f"\nNext: python analyze_stage.py --extractions {out_path}")


if __name__ == "__main__":
    main()
