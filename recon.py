"""
recon.py — corpus reconnaissance to DRAFT the controlled vocabularies before extraction.

What it does, end to end, from your articles CSV:
  1. ACTORS    : NER over the corpus -> ranked candidate GPE/ORG/PERSON/NORP entities.
  2. ALIASES   : groups surface forms that likely refer to the same actor (for you to approve).
  3. BLOC      : emits a pre-filled, EDITABLE bloc skeleton (your analytical call).
  4. RELATIONS : mines the verbs/predicates that occur between two actors -> ranked verb list,
                 so you can confirm your 12 canonical relations cover what's actually in the text.

It PROPOSES; you DISPOSE. NER cannot decide blocs or guarantee alias links — review the output.

Setup:
  pip install spacy pandas
  python -m spacy download en_core_web_sm        # fast; use en_core_web_trf for best NER
Run:
  python recon.py --csv articles.csv --text-col body --date-col date --sample 1500
Outputs (CSV/JSON next to the script):
  recon_actors.csv, recon_alias_groups.json, recon_bloc_skeleton.json, recon_relation_verbs.csv
"""

import argparse
import json
import re
from collections import Counter, defaultdict

import pandas as pd
import spacy


# ---------------------------------------------------------------------------
# Seed knowledge — only to help GROUP and PRE-FILL, never to limit discovery.
# Discovery below is open: anything NER finds is reported, seeded or not.
# ---------------------------------------------------------------------------
SEED_CANON = {
    "US": ["united states", "u.s.", "us", "washington", "white house", "america", "american",
           "trump", "vance", "witkoff", "kushner", "rubio", "state department", "pentagon"],
    "Israel": ["israel", "israeli", "netanyahu", "bibi", "idf", "tel aviv"],
    "Iran": ["iran", "iranian", "tehran", "khamenei", "pezeshkian", "araghchi", "irgc",
             "ghalibaf", "larijani"],
    "Pakistan": ["pakistan", "pakistani", "islamabad", "sharif", "shehbaz", "munir",
                 "ishaq dar", "asim munir"],
    "Oman": ["oman", "omani", "muscat", "busaidi"],
    "Qatar": ["qatar", "qatari", "doha"],
    "Saudi_Arabia": ["saudi arabia", "saudi", "riyadh", "mbs", "mohammed bin salman"],
    "UAE": ["uae", "emirates", "abu dhabi", "dubai"],
    "Turkey": ["turkey", "turkiye", "turkish", "ankara", "erdogan"],
    "China": ["china", "chinese", "beijing"],
    "Russia": ["russia", "russian", "moscow", "putin"],
    "Egypt": ["egypt", "egyptian", "cairo", "sisi"],
}
SEED_BLOC = {
    "US": "US_bloc", "Israel": "US_bloc", "Iran": "Iran_bloc", "Pakistan": "Broker",
    "Oman": "Mediators", "Qatar": "Mediators", "Egypt": "Mediators",
    "Saudi_Arabia": "Gulf", "UAE": "Gulf",
    "Turkey": "OtherPowers", "China": "OtherPowers", "Russia": "OtherPowers",
}

# Canonical relation -> seed verb cues (only to BUCKET mined verbs; unmatched verbs are
# reported under "UNMAPPED" so you can spot relations/synonyms you haven't covered).
SEED_REL_CUES = {
    "HOST_TALKS": ["host", "convene", "bring together", "welcome"],
    "MEDIATE": ["mediate", "broker", "facilitate", "shuttle", "go-between"],
    "CONSULT": ["meet", "talk", "discuss", "phone", "call", "hold talks", "consult"],
    "AGREE": ["agree", "sign", "reach", "accept", "deal", "ceasefire"],
    "SUPPORT": ["back", "support", "praise", "side", "welcome", "endorse"],
    "AID": ["aid", "supply", "assist", "provide", "send"],
    "DEMAND": ["urge", "call on", "press", "demand", "insist"],
    "REJECT": ["reject", "refuse", "dismiss", "rule out", "deny"],
    "ACCUSE": ["accuse", "condemn", "blame", "criticise", "criticize", "slam"],
    "THREATEN": ["threaten", "warn", "vow", "retaliate"],
    "SANCTION": ["sanction", "blacklist", "impose", "penalise", "penalize"],
    "ASSAULT": ["strike", "bomb", "attack", "hit", "launch", "kill", "target"],
}


def normalize(s):
    return re.sub(r"[^a-z\s]", "", s.lower()).strip()


def canon_for(surface, seed):
    n = normalize(surface)
    for canon, forms in seed.items():
        if any(n == f or f in n.split() or n in f for f in forms):
            return canon
    return None


# ---------------------------------------------------------------------------
# 1 + 2 — actor discovery and alias grouping
# ---------------------------------------------------------------------------
def discover_actors(nlp, texts):
    counts = Counter()
    for doc in nlp.pipe(texts, batch_size=64):
        for ent in doc.ents:
            if ent.label_ in {"GPE", "ORG", "PERSON", "NORP"}:
                surf = ent.text.strip()
                if len(surf) > 1:
                    counts[surf] += 1
    return counts


def group_aliases(counts):
    """Bucket each discovered surface form under a seeded canonical actor when it matches;
    everything else lands under UNRESOLVED for you to map by hand."""
    groups = defaultdict(Counter)
    for surf, c in counts.items():
        canon = canon_for(surf, SEED_CANON) or "UNRESOLVED"
        groups[canon][surf] += c
    return {k: dict(v.most_common()) for k, v in groups.items()}


# ---------------------------------------------------------------------------
# 4 — relation-verb mining between actor pairs
# ---------------------------------------------------------------------------
def mine_relation_verbs(nlp, texts):
    """For sentences containing >=2 actor mentions, collect the governing verb (lemma)
    plus a short particle/object cue, and bucket it by seeded relation. Verbs that match
    no seed cue are reported under UNMAPPED — that's your signal to add a synonym or a
    new relation type."""
    by_rel = defaultdict(Counter)
    for doc in nlp.pipe(texts, batch_size=64):
        for sent in doc.sents:
            actors = [e for e in sent.ents
                      if e.label_ in {"GPE", "ORG", "PERSON", "NORP"}
                      and canon_for(e.text, SEED_CANON)]
            canons = {canon_for(e.text, SEED_CANON) for e in actors}
            if len(canons) < 2:
                continue                       # need a directed pair candidate
            for tok in sent:
                if tok.pos_ == "VERB":
                    prt = "".join(" " + c.text.lower() for c in tok.children
                                  if c.dep_ in {"prt", "prep"})[:12]
                    phrase = (tok.lemma_.lower() + prt).strip()
                    rel = None
                    for r, cues in SEED_REL_CUES.items():
                        if any(cue in phrase or phrase in cue for cue in cues):
                            rel = r
                            break
                    by_rel[rel or "UNMAPPED"][phrase] += 1
    return {k: dict(v.most_common(80)) for k, v in by_rel.items()}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", nargs="+", required=True,
                    help="one or more CSVs with text to recon; must have a date column")
    ap.add_argument("--text-col", default="body")
    ap.add_argument("--date-col", default="date")
    ap.add_argument("--sample", type=int, default=3000,
                    help="recon on a random sample; 0 = whole corpus")
    ap.add_argument("--model", default="en_core_web_sm")
    args = ap.parse_args()


    df = pd.concat([pd.read_csv(f, parse_dates=[args.date_col]) for f in args.csv], ignore_index=True)
    if args.sample and len(df) > args.sample:
        df = df.sample(args.sample, random_state=0)
    texts = df[args.text_col].dropna().astype(str).tolist()
    print(f"Recon on {len(texts)} documents...")

    nlp = spacy.load(args.model)

    counts = discover_actors(nlp, texts)
    pd.DataFrame(counts.most_common(200), columns=["surface", "count"]).to_csv(
        "recon_actors.csv", index=False)

    groups = group_aliases(counts)
    with open("recon_alias_groups.json", "w") as f:
        json.dump(groups, f, indent=2, ensure_ascii=False)

    with open("recon_bloc_skeleton.json", "w") as f:
        json.dump({"_edit_me": "assign each canonical actor to a bloc; this is your call",
                   "bloc": SEED_BLOC}, f, indent=2)

    verbs = mine_relation_verbs(nlp, texts)
    rows = [(rel, phrase, c) for rel, d in verbs.items() for phrase, c in d.items()]
    pd.DataFrame(rows, columns=["relation_bucket", "verb_phrase", "count"]).to_csv(
        "recon_relation_verbs.csv", index=False)

    print("Wrote: recon_actors.csv, recon_alias_groups.json, "
          "recon_bloc_skeleton.json, recon_relation_verbs.csv")
    print("\nNext: review recon_actors.csv (any actor missing from the seed?), approve the "
          "UNRESOLVED group in recon_alias_groups.json, edit the bloc skeleton, and scan the "
          "UNMAPPED bucket in recon_relation_verbs.csv for verbs your 12 relations don't cover.")


if __name__ == "__main__":
    main()