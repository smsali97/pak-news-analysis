"""
analyze_stage.py — STAGE 2: deterministic network build + brokerage measures.

Reads cached extractions (out/extractions.jsonl) and computes, per outlet x phase, Pakistan's
brokerage between the US and Iran plus a mediator comparison. NO API tokens spent here.

Two weightings are reported side by side:
  flat      — every positive interaction = 1 vote (salience / framing).
  goldstein — Goldstein-inspired substance weights (substance.py): hosting/mediating > urging >
              praising; reporting verbs drop to 0. The substantive brokerage measure.

Headline measure is current-flow betweenness (parallel channels), not shortest-path.

Usage:  python analyze_stage.py --extractions out/extractions.jsonl
"""

import argparse
import json
import os
from collections import defaultdict

import pandas as pd

import extraction as ex
import mediation_pipeline as mp
import substance as sb

FOCUS = "Pakistan"
MEDIATORS = ["Pakistan", "Oman", "Qatar", "Turkey", "Egypt", "UK", "China", "UN", "Saudi_Arabia"]


def load_edges(path):
    edges = defaultdict(list)
    n_recs = n_drops = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            n_recs += 1
            n_drops += len(rec.get("drops", []))
            for e in rec.get("edges", []):
                edges[(e["outlet"], e["phase"])].append(e)
    return edges, n_recs, n_drops


def rank_of(fb, node):
    r = sorted(fb, key=fb.get, reverse=True)
    return r.index(node) + 1 if node in r else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extractions", default="out/extractions.jsonl")
    ap.add_argument("--out-dir", default="out")
    args = ap.parse_args()

    if not os.path.exists(args.extractions):
        raise SystemExit(f"no extractions at {args.extractions} — run extract_stage.py first")
    os.makedirs(args.out_dir, exist_ok=True)

    edges, n_recs, n_drops = load_edges(args.extractions)
    flat = [e for es in edges.values() for e in es]
    print(f"Read {n_recs} chunk records, {len(flat)} validated edges ({n_drops} candidates dropped).")
    if not flat:
        print("No validated edges — inspect 'drops' in the JSONL.")
        return
    pd.DataFrame(flat)[["outlet", "phase", "source", "target", "polarity",
                        "raw_verb", "quote", "doc_id"]].to_csv(
        os.path.join(args.out_dir, "edges_spotcheck.csv"), index=False)

    pak_rows, med_flat, med_gold = [], [], []
    for (outlet, phase), es in sorted(edges.items()):
        G = ex.build_signed_network(es)
        Cf = ex.cooperation_view(G)               # flat: count-weighted
        Cg = sb.weighted_cooperation_view(es)     # goldstein: substance-weighted
        B = ex.brokering_view(G)                  # brokering-verbs-only validation
        fbf, fbg = mp.flow_brokerage(Cf), mp.flow_brokerage(Cg)
        fb_brk = mp.flow_brokerage(B)
        infl = mp.influence_share(G)

        pak_rows.append({
            "outlet": outlet, "phase": phase,
            "pos_edges": sum(1 for e in es if e["valence"] == "coop"),
            "PAK_flat": round(fbf.get(FOCUS, 0), 3), "rank_flat": rank_of(fbf, FOCUS),
            "PAK_gold": round(fbg.get(FOCUS, 0), 3), "rank_gold": rank_of(fbg, FOCUS),
            "PAK_gold_brokering": round(fb_brk.get(FOCUS, 0), 3),
            "PAK_gf": round(mp.gould_fernandez_liaison(Cf, FOCUS), 0),
            "PAK_infl": round(infl.get(FOCUS, 0), 3),
        })
        med_flat.append({"outlet": outlet, "phase": phase, **{m: round(fbf.get(m, 0), 3) for m in MEDIATORS}})
        med_gold.append({"outlet": outlet, "phase": phase, **{m: round(fbg.get(m, 0), 3) for m in MEDIATORS}})

    pak = pd.DataFrame(pak_rows)
    pak.to_csv(os.path.join(args.out_dir, "results.csv"), index=False)
    pd.DataFrame(med_gold).to_csv(os.path.join(args.out_dir, "mediators.csv"), index=False)

    print("\n=== Pakistan: flat (salience) vs Goldstein (substance) — current-flow betweenness ===")
    print(pak.to_string(index=False))
    print("\n=== Mediator comparison — GOLDSTEIN-weighted flow-brokerage (who substantively brokered?) ===")
    print(pd.DataFrame(med_gold).to_string(index=False))
    print("\n=== same, FLAT votes (for contrast) ===")
    print(pd.DataFrame(med_flat).to_string(index=False))
    print(f"\nWrote {args.out_dir}/results.csv (Pakistan) and {args.out_dir}/mediators.csv (Goldstein).")
    print("Read: if PAK_gold stays high next to PAK_flat, the finding survives substance weighting "
          "(hosting/mediating counted above urging/praising). Compare Dawn vs Guardian for framing.")


if __name__ == "__main__":
    main()