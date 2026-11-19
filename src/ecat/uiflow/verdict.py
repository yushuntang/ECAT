"""One verdict artifact: every gate and every number in a single file.

Until now the chain was four commands with three different exit codes (1 spec/flow error, 2 spec
names a screen the port does not ship, 4 degraded graph), and a human had to read them in the
right order and decide. This module runs the same gates in the right order and writes one JSON +
one Markdown with a single status and a single exit code:

    PASS     0   port check clean, flows honest, port measured, FN computed
    FAIL     1   a gate rejected the run (spec typo, dishonest flows) — reasons listed
    BLOCKED  4   the port could not be measured (dependency-blocked or no passing flow) — no FN
    ERROR    2   inputs missing/unreadable

The FN number is only ever printed when status is PASS; every other status carries the reason
instead, so a CI job cannot pick up a number that the gates refused to vouch for.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"
EXIT = {PASS: 0, FAIL: 1, BLOCKED: 4, "ERROR": 2}


def _text_count(label: str) -> int:
    """How many visible strings a discovery label carries: `Act[$x] "A/B"` -> 2."""
    body = label.split('"', 1)[-1].rstrip('"') if '"' in label else ""
    return len([part for part in body.split("/") if part.strip()]) or 1


def classify_missing(result: Any, known_screens: set[str]) -> list[dict[str, str]]:
    """Why a spec row is missing, and what to do about it.

    "Missing" used to be one bucket, which sent people to the wrong place: a page the port ships
    but nothing navigates to is an entry bug, a page the port lacks is a port bug, and a screen that
    simply showed less content is a data problem.
    """
    rows: list[dict[str, str]] = []
    for row in result.missing:
        declared = str(row.port_page or "")
        stem = declared.replace("\\", "/").rsplit("/", 1)[-1]
        if stem.endswith(".ets"):
            stem = stem[: -len(".ets")]
        if stem and stem not in known_screens:
            action, kind = "补这一页，或确认它是否该存在", "missing_page"
        elif stem:
            action, kind = "页面在端口里，但没有 flow 证明能到——查入口/路由", "entry_broken"
        else:
            action, kind = "spec 这一行没填 port.screen", "unmapped"
        rows.append({"screen": row.screen, "kind": kind, "action": action})
    return rows


def data_parity(result: Any, port_graph: Any) -> list[str]:
    """Warn when the port shows far less content than Android on the same screen.

    Page identity survived an empty device (the page is there), so FN said 0% while the port was
    showing "no media files have been found" and Android showed a folder list. That is a data
    difference, and the report has to say so instead of letting the number imply "all good".
    """
    notes: list[str] = []
    port_pages = {p.label: p for p in getattr(port_graph, "pages", [])}
    for row in result.present:
        port_page = port_pages.get(row.port_page)
        if port_page is None:
            continue
        shown = int(((port_page.extra or {}).get("data") or {}).get("labels") or 0)
        expected = _text_count(row.android_page)
        if shown and expected >= 3 and shown <= 2:
            notes.append(
                f"数据可能不对等：{row.screen} 端口只渲染 {shown} 个标签，Android 侧有 {expected} 段文案"
            )
    return notes


@dataclass
class Check:
    name: str
    status: str  # pass | fail | blocked | skip
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"check": self.name, "status": self.status, "detail": self.detail}


@dataclass
class Verdict:
    bundle: str = ""
    generated: str = ""
    status: str = PASS
    fn_rate: float | None = None
    numbers: dict[str, Any] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    dead_pages: list[str] = field(default_factory=list)
    missing_pages: list[str] = field(default_factory=list)
    #: per missing page: why it is missing and what to do (`missing_page` / `entry_broken` / `unmapped`)
    missing_detail: list[dict[str, str]] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    #: what changed since the previous run of this bundle (None = history disabled)
    history: dict[str, Any] | None = None

    @property
    def exit_code(self) -> int:
        return EXIT.get(self.status, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "ecat.uiflow.verdict/1",
            "bundle": self.bundle,
            "generated": self.generated,
            "status": self.status,
            "exit_code": self.exit_code,
            "fn_rate": self.fn_rate,
            "numbers": self.numbers,
            "checks": [c.to_dict() for c in self.checks],
            "reasons": self.reasons,
            "dead_pages": self.dead_pages,
            "missing_pages": self.missing_pages,
            "missing_detail": self.missing_detail,
            "artifacts": self.artifacts,
            "history": self.history,
        }

    def to_markdown(self) -> str:
        head = {
            PASS: "**PASS**",
            FAIL: "**FAIL** — 有一道闸门拒绝出数",
            BLOCKED: "**BLOCKED** — 端口没被量到，FN 不可用",
        }.get(self.status, f"**{self.status}**")
        lines = [
            f"# UI 覆盖验收 — {self.bundle or '(bundle 未指定)'}",
            "",
            f"- 结论：{head}",
            f"- 时间：{self.generated}",
        ]
        if self.fn_rate is not None:
            lines.append(f"- FN 率：**{self.fn_rate:.1%}**")
        else:
            lines.append("- FN 率：**未出数**（见下方理由）")
        if self.numbers:
            parts = [f"{k}={v}" for k, v in self.numbers.items()]
            lines.append(f"- 数字：{' · '.join(parts)}")
        lines += ["", "## 闸门", "", "| check | status | detail |", "|---|---|---|"]
        for c in self.checks:
            lines.append(f"| {c.name} | {c.status} | {c.detail} |")
        if self.reasons:
            lines += ["", "## 为什么不出数 / 为什么不过"]
            lines += [f"- {r}" for r in self.reasons]
        if self.missing_pages:
            lines += ["", f"## 缺失页（{len(self.missing_pages)}）"]
            if self.missing_detail:
                lines += ["", "| 页 | 类型 | 下一步 |", "|---|---|---|"]
                for row in self.missing_detail:
                    lines.append(f"| `{row['screen']}` | {row['kind']} | {row['action']} |")
            else:
                lines += [f"- `{p}`" for p in self.missing_pages]
        if self.dead_pages:
            lines += ["", f"## 死页（注册但用户到不了，{len(self.dead_pages)}）"]
            lines += [f"- `{p}`" for p in self.dead_pages]
        if self.artifacts:
            lines += ["", "## 证据文件"]
            lines += [f"- {k}: `{v}`" for k, v in self.artifacts.items()]
        if self.history is not None:
            from ecat.uiflow.history import to_markdown as history_markdown

            lines += ["", history_markdown(self.history).rstrip()]
        return "\n".join(lines) + "\n"

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        path.with_suffix(".md").write_text(self.to_markdown(), encoding="utf-8")
        return path


def build_verdict(
    *,
    base: Path | str,
    history_dir: Path | str | None = None,
    port_report: Path | str,
    spec: Path | str,
    flows: Path | str | None = None,
    port_dir: Path | str | None = None,
    bundle: str = "",
) -> Verdict:
    """Run every gate, then record the verdict in the bundle's history (unless disabled)."""
    verdict = _build_verdict(base=base, port_report=port_report, spec=spec, flows=flows,
                             port_dir=port_dir, bundle=bundle)
    if history_dir is not None:
        from ecat.uiflow.history import Entry, append_entry, delta, read_history

        entries = read_history(history_dir, verdict.bundle)
        verdict.history = delta(entries[-1] if entries else None, Entry.from_verdict(verdict.to_dict()))
        append_entry(history_dir, Entry.from_verdict(verdict.to_dict()))
    return verdict


def _build_verdict(
    *,
    base: Path | str,
    port_report: Path | str,
    spec: Path | str,
    flows: Path | str | None = None,
    port_dir: Path | str | None = None,
    bundle: str = "",
) -> Verdict:
    """Run every gate in order; stop reporting numbers as soon as one refuses."""
    from ecat.uiflow.audit import audit_flows
    from ecat.uiflow.audit import load_spec as load_flow_spec
    from ecat.uiflow.flows_report import graph_from_flows_report
    from ecat.uiflow.models import PageGraph
    from ecat.uiflow.portcheck import check_spec
    from ecat.uiflow.spec import UiflowSpec, diff_against_spec

    verdict = Verdict(bundle=bundle, generated=time.strftime("%Y-%m-%d %H:%M:%S"))
    known_screens: set[str] = set()
    report_payload = json.loads(Path(port_report).read_text(encoding="utf-8"))

    # which app was actually measured, if the runner said so
    app = report_payload.get("app") or {}
    measured_version = str(app.get("version") or "")
    measured_bundle = str(app.get("bundle") or "")
    if measured_bundle and bundle and measured_bundle != bundle:
        verdict.status = FAIL
        verdict.reasons.append(
            f"测的是 {measured_bundle}，报告头写的是 {bundle} —— 装错包了"
        )
    measured_sha = str(app.get("sha256") or "")
    verdict.numbers["app"] = f"{measured_bundle or bundle or '?'} v{measured_version or '?'}"
    if measured_sha:
        verdict.numbers["app_sha256"] = measured_sha[:12]

    # 1. spec vs the port's real page registry
    if port_dir is not None:
        pc = check_spec(UiflowSpec.load(spec), port_dir)
        errors = [f for f in pc.findings if f.severity == "error"]
        orphan = [f.screen for f in pc.findings if f.check == "orphan_page"]
        verdict.dead_pages = orphan
        known_screens = pc.pages.known_screens
        verdict.checks.append(Check(
            "port-check", "fail" if errors else "pass",
            "; ".join(f.detail for f in errors) or
            f"registered={len(pc.pages.registered)} entry={pc.pages.entry} dead={len(orphan)}",
        ))
        if errors:
            verdict.status = FAIL
            verdict.reasons += [f"spec 屏名在端口不存在：{f.detail}" for f in errors]
    else:
        verdict.checks.append(Check("port-check", "skip", "没给 --port-dir"))

    # 2. are the flows able to tell the truth?
    if flows is not None:
        au = audit_flows(load_flow_spec(flows), report_payload)
        high = [f for f in au.findings if f.severity == "high"]
        verdict.checks.append(Check(
            "flow-audit", "fail" if high else "pass",
            "; ".join(f"{f.flow}: {f.check}" for f in high) or
            f"flows={au.flows} findings={au.by_severity or 'none'}",
        ))
        if high:
            verdict.status = FAIL
            verdict.reasons += [f"flow 说不了真话：{f.flow} {f.check} — {f.detail}" for f in high]
    else:
        verdict.checks.append(Check("flow-audit", "skip", "没给 --flows"))

    # 3. port graph
    c_graph = graph_from_flows_report(report_payload)
    stats = c_graph.stats or {}
    blocked = int(stats.get("dependency_skips") or 0)
    passed = int(stats.get("pass") or 0)
    reached = [p for p in c_graph.pages if p.countable]
    verdict.checks.append(Check(
        "port-measured", "blocked" if (blocked or not passed) else "pass",
        (f"{blocked} flow(s) blocked by a failed dependency" if blocked else "") +
        ("" if passed else (" ; " if blocked else "") + "no flow passed") or
        f"pass={passed} pages={len(reached)} edges={len(c_graph.edges)}",
    ))
    if blocked or not passed:
        verdict.status = BLOCKED
        if blocked:
            verdict.reasons.append(f"{blocked} 条 flow 因依赖失败被跳过，从没跑过 → 不能据此判缺页")
        if not passed:
            verdict.reasons.append("没有任何 flow 通过，端口覆盖未被量到")
        verdict.numbers = {**verdict.numbers, "port_pages": len(reached),
                           "edges": len(c_graph.edges), **stats}
        return verdict

    # 4. diff(B, C) through the spec
    spec_obj = UiflowSpec.load(spec)
    unconfirmed = spec_obj.unconfirmed()
    verdict.checks.append(Check(
        "spec-reviewed", "warn" if unconfirmed else "pass",
        f"{len(unconfirmed)} 行是自动配对的，没人确认过："
        + ", ".join(f"{s.screen}->{s.port_screen}" for s in unconfirmed[:3])
        if unconfirmed else f"{len(spec_obj.screens)} 行全部人工确认过",
    ))
    if unconfirmed:
        verdict.reasons.append(
            f"spec 里有 {len(unconfirmed)} 行是启发式配对（auto=true）——FN 的可信度取决于这些配对对不对："
            f"看过后跑 `ecat uiflow spec-confirm --spec <spec>`"
        )
    verdict.numbers["unconfirmed_spec_rows"] = len(unconfirmed)
    result = diff_against_spec(PageGraph.load(base), c_graph, spec_obj)
    app_label = verdict.numbers.get("app", "")
    app_sha = verdict.numbers.get("app_sha256", "")
    verdict.numbers = {
        **({"app": app_label} if app_label else {}),
        **({"app_sha256": app_sha} if app_sha else {}),
        "spec_screens": len(result.rows),
        "android_reached": f"{result.android_reached}/{result.android_pages}",
        "port_reached": f"{len(result.present)}/{result.port_pages}",
        "flow_pass": passed,
        "edges": len(c_graph.edges),
        "dead_pages": len(verdict.dead_pages),
        "unconfirmed_spec_rows": len(unconfirmed),
    }
    verdict.missing_pages = [r.screen for r in result.missing]
    verdict.missing_detail = classify_missing(result, known_screens)
    parity = data_parity(result, c_graph)
    verdict.checks.append(Check(
        "data-parity", "warn" if parity else "pass",
        "; ".join(parity) if parity else "两边的页面内容量相当",
    ))
    if parity:
        verdict.reasons += parity
    state_fn = result.state_fn_rate
    if state_fn is None:
        verdict.checks.append(Check("state-coverage", "skip", "spec 没声明任何状态（tab/筛选/面板）"))
    else:
        missing_states = [f"{r.screen}/{r.state}" for r in result.states_missing]
        verdict.checks.append(Check(
            "state-coverage", "fail" if missing_states else "pass",
            f"状态 FN={state_fn:.1%} · 缺 {len(missing_states)}" +
            (f"：{', '.join(missing_states[:4])}" if missing_states else ""),
        ))
        verdict.numbers["states"] = f"{result.states_total - len(missing_states)}/{result.states_total}"
        verdict.numbers["state_fn"] = f"{state_fn:.1%}"
        if missing_states:
            verdict.status = FAIL
            verdict.reasons += [f"端口缺状态：{m}" for m in missing_states]
    if not result.valid:
        verdict.status = BLOCKED
        verdict.reasons.append(f"diff 拒绝出数：{result.invalid_reason}")
        return verdict

    verdict.fn_rate = result.fn_rate
    verdict.checks.append(Check(
        "diff", "pass" if not result.missing else "fail",
        f"FN={result.fn_rate:.1%} missing={len(result.missing)}",
    ))
    if result.missing:
        verdict.status = FAIL
        verdict.reasons += [f"端口缺页：{r.screen}" for r in result.missing]
    return verdict
