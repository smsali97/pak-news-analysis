"""
substance.py — Goldstein-INSPIRED substance weighting for cooperation/conflict edges.

Replaces flat +1 votes with substance-graded weights so HOSTING outweighs URGING, the way
the Goldstein scale grades CAMEO events (material acts at the extremes, verbal/intent in the
middle, cheap talk near +1). Built from the corpus's actual raw_verb frequencies.

Matching is WORD-BOUNDARY token + inflection lookup (NOT substring): 'aid' never matches
'said', 'sign' never matches 'designate'/'resign'/'signal'. Sign comes from the LLM polarity;
magnitude from the verb's tier.

Reporting/attribution verbs (say/announce/report/...) map to 0 ONLY when no content verb is
present, so 'announced new sanctions' still scores as a sanction while a bare 'said' drops out.

API:  substance(raw_verb, polarity) -> (signed_weight, tier_label)
"""

import re

# ---- content tiers (inflections generated automatically). priority: lower wins. ----
_POS = {  # label: (priority, weight, [bases])
    "intent":      (1, 3.0, ["offer", "propose", "pitch", "plan", "seek", "explore",
                             "willing", "ready", "prepared", "volunteer"]),
    "substantive": (2, 6.0, ["host", "convene", "reconvene", "mediate", "broker", "facilitate",
                             "shuttle", "sign", "ratify", "ceasefire", "truce", "agree", "reach",
                             "accept", "accord", "aid", "supply", "arm", "withdraw", "release",
                             "concede", "ease", "lift", "provide", "send", "deliver", "approve", "assist", "restore",
                             "resume", "normalise", "normalize", "guarantee"]),
    "positioning": (3, 3.0, ["urge", "call", "press", "pressure", "insist", "negotiate", "meet",
                             "talk", "discuss", "consult", "phone", "speak", "welcome", "back",
                             "support", "endorse", "pledge", "work", "deescalate", "encourage",
                             "invite", "commit", "engage", "coordinate", "hold", "ask", "request",
                             "assure", "reassure", "stand", "approach", "cooperate", "partner", "draft", "recognise",
                             "recognize", "accommodate"]),
    "stance":      (4, 1.0, ["praise", "thank", "laud", "credit", "nominate", "hail", "commend",
                             "congratulate", "reaffirm", "emphasise", "emphasize", "underscore",
                             "appreciate", "hope", "want", "consider", "express", "signal",
                             "acknowledge", "stress", "apologise", "apologize", "regret", "note", "affirm", "reiterate",
                             "grateful", "supportive", "value", "salute", "respect"]),
}
_NEG = {
    "assault":  (0, -9.0, ["strike", "bomb", "attack", "kill", "hit", "launch", "destroy", "raid",
                           "invade", "seize", "blockade", "airstrike", "target", "shell", "fire",
                           "storm", "assault", "intercept", "shoot", "down", "neutralise", "neutralize", "obliterate",
                           "eliminate", "damage", "wound", "injure", "raze", "flatten", "pound"]),
    "threaten": (1, -6.0, ["threaten", "warn", "vow", "retaliate", "menace", "threat"]),
    "sanction": (2, -5.0, ["sanction", "blacklist", "embargo", "snapback", "reimpose", "impose",
                           "penalise", "penalize"]),
    "reject":   (3, -4.0, ["reject", "refuse", "deny", "dismiss", "rule", "block", "breach", "veto"]),
    "demand":   (4, -3.0, ["demand", "urge", "call", "press", "insist", "pressure", "order",
                           "push", "prod", "ask"]),
    "accuse":   (5, -3.0, ["accuse", "condemn", "blame", "criticise", "criticize", "slam",
                           "denounce", "protest", "suppress", "oppress", "repress", "crack", "lash", "oppose",
                           "decry", "rebuke"]),
}
_REPORT = {"say", "tell", "report", "claim", "add", "announce", "state", "cite", "describe",
           "declare", "suggest", "argue", "indicate", "comment", "remark", "confirm"}
_IRREG = {"said": "say", "told": "tell", "sent": "send", "brought": "bring", "met": "meet",
          "spoke": "speak", "spoken": "speak", "held": "hold", "struck": "strike",
          "sought": "seek", "withdrew": "withdraw", "withdrawn": "withdraw", "made": "make", "stood": "stand", "shot": "shoot"}
_PARTICLES = {"to","on","in","with","for","of","at","by","as","the","a","an","against","over",
              "after","during","about","from","into","upon","out","off","between","amid",
              "despite","before","within","across","through","since","following","given","under",
              "toward","towards","alongside","without","because","compared","excluding",
              "including","aboard","throughout","down","up","away","that","it","its","their","and"}


_VOWELS = set("aeiou")

def _inflect(base):
    forms = {base}
    if base.endswith("e"):
        forms |= {base + "s", base + "d", base[:-1] + "ing"}
    elif base.endswith("y") and len(base) > 1 and base[-2] not in _VOWELS:
        forms |= {base[:-1] + "ies", base[:-1] + "ied", base + "ing"}   # deny->denies/denied
    elif base.endswith(("s", "sh", "ch", "x", "z")):
        forms |= {base + "es", base + "ed", base + "ing"}
    else:
        forms |= {base + "s", base + "ed", base + "ing"}
    return forms


def _build(tier_dict):
    lu = {}
    for label, (prio, w, bases) in tier_dict.items():
        for b in bases:
            for f in _inflect(b):
                lu.setdefault(f, (prio, w, label))
    return lu


_POS_LU, _NEG_LU = _build(_POS), _build(_NEG)
_REPORT_FORMS = set()
for _b in _REPORT:
    _REPORT_FORMS |= _inflect(_b)


def _tokens(raw_verb):
    return [_IRREG.get(t, t) for t in re.findall(r"[a-z]+", str(raw_verb).lower())
            if t not in _PARTICLES]


def substance(raw_verb, polarity):
    """Return (signed_weight, tier_label). Content verb wins; a bare reporting verb -> 0."""
    if polarity == "neutral":
        return 0.0, "neutral"
    pos = polarity == "positive"
    lu = _POS_LU if pos else _NEG_LU
    toks = _tokens(raw_verb)
    best = None
    for t in toks:
        hit = lu.get(t)
        if hit and (best is None or hit[0] < best[0]):
            best = hit
    if best:
        return best[1], best[2]
    if any(t in _REPORT_FORMS for t in toks):          # reporting only, no content verb -> drop
        return 0.0, "report_drop"
    return (2.0, "other_positive") if pos else (-3.0, "other_negative")


from collections import defaultdict as _dd
import networkx as _nx


def weighted_cooperation_view(edges):
    """Cooperation graph whose edge strength = SUM of substance weights of positive edges
    (reporting verbs contribute 0 and drop out). Feed to mediation_pipeline.flow_brokerage."""
    w = _dd(float)
    for e in edges:
        if e.get("polarity") == "positive":
            wt, _ = substance(e.get("raw_verb", ""), "positive")
            if wt > 0:
                w[(e["source"], e["target"])] += wt
    G = _nx.DiGraph()
    for (s, t), c in w.items():
        if c > 0:
            G.add_edge(s, t, strength=c, distance=1.0 / c)
    return G