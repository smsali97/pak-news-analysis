"""
score_annotations.py — score the human annotation against the model's labels.

Reads one or more filled annotation_sample.csv files + the hidden annotation_key.csv and reports:
  - edge precision (actors + direction + real-event + polarity all correct)
  - the looser checks broken out (actor/direction/real-event/polarity each)
  - polarity confusion matrix (model rows vs human columns) + accuracy
  - mediation precision on the load-bearing Pakistan / US / Iran positive edges
  - precision split by outlet (so the Dawn–Guardian gap isn't a labeling artifact)
  - Cohen's kappa between annotators (if you pass 2+ files), on the overlapping rows

Usage:
  python score_annotations.py annotation/annotation_sample.csv
  python score_annotations.py annotation/ann_alice.csv annotation/ann_bob.csv
  (key defaults to annotation/annotation_key.csv; override with --key)
"""

import argparse
from collections import Counter

import numpy as np
import pandas as pd

FOCAL = {"Pakistan", "US", "Iran"}


def yn(x):
    s = str(x).strip().lower()
    if s in {"y", "yes", "true", "1"}: return True
    if s in {"n", "no", "false", "0"}: return False
    return None


def pol(x):
    s = str(x).strip().lower()
    return s if s in {"positive", "negative", "neutral"} else None


def med(x):
    s = str(x).strip().lower()
    return s if s in {"mediation", "goodwill", "not"} else None


def cohen_kappa(a, b):
    """Cohen's kappa for two aligned label lists (nulls already removed)."""
    cats = sorted(set(a) | set(b))
    n = len(a)
    if n == 0: return float("nan")
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[c] / n) * (cb[c] / n) for c in cats)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def load(path, key):
    h = pd.read_csv(path).set_index("id")
    df = key.join(h[["actors_ok", "direction_ok", "polarity", "real_event", "mediation"]], how="inner")
    df["a_ok"] = df["actors_ok"].map(yn)
    df["d_ok"] = df["direction_ok"].map(yn)
    df["r_ok"] = df["real_event"].map(yn)
    df["h_pol"] = df["polarity"].map(pol)
    df["h_med"] = df["mediation"].map(med)
    return df


def report(name, df):
    done = df.dropna(subset=["a_ok", "d_ok", "r_ok", "h_pol"])
    n = len(done)
    print(f"\n{'='*70}\nANNOTATOR: {name}\n{'='*70}")
    print(f"Rows scored (fully filled): {n} of {len(df)}")
    if n == 0:
        print("  -> nothing scored yet; fill the sheet and re-run.")
        return
    a = done["a_ok"].mean(); d = done["d_ok"].mean(); r = done["r_ok"].mean()
    pol_match = (done["h_pol"] == done["llm_polarity"]).mean()
    correct = done["a_ok"] & done["d_ok"] & done["r_ok"] & (done["h_pol"] == done["llm_polarity"])
    print(f"\n  EDGE PRECISION (actors & direction & real-event & polarity all correct): {correct.mean():.1%}")
    print(f"    actors correct ....... {a:.1%}")
    print(f"    direction correct .... {d:.1%}")
    print(f"    real event ........... {r:.1%}")
    print(f"    polarity matches ..... {pol_match:.1%}")

    print("\n  POLARITY confusion (rows = model, cols = human):")
    cm = pd.crosstab(done["llm_polarity"], done["h_pol"], dropna=False)
    print("   " + cm.to_string().replace("\n", "\n   "))

    # mediation precision on load-bearing positive edges (human-positive, touching focal actors)
    foc = done[(done.source.isin(FOCAL) | done.target.isin(FOCAL)) & (done["h_pol"] == "positive")]
    fm = foc["h_med"].dropna()
    if len(fm):
        share = (fm == "mediation").mean()
        print(f"\n  MEDIATION precision (human-positive edges touching Pakistan/US/Iran): {share:.1%} genuine mediation")
        print("    breakdown:", dict(fm.value_counts()))
    pak = done[((done.source == "Pakistan") | (done.target == "Pakistan")) & (done["h_pol"] == "positive")]
    pm = pak["h_med"].dropna()
    if len(pm):
        print(f"    Pakistan only: {(pm=='mediation').mean():.1%} mediation  ({dict(pm.value_counts())})")

    print("\n  EDGE PRECISION by outlet:")
    for outlet, g in done.groupby("outlet"):
        c = (g["a_ok"] & g["d_ok"] & g["r_ok"] & (g["h_pol"] == g["llm_polarity"]))
        print(f"    {outlet:9s} n={len(g):3d}  precision {c.mean():.1%}  polarity {(g['h_pol']==g['llm_polarity']).mean():.1%}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="filled annotation_sample.csv file(s)")
    ap.add_argument("--key", default="annotation/annotation_key.csv")
    args = ap.parse_args()
    key = pd.read_csv(args.key).set_index("id")

    loaded = {f: load(f, key) for f in args.files}
    for f, df in loaded.items():
        report(f, df)

    if len(args.files) >= 2:
        a, b = args.files[0], args.files[1]
        da, db = loaded[a], loaded[b]
        common = da.index.intersection(db.index)
        print(f"\n{'='*70}\nINTER-ANNOTATOR AGREEMENT (Cohen's kappa)\n{'='*70}")
        print(f"Overlapping rows: {len(common)}  ({a}  vs  {b})")
        for field, col in [("polarity", "h_pol"), ("actors_ok", "a_ok"), ("real_event", "r_ok"), ("mediation", "h_med")]:
            pair = [(da.at[i, col], db.at[i, col]) for i in common]
            pair = [(x, y) for x, y in pair if pd.notna(x) and pd.notna(y)]
            if pair:
                xs, ys = zip(*pair)
                k = cohen_kappa(list(xs), list(ys))
                print(f"  {field:13s} n={len(pair):3d}  kappa = {k:.2f}   {'(substantial+)' if k>=0.6 else '(weak — revisit codebook)'}")


if __name__ == "__main__":
    main()
