"""
Pakistan-as-mediator brokerage pipeline (US-Iran conflict, Oct 2025-Jun 2026).

Two strictly separated stages:
  STAGE 1  LLM = narrow, span-grounded EXTRACTOR (one chunk -> directed triples).
  STAGE 2  Deterministic Python = network build + brokerage measurement + change.

The LLM never sees the network and never reasons about centrality. Every edge
must carry verbatim evidence that is programmatically verified against the source
text; unverifiable edges are dropped. This is the main anti-contamination control.
"""

import json
import re
from itertools import product
from collections import defaultdict

import networkx as nx
import numpy as np


# ---------------------------------------------------------------------------
# Bloc partition (single source of truth) — synced to vocab.json via vocab.py.
# The Gould-Fernandez roles read ACTORS[node] as the node's bloc. The legacy
# build_network/EVENT_TYPES below are superseded by extraction.py and unused in
# the current flow (extraction builds the signed network; feed cooperation_view
# of that graph to the measures here).
# ---------------------------------------------------------------------------
from vocab import BLOC, ALIASES   # noqa: E402,F401
ACTORS = BLOC                     # {actor: bloc} for all 23 actors

# PLOVER-aligned relation types -> Goldstein-style weight (-10 hostile .. +10 coop)
# and valence flag used to split the cooperation vs. conflict subgraphs.
EVENT_TYPES = {
    # verbal / material cooperation  (the mediation-relevant edges)
    "HOST_TALKS":   (8.0,  "coop"),
    "MEDIATE":      (7.0,  "coop"),
    "CONSULT":      (5.0,  "coop"),
    "AGREE":        (6.0,  "coop"),
    "SUPPORT":      (4.0,  "coop"),
    "AID":          (5.0,  "coop"),
    "CONCEDE":      (3.0,  "coop"),
    # verbal / material conflict
    "DEMAND":       (-3.0, "conflict"),
    "DISAPPROVE":   (-2.0, "conflict"),
    "REJECT":       (-4.0, "conflict"),
    "THREATEN":     (-6.0, "conflict"),
    "SANCTION":     (-5.0, "conflict"),
    "ASSAULT":      (-9.0, "conflict"),
}

# Modalities that mean "this did NOT happen as a real event" -> exclude from graph.
NON_EVENT_MODES = {"negated", "hypothetical", "future"}


# ---------------------------------------------------------------------------
# STAGE 1 - the extraction prompt (send per text chunk, temperature = 0)
# ---------------------------------------------------------------------------
EXTRACTION_PROMPT = """You are a strict information-extraction tool. Read ONLY the
NEWS TEXT below. Do not use any outside knowledge, dates, or events you happen to
know -- if it is not stated in this text, it does not exist for you.

Extract every directed relation between two STATE ACTORS. Output a JSON array of
objects with EXACTLY these fields:

  "source"     : one of {actors}   (the actor performing/stating the relation)
  "target"     : one of {actors}   (the actor it is directed at)
  "relation"   : one of {relations}
  "speaker"    : who is asserting this in the text -- a state actor from the list,
                 or "journalist" / "analyst" / "unknown" if it is commentary
  "modality"   : "asserted" | "negated" | "hypothetical" | "historical" | "future"
  "quote_span" : a VERBATIM substring copied exactly from the NEWS TEXT that is the
                 evidence for this relation (no paraphrase, must appear character-for-
                 character in the text)

Rules:
- Resolve leaders/capitals to their state (e.g. "Sharif"/"Islamabad" -> "Pakistan").
- Only the listed actors. If a relation involves an actor not in the list, skip it.
- One object per relation. If a sentence has several, emit several objects.
- If the relation is attributed to commentators rather than performed by a state
  actor, set "speaker" accordingly so it can be filtered later.
- Output ONLY the JSON array. No prose, no markdown fences.

NEWS TEXT:
\"\"\"{chunk}\"\"\"
"""


def build_prompt(chunk: str) -> str:
    return EXTRACTION_PROMPT.format(
        actors=sorted(ACTORS), relations=sorted(EVENT_TYPES), chunk=chunk
    )


# ---------------------------------------------------------------------------
# STAGE 1b - validation: faithfulness (quote present) + schema sanity.
# This is the anti-hallucination gate. Drop anything that fails.
# ---------------------------------------------------------------------------
def resolve_actor(name: str):
    if not isinstance(name, str):
        return None
    key = name.strip().lower()
    if name in ACTORS:
        return name
    return ALIASES.get(key)


def validate_edges(raw_edges, source_text, doc_id, phase, outlet):
    """Keep only edges whose quote_span literally appears in source_text and that
    are real, asserted, state-actor events. Returns clean edge dicts."""
    clean = []
    norm_text = re.sub(r"\s+", " ", source_text)
    for e in raw_edges:
        try:
            src, tgt = resolve_actor(e["source"]), resolve_actor(e["target"])
            rel, mode = e["relation"], e.get("modality", "asserted")
            span = re.sub(r"\s+", " ", e.get("quote_span", "")).strip()
        except (KeyError, TypeError):
            continue
        if not (src and tgt) or src == tgt:
            continue
        if rel not in EVENT_TYPES or mode in NON_EVENT_MODES:
            continue
        # faithfulness check: evidence must exist in the source text
        if not span or span not in norm_text:
            continue
        # drop pure commentary (speaker is not a state actor)
        if e.get("speaker") in {"analyst", "journalist", "unknown"}:
            continue
        weight, valence = EVENT_TYPES[rel]
        clean.append(dict(source=src, target=tgt, relation=rel, weight=weight,
                          valence=valence, doc_id=doc_id, phase=phase, outlet=outlet))
    return clean


# ---------------------------------------------------------------------------
# STAGE 2 - build one weighted directed network per (phase, outlet)
# ---------------------------------------------------------------------------
def build_network(edges, valence="coop"):
    """Aggregate validated edges into a weighted DiGraph.
    valence='coop' builds the cooperation/mediation subgraph (the one that matters
    for brokerage); 'conflict' builds the hostility network; None = all edges."""
    agg = defaultdict(lambda: {"count": 0, "wsum": 0.0, "rels": defaultdict(int)})
    for e in edges:
        if valence and e["valence"] != valence:
            continue
        k = (e["source"], e["target"])
        agg[k]["count"] += 1
        agg[k]["wsum"] += abs(e["weight"])
        agg[k]["rels"][e["relation"]] += 1

    G = nx.DiGraph()
    G.add_nodes_from(ACTORS)
    for (s, t), d in agg.items():
        strength = d["wsum"]                    # tie strength = summed |weight|
        G.add_edge(s, t,
                   count=d["count"],
                   strength=strength,
                   distance=1.0 / strength,     # strong tie => short distance
                   rels=dict(d["rels"]))
    return G


# ---------------------------------------------------------------------------
# THE KEY MEASURE - Pakistan's brokerage specifically on the US-Iran axis
# ---------------------------------------------------------------------------
def pairwise_brokerage(G, broker="Pakistan", s="US", t="Iran"):
    """Fraction of strongest-cooperation paths between s and t that route through
    `broker`. ~0 when US and Iran connect directly; ~1 when the only bridge is
    s -> broker -> t. This is the cleanest operationalization of 'Pakistan
    in-between the US and Iran'. Computed both directions (undirected reach)."""
    shares = []
    for a, b in [(s, t), (t, s)]:
        if not G.has_node(a) or not G.has_node(b) or not nx.has_path(G, a, b):
            continue
        paths = list(nx.all_shortest_paths(G, a, b, weight="distance"))
        if paths:
            shares.append(sum(broker in p[1:-1] for p in paths) / len(paths))
    return float(np.mean(shares)) if shares else 0.0


def gould_fernandez_liaison(G, broker="Pakistan"):
    """Weighted count of LIAISON two-paths a -> broker -> b where a, broker, b are
    all in different blocs (brokering between two groups the broker doesn't belong
    to). The formal Gould-Fernandez liaison role. For the full weighted-normalized
    version (WNGF) see Hamilton et al. 2022 / R `sna::brokerage`."""
    if not G.has_node(broker):
        return 0.0
    gb = ACTORS[broker]
    total = 0.0
    for a, b in product(G.predecessors(broker), G.successors(broker)):
        if a == b:
            continue
        ga, gbb = ACTORS.get(a), ACTORS.get(b)
        if gb != ga and gb != gbb and ga != gbb:     # three distinct groups
            total += min(G[a][broker]["strength"], G[broker][b]["strength"])
    return total


def standard_centralities(G, node="Pakistan"):
    """Global complements: normalized betweenness, PageRank, Burt constraint,
    effective size. Constraint/eff_size confirm a structural-hole (broker) position."""
    btw = nx.betweenness_centrality(G, weight="distance", normalized=True).get(node, 0.0)
    try:
        pr = nx.pagerank(G, weight="strength").get(node, 0.0)
    except Exception:
        pr = 0.0
    UG = G.to_undirected()
    try:
        constraint = nx.constraint(UG, weight="strength").get(node, np.nan)
        eff = nx.effective_size(UG, weight="strength").get(node, np.nan)
    except Exception:
        constraint, eff = np.nan, np.nan
    return dict(betweenness=btw, pagerank=pr,
                constraint=constraint, effective_size=eff)


# ---------------------------------------------------------------------------
# Change detection across phases + bootstrap CIs (is the change > noise?)
# ---------------------------------------------------------------------------
def bootstrap_ci(edges, fn, n=500, valence="coop", seed=0):
    """Resample documents (not edges) with replacement, rebuild, recompute `fn`,
    return (mean, lo, hi) 95% CI. Resampling at the document level respects that
    extraction error is correlated within a document."""
    rng = np.random.default_rng(seed)
    by_doc = defaultdict(list)
    for e in edges:
        by_doc[e["doc_id"]].append(e)
    docs = list(by_doc)
    if not docs:
        return 0.0, 0.0, 0.0
    vals = []
    for _ in range(n):
        sample = [e for d in rng.choice(docs, len(docs)) for e in by_doc[d]]
        vals.append(fn(build_network(sample, valence=valence)))
    vals = np.array(vals)
    return float(vals.mean()), float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def compare_phases(edges_by_phase, outlet="dawn"):
    """edges_by_phase: {phase_label: [validated edge dicts]} for ONE outlet.
    Tracks Pakistan's brokerage trajectory + bootstrap CIs across the 3 phases,
    alongside baseline mediators so 'Pakistan rises as Oman/Qatar fall' is visible."""
    report = {}
    for phase, edges in edges_by_phase.items():
        G = build_network(edges, valence="coop")     # mediation = cooperation graph
        row = {
            "outlet": outlet,
            "n_nodes": G.number_of_nodes(),
            "n_edges": G.number_of_edges(),
            "PAK_us_iran_brokerage": pairwise_brokerage(G),
            "PAK_gf_liaison": gould_fernandez_liaison(G),
            **{f"PAK_{k}": v for k, v in standard_centralities(G, "Pakistan").items()},
            # baselines: other mediators on the same US-Iran axis
            "OMAN_us_iran_brokerage": pairwise_brokerage(G, broker="Oman"),
            "QATAR_us_iran_brokerage": pairwise_brokerage(G, broker="Qatar"),
        }
        m, lo, hi = bootstrap_ci(edges, pairwise_brokerage)
        row["PAK_us_iran_brokerage_CI"] = (m, lo, hi)
        report[phase] = row
    return report


# ---------------------------------------------------------------------------
# Orchestration sketch (wire your own LLM client + chunked corpus here)
# ---------------------------------------------------------------------------
def run(corpus, call_llm):
    """corpus: iterable of dicts {text, doc_id, phase, outlet} (chunk articles to
    ~paragraph level). call_llm(prompt)->str returns the model's JSON string.
    Phases (corrected): P1 = Oct 2025-Feb 27 2026 (incl. Oman talks);
    P2 = Feb 28-~May 2026 (war + Pakistan ceasefire + Apr 11-12 Islamabad Talks);
    P3 = ~May-Jun 2026 (post-ceasefire / framework deal)."""
    edges_by_outlet_phase = defaultdict(list)
    for chunk in corpus:
        raw = call_llm(build_prompt(chunk["text"]))
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        clean = validate_edges(parsed, chunk["text"], chunk["doc_id"],
                               chunk["phase"], chunk["outlet"])
        edges_by_outlet_phase[(chunk["outlet"], chunk["phase"])].extend(clean)

    results = {}
    for outlet in {o for o, _ in edges_by_outlet_phase}:
        by_phase = {p: edges_by_outlet_phase[(outlet, p)]
                    for (o, p) in edges_by_outlet_phase if o == outlet}
        results[outlet] = compare_phases(by_phase, outlet=outlet)
    return results     # compare results["dawn"] vs results["guardian"] for framing bias


# ---------------------------------------------------------------------------
# Current-flow (random-walk) betweenness — the CORRECT brokerage measure when the
# two parties also talk directly. Shortest-path betweenness returns 0 for a broker
# whenever a direct US-Iran cooperation edge exists (the geodesic skips the broker),
# but mediation is a PARALLEL channel, not the only one. Current-flow betweenness
# credits a node for carrying flow across all paths, so a broker beside a direct tie
# still scores. Feed a cooperation_view graph.
# ---------------------------------------------------------------------------
def flow_brokerage(G):
    """Return {node: current-flow betweenness} on the cooperation graph's largest
    connected component (undirected, weighted by tie strength), normalized 0..1."""
    UG = G.to_undirected()
    for _, _, d in UG.edges(data=True):
        d["weight"] = d.get("strength", 1.0)
    if UG.number_of_edges() == 0:
        return {}
    cc = max(nx.connected_components(UG), key=len)
    H = UG.subgraph(cc).copy()
    try:
        return nx.current_flow_betweenness_centrality(H, weight="weight", normalized=True)
    except Exception:
        return {}


def influence_share(G):
    """Normalized engagement share per node (removes phase-volume artifacts). Use a
    signed_network graph (has coop/conf/neu) so neutral counts toward salience."""
    raw = {n: 0 for n in G.nodes()}
    for s, t, d in G.edges(data=True):
        w = d.get("coop", 0) + d.get("conf", 0) + d.get("neu", 0)
        raw[s] += w
        raw[t] += w
    tot = sum(raw.values()) or 1
    return {n: v / tot for n, v in raw.items()}