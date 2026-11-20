"""Compare screens by element *types* instead of text.

Names and copy differ across platforms by nature ("Plant list" vs "PlantDrawer"), so the only
cross-platform signal worth trusting is structural: which kinds of components a screen is made of
and in what proportion. This module turns a component-type histogram into a vector and compares two
screens with cosine similarity.

It is deliberately additive: page identity stays what it is (a layout hash on the Android side, a
flow-declared screen on the port side). Structure is used to *check* a pairing and to propose one,
never to silently redefine a page — the last time a matching rule was invented here, a typo in a
spec turned into a missing-page number.
"""

from __future__ import annotations

import math
from typing import Any

#: below this, two screens are probably not the same screen
SUSPICIOUS = 0.55
#: above this, the structural evidence is strong enough to propose a pairing
CONFIDENT = 0.80


def histogram(types: dict[str, Any] | None) -> dict[str, float]:
    """Keep only usable counts, normalised so screen size does not dominate the comparison."""
    if not isinstance(types, dict):
        return {}
    clean = {
        str(k).strip().lower(): float(v)
        for k, v in types.items()
        if isinstance(v, (int, float)) and v > 0 and str(k).strip()
    }
    total = sum(clean.values())
    return {k: v / total for k, v in clean.items()} if total else {}


def _counts(types: dict[str, Any] | None) -> dict[str, float]:
    if not isinstance(types, dict):
        return {}
    return {
        str(k).strip().lower(): float(v)
        for k, v in types.items()
        if isinstance(v, (int, float)) and v > 0 and str(k).strip()
    }


def cosine(a: dict[str, Any] | None, b: dict[str, Any] | None) -> float | None:
    """Cosine similarity of two type histograms; None when either side has no data.

    Uses raw counts on purpose: cosine already ignores scale, so pre-normalising and then dividing
    by the magnitudes again inflates every score (3:1 vs 1:3 came out as 0.60 instead of 0.375).
    """
    left, right = _counts(a), _counts(b)
    if not left or not right:
        return None
    keys = set(left) | set(right)
    dot = sum(left.get(k, 0.0) * right.get(k, 0.0) for k in keys)
    norm = math.sqrt(sum(v * v for v in left.values())) * math.sqrt(sum(v * v for v in right.values()))
    return round(dot / norm, 4) if norm else None


def compare_screens(
    result: Any, base_graph: Any, port_graph: Any, *, suspicious: float = SUSPICIOUS
) -> list[dict[str, Any]]:
    """Per spec row: how structurally alike the two paired screens are."""
    base_by_label = {p.label: p for p in getattr(base_graph, "pages", [])}
    port_by_label = {p.label: p for p in getattr(port_graph, "pages", [])}
    rows: list[dict[str, Any]] = []
    for row in result.rows:
        if not row.port_present:
            continue
        base_page = base_by_label.get(row.android_page)
        port_page = port_by_label.get(row.port_page)
        score = cosine(
            (getattr(base_page, "extra", {}) or {}).get("types") if base_page else None,
            (getattr(port_page, "extra", {}) or {}).get("types") if port_page else None,
        )
        rows.append({
            "screen": row.screen,
            "similarity": score,
            "verdict": (
                "no-structure-data" if score is None
                else "suspicious" if score < suspicious
                else "plausible"
            ),
        })
    return rows


def to_markdown(rows: list[dict[str, Any]]) -> str:
    lines = ["## 结构相似度（元素类型直方图）", "", "| screen | similarity | verdict |", "|---|---|---|"]
    for row in rows:
        score = "—" if row["similarity"] is None else f"{row['similarity']:.2f}"
        lines.append(f"| {row['screen']} | {score} | {row['verdict']} |")
    if any(r["verdict"] == "no-structure-data" for r in rows):
        lines += ["", "`no-structure-data`: 有一侧没有记录元素类型（老报告或 DroidBot 侧未采集），"
                      "这一项不可判断，不要当成 0 分。"]
    return "\n".join(lines) + "\n"
