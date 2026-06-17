"""
make_annotation_sample.py — build a blind human-validation sample from edges_spotcheck.csv.

Outputs (in annotation/):
  annotation_sample.csv  — what the annotator fills in. BLINDED: shows source->target and the
                           quote + a link to the article, but NOT the model's polarity/verb
                           (so polarity is judged fresh and unanchored). 5 empty label columns.
  annotation_key.csv     — hidden answer key (model's labels) for scoring; do NOT show annotators.

Sampling is stratified to oversample the load-bearing edges (touching Pakistan / US / Iran) and
balanced across outlets, hypothesis-blind (outlet/phase live only in the key). Seeded = reproducible.

Usage:  python make_annotation_sample.py
"""

import os
import pandas as pd

SPOT = "out/edges_spotcheck.csv"
ART = "articles.csv"
OUTDIR = "annotation"
SEED = 42
N_PAK, N_AXIS, N_OTHER = 90, 70, 40     # ~200 edges total
LABEL_COLS = ["actors_ok", "direction_ok", "polarity", "real_event", "mediation", "notes"]


def balanced(df, n):
    """Sample ~n rows, split evenly across outlets where possible."""
    if df.empty:
        return df
    parts, per = [], max(1, n // df["outlet"].nunique())
    for _, g in df.groupby("outlet"):
        parts.append(g.sample(min(per, len(g)), random_state=SEED))
    out = pd.concat(parts)
    if len(out) < n:                    # top up randomly if a stratum was thin
        rest = df.drop(out.index)
        out = pd.concat([out, rest.sample(min(n - len(out), len(rest)), random_state=SEED)])
    return out


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    df = pd.read_csv(SPOT)

    # join article url + headline via doc_id = "{row}_{chunk}" (row = articles.csv index)
    art = pd.read_csv(ART)
    def lookup(doc_id, col):
        try:
            return art.iloc[int(str(doc_id).split("_")[0])][col]
        except (ValueError, IndexError, KeyError):
            return ""
    df["url"] = df["doc_id"].map(lambda d: lookup(d, "url"))
    df["headline"] = df["doc_id"].map(lambda d: lookup(d, "headline"))

    focal = {"Pakistan", "US", "Iran"}
    is_pak = (df.source == "Pakistan") | (df.target == "Pakistan")
    is_axis = (df.source.isin(focal) | df.target.isin(focal)) & ~is_pak
    pak, axis, other = df[is_pak], df[is_axis], df[~is_pak & ~is_axis]

    sample = pd.concat([balanced(pak, N_PAK), balanced(axis, N_AXIS), balanced(other, N_OTHER)])
    sample = sample.drop_duplicates(subset=["doc_id", "source", "target", "quote"])
    sample = sample.sample(frac=1, random_state=SEED).reset_index(drop=True)   # shuffle
    sample.insert(0, "id", [f"E{ i+1:03d}" for i in range(len(sample))])

    # BLINDED sheet for the annotator — no model polarity/verb/outlet/phase
    vis = sample[["id", "source", "target", "quote", "headline", "url"]].copy()
    for c in LABEL_COLS:
        vis[c] = ""
    vis.to_csv(os.path.join(OUTDIR, "annotation_sample.csv"), index=False)

    # hidden key for scoring later
    key = sample[["id", "outlet", "phase", "source", "target", "polarity", "raw_verb", "doc_id"]]
    key = key.rename(columns={"polarity": "llm_polarity", "raw_verb": "llm_verb"})
    key.to_csv(os.path.join(OUTDIR, "annotation_key.csv"), index=False)

    print(f"Wrote {len(sample)} edges to {OUTDIR}/annotation_sample.csv (blinded) + annotation_key.csv (answers).")
    print("Mix — Pakistan-incident:", int(((sample.source == 'Pakistan') | (sample.target == 'Pakistan')).sum()),
          "| outlets:", dict(sample.outlet.value_counts()),
          "| phases:", dict(sample.phase.value_counts()))


if __name__ == "__main__":
    main()
