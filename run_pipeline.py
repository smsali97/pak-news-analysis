"""
run_pipeline.py — thin convenience wrapper: Stage 1 (extract) then Stage 2 (analyze).

The real work lives in two separated scripts so you only spend API tokens once:
  extract_stage.py  — LLM extraction, cached + resumable to out/extractions.jsonl
  analyze_stage.py  — deterministic network build + brokerage measures (no tokens)

Use those directly when you want to re-run the analysis without re-calling the model.
This wrapper just chains them for a one-shot end-to-end run.

Usage:
  export OPENAI_API_KEY=sk-...        # or: direnv allow  (.envrc holds it)
  python run_pipeline.py --csv articles.csv --provider openai --sample 150 --fresh
"""

import argparse
import os
import sys

import extract_stage
import analyze_stage


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
    ap.add_argument("--workers", type=int, default=8, help="concurrent API calls")
    ap.add_argument("--fresh", action="store_true", help="ignore + overwrite existing extractions")
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--out-file", default="extractions.jsonl")
    args = ap.parse_args()

    out_path = os.path.join(args.out_dir, args.out_file)

    # --- Stage 1: extraction (token-spending, cached) ---
    sys.argv = ["extract_stage.py",
                "--csv", args.csv, "--text-col", args.text_col, "--date-col", args.date_col,
                "--outlet-col", args.outlet_col, "--outlet-default", args.outlet_default,
                "--provider", args.provider, "--sample", str(args.sample),
                "--workers", str(args.workers), "--out-dir", args.out_dir,
                "--out-file", args.out_file]
    if args.no_chunk:
        sys.argv.append("--no-chunk")
    if args.fresh:
        sys.argv.append("--fresh")
    extract_stage.main()

    # --- Stage 2: analysis (deterministic, free) ---
    print("\n" + "=" * 70)
    sys.argv = ["analyze_stage.py", "--extractions", out_path, "--out-dir", args.out_dir]
    analyze_stage.main()


if __name__ == "__main__":
    main()
