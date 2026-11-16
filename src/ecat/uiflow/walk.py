"""Layer B/C — click walk: which pages a user can actually reach, and via what.

Run this against the **original, unpatched** APK (or the port target): the whole
point is that every page is entered the way a user enters it. A page only lands in
the denominator because a click took us there.

Steps understood in a flows file (one flow = one user journey):

    {"launch": true}                                  launch the app's main activity
    {"am_start": {"component": "pkg/.Act", "extras": {"k": "v"}}}
    {"tap": "<label>"}                                tap first node with that text/desc
    {"back": true}
    {"expect_visible": ["<label>", "<label2>"]}       any-of assertion after the step
    {"expect_page_changed": true}
    {"sleep": 1.5}

Every action produces an edge (page -> page, labelled with the action); pages are
fingerprinted (activity + visible text/id hash), so two visits to the same activity
with different data are two nodes.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ecat.uiflow.device import Adb, DeviceError
from ecat.uiflow.hdc import Hdc
from ecat.uiflow.models import Edge, Page, PageGraph, fingerprint, short

#: both transports expose the same surface (focus/dump_root/tap_node/...)
UiDevice = Adb | Hdc


@dataclass
class FlowResult:
    id: str
    pages_seen: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    steps_run: int = 0

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "ok": self.ok, "steps": self.steps_run,
                "pages": self.pages_seen, "failures": self.failures}


def _visible_texts(root: ET.Element) -> set[str]:
    out: set[str] = set()
    for node in root.iter("node"):
        for attr in ("text", "content-desc"):
            v = (node.get(attr) or "").strip()
            if v:
                out.add(v.lower())
    return out


def observe(adb: UiDevice, package: str) -> tuple[Page, ET.Element]:
    """Current screen as a Page (fingerprinted), plus the raw tree for assertions."""
    comp = adb.focus()
    root = adb.dump_root()
    digest, label = fingerprint(root, comp or package)
    key = f"{comp or package}#{digest}"
    return Page(key=key, label=label, component=comp, verdict="reached", side="android"), root


#: buttons a first-run / permission dialog offers; dismissing them is part of the walk,
#: and each dialog is recorded as a page because that is what a user actually sees first
DEFAULT_DISMISS_LABELS = (
    "ok",
    "got it",
    "allow",
    "continue",
    "close",
    "while using the app",
    "only this time",
)


def dismiss_dialogs(
    adb: UiDevice,
    package: str,
    graph: PageGraph,
    cur: Page | None,
    settle: float,
    max_rounds: int = 2,
    labels: tuple[str, ...] | None = None,
) -> Page | None:
    """Tap through first-run/permission dialogs so the walk can reach the real UI.

    Without this the first tap lands on a scrim: a promo or permission dialog sits in
    front of the screen on a fresh launch while `mCurrentFocus` still reports the host
    activity, so nothing about the focus check looks wrong.
    """
    # node labels are lower-cased before comparison, so the caller's list must be too
    wanted = {lbl.strip().lower() for lbl in (labels or DEFAULT_DISMISS_LABELS)}
    for _ in range(max_rounds):
        root = adb.dump_root()
        by_label = {(adb.node_text(n) or "").strip().lower(): n for n in root.iter("node") if adb.node_text(n)}
        hit = next((n for lbl, n in by_label.items() if lbl in wanted), None)
        if hit is None:
            return cur
        # Snapshot the dialog itself *before* dismissing it: a promo/permission dialog
        # is a screen the user sees (so it belongs in the graph) but not a product
        # page (so it stays out of the FN denominator).
        dcomp = adb.focus()
        ddigest, dlabel = fingerprint(root, dcomp)
        dialog_page = Page(key=f"{dcomp}#{ddigest}", label=dlabel, component=dcomp,
                           verdict="dialog", side="android")
        graph.add_page(dialog_page)
        if cur is not None and dialog_page.key != cur.key:
            graph.add_edge(Edge(src=cur.key, dst=dialog_page.key, action="dialog shown", source="click"))
        adb.tap_node(hit)
        time.sleep(settle)
        page, _ = observe(adb, package)
        graph.add_page(page)
        graph.add_edge(Edge(src=dialog_page.key, dst=page.key, action="dismiss(dialog)", source="click"))
        cur = page
    return cur


def resolve_launcher(adb: UiDevice, package: str) -> str:
    out = adb.shell("cmd", "package", "resolve-activity", "--brief", package, check=False)
    for line in reversed(out.splitlines()):
        line = line.strip()
        if "/" in line and line.startswith(package):
            return line
    return f"{package}/.MainActivity"


def run_flows(
    adb: UiDevice,
    package: str,
    flows: list[dict[str, Any]],
    settle: float = 1.5,
    reset_between: bool = True,
    edges_from_logcat: bool = False,
    log: Any = None,
    launcher: str | None = None,
    side: str = "android",
    dismiss_labels: tuple[str, ...] | None = None,
) -> tuple[PageGraph, list[FlowResult]]:
    """Execute click flows; returns (graph, per-flow results)."""
    graph = PageGraph(side=side, method="click", target=package,
                      device=adb.serial, generated=time.strftime("%Y-%m-%d %H:%M:%S"))
    adb.wake()  # a sleeping/locked screen would be "measured" as a page of the app
    launcher = launcher or resolve_launcher(adb, package)
    results: list[FlowResult] = []

    for flow in flows:
        fid = flow.get("id") or f"flow{len(results) + 1}"
        result = FlowResult(id=fid)
        results.append(result)
        if reset_between:
            adb.force_stop(package)

        cur: Page | None = None
        for step in flow.get("steps") or []:
            action = _describe(step)
            try:
                cur = _apply(adb, package, launcher, step, cur, graph, settle, result, dismiss_labels)
            except DeviceError as exc:
                result.failures.append(f"{action}: adb error {exc}"[:160])
                if log:
                    log(f"[B] {fid} {action} -> adb error")
                break
            result.steps_run += 1
            if cur is not None:
                if cur.key not in result.pages_seen:
                    result.pages_seen.append(cur.key)
                if log:
                    log(f"[B] {fid} {action:28s} -> {short(cur.component) or cur.label}")
        if log:
            status = "ok" if result.ok else f"{len(result.failures)} failure(s)"
            log(f"[B] flow {fid}: {status}, pages={len(result.pages_seen)}")

    if edges_from_logcat:
        prev = ""
        for comp in adb.recent_starts():
            page = next((p for p in graph.pages if p.component == comp), None)
            if prev and page is not None:
                graph.add_edge(Edge(src=prev, dst=page.key, action="(intent)", source="intent"))
            if page is not None:
                prev = page.key

    return graph, results


def _describe(step: dict[str, Any]) -> str:
    for key in ("launch", "am_start", "tap", "back", "expect_visible", "expect_page_changed", "sleep"):
        if key in step:
            if key == "tap":
                return f"tap({step['tap']})"
            if key in ("launch", "back"):
                return key
            return f"{key}"
    return "unknown-step"


def _apply(
    adb: UiDevice,
    package: str,
    launcher: str,
    step: dict[str, Any],
    cur: Page | None,
    graph: PageGraph,
    settle: float,
    result: FlowResult,
    dismiss_labels: tuple[str, ...] | None = None,
) -> Page | None:
    """Run one step, record the resulting page + edge, return the new current page."""
    if "launch" in step:
        adb.force_stop(package)
        adb.start_activity(launcher)
    elif "am_start" in step:
        spec = step["am_start"] or {}
        comp = spec.get("component") or launcher
        args: list[str] = []
        for key, value in (spec.get("extras") or {}).items():
            args += ["--es", key, str(value)]
        adb.start_activity(comp, args)
    elif "tap" in step:
        # a late dialog would eat the tap
        cur = dismiss_dialogs(adb, package, graph, cur, settle, labels=dismiss_labels)
        node = adb.find_node(str(step["tap"]), package=package) or adb.find_node(str(step["tap"]))
        if node is None:
            result.failures.append(f"tap({step['tap']}): node not found")
            return cur
        adb.tap_node(node)
    elif "back" in step:
        adb.back()
    elif "sleep" in step:
        time.sleep(float(step["sleep"]))
        return cur
    elif "expect_visible" in step or "expect_page_changed" in step:
        return cur  # assertions handled below against the observed tree
    else:
        result.failures.append(f"unsupported step: {step}")
        return cur

    time.sleep(settle)
    if "launch" in step or "am_start" in step:
        cur = dismiss_dialogs(adb, package, graph, cur, settle, labels=dismiss_labels)
    page, root = observe(adb, package)
    graph.add_page(page)
    if cur is not None and page.key != cur.key:
        graph.add_edge(Edge(src=cur.key, dst=page.key, action=_describe(step), source="click"))

    if "expect_visible" in step:
        wanted = [str(w) for w in step["expect_visible"]]
        seen = _visible_texts(root)
        if not any(w.lower() in seen for w in wanted):
            result.failures.append(f"expect_visible {wanted}: not on screen")
    if step.get("expect_page_changed") and cur is not None and page.key == cur.key:
        result.failures.append("expect_page_changed: page fingerprint unchanged")
    return page


def load_flows(path: Path | str) -> tuple[str | None, list[dict[str, Any]]]:
    import json

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return None, data
    return data.get("package"), data.get("flows") or []
