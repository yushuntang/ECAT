"""Layer C input — the port side, taken from the existing functional gate.

ECAT already owns flow execution on the port target (the `ui_flows` runner drives the
OpenHarmony device and the functional gate scores screen pairs). This module does not
add a second flow format: it reads a `gate.json` report and turns its screens into the
same `PageGraph` the diff consumes.

    gate.json  {"rubric": {...}, "summary": {...},
                "screens": [{"screen": "Home", "verdict": "pass", "score": 0.93, ...}]}

Every screen in the report is a screen the port renders — the gate had a port
screenshot to score. The gate verdict (pass / suspect / fail) is kept in `extra`, so
the diff can count pages regardless of visual fidelity, and a report can still point at
the ones that exist but look wrong.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ecat.uiflow.models import Page, PageGraph


class GateReportError(ValueError):
    """The file is not a functional-gate report."""


def load_gate_report(path: Path | str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "screens" not in payload:
        raise GateReportError(
            f"{path} has no `screens` array — expected a functional-gate report "
            '({"screens": [{"screen": ..., "verdict": ...}], ...})'
        )
    return payload


def graph_from_gate_report(
    payload: dict[str, Any],
    *,
    side: str = "oh",
    bundle: str = "",
    only_pass: bool = False,
    device: str = "",
) -> PageGraph:
    """gate.json -> PageGraph(side=port). Screens are the port's page set."""
    screens = payload.get("screens") or []
    graph = PageGraph(
        side=side,
        method="gate",  # scored from screen pairs, not from a click walk
        target=bundle or f"gate report ({len(screens)} screens)",
        device=device or str(payload.get("device") or ""),
        generated=str(payload.get("generated") or ""),
        engine="functional-gate",
    )

    for entry in screens:
        name = str(entry.get("screen") or "").strip()
        if not name:
            continue
        verdict = str(entry.get("verdict") or "unknown").lower()
        if only_pass and verdict != "pass":
            continue
        missing = entry.get("missing") or []
        page = Page(
            key=f"gate:{name}",
            label=name,
            component=f"{bundle or 'port'}#{name}",
            verdict="reached",  # the port renders this screen; fidelity is `extra`
            side=side,
            extra={
                "gate_verdict": verdict,
                "score": entry.get("score"),
                "missing": missing[:10] if isinstance(missing, list) else missing,
                "reason": entry.get("reason"),
                "evidence": entry.get("evidence"),
            },
        )
        graph.add_page(page)
    return graph


def gate_summary(payload: dict[str, Any]) -> str:
    s = payload.get("summary") or {}
    return (
        f"gate: {s.get('screens', '?')} screen(s) — pass {s.get('pass', '?')}, "
        f"suspect {s.get('suspect', '?')}, fail {s.get('fail', '?')}"
    )
