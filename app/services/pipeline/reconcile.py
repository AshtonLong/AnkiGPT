"""Phase 4 — global reconciliation.

Workers see one unit at a time, so two units that both define "activation energy"
produce two cards. The old exact-string dedupe missed everything but verbatim repeats.
Here every surviving card is embedded, near-duplicates are clustered by cosine
similarity, and a model decides per cluster which to keep (or merges them). Then a
coverage audit walks the map unit by unit asking what testable facts have no card, and
the orchestrator spawns one bounded round of gap-filler tasks.
"""

import logging
import re

import numpy as np

from ..llm import extract_json, json_schema_format
from .critic import card_answer, card_prompt

logger = logging.getLogger(__name__)

RECONCILE_PROMPT_VERSION = "reconcile-v1"
COVERAGE_PROMPT_VERSION = "coverage-v1"
MAX_CLUSTERS_PER_CALL = 15
COVERAGE_SOURCE_CAP = 20000


def card_text_for_embedding(card):
    """Question + answer, with cloze markers stripped, so two phrasings of one fact land close."""
    text = f"{card_prompt(card)} — {card_answer(card)}"
    return re.sub(r"\s+", " ", text).strip()[:1500]


def normalized_key(card):
    return re.sub(r"[^a-z0-9]+", " ", card_text_for_embedding(card).lower()).strip()


def exact_duplicate_indices(cards):
    """Cheap fallback / pre-pass: indices of cards that repeat an earlier card verbatim."""
    seen = {}
    dupes = []
    for i, c in enumerate(cards):
        key = normalized_key(c)
        if key in seen:
            dupes.append(i)
        else:
            seen[key] = i
    return dupes


def cluster_by_similarity(vectors, threshold=0.9):
    """Union-find over pairs with cosine >= threshold. Returns clusters of size >= 2."""
    if not vectors:
        return []
    mat = np.asarray(vectors, dtype=np.float32)
    if mat.ndim != 2 or mat.shape[0] < 2:
        return []
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms
    sims = mat @ mat.T
    n = mat.shape[0]
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    ii, jj = np.where(np.triu(sims, k=1) >= threshold)
    for i, j in zip(ii.tolist(), jj.tolist()):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    clusters = [sorted(g) for g in groups.values() if len(g) > 1]
    clusters.sort(key=lambda g: g[0])
    return clusters


MERGE_SCHEMA = json_schema_format(
    "duplicate_resolution",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "clusters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "cluster": {"type": "integer"},
                        "keep": {"type": "array", "items": {"type": "integer"}},
                        "reason": {"type": "string"},
                    },
                    "required": ["cluster", "keep", "reason"],
                },
            }
        },
        "required": ["clusters"],
    },
)

MERGE_SYSTEM = """You resolve near-duplicate flashcards. Each cluster contains cards an embedding model found similar. For each cluster decide which card indexes to KEEP:
- If the cards test the same fact, keep exactly one: the clearest, most precise, best-formed card (prefer the one with the tighter answer and the better source grounding).
- If they test genuinely different facts (e.g. a definition vs. its exception, or the two directions of a relationship), keep all that differ.
- Never keep zero cards from a cluster.
Return only JSON."""


def merge_messages(clusters, cards):
    blocks = []
    for ci, cluster in enumerate(clusters):
        lines = [f"Cluster {ci}:"]
        for idx in cluster:
            c = cards[idx]
            if c["type"] == "basic":
                lines.append(f"  [{idx}] basic · front: {c.get('front')} · back: {c.get('back')}")
            else:
                lines.append(f"  [{idx}] cloze · {c.get('cloze_text')}")
        blocks.append("\n".join(lines))
    return [
        {"role": "system", "content": MERGE_SYSTEM},
        {"role": "user", "content": "\n\n".join(blocks)},
    ]


def resolve_clusters(client, clusters, cards):
    """Pure: returns {"drop": set(indices), "usage": {...}, "decisions": [...]}."""
    drop = set()
    decisions = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
    for start in range(0, len(clusters), MAX_CLUSTERS_PER_CALL):
        batch = clusters[start : start + MAX_CLUSTERS_PER_CALL]
        result = client.chat("reconcile", merge_messages(batch, cards), response_format=MERGE_SCHEMA, max_tokens=6000)
        usage["prompt_tokens"] += result.input_tokens
        usage["completion_tokens"] += result.output_tokens
        usage["cost"] += result.cost
        try:
            data = extract_json(result.content)
        except Exception as exc:
            logger.warning("Duplicate resolution returned bad JSON: %s", exc)
            data = {}
        answered = {}
        for item in data.get("clusters") or []:
            try:
                answered[int(item.get("cluster"))] = item
            except (TypeError, ValueError):
                continue
        for local_i, cluster in enumerate(batch):
            item = answered.get(local_i)
            keep = set()
            if item:
                keep = {int(k) for k in item.get("keep") or [] if isinstance(k, int) and int(k) in cluster}
            if not keep:
                keep = {cluster[0]}  # model gave nothing usable: keep the first, drop the rest
            for idx in cluster:
                if idx not in keep:
                    drop.add(idx)
            decisions.append({"cluster": cluster, "keep": sorted(keep), "reason": (item or {}).get("reason", "")})
    return {"drop": drop, "usage": usage, "decisions": decisions}


COVERAGE_SCHEMA = json_schema_format(
    "coverage_audit",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "coverage_score": {"type": "integer"},
            "missing": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "fact": {"type": "string"},
                        "importance": {"type": "integer"},
                        "source_quote": {"type": "string"},
                    },
                    "required": ["fact", "importance", "source_quote"],
                },
            },
        },
        "required": ["coverage_score", "missing"],
    },
)

COVERAGE_SYSTEM = """You audit flashcard coverage. You get one unit of source text and the prompts of every card written from it. List the exam-relevant, testable facts in the source that NO card covers.

- Be strict about "testable": definitions, formulas, mechanisms, distinctions, conditions, numbers the source emphasises. Not filler or asides.
- importance 1-3: 3 = an examiner would very likely ask it; 1 = nice to have.
- Quote a short verbatim span for each missing fact.
- Do not list facts that an existing card already tests, even if phrased differently.
- coverage_score 0-100: how completely the cards cover the unit's testable content.
Return only JSON."""


def coverage_messages(unit, card_prompts):
    source = unit.text if len(unit.text) <= COVERAGE_SOURCE_CAP else unit.text[:COVERAGE_SOURCE_CAP] + "\n...[truncated]"
    prompts = "\n".join(f"- {p}" for p in card_prompts) or "(no cards)"
    return [
        {"role": "system", "content": COVERAGE_SYSTEM},
        {"role": "user", "content": f"UNIT: {unit.title}\n\nSOURCE:\n{source}\n\nEXISTING CARD PROMPTS:\n{prompts}"},
    ]


def audit_unit(client, unit, card_prompts):
    """Pure: returns {"score": int, "missing": [...], "usage": {...}}."""
    result = client.chat("reconcile", coverage_messages(unit, card_prompts), response_format=COVERAGE_SCHEMA, max_tokens=5000)
    try:
        data = extract_json(result.content)
    except Exception:
        data = {"coverage_score": None, "missing": []}
    missing = []
    for item in data.get("missing") or []:
        fact = (item.get("fact") or "").strip()
        if not fact:
            continue
        try:
            importance = max(1, min(3, int(item.get("importance") or 1)))
        except (TypeError, ValueError):
            importance = 1
        missing.append({"fact": fact[:400], "importance": importance, "source_quote": (item.get("source_quote") or "")[:300]})
    score = data.get("coverage_score")
    try:
        score = max(0, min(100, int(score))) if score is not None else None
    except (TypeError, ValueError):
        score = None
    return {"score": score, "missing": missing, "usage": dict(result.usage), "model": result.model}
