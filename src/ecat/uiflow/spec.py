"""The spec is the join key between the two sides.

Cross-platform matching by name or text cannot work: the Android side is discovered as
`GardenActivity[$hash] "My garden…"` and the port side reports `Garden.ets`; the copy on
the two sides is different by definition (and a copy difference is a defect to be scored
by T3, not an identity). So identity is declared once, in a spec file:

    {"schema_version": 1,
     "screens": [{"screen": "garden",
                  "android": {"structure": "c6ca5b87b6", "activity": ".activities.GardenActivity"},
                  "port": {"screen": "Garden.ets"}}]}

* Android side is matched on the **structure hash** (content-free layout fingerprint that
  DroidBot already computes), optionally narrowed by activity.
* Port side is matched on the **screen name** the flow spec / T2 report uses.
* `screen` is the identity both sides are counted by; no fuzzy matching anywhere.

`spec_from_graph` generates the skeleton from a discovery graph, so a human only fills in
the port screen names once.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ecat.uiflow.models import Page, PageGraph, short

SCHEMA_VERSION = 1


@dataclass
@dataclass
class StateSpec:
    """A view *inside* a page: a tab, a filter, a panel.

    Android side is matched by visible text (the only per-state evidence discovery gives us);
    the port side is matched by the `state` a flow step declares once it lands there, so the
    evidence is always something a flow actually observed.
    """

    id: str
    android_text: str = ""
    port_state: str = ""
    note: str = ""

    @property
    def port_key(self) -> str:
        return (self.port_state or self.id).strip().lower()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "android": {"text": self.android_text},
            "port": {"state": self.port_state or self.id},
            **({"note": self.note} if self.note else {}),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StateSpec:
        android = d.get("android") or {}
        port = d.get("port") or {}
        return cls(
            id=str(d.get("id") or ""),
            android_text=str(android.get("text") or ""),
            port_state=str(port.get("state") or ""),
            note=str(d.get("note") or ""),
        )


@dataclass
class ScreenSpec:
    screen: str
    android_structure: str = ""
    android_activity: str = ""
    android_text: str = ""  # optional: exact substring of the page label, declares a split
    port_screen: str = ""
    note: str = ""
    #: True when the port screen was proposed by a heuristic, not chosen by a human
    auto: bool = False
    #: optional views inside this page (tab / filter / panel)
    states: list[StateSpec] = field(default_factory=list)

    @property
    def android_key(self) -> str:
        return (self.android_structure or "").strip().lower()

    @property
    def port_key(self) -> str:
        return (self.port_screen or "").strip().lower()

    def to_dict(self) -> dict[str, Any]:
        return {
            "screen": self.screen,
            "android": {
                "structure": self.android_structure,
                "activity": self.android_activity,
                **({"text": self.android_text} if self.android_text else {}),
            },
            "port": {"screen": self.port_screen},
            **({"note": self.note} if self.note else {}),
            **({"auto": True} if self.auto else {}),
            **({"states": [s.to_dict() for s in self.states]} if self.states else {}),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ScreenSpec:
        android = d.get("android") or {}
        port = d.get("port") or {}
        return cls(
            screen=str(d.get("screen") or ""),
            android_structure=str(android.get("structure") or ""),
            android_activity=str(android.get("activity") or ""),
            android_text=str(android.get("text") or ""),
            port_screen=str(port.get("screen") or ""),
            note=str(d.get("note") or ""),
            auto=bool(d.get("auto", False)),
            states=[StateSpec.from_dict(s) for s in (d.get("states") or []) if isinstance(s, dict)],
        )


@dataclass
class UiflowSpec:
    screens: list[ScreenSpec] = field(default_factory=list)
    android_target: str = ""
    port_target: str = ""
    generated: str = ""
    #: APK this spec was generated from — a stale spec is worse than no spec
    android_sha256: str = ""

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated": self.generated or time.strftime("%Y-%m-%d %H:%M:%S"),
            "android_target": self.android_target,
            "port_target": self.port_target,
            "android_sha256": self.android_sha256,
            "screens": [s.to_dict() for s in self.screens],
            "unfilled_port_screens": sum(1 for s in self.screens if not s.port_key),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path | str) -> UiflowSpec:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload.get("screens"), list):
            raise ValueError(f"{path}: spec has no `screens` array")
        version = int(payload.get("schema_version", SCHEMA_VERSION))
        if version > SCHEMA_VERSION:
            raise ValueError(f"{path}: unsupported spec schema_version={version}")
        return cls(
            screens=[ScreenSpec.from_dict(s) for s in payload["screens"]],
            android_target=str(payload.get("android_target") or ""),
            port_target=str(payload.get("port_target") or ""),
            android_sha256=str(payload.get("android_sha256") or ""),
            generated=str(payload.get("generated") or ""),
        )

    def android_screens(self) -> list[ScreenSpec]:
        return [s for s in self.screens if s.android_key]

    def missing_port_screens(self) -> list[ScreenSpec]:
        return [s for s in self.screens if not s.port_key]

    def state_count(self) -> int:
        return sum(len(s.states) for s in self.screens)

    def unconfirmed(self) -> list[ScreenSpec]:
        """Rows whose port screen was guessed — the pairing has not been looked at yet."""
        return [s for s in self.screens if s.auto]


#: words that carry no identity: "GardenActivity" and "Garden" are the same screen
NAME_NOISE = {"activity", "screen", "page", "fragment", "ets", "view", "compose", "main"}


def _tokens(text: str) -> set[str]:
    import re as _re

    spaced = _re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {
        t for t in _re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t and t not in NAME_NOISE
    }


def score_candidate(label: str, page_stem: str) -> float:
    """How plausible is it that `label` (an Android page) is `page_stem` (a port page)?

    Deliberately dumb and explainable: names, not pixels. It only proposes; a human confirms.
    """
    lab, stem = _tokens(label), _tokens(page_stem)
    if not lab or not stem:
        return 0.0
    if lab == stem:
        return 1.0
    if lab <= stem or stem <= lab:
        return 0.8  # one name contains the other ("Garden" vs "GardenList")
    shared = lab & stem
    if shared:
        # partial overlap is weak evidence, never a confident pairing
        return round(0.5 * len(shared) / len(lab | stem), 2)
    label_l, stem_l = label.strip().lower(), page_stem.strip().lower()
    if stem_l.startswith(label_l) or label_l.startswith(stem_l):
        return 0.5
    return 0.0


def label_text(label: str) -> str:
    """`GardenActivity[$ab12] "Add plant/My garden"` -> `GardenActivity Add plant My garden`.

    The quoted part is the only per-screen evidence discovery gives us, so it must not be thrown
    away before scoring — otherwise two activities with different content look identical.
    """
    import re as _re

    cleaned = _re.sub(r"\[\$[0-9a-fA-F]+\]", " ", label)
    return cleaned.replace('"', " ").replace("/", " ")


def propose_port_screens(
    pages: list[Page], port_pages: list[str], *, taken: set[str] | None = None, floor: float = 0.4
) -> list[tuple[str, float]]:
    """Ranked candidates for one Android page, best first, ignoring already-used port pages."""
    taken = taken or set()
    labels = {label_text(p.label).strip() for p in pages}
    scored: list[tuple[str, float]] = []
    for stem in port_pages:
        if stem in taken:
            continue
        best = max((score_candidate(lab, stem) for lab in labels if lab), default=0.0)
        if best >= floor:
            scored.append((stem, round(best, 2)))
    return sorted(scored, key=lambda c: (-c[1], c[0]))


def spec_from_graph(
    graph: PageGraph, *, name_prefix: str = "", port_pages: list[str] | None = None
) -> UiflowSpec:
    """Skeleton spec from a discovery graph: one screen per discovered page."""
    grouped: dict[str, list[Page]] = {}
    for page in graph.countable():
        structure = str((page.extra or {}).get("structure") or "")
        grouped.setdefault(structure.lower(), []).append(page)

    spec = UiflowSpec(
        android_target=graph.target,
        android_sha256=str((graph.stats or {}).get("app_sha256") or ""),
    )
    taken: set[str] = set()
    for structure, pages in grouped.items():
        first = pages[0]
        activity = str((first.extra or {}).get("activity") or first.component.rsplit("/", 1)[-1])
        label = first.label.split("[", 1)[0] if "[" in first.label else first.label
        screen = f"{name_prefix}{label or short(activity)}@{structure[:6] or 'nostruct'}"
        notes: list[str] = []
        port_screen, auto = "", False

        if port_pages:
            # one port page may only be claimed once; leftovers stay empty for a human
            ranked = propose_port_screens(pages, port_pages, taken=taken)
            if ranked:
                port_screen, score = ranked[0]
                auto = True
                taken.add(port_screen)
                others = ", ".join(f"{c}({s:.2f})" for c, s in ranked[1:4])
                notes.append(
                    f"AUTO 候选 {port_screen} (score {score:.2f})" + (f"；其它候选：{others}" if others else "")
                )

        if len(pages) > 1:
            # Same content-free layout can host two different screens (e.g. "garden" and
            # "plant list"). Show what got merged; split them by adding `android.text`.
            merged = ", ".join(p.label.split("[", 1)[0].strip() for p in pages[:4])
            notes.append(f"{len(pages)} states share this layout ({merged}…) — add android.text to split")

        spec.screens.append(
            ScreenSpec(
                screen=screen,
                android_structure=structure,
                android_activity=activity,
                port_screen=port_screen,
                note="；".join(notes),
                auto=auto,
            )
        )
    return spec


# ── spec-mode diff ────────────────────────────────────────────────────────────
@dataclass
class SpecRow:
    screen: str
    android_reached: bool
    port_present: bool
    android_page: str = ""
    port_page: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "screen": self.screen,
            "android_reached": self.android_reached,
            "port_present": self.port_present,
            "android_page": self.android_page,
            "port_page": self.port_page,
            **({"note": self.note} if self.note else {}),
        }


@dataclass
@dataclass
class StateRow:
    screen: str
    state: str
    android_seen: bool
    port_seen: bool

    def to_dict(self) -> dict[str, Any]:
        return {"screen": self.screen, "state": self.state,
                "android_seen": self.android_seen, "port_seen": self.port_seen}


@dataclass
class SpecDiffResult:
    rows: list[SpecRow] = field(default_factory=list)
    unlisted_android: list[str] = field(default_factory=list)
    unlisted_port: list[str] = field(default_factory=list)
    android_pages: int = 0
    port_pages: int = 0
    warnings: list[str] = field(default_factory=list)
    #: False when the numbers must not be quoted (empty side, no structural match)
    valid: bool = True
    invalid_reason: str = ""
    #: state-level rows (tabs / filters / panels inside a page)
    state_rows: list[StateRow] = field(default_factory=list)

    @property
    def android_reached(self) -> int:
        return sum(1 for r in self.rows if r.android_reached)

    @property
    def missing(self) -> list[SpecRow]:
        return [r for r in self.rows if r.android_reached and not r.port_present]

    @property
    def present(self) -> list[SpecRow]:
        return [r for r in self.rows if r.port_present]

    @property
    def fn_rate(self) -> float:
        if not self.valid:
            return 0.0
        reached = self.android_reached
        return round(len(self.missing) / reached, 4) if reached else 0.0

    @property
    def states_total(self) -> int:
        return len(self.state_rows)

    @property
    def states_missing(self) -> list[StateRow]:
        return [r for r in self.state_rows if r.android_seen and not r.port_seen]

    @property
    def state_fn_rate(self) -> float | None:
        """State-level FN; None when the spec declares no states at all."""
        if not self.state_rows:
            return None
        seen = sum(1 for r in self.state_rows if r.android_seen)
        return round(len(self.states_missing) / seen, 4) if seen else 0.0

    @property
    def spec_coverage(self) -> float:
        """How much of the Android page set the spec actually claims."""
        return round(self.android_reached / self.android_pages, 4) if self.android_pages else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": "spec",
            "android_pages": self.android_pages,
            "port_pages": self.port_pages,
            "spec_screens": len(self.rows),
            "android_reached": self.android_reached,
            "port_present": len(self.present),
            "missing": len(self.missing),
            "fn_rate": self.fn_rate,
            "spec_coverage": self.spec_coverage,
            "unlisted_android": self.unlisted_android,
            "unlisted_port": self.unlisted_port,
            "valid": self.valid,
            "invalid_reason": self.invalid_reason,
            "states_total": self.states_total,
            "states_missing": len(self.states_missing),
            "state_fn_rate": self.state_fn_rate,
            "state_rows": [r.to_dict() for r in self.state_rows],
            "warnings": self.warnings,
            "rows": [r.to_dict() for r in self.rows],
        }

    def to_markdown(self) -> str:
        lines = [
            "# UI flow parity — spec 模式（逐屏两列）",
            "",
            f"- spec 屏数 **{len(self.rows)}** · Android 到达 **{self.android_reached}/{self.android_pages}** "
            f"（spec 覆盖 {self.spec_coverage:.1%}）· 端口有 **{len(self.present)}/{self.port_pages}**",
            (
                f"- **FN 率 = {len(self.missing)}/{self.android_reached} = {self.fn_rate:.1%}**"
                "（分母只数 spec 里 Android 真到了的屏）"
                if self.valid
                else f"- **这个数字不可用**：{self.invalid_reason}"
            ),
            f"- spec 未收录的 Android 页 **{len(self.unlisted_android)}** · 未收录的端口屏 "
            f"**{len(self.unlisted_port)}**",
        ]
        if self.warnings:
            lines += ["", "## 警告", *[f"- {w}" for w in self.warnings]]
        lines += [
            "",
            "| screen | android | port | 说明 |",
            "|---|---|---|---|",
        ]
        for row in self.rows:
            a = "✓" if row.android_reached else "—"
            p = "✓" if row.port_present else "✗"
            lines.append(f"| `{row.screen}` | {a} | {p} | {row.note or row.android_page} |")
        if self.unlisted_android:
            lines += ["", "## spec 未收录的 Android 页（覆盖缺口，不是 FN）", ""]
            lines += [f"- `{p}`" for p in self.unlisted_android]
        if self.unlisted_port:
            lines += ["", "## spec 未收录的端口屏", ""]
            lines += [f"- `{p}`" for p in self.unlisted_port]
        return "\n".join(lines) + "\n"

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        path.with_suffix(".md").write_text(self.to_markdown(), encoding="utf-8")
        return path


def diff_against_spec(base: PageGraph, port: PageGraph, spec: UiflowSpec) -> SpecDiffResult:
    """Per-screen two-column comparison; no name or text matching anywhere."""
    if base.method != "click":
        raise ValueError(
            f"spec diff needs a discovery/walk graph as the Android side (method=click), got {base.method!r}"
        )
    base_pages = base.countable()
    port_pages = port.countable()

    # "a page" = one content-free layout. Data variants (the same screen showing another
    # item) are states of that page, not extra pages: otherwise adding a row to a list
    # would raise the denominator and lower FN for no reason.
    by_structure: dict[str, list[Page]] = {}
    for page in base_pages:
        key = str((page.extra or {}).get("structure") or "").strip().lower() or page.key
        by_structure.setdefault(key, []).append(page)
    def screen_key(value: str) -> str:
        """`Garden.ets`, `pages/Garden` and `garden` are the same screen."""
        key = value.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
        return key[: -len(".ets")] if key.endswith(".ets") else key

    by_screen: dict[str, list[Page]] = {}
    for page in port_pages:
        for candidate in {screen_key(page.label), screen_key(page.key.split(":", 1)[-1])}:
            if candidate:
                by_screen.setdefault(candidate, []).append(page)

    used_android: set[str] = set()
    used_port: set[str] = set()
    result = SpecDiffResult(android_pages=len(by_structure), port_pages=len(port_pages))

    for screen in spec.screens:
        wanted_text = (screen.android_text or "").strip().lower()
        candidates = by_structure.get(screen.android_key, [])
        a_page = next((p for p in candidates if not wanted_text or wanted_text in p.label.lower()), None)
        if a_page is not None and screen.android_key in used_android and not wanted_text:
            a_page = None  # one spec row per layout unless android.text splits it
        p_page = None
        if screen.port_key:
            p_page = next(
                (p for p in by_screen.get(screen_key(screen.port_screen), []) if p.key not in used_port),
                None,
            )
        if a_page is not None:
            used_android.add(screen.android_key)
        if p_page is not None:
            used_port.add(p_page.key)
        # ── state level: tabs / filters / panels inside this page ──────────────
        if screen.states:
            port_states = {
                str(s).strip().lower() for s in ((p_page.extra or {}).get("states") or [])
            } if p_page is not None else set()
            for state in screen.states:
                want = _tokens(state.android_text)
                if not want:
                    continue  # no Android evidence declared: nothing to prove
                android_seen = any(
                    want <= _tokens(label_text(page.label)) for page in candidates
                )
                result.state_rows.append(
                    StateRow(
                        screen=screen.screen,
                        state=state.id,
                        android_seen=android_seen,
                        port_seen=state.port_key in port_states,
                    )
                )

        result.rows.append(
            SpecRow(
                screen=screen.screen,
                android_reached=a_page is not None,
                port_present=p_page is not None,
                android_page=a_page.label if a_page else "",
                port_page=p_page.label if p_page else "",
                note=screen.note,
            )
        )

    result.unlisted_android = [
        f"{pages[0].label} ×{len(pages)}" if len(pages) > 1 else pages[0].label
        for structure, pages in by_structure.items()
        if structure not in used_android
    ]
    result.unlisted_port = [p.label for p in port_pages if p.key not in used_port]

    if spec.missing_port_screens():
        result.warnings.append(
            f"{len(spec.missing_port_screens())} spec screen(s) have no port screen declared — "
            "they always count as missing; fill `port.screen` to make the number real"
        )
    if result.unlisted_android:
        result.warnings.append(
            f"{len(result.unlisted_android)} Android page(s) are not in the spec: FN is bounded by "
            "spec coverage, regenerate the spec skeleton after discovery changes"
        )
    base_sha = str((base.stats or {}).get("app_sha256") or "")
    if spec.android_sha256 and base_sha and spec.android_sha256 != base_sha:
        result.warnings.append(
            f"spec was generated from a different build ({spec.android_sha256[:12]}… ≠ {base_sha[:12]}…): "
            "structures may have moved — regenerate with `spec-from-discovery`"
        )
    if result.android_reached == 0 and not result.rows:
        result.valid = False
        result.invalid_reason = "the spec lists no screen with an android.structure"
    elif result.android_reached == 0:
        result.valid = False
        result.invalid_reason = (
            "no spec screen matched an Android page — stale spec, or the graph came from another build"
        )
    elif result.port_pages == 0:
        result.valid = False
        result.invalid_reason = "C has no pages — the T2 report is empty or was not imported"

    if result.android_reached == 0:
        result.warnings.append(
            "no spec screen matched an Android page — check `android.structure` against the graph "
            "(stale spec or a different build)"
        )
    return result
