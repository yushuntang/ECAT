"""Data model for UI flow coverage: pages, edges, graphs.

Everything here is pure data + stdlib so it can be unit-tested without a device.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

#: clock/counter chrome: "02", "02, :, 03", "100 %" — volatile, never page identity
_NUMERICISH = re.compile(r"^[\s\d,:.%/-]*$")

#: verdicts that mean "the page was actually reached" and may enter the FN denominator
COUNTABLE_VERDICTS = frozenset({"reached", "reached_trampoline"})

VERDICTS = (
    "reached",
    #: a flow listed this screen but never proved it (marker missing, weak, or signature unmoved)
    "declared",
    "reached_trampoline",
    "finished",
    "redirect",
    "blocked_dialog",
    "dialog",
    "empty_frame",
    "crashed_abi",
    "crashed_app",
    "rejected",
    "unknown",
)

#: "amstart" = Layer A (deterministic entry), "click" = discovery/walk of the real
#: entry, "gate" = the port side imported from the functional-gate report
METHODS = ("amstart", "click", "gate")


def norm(text: str) -> str:
    """Normalise a label/component for cross-side matching (case, punctuation)."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def short(component: str) -> str:
    """`com.x/.activities.FooActivity` -> `FooActivity`."""
    return (component or "").rsplit(".", 1)[-1].rsplit("/", 1)[-1]


def fingerprint(root: ET.Element, activity: str) -> tuple[str, str]:
    """(stable hash, human label) for one screen state.

    Built from visible **text/content-desc only** — no resource-ids, no bare counts.
    Resource-ids and badge numbers ("1" unread counts, list positions) change
    between visits of the same screen and would split one page into several
    "different" nodes, inflating every page count downstream.
    """
    parts: list[str] = []
    for node in root.iter("node"):
        for attr in ("text", "content-desc"):
            value = (node.get(attr) or "").strip()
            if not value or _NUMERICISH.match(value):
                continue
            parts.append(value)
    return fingerprint_texts(parts, activity)


def fingerprint_texts(texts: Iterable[str], component: str = "", *, digest_len: int = 10) -> tuple[str, str]:
    """(stable hash, human label) for a screen described only by what a user sees.

    One identity rule for every engine: a live adb/uitest dump and DroidBot's UTG
    both reduce to a set of strings, and the page *is* that set. View structure is
    the wrong key — it ignores text (every detail page of a list app shares one
    structure) and it splits one screen visited twice into two nodes. Labels carry
    the first short texts so two rows of a report are told apart by what they show,
    not by a hash.
    """
    values = [t.strip() for t in texts if (t or "").strip()]
    digest = hashlib.sha1("\n".join(sorted(values)).encode("utf-8")).hexdigest()[:digest_len]
    # label head: the two shortest short texts, ties broken alphabetically. Document
    # order is not stable between two dumps of the same screen (one run lists the tab
    # titles first, the next lists the title bar), so picking "the first two" made the
    # label change while the fingerprint stayed the same.
    head_parts = sorted({v for v in values if len(v) <= 40}, key=lambda v: (len(v), v))[:2]
    head = "/".join(sorted(head_parts))[:48]
    label = f"{short(component) or 'Screen'}[${digest}]" + (f' "{head}"' if head else "")
    return digest, label


@dataclass
class Page:
    """One screen state."""

    key: str
    label: str
    component: str
    verdict: str = "reached"
    detail: str = ""
    side: str = "android"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def countable(self) -> bool:
        return self.verdict in COUNTABLE_VERDICTS

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "component": self.component,
            "verdict": self.verdict,
            "detail": self.detail,
            "side": self.side,
        }
        if self.extra:
            d["extra"] = self.extra
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Page:
        return cls(
            key=d["key"],
            label=d.get("label") or d["key"],
            component=d.get("component", ""),
            verdict=d.get("verdict", "reached"),
            detail=d.get("detail", ""),
            side=d.get("side", "android"),
            extra=d.get("extra") or {},
        )


@dataclass
class Edge:
    """One action that moves between two pages."""

    src: str
    dst: str
    action: str
    source: str = "click"  # click | amstart | back | intent

    def to_dict(self) -> dict[str, Any]:
        return {"src": self.src, "dst": self.dst, "action": self.action, "source": self.source}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Edge:
        return cls(
            src=d["src"],
            dst=d["dst"],
            action=d.get("action", ""),
            source=d.get("source", "click"),
        )


@dataclass
class PageGraph:
    """Pages + edges for one side of the comparison."""

    side: str = "android"
    method: str = "click"  # must be "click" to be an FN denominator
    target: str = ""
    device: str = ""
    generated: str = ""
    #: how the graph was produced: "droidbot", "functional-gate:...", ... — kept for the report
    engine: str = ""
    #: per-source counters that must travel with the graph (flow pass/fail/skip, ...)
    stats: dict[str, Any] = field(default_factory=dict)
    pages: list[Page] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    # ── mutation ──────────────────────────────────────────────────────────────
    def add_page(self, page: Page) -> Page:
        for existing in self.pages:
            if existing.key == page.key:
                return existing
        self.pages.append(page)
        return page

    def add_edge(self, edge: Edge) -> None:
        if any(
            e.src == edge.src and e.dst == edge.dst and e.action == edge.action for e in self.edges
        ):
            return
        self.edges.append(edge)

    # ── queries ───────────────────────────────────────────────────────────────
    def by_key(self, key: str) -> Page | None:
        return next((p for p in self.pages if p.key == key), None)

    def countable(self) -> list[Page]:
        return [p for p in self.pages if p.countable]

    def verdict_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for p in self.pages:
            counts[p.verdict] = counts.get(p.verdict, 0) + 1
        return counts

    def summary(self) -> dict[str, Any]:
        counts = self.verdict_counts()
        return {
            "pages": len(self.pages),
            "countable": len(self.countable()),
            "edges": len(self.edges),
            "by_verdict": counts,
        }

    # ── persistence ───────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "side": self.side,
            "method": self.method,
            "target": self.target,
            "device": self.device,
            "generated": self.generated or time.strftime("%Y-%m-%d %H:%M:%S"),
            "engine": self.engine,
            "stats": self.stats,
            "summary": self.summary(),
            "pages": [p.to_dict() for p in self.pages],
            "edges": [e.to_dict() for e in self.edges],
        }

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PageGraph:
        version = d.get("schema_version", SCHEMA_VERSION)
        if version > SCHEMA_VERSION:
            raise ValueError(f"unsupported graph schema_version={version}")
        return cls(
            side=d.get("side", "android"),
            method=d.get("method", "click"),
            target=d.get("target", ""),
            device=d.get("device", ""),
            generated=d.get("generated", ""),
            engine=d.get("engine", ""),
            stats=d.get("stats") or {},
            pages=[Page.from_dict(p) for p in d.get("pages", [])],
            edges=[Edge.from_dict(e) for e in d.get("edges", [])],
        )

    @classmethod
    def load(cls, path: Path | str) -> PageGraph:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_pages(cls, pages: Iterable[Page], **kw: Any) -> PageGraph:
        g = cls(**kw)
        for p in pages:
            g.add_page(p)
        return g
