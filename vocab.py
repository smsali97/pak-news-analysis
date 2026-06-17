"""
vocab.py — load the reconciled vocabulary and resolve actor mentions correctly.

Fixes the recon bug: the old matcher used substring tests (`n in f`), so "islam"
matched "islamabad" and "ai" matched "busaidi". Here resolution is EXACT on a
normalized alias lookup (longest-form first), so partial strings no longer leak.

Usage in extraction.py:
    from vocab import ACTORS, BLOC, ALIASES, resolve_actor, EXCLUDED
Replace the hand-written ACTORS/ALIASES/BLOC and the old resolve_actor with these.
"""

import json
import re
from pathlib import Path

_VOCAB = json.loads((Path(__file__).parent / "vocab.json").read_text())

ACTORS = _VOCAB["actors"]
BLOC = _VOCAB["bloc"]
ALIASES = _VOCAB["aliases"]
EXCLUDED = _VOCAB.get("excluded_frequent", {})

# Build a normalized surface-form -> canonical lookup. Canonical names map to
# themselves; every alias maps to its canonical actor.
def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()

_LOOKUP = {}
for _canon in ACTORS:
    _LOOKUP[_norm(_canon.replace("_", " "))] = _canon
    _LOOKUP[_norm(_canon)] = _canon
for _canon, _forms in ALIASES.items():
    for _f in _forms:
        _LOOKUP[_norm(_f)] = _canon

_EXCLUDED_NORM = {_norm(k) for k in EXCLUDED}


def resolve_actor(name):
    """Return the canonical actor for a surface mention, or None.
    Exact normalized match only — no substring leakage. Explicitly excluded
    entities resolve to None so they never enter the network."""
    if not isinstance(name, str):
        return None
    n = _norm(name)
    if not n or n in _EXCLUDED_NORM:
        return None
    if n in _LOOKUP:
        return _LOOKUP[n]
    # one safe fallback: if the mention is "<alias>'s" or "the <alias>", strip and retry
    n2 = re.sub(r"^the ", "", n).rstrip("s") if n.startswith("the ") else n
    return _LOOKUP.get(n2)


def actor_list_for_prompt():
    """Comma-joined canonical actors for the extraction prompt's ACTORS line."""
    return ", ".join(ACTORS)


# --- coarse relevance detection (for a free, pre-LLM article filter) ----------
# One regex per canonical actor matching any of its alias surface forms as a whole
# token. Lookarounds (not \b) so punctuated aliases like "u.s." still match. This
# is deliberately permissive: a false match just means we don't skip an article.
def _actor_pattern(canon):
    forms = {canon.lower().replace("_", " "), canon.lower()} | {f.lower() for f in ALIASES.get(canon, [])}
    alts = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
    return re.compile(r"(?<![a-z0-9])(?:" + alts + r")(?![a-z0-9])", re.I)

_ACTOR_PATTERNS = {a: _actor_pattern(a) for a in ACTORS}


def actors_mentioned(text, actors=None):
    """Set of canonical actors whose alias appears as a whole token in `text`.
    Restrict the search to `actors` if given. Coarse — for filtering, not resolution."""
    text = text or ""
    pool = actors or ACTORS
    return {a for a in pool if _ACTOR_PATTERNS[a].search(text)}


if __name__ == "__main__":
    for t in ["Islamabad", "Sharif", "AI", "Anas Al Sharif", "Washington",
              "Tehran", "India", "Dawn", "the United States", "Xi Jinping", "IAEA"]:
        print(f"{t!r:24} -> {resolve_actor(t)}")