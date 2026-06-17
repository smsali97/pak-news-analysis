"""
extraction.py — headlines -> signed dyadic interactions -> signed network.

Design (polarity-cumulative): no fixed relation taxonomy. For each interaction between two
state actors the LLM judges POLARITY (positive / negative / neutral) IN CONTEXT and records
the free-text raw_verb. Every stanced interaction is one vote; the edge weight is the
cumulative tally (coop = #positive, conflict = #negative). Nothing is dropped for failing to
match a label, and unit votes keep weights comparable across phases (frequency carries intensity).

raw_verb is ALWAYS kept, so any relation categorization can be reconstructed in post-processing
later (e.g. a brokering-behaviour filter: keep only broker/mediate/facilitate/host/convene
verbs) to check that positive centrality is real mediation, not ambient goodwill.

Pairs with vocab.py (actors/aliases/bloc) and mediation_pipeline.py (brokerage measures).
Two-phase: before/after the Apr 2026 Islamabad Talks.
"""

import json
import os
import re
from collections import Counter, defaultdict

import networkx as nx
from vocab import ACTORS, BLOC, ALIASES, resolve_actor, actor_list_for_prompt  # noqa: F401

POLARITY = {"positive": "coop", "negative": "conflict", "neutral": "neutral"}
NON_EVENT_MODES = {"negated", "hypothetical", "future"}

# Optional: verbs that indicate actual brokering behaviour, for the post-hoc validation view.
BROKERING_VERBS = ("broker", "mediate", "facilitate", "host", "convene", "shuttle",
                   "bring together", "go-between", "pass message", "relay")

PHASE_BOUNDARY = "2026-04-08"   # < boundary -> "pre"; >= -> "post"


def phase_of(date_str):
    return "pre" if date_str < PHASE_BOUNDARY else "post"


# ---------------------------------------------------------------------------
# Extraction prompt. {actors} and {chunk} injected by build_prompt.
# ---------------------------------------------------------------------------
EXTRACTION_PROMPT = '''You are a strict information-extraction tool. Read ONLY the NEWS TEXT below. Use no
outside knowledge, dates, or events — if it is not stated in this text, it does not exist.

Extract every directed INTERACTION FROM one state actor TO another. Output a JSON object with a
single key "interactions" whose value is an ARRAY of objects, each with EXACTLY these fields:

  "source"     : one of the ACTORS below (the actor doing/stating the interaction)
  "target"     : one of the ACTORS below (the actor it is directed at)
  "polarity"   : "positive" | "negative" | "neutral"
  "raw_verb"   : the actual word/phrase describing the interaction (copy it, any verb is fine)
  "speaker"    : the state actor asserting it, or "journalist"/"analyst"/"unknown"
  "modality"   : "asserted" | "negated" | "hypothetical" | "future" | "historical"
  "quote_span" : a VERBATIM substring copied exactly from the NEWS TEXT as evidence
                 (must appear character-for-character; no paraphrase)

ACTORS (resolve leaders/capitals to the state, e.g. "Sharif"/"Islamabad" -> "Pakistan";
skip any interaction whose actor is not on this list):
{actors}

POLARITY — judge from THIS sentence's context, not the dictionary meaning of the verb:
  positive = cooperative/friendly: talks, meetings, support, praise, aid, agreement, ceasefire,
             mediation, hosting, concessions, a mediator urging restraint.
  negative = hostile/adversarial: attacks, strikes, threats, accusations, condemnation, sanctions,
             rejections, blockades/seizures, ultimatums, demands to surrender.
  neutral  = contact or interaction with no clear cooperative or hostile stance.
Context decides borderline cases:
  "Pakistan urged both sides to extend the ceasefire" -> positive (a mediator's push)
  "Israel demanded Iran surrender or face attack"      -> negative (an ultimatum)
  "officials from the two sides met briefly"            -> neutral

IMPORTANT — do NOT skip an interaction just because the verb is unusual or non-standard.
Capture "leaned on", "courted", "snubbed", "rebuffed", "rallied behind", "froze out", etc.,
by assigning the right polarity. The verb is free text.

Skip ONLY: non-events (negated/hypothetical/future), pure attribution with no interaction
("said the situation was calm"), and anything involving a non-listed actor.
If an interaction is attributed to commentators rather than performed by a state actor, set
"speaker" accordingly so it can be filtered out.
Output ONLY the JSON object {"interactions": [ ... ]}. If there are no interactions, output
{"interactions": []}. No prose, no markdown fences.

Examples:
NEWS TEXT: "Pakistan's prime minister hosted negotiators from Washington and Tehran in
Islamabad, where he urged both sides to extend the ceasefire."
OUTPUT: {"interactions": [
 {"source":"Pakistan","target":"US","polarity":"positive","raw_verb":"hosted negotiators","speaker":"Pakistan","modality":"asserted","quote_span":"hosted negotiators from Washington and Tehran in Islamabad"},
 {"source":"Pakistan","target":"Iran","polarity":"positive","raw_verb":"hosted negotiators","speaker":"Pakistan","modality":"asserted","quote_span":"hosted negotiators from Washington and Tehran in Islamabad"},
 {"source":"Pakistan","target":"US","polarity":"positive","raw_verb":"urged","speaker":"Pakistan","modality":"asserted","quote_span":"urged both sides to extend the ceasefire"},
 {"source":"Pakistan","target":"Iran","polarity":"positive","raw_verb":"urged","speaker":"Pakistan","modality":"asserted","quote_span":"urged both sides to extend the ceasefire"}
]}

NEWS TEXT: "Israel demanded Iran abandon enrichment or face attack, and later struck Natanz."
OUTPUT: {"interactions": [
 {"source":"Israel","target":"Iran","polarity":"negative","raw_verb":"demanded","speaker":"Israel","modality":"asserted","quote_span":"demanded Iran abandon enrichment"},
 {"source":"Israel","target":"Iran","polarity":"negative","raw_verb":"or face attack","speaker":"Israel","modality":"asserted","quote_span":"abandon enrichment or face attack"},
 {"source":"Israel","target":"Iran","polarity":"negative","raw_verb":"struck","speaker":"Israel","modality":"asserted","quote_span":"later struck Natanz"}
]}

NEWS TEXT: "China quietly leaned on Tehran to keep the talks alive, analysts said."
OUTPUT: {"interactions": [
 {"source":"China","target":"Iran","polarity":"positive","raw_verb":"leaned on","speaker":"analyst","modality":"asserted","quote_span":"China quietly leaned on Tehran to keep the talks alive"}
]}

NEWS TEXT:
"""{chunk}"""'''


def build_prompt(chunk):
    return EXTRACTION_PROMPT.replace("{actors}", actor_list_for_prompt()).replace("{chunk}", chunk)


# ---------------------------------------------------------------------------
# LLM extractor (provider-agnostic; pick one adapter)
# ---------------------------------------------------------------------------
def call_llm_openai(prompt, model="gpt-4o-2024-08-06"):
    from openai import OpenAI
    import os
    api_key = os.getenv("OPENAI_API_KEY")
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    r = client.chat.completions.create(
        model=model, temperature=0,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    return r.choices[0].message.content


def call_llm_gemini(prompt, model="gemini-1.5-pro"):
    import google.generativeai as genai
    m = genai.GenerativeModel(model)
    r = m.generate_content(prompt, generation_config={"temperature": 0,
                                                       "response_mime_type": "application/json"})
    return r.text


def _loads(raw):
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():                  # wrapped: {"interactions": [...]}
            if isinstance(v, list):
                return v
        if {"source", "target"} <= obj.keys():  # model returned a single bare edge
            return [obj]
    return []


def extract_triples(chunk, call_llm):
    return _loads(call_llm(build_prompt(chunk)))


# Typographic normalization so the faithfulness gate doesn't reject a span just
# because the model straightened a curly quote or swapped an en-dash for a hyphen.
_PUNCT_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "―": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", " ": " ",
    "…": "...", "•": " ", "·": " ", "‧": " ",
    "﻿": "", "​": "",
}


def canon_text(s):
    """Casefold + collapse whitespace + fold typographic punctuation to ASCII.
    Used on BOTH the source text and the quote_span before the substring check."""
    s = s or ""
    for k, v in _PUNCT_MAP.items():
        s = s.replace(k, v)
    return re.sub(r"\s+", " ", s).strip().casefold()


def validate_edges_verbose(raw_edges, source_text, doc_id, phase, outlet):
    """Same gates as validate_edges, but also returns a per-edge drop log so you can
    SEE which gate killed an edge (the 'rationale'). Returns (clean, drops)."""
    clean, drops, norm = [], [], canon_text(source_text)
    for e in raw_edges:
        if not isinstance(e, dict):
            drops.append({"edge": e, "reason": "not an object"})
            continue
        rs, rt = e.get("source"), e.get("target")
        s, t = resolve_actor(rs), resolve_actor(rt)
        pol, mode = e.get("polarity"), e.get("modality", "asserted")
        span = (e.get("quote_span") or "").strip()
        if not s:
            drops.append({"edge": e, "reason": f"unresolved source: {rs!r}"})
            continue
        if not t:
            drops.append({"edge": e, "reason": f"unresolved target: {rt!r}"})
            continue
        if s == t:
            drops.append({"edge": e, "reason": f"self-loop ({s})"})
            continue
        if pol not in POLARITY:
            drops.append({"edge": e, "reason": f"bad polarity: {pol!r}"})
            continue
        if mode in NON_EVENT_MODES:
            drops.append({"edge": e, "reason": f"non-event modality: {mode}"})
            continue
        if not span:
            drops.append({"edge": e, "reason": "empty quote_span"})
            continue
        if canon_text(span) not in norm:
            drops.append({"edge": e, "reason": "quote_span not in text (hallucination gate)"})
            continue
        if e.get("speaker") in {"analyst", "journalist", "unknown"}:
            drops.append({"edge": e, "reason": f"commentary speaker: {e.get('speaker')!r}"})
            continue
        clean.append(dict(source=s, target=t, polarity=pol, valence=POLARITY[pol],
                          raw_verb=(e.get("raw_verb") or "").lower(), quote=span,
                          doc_id=doc_id, phase=phase, outlet=outlet))
    return clean, drops


def validate_edges(raw_edges, source_text, doc_id, phase, outlet):
    """Faithfulness + modality + state-actor gates. Each kept interaction is one signed vote."""
    clean, _ = validate_edges_verbose(raw_edges, source_text, doc_id, phase, outlet)
    return clean


# ---------------------------------------------------------------------------
# ONE signed network from cumulative votes; project to cooperation/conflict
# ---------------------------------------------------------------------------
def build_signed_network(edges):
    """coop = #positive votes, conf = #negative votes, neu = #neutral; signed = coop - conf.
    'broker' = #positive votes whose raw_verb is a brokering verb (post-hoc validation)."""
    agg = defaultdict(lambda: {"coop": 0, "conf": 0, "neu": 0, "broker": 0, "verbs": Counter()})
    for e in edges:
        a = agg[(e["source"], e["target"])]
        a[{"coop": "coop", "conflict": "conf", "neutral": "neu"}[e["valence"]]] += 1
        a["verbs"][e["raw_verb"]] += 1
        if e["valence"] == "coop" and any(b in e["raw_verb"] for b in BROKERING_VERBS):
            a["broker"] += 1
    G = nx.DiGraph()
    G.add_nodes_from(ACTORS)
    for (s, t), a in agg.items():
        G.add_edge(s, t, signed=a["coop"] - a["conf"], coop=a["coop"], conf=a["conf"],
                   neu=a["neu"], broker=a["broker"], verbs=dict(a["verbs"]))
    return G


def _project(G, field):
    P = nx.DiGraph()
    P.add_nodes_from(G.nodes())
    for s, t, d in G.edges(data=True):
        if d.get(field, 0) > 0:
            P.add_edge(s, t, strength=d[field], distance=1.0 / d[field])
    return P


def cooperation_view(G):
    """Positive-vote subgraph for the shortest-path bridge score (mediation_pipeline)."""
    return _project(G, "coop")


def conflict_view(G):
    return _project(G, "conf")


def brokering_view(G):
    """Validation view: only positive ties carried by actual brokering verbs. If Pakistan's
    bridge score holds here too, its centrality is real mediation, not ambient goodwill."""
    return _project(G, "broker")


def run(corpus, call_llm):
    """corpus: iterable of {text, doc_id, date, outlet}. Returns {outlet: {phase: signed_G}}
    plus raw validated edges for spot-checking."""
    edges = defaultdict(list)
    for c in corpus:
        ph = phase_of(c["date"])
        edges[(c["outlet"], ph)].extend(
            validate_edges(extract_triples(c["text"], call_llm),
                           c["text"], c["doc_id"], ph, c["outlet"]))
    graphs = defaultdict(dict)
    for (outlet, ph), es in edges.items():
        graphs[outlet][ph] = build_signed_network(es)
    return graphs, edges


def node_influence(G, include_neutral=True):
    """Total engagement per actor = summed interaction volume over coop + conf (+ neutral).
    Neutral ties count toward influence/SIZE even though they are 0 net polarity and are
    excluded from the cooperation shortest-path strength — a heavily engaged actor is salient.
    Use this for node SIZE in visualisations and as a salience measure alongside brokerage."""
    inf = {n: 0 for n in G.nodes()}
    for s, t, d in G.edges(data=True):
        w = d["coop"] + d["conf"] + (d["neu"] if include_neutral else 0)
        inf[s] += w
        inf[t] += w
    return inf


def engagement_view(G):
    """Undirected-ish total-contact projection (coop+conf+neutral) — the network where neutral
    ties are NOT ignored. For descriptive structure/size, not for the signed bridge score."""
    E = nx.DiGraph()
    E.add_nodes_from(G.nodes())
    for s, t, d in G.edges(data=True):
        tot = d["coop"] + d["conf"] + d["neu"]
        if tot > 0:
            E.add_edge(s, t, strength=tot, distance=1.0 / tot)
    return E
