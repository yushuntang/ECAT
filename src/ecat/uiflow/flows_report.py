"""Layer C input — the port side, from the T2 functional gate (ui_flows).

`docs/ecat-validation-layers.en.md` splits validation into two side channels:

    T2 functional gate  .ecat/ui_flows.json + tools/ui_runner.py
                        control-tree assertions on the port device -> behaviour
    T3 visual gate      tools/visual_gate.py -> screenshot scoring

This module reads the **T2** report only:

    {"runner": "...", "device": "...", "bundle": "...",
     "flows": [{"id": "...", "status": "pass|fail|error|skip", "screens": ["Index.ets"], ...}],
     "summary": {...}}

`flows[].screens` is what the flow declares it exercises, so a passing flow is direct
evidence that the port reaches that screen. A failing flow is split by *where* it failed:
if the failing step is an assertion (`expect_visible` / `expect_absent` /
`expect_page_changed`), the navigation already happened and only the assertion failed —
the screen was reached, so it counts as a page and carries `assert_failed` in `extra`.
Any other failure never got to the screen.

Visual fidelity is deliberately out of scope here: it belongs to T3, which is scored
per screenshot pair and must not leak into the page-coverage percentage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ecat.uiflow.models import Edge, Page, PageGraph

#: steps whose failure means "we got there, the screen just did not say what we expected"
ASSERTION_STEPS = frozenset({"expect_visible", "expect_absent", "expect_page_changed"})


class FlowsReportError(ValueError):
    """The file is not a ui_runner (T2) report."""


def load_flows_report(path: Path | str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("flows"), list):
        raise FlowsReportError(
            f"{path} has no `flows` array — expected a T2 report from tools/ui_runner.py "
            '({"flows": [{"id", "status", "screens"}], "summary": {...}})'
        )
    return payload


def _screen_pages(
    payload: dict[str, Any], include_failed_assertions: bool
) -> tuple[dict[str, Page], dict[str, int]]:
    pages: dict[str, Page] = {}
    stats = {"pass": 0, "fail_assert": 0, "fail_nav": 0, "error": 0, "skip": 0,
             "no_screens": 0, "partial": 0, "dependency_skips": 0, "weak_assertions": 0}

    for flow in payload.get("flows") or []:
        status = str(flow.get("status") or "").lower()
        screens = [str(s).strip() for s in (flow.get("screens") or []) if str(s).strip()]
        blocked = status == "skip" and str(flow.get("step_name") or "") == "dependency"
        if blocked:
            # counted even when the flow carries no `screens`: the point is that it never ran
            stats["dependency_skips"] += 1
        if not screens:
            stats["no_screens"] += 1
            continue

        # What the step trace proves was actually on screen, regardless of how the flow
        # ended: a failure at a later tap must not erase a screen already visited.
        for entry in flow.get("trace") or []:
            if isinstance(entry, dict) and entry.get("weak"):
                stats["weak_assertions"] += 1

        fail_step = flow.get("step") if status != "pass" and isinstance(flow.get("step"), int) else None
        proven = observed_markers(flow.get("trace") or [], upto_step=fail_step)

        # Data fingerprint: how much content this screen actually showed. A port with an empty
        # device and an Android side full of rows look identical at page level, and then FN
        # blames the port for missing pages it never had data to render.
        elements_by_screen: dict[str, dict[str, Any]] = {}
        type_hist: dict[str, dict[str, int]] = {}
        for entry in proven:
            types = entry.get("types")
            size = entry.get("screen_size")
            elems = entry.get("elements")
            if isinstance(size, list) and len(size) == 2 and isinstance(elems, list) and elems:
                elements_by_screen[str(entry["screen"]).strip()] = {
                    "size": size,
                    "elements": elems,
                }
            if isinstance(types, dict) and types:
                bucket = type_hist.setdefault(str(entry["screen"]).strip(), {})
                for name, count in types.items():
                    if isinstance(count, int):
                        bucket[str(name)] = max(bucket.get(str(name), 0), count)

        fingerprint: dict[str, dict[str, Any]] = {}
        for entry in proven:
            sig = str(entry.get("sig") or "")
            tokens = [t for t in sig.split("|") if t]
            if tokens:
                fingerprint[str(entry["screen"]).strip()] = {
                    "labels": len(tokens),
                    "sample": sorted(tokens)[:6],
                }

        states_by_screen: dict[str, set[str]] = {}
        for entry in proven:
            if entry.get("state"):
                states_by_screen.setdefault(str(entry["screen"]).strip(), set()).add(
                    str(entry["state"]).strip().lower()
                )

        traced: dict[str, int] = {}
        for entry in proven:
            traced[str(entry["screen"]).strip()] = int(entry.get("step") or 0)

        reached = False
        verdict = "unknown"
        if status == "pass":
            reached, verdict, stats["pass"] = True, "reached", stats["pass"] + 1
        elif status == "fail" and str(flow.get("step_name") or "") in ASSERTION_STEPS:
            # navigation happened; the assertion is what failed
            stats["fail_assert"] += 1
            reached = include_failed_assertions
            verdict = "reached" if reached else "finished"
        elif status == "fail":
            stats["fail_nav"] += 1
            verdict = "finished"
        elif status == "error":
            stats["error"] += 1
        elif status == "skip":
            stats["skip"] += 1

        if not reached and traced:
            stats["partial"] = stats.get("partial", 0) + 1

        # When the flow marks its steps, the markers are authoritative: a screen that was only
        # *declared* is not evidence of arrival (the port may never have opened it, and a passing
        # assertion can be satisfied by the previous screen's own labels).
        has_markers = any(isinstance(e, dict) and e.get("screen") for e in flow.get("trace") or [])
        if has_markers:
            stats["marker_verified"] = stats.get("marker_verified", 0) + 1

        for screen in screens:
            existing = pages.get(screen)
            if existing is not None and existing.verdict == "reached":
                if states_by_screen.get(screen):
                    merged = set(existing.extra.get("states") or []) | states_by_screen[screen]
                    existing.extra["states"] = sorted(merged)
                continue  # a later failing flow must not demote a screen another flow proved
            screen_reached = screen in traced if has_markers else reached or screen in traced
            page = Page(
                key=f"flow:{screen}",
                label=screen,
                component=f"{payload.get('bundle') or 'port'}#{screen}",
                # a declared-but-unproven screen is neither reached nor "finished": it is a claim
                verdict="reached" if screen_reached else ("declared" if has_markers else verdict),
                side="oh",
                extra={
                    "flow": flow.get("id"),
                    "flow_status": status,
                    "step_name": flow.get("step_name"),
                    "message": flow.get("message") or "",
                    "assert_failed": status == "fail",
                    "reached_via_trace": bool(screen_reached and not reached),
                    **({"states": sorted(states_by_screen[screen])} if states_by_screen.get(screen) else {}),
                    **({"data": fingerprint[screen]} if fingerprint.get(screen) else {}),
                    **({"types": type_hist[screen]} if type_hist.get(screen) else {}),
                    **({"geometry": elements_by_screen[screen]} if elements_by_screen.get(screen) else {}),
                },
            )
            pages[screen] = page
    return pages, stats


def observed_markers(
    trace: list[dict[str, Any]], *, upto_step: int | None = None
) -> list[dict[str, Any]]:
    """Trace entries whose `screen` claim is backed by an observed page change.

    A flow step says which screen it *lands on*; the runner also records the screen signature.
    When the signature did not move, the claim is just a claim — the navigation silently did
    nothing (this is exactly what a dead route looks like). Crediting those markers made a
    broken port read as "all pages present", so only markers with a changed signature count.
    """
    markers: list[dict[str, Any]] = []
    prev: str | None = None
    for entry in trace or []:
        if not isinstance(entry, dict) or not entry.get("screen"):
            continue
        if entry.get("weak"):
            # the runner found this assertion already true on the previous screen: it does not
            # prove we are anywhere new, so it cannot be evidence of having reached this screen
            continue
        step = int(entry.get("step") or 0)
        if upto_step is not None and step > upto_step:
            continue
        sig = str(entry.get("sig") or "")
        moved = prev is None or not sig or sig != prev
        if moved:
            markers.append(entry)
        prev = sig or prev
    return markers


def graph_from_flows_report(
    payload: dict[str, Any],
    *,
    side: str = "oh",
    bundle: str = "",
    device: str = "",
    include_failed_assertions: bool = True,
) -> PageGraph:
    """T2 report -> PageGraph(side=port). One page per screen the flows declare."""
    pages, stats = _screen_pages(payload, include_failed_assertions)
    graph = PageGraph(
        side=side,
        method="flows",  # asserted on the control tree, not scored from screenshots
        target=bundle or str(payload.get("bundle") or "port"),
        device=device or str(payload.get("device") or ""),
        generated="",
        engine=f"functional-gate:{payload.get('runner') or 'ui_runner'}",
    )
    for page in pages.values():
        graph.add_page(page)
    graph.stats.update(stats)

    graph.stats["edges"] = _add_edges(graph, payload, pages)
    return graph


def _add_edges(graph: PageGraph, payload: dict[str, Any], pages: dict[str, Page]) -> str:
    """Action-level edges from the runner's step trace; screen-order edges as a fallback.

    `ui_runner` records one trace entry per step and, when the flow spec marks a step with
    `"screen": "<name>"`, the screen that step lands on. Consecutive markers give
    `action -> next screen`, i.e. a real edge label. Reports produced before traces
    existed (or flows without `screen` markers) fall back to the declared screen order,
    which says "these two screens are related" but not "by which action".
    """
    mode = "flow-order (coarse)"
    for flow in payload.get("flows") or []:
        status = str(flow.get("status") or "").lower()
        fail_step = flow.get("step")
        trace = [e for e in (flow.get("trace") or []) if isinstance(e, dict)]
        markers = observed_markers(
            trace,
            upto_step=None if status == "pass" or not isinstance(fail_step, int) else fail_step,
        )
        if not markers and status != "pass":
            continue  # nothing proven by a failed flow without markers

        states_by_screen: dict[str, set[str]] = {}
        for entry in trace:
            if entry.get("screen") and entry.get("state"):
                states_by_screen.setdefault(str(entry["screen"]).strip(), set()).add(
                    str(entry["state"]).strip().lower()
                )

        if markers:
            mode = "trace (action-level)"
            current = ""
            for entry in markers:
                screen = str(entry["screen"]).strip()
                if not screen:
                    continue
                if screen not in pages:  # a screen the flow visits but never declares
                    page = Page(
                        key=f"flow:{screen}",
                        label=screen,
                        component=f"{payload.get('bundle') or 'port'}#{screen}",
                        verdict="reached",
                        side="oh",
                        extra={
                            "flow": flow.get("id"),
                            "flow_status": "pass",
                            "from_trace": True,
                            **({"states": sorted(states_by_screen[screen])} if states_by_screen.get(screen) else {}),
                        },
                    )
                    pages[screen] = page
                    graph.add_page(page)
                if current and current != screen:
                    graph.add_edge(
                        Edge(
                            src=f"flow:{current}",
                            dst=f"flow:{screen}",
                            action=str(entry.get("action") or "step"),
                            source="trace",
                        )
                    )
                current = screen
            continue

        screens = [str(s).strip() for s in (flow.get("screens") or []) if str(s).strip()]
        for src, dst in zip(screens, screens[1:], strict=False):
            graph.add_edge(
                Edge(src=f"flow:{src}", dst=f"flow:{dst}",
                     action=f"flow:{flow.get('id')}", source="flow-order")
            )
    return mode


NOISE_TOKENS = (
    "clockstatus", "livemetaba", "livecapsule", "statusbarbridge", "windowscene", "__common__",
)


def _usable(token: str) -> bool:
    low = token.lower()
    return len(low) >= 3 and not any(noise in low for noise in NOISE_TOKENS)


def suggest_assertions(payload: dict[str, Any], *, limit: int = 4) -> dict[str, list[str]]:
    """Labels that appear on one screen and not on the previous one.

    Those are the only assertions that can prove arrival: anything already visible before the tap
    is satisfied by the screen we never left (that is how a green run hid a dead route here).
    """
    out: dict[str, list[str]] = {}
    for flow in payload.get("flows") or []:
        prev: set[str] | None = None
        for entry in flow.get("trace") or []:
            if not isinstance(entry, dict):
                continue
            screen, sig = entry.get("screen"), str(entry.get("sig") or "")
            if not screen or not sig:
                continue
            tokens = {t for t in sig.split("|") if _usable(t)}
            if prev is not None:
                fresh = sorted(tokens - prev)[:limit]
                if fresh:
                    bucket = out.setdefault(str(screen).strip(), [])
                    for token in fresh:
                        if token not in bucket:
                            bucket.append(token)
            prev = tokens
    return out


def flows_summary(payload: dict[str, Any]) -> str:
    s = payload.get("summary") or {}
    return (
        f"flows: {s.get('total', '?')} total — pass {s.get('passed', '?')}, "
        f"fail {s.get('failed', '?')}, error {s.get('errored', '?')}, skip {s.get('skipped', '?')}"
        f" · runner {payload.get('runner') or '?'}"
    )
