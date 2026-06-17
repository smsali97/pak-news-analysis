"""
export_viz.py — build the animation data file for viz/index.html.

Reads the cached extractions (out/extractions.jsonl), does all the graph math
offline using the existing pipeline functions, and writes viz/data.js as
    window.VIZ_DATA = { ... }
so the front-end loads it with a plain <script> tag (no server / no CORS).

What it emits:
  meta        — date range, weeks, phase boundary, bloc colors, size/score scales
  nodes_meta  — per actor: {id, bloc, color, x, y}  (one FIXED layout, shared by
                both outlets so nodes don't jump when you toggle)
  frames      — keyed "outlet|mode|week" -> per-frame node sizes + brokerage +
                aggregated dyad edges + broker leaderboard
  events      — every validated edge with date/quote/url for the citation panel
  timeline    — labeled event markers (talks boundary + auto-detected anchors)

Usage:
  python export_viz.py
  # then open viz/index.html
"""

import json
import os
from collections import defaultdict
from datetime import date, timedelta

import networkx as nx
import pandas as pd

import extraction as ex
import mediation_pipeline as mp
import substance as sb
from vocab import BLOC

EXTRACTIONS = "out/extractions.jsonl"
ARTICLES = "articles.csv"
OUT = "viz/data.js"
PHASE_BOUNDARY = ex.PHASE_BOUNDARY            # "2026-04-08"
WINDOW_DAYS = 28                               # trailing window for "window" mode
OUTLETS = ["dawn", "guardian"]
MODES = ["window", "cumulative"]

# one stable colour per bloc (Pakistan = "Broker" gets the standout amber)
BLOC_COLORS = {
    "Broker":        "#f5a623",   # Pakistan — standout
    "US_bloc":       "#4a90e2",
    "Iran_bloc":     "#43c59e",
    "E3":            "#9b59b6",
    "Mediators":     "#e2864a",
    "Arab_states":   "#d9b44a",
    "International":  "#8a93a3",
}
DEFAULT_COLOR = "#8a93a3"


def parse_date(s):
    return date.fromisoformat(s[:10])


def polarity_of(coop, conf):
    if coop > conf:
        return "coop"
    if conf > coop:
        return "conflict"
    return "neutral"


def load_records():
    recs = []
    with open(EXTRACTIONS) as f:
        for line in f:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    return recs


def build_events(recs):
    """Flatten to one event per edge, inheriting the record date + joining url/headline."""
    art = pd.read_csv(ARTICLES)
    events = []
    for r in recs:
        d = r["date"][:10]
        # doc_id = "{row}_{chunk}"; row is the original articles.csv index
        try:
            row_i = int(r["doc_id"].split("_")[0])
            arow = art.iloc[row_i]
            url = "" if pd.isna(arow.get("url")) else str(arow["url"])
            headline = "" if pd.isna(arow.get("headline")) else str(arow["headline"])
        except (ValueError, IndexError, KeyError):
            url, headline = "", ""
        for e in r["edges"]:
            events.append({
                "date": d, "outlet": e["outlet"],
                "source": e["source"], "target": e["target"],
                "polarity": e["polarity"], "valence": e["valence"],
                "verb": e.get("raw_verb", ""), "quote": e.get("quote", ""),
                "url": url, "headline": headline,
            })
    return events


def fixed_layout(events):
    """One spring layout on the union of all edges (both outlets), normalized [0,1]."""
    G = nx.Graph()
    for e in events:
        if e["source"] != e["target"]:
            G.add_edge(e["source"], e["target"])
    pos = nx.spring_layout(G, seed=0, k=0.9, iterations=200)
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    sx = (maxx - minx) or 1.0
    sy = (maxy - miny) or 1.0
    nodes = []
    for n, (x, y) in pos.items():
        bloc = BLOC.get(n, "International")
        nodes.append({
            "id": n, "bloc": bloc, "color": BLOC_COLORS.get(bloc, DEFAULT_COLOR),
            "x": round((x - minx) / sx, 4), "y": round((y - miny) / sy, 4),
        })
    return nodes, set(G.nodes())


def edges_for(events, outlet, mode, week_end, min_date):
    lo = week_end - timedelta(days=WINDOW_DAYS)
    out = []
    for e in events:
        if e["outlet"] != outlet:
            continue
        ed = parse_date(e["date"])
        if ed >= week_end:
            continue
        if mode == "window" and ed < lo:
            continue
        out.append(e)
    return out


def make_frame(edge_events):
    """Build the signed network for this slice and compute everything the frame needs."""
    edges = [{"source": e["source"], "target": e["target"], "valence": e["valence"],
              "raw_verb": e["verb"]} for e in edge_events]
    G = ex.build_signed_network(edges)
    infl = ex.node_influence(G)
    fb = mp.flow_brokerage(ex.cooperation_view(G))                       # flat (salience)
    fb_g = mp.flow_brokerage(sb.weighted_cooperation_view(               # Goldstein (substance)
        [{"source": e["source"], "target": e["target"], "polarity": e["polarity"], "raw_verb": e["verb"]} for e in edge_events]))
    fb_brk = mp.flow_brokerage(ex.brokering_view(G))

    nodes = []
    for n in G.nodes():
        size = infl.get(n, 0)
        broker, broker_g = round(fb.get(n, 0.0), 4), round(fb_g.get(n, 0.0), 4)
        if size == 0 and broker == 0 and broker_g == 0:
            continue
        nodes.append({"id": n, "size": size, "broker": broker, "broker_g": broker_g,
                      "broker_real": round(fb_brk.get(n, 0.0), 4)})

    out_edges = []
    for s, t, d in G.edges(data=True):
        coop, conf, neu = d.get("coop", 0), d.get("conf", 0), d.get("neu", 0)
        w = coop + conf + neu
        if w == 0:
            continue
        out_edges.append({"s": s, "t": t, "coop": coop, "conf": conf, "neu": neu,
                          "w": w, "pol": polarity_of(coop, conf)})

    ranked = sorted(((round(v, 4), n) for n, v in fb.items() if v > 0), reverse=True)
    leaderboard = [{"id": n, "score": v, "rank": i + 1} for i, (v, n) in enumerate(ranked)]
    pak_rank = next((r["rank"] for r in leaderboard if r["id"] == "Pakistan"), None)
    return {"nodes": nodes, "edges": out_edges,
            "leaderboard": leaderboard[:8], "pak_rank": pak_rank}


# Curated story chapters (date ranges grounded in the data profile: volume spike in
# March = war; Pakistan vaults to #1 in Dawn late March; talks boundary 8 Apr).
CHAPTERS = [
    {"id": 1, "title": "Setting the board",
     "lead": "Qatar, Egypt and Turkey broker the Gaza ceasefire while Oman — Iran's traditional channel — nudges Washington and Tehran toward talks. Yet Dawn already frames Pakistan as a lead broker.",
     "start": "2025-10-06", "end": "2026-01-01"},
    {"id": 2, "title": "Pressure on Iran",
     "lead": "Sanctions, snapback and nuclear brinkmanship escalate through the winter. The UN and the old Gulf mediators stay busy as the major powers circle Tehran.",
     "start": "2026-01-01", "end": "2026-03-01"},
    {"id": 3, "title": "War with Iran",
     "lead": "Conflict erupts and coverage explodes — the US, Israel and Iran dominate the board.",
     "start": "2026-03-01", "end": "2026-03-28"},
    {"id": 4, "title": "Pakistan brokers the talks",
     "lead": "Pakistan moves to the centre — hosting and facilitating — as the Islamabad talks convene on 8 April.",
     "start": "2026-03-28", "end": "2026-05-01"},
    {"id": 5, "title": "After the ceasefire",
     "lead": "A framework holds. Even the Guardian now counts Pakistan among the brokers.",
     "start": "2026-05-01", "end": "2026-06-16"},
]


def hero_citation(sub):
    """Pick the most story-worthy quote in a slice: a Pakistan brokering line if any."""
    pk = [e for e in sub if (e["source"] == "Pakistan" or e["target"] == "Pakistan") and e["polarity"] == "positive"]
    brk = [e for e in pk if any(b in (e["verb"] or "") for b in ex.BROKERING_VERBS)]
    pool = brk or pk or sub
    pool = sorted(pool, key=lambda e: (not (45 <= len(e["quote"]) <= 200), e["date"]))
    return pool[0] if pool else None


# Combatants/principals — NOT brokers. Brokerage = the third parties between them.
PRINCIPALS = {"US", "Iran", "Israel", "Hezbollah", "Lebanon"}


def outlet_summary(events, outlet, start, end):
    sub = [e for e in events if e["outlet"] == outlet and start <= e["date"] < end]
    edges = [{"source": e["source"], "target": e["target"], "valence": e["valence"],
              "raw_verb": e["verb"]} for e in sub]
    G = ex.build_signed_network(edges)
    C = ex.cooperation_view(G)
    fb = mp.flow_brokerage(C)
    fb_g = mp.flow_brokerage(sb.weighted_cooperation_view(
        [{"source": e["source"], "target": e["target"], "polarity": e["polarity"], "raw_verb": e["verb"]} for e in sub]))
    # rank only third-party brokers (exclude the combatants), flat + substance
    med = [n for n in sorted(fb, key=fb.get, reverse=True) if fb[n] > 0 and n not in PRINCIPALS]
    med_g = [n for n in sorted(fb_g, key=fb_g.get, reverse=True) if fb_g[n] > 0 and n not in PRINCIPALS]
    pak_rank = (med.index("Pakistan") + 1) if "Pakistan" in med else None
    pak_rank_g = (med_g.index("Pakistan") + 1) if "Pakistan" in med_g else None
    best_dyad, best_w = None, 0
    for s, t, d in C.edges(data=True):
        if d.get("strength", 0) > best_w:
            best_w, best_dyad = d["strength"], [s, t]
    hero = hero_citation(sub)
    return {
        "articles": len({e.get("quote", "")[:30] for e in sub}),
        "ties": C.number_of_edges(),
        "pak_rank": pak_rank, "pak_score": round(fb.get("Pakistan", 0.0), 3),
        "pak_rank_g": pak_rank_g, "pak_score_g": round(fb_g.get("Pakistan", 0.0), 3),
        "lead_broker": med[0] if med else None,
        "lead_broker_g": med_g[0] if med_g else None,
        "lead_score": round(fb[med[0]], 3) if med else 0,
        "top3": [{"id": n, "score": round(fb[n], 3)} for n in med[:3]],
        "busiest": best_dyad,
        "hero": {k: hero[k] for k in ("date", "source", "target", "verb", "quote", "url", "headline")} if hero else None,
    }


def build_chapters(events, week_ends):
    def week_of(dstr):
        for i, we in enumerate(week_ends):
            if we.isoformat() > dstr:
                return i
        return len(week_ends) - 1
    out = []
    for ch in CHAPTERS:
        out.append({**ch,
                    "start_week": week_of(ch["start"]),
                    "end_week": week_of(ch["end"]) ,
                    "dawn": outlet_summary(events, "dawn", ch["start"], ch["end"]),
                    "guardian": outlet_summary(events, "guardian", ch["start"], ch["end"])})
    return out


def detect_timeline(events):
    """Talks boundary + first-occurrence anchors from headline keywords."""
    marks = [{"date": PHASE_BOUNDARY, "label": "Islamabad talks"}]
    keywords = [("ceasefire", "Ceasefire"), ("strike", "Strikes / war"),
                ("snapback", "Snapback")]
    seen = {}
    for e in sorted(events, key=lambda x: x["date"]):
        h = (e.get("headline") or "").lower()
        for kw, label in keywords:
            if kw not in seen and kw in h:
                seen[kw] = {"date": e["date"], "label": label}
    marks.extend(seen.values())
    return sorted(marks, key=lambda m: m["date"])


def main():
    os.makedirs("viz", exist_ok=True)
    recs = load_records()
    events = build_events(recs)
    print(f"Loaded {len(recs)} records, {len(events)} edge-events.")

    dates = [parse_date(e["date"]) for e in events]
    min_date, max_date = min(dates), max(dates)
    num_weeks = (max_date - min_date).days // 7 + 1
    nodes_meta, present = fixed_layout(events)
    print(f"Date range {min_date}..{max_date}  ({num_weeks} weeks)  {len(nodes_meta)} actors placed.")

    # weekly slices and per-event week index
    week_ends = [min_date + timedelta(days=7 * (w + 1)) for w in range(num_weeks)]
    for e in events:
        e["week"] = (parse_date(e["date"]) - min_date).days // 7

    frames = {}
    max_size = 1
    max_score = 0.001
    for outlet in OUTLETS:
        for mode in MODES:
            for w, week_end in enumerate(week_ends):
                fr = make_frame(edges_for(events, outlet, mode, week_end, min_date))
                fr["week"] = w
                fr["date"] = (week_end - timedelta(days=1)).isoformat()
                frames[f"{outlet}|{mode}|{w}"] = fr
                max_size = max(max_size, *(n["size"] for n in fr["nodes"]), 1)
                max_score = max(max_score, *(n["broker"] for n in fr["nodes"]), 0.001)
    print(f"Built {len(frames)} frames. max_size={max_size} max_broker={max_score:.3f}")

    data = {
        "meta": {
            "min_date": min_date.isoformat(), "max_date": max_date.isoformat(),
            "num_weeks": num_weeks, "week_dates": [d.isoformat() for d in week_ends],
            "phase_boundary": PHASE_BOUNDARY, "window_days": WINDOW_DAYS,
            "outlets": OUTLETS, "modes": MODES,
            "bloc_colors": BLOC_COLORS, "max_size": max_size, "max_score": max_score,
        },
        "nodes_meta": nodes_meta,
        "frames": frames,
        "events": [{k: e[k] for k in ("date", "week", "outlet", "source", "target",
                                      "polarity", "valence", "verb", "quote", "url", "headline")}
                   for e in events],
        "timeline": detect_timeline(events),
        "chapters": build_chapters(events, week_ends),
    }

    with open(OUT, "w") as f:
        f.write("window.VIZ_DATA = ")
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    kb = os.path.getsize(OUT) / 1024
    print(f"Wrote {OUT} ({kb:.0f} KB). Timeline: {[m['label'] for m in data['timeline']]}")

    # sanity: the load-bearing finding must survive the export
    def pak_flow(outlet, phase):
        ph_week = next(i for i, d in enumerate(week_ends) if d.isoformat() >= PHASE_BOUNDARY)
        w = (ph_week - 1) if phase == "pre" else (num_weeks - 1)
        fr = frames[f"{outlet}|cumulative|{w}"]
        return next((n["broker"] for n in fr["nodes"] if n["id"] == "Pakistan"), 0.0), fr["pak_rank"]
    for o in OUTLETS:
        flow, rank = pak_flow(o, "pre")
        print(f"  sanity {o} pre-talks (cumulative): Pakistan flow={flow} rank={rank}")


if __name__ == "__main__":
    main()
