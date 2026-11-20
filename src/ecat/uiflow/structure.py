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


#: The two platforms name the same idea differently (android.widget.FrameLayout vs Column), and the
#: Android dump is container-heavy where the ArkUI dump is leaf-heavy. Comparing raw class names
#: produced 0.04 for the *same* dialog on both sides, so types are bucketed first.
TYPE_BUCKETS = (
    ("text", ("textview", "text", "span", "label", "richtext")),
    ("button", ("button", "toggle", "checkbox", "radio", "switch", "chip")),
    ("image", ("imageview", "image", "icon", "symbolglyph", "avatar")),
    ("input", ("edittext", "textinput", "textfield", "searchfield", "input")),
    ("list", ("recyclerview", "listview", "gridview", "list", "grid", "table", "waterflow")),
    ("scroll", ("scrollview", "scroll", "swiper", "pager", "viewpager")),
    ("container", (
        "layout", "viewgroup", "column", "row", "stack", "flex", "relative", "absolute",
        "frame", "constraint", "container", "tabs", "navigation", "toolbar", "drawer",
    )),
)


def canonical_type(name: str) -> str:
    """`android.widget.FrameLayout` and `Column` are both containers; that is the comparable part."""
    low = name.strip().lower().rsplit(".", 1)[-1]
    for bucket, needles in TYPE_BUCKETS:
        if any(needle in low for needle in needles):
            return bucket
    return "other"


def _counts(types: dict[str, Any] | None, *, canonical: bool = True) -> dict[str, float]:
    if not isinstance(types, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in types.items():
        if not isinstance(value, (int, float)) or value <= 0 or not str(key).strip():
            continue
        name = canonical_type(str(key)) if canonical else str(key).strip().lower()
        out[name] = out.get(name, 0.0) + float(value)
    return out


#: dropped before comparing when both sides still have something to say: container counts reflect
#: how many layout nodes each platform's dump happens to emit, not what the screen offers
LAYOUT_BUCKETS = {"container", "scroll"}


def _roles(counts: dict[str, float]) -> dict[str, float]:
    rest = {k: v for k, v in counts.items() if k not in LAYOUT_BUCKETS}
    return rest or counts


def cosine(a: dict[str, Any] | None, b: dict[str, Any] | None) -> float | None:
    """Cosine similarity of two type histograms; None when either side has no data.

    Uses raw counts on purpose: cosine already ignores scale, so pre-normalising and then dividing
    by the magnitudes again inflates every score (3:1 vs 1:3 came out as 0.60 instead of 0.375).
    """
    left, right = _roles(_counts(a)), _roles(_counts(b))
    if not left or not right:
        return None
    keys = set(left) | set(right)
    dot = sum(left.get(k, 0.0) * right.get(k, 0.0) for k in keys)
    norm = math.sqrt(sum(v * v for v in left.values())) * math.sqrt(sum(v * v for v in right.values()))
    return round(dot / norm, 4) if norm else None


def slots(geometry: dict[str, Any] | None, *, grid: int = 3) -> set[str]:
    """`role@rowcol` for each element, e.g. `image@11`. Position is what makes a grid of thumbnails
    different from one big picture — the thing role counts alone could not tell apart."""
    if not isinstance(geometry, dict):
        return set()
    size = geometry.get("size") or []
    elements = geometry.get("elements") or []
    if len(size) != 2 or not elements:
        return set()
    width, height = float(size[0] or 0), float(size[1] or 0)
    if width <= 0 or height <= 0:
        return set()
    out: set[str] = set()
    for element in elements:
        if not isinstance(element, (list, tuple)) or len(element) < 3:
            continue
        role = canonical_type(str(element[0]))
        x, y = float(element[1]), float(element[2])
        row = min(grid - 1, max(0, int(grid * y / height)))
        col = min(grid - 1, max(0, int(grid * x / width)))
        out.add(f"{role}@{row}{col}")
    return out


def jaccard(a: set[str], b: set[str]) -> float | None:
    if not a or not b:
        return None
    return round(len(a & b) / len(a | b), 4)


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
        base_extra = (getattr(base_page, "extra", {}) or {}) if base_page else {}
        port_extra = (getattr(port_page, "extra", {}) or {}) if port_page else {}
        score = jaccard(slots(base_extra.get("geometry")), slots(port_extra.get("geometry")))
        basis = "geometry"
        if score is None:
            score = cosine(base_extra.get("types"), port_extra.get("types"))
            basis = "roles"
        rows.append({
            "screen": row.screen,
            "similarity": score,
            "basis": basis if score is not None else "",
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
