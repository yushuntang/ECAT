"""Audit a flow spec for flows that cannot tell the truth.

A green flow is not evidence. Three shapes recur — the same three a conference talk on
"tests that lie" names, and two of them are already visible in this project's T2 spec:

* **cannot fail**   no assertion at all, or taps with no navigation assertion
* **tautological**  the expectation is already true on the start screen, so it stays green
                    no matter what (needs the start labels; flagged structurally here when a
                    flow has `reset` *and* content expectations, i.e. the data it expects is
                    wiped by its own first step)
* **structure/data sensitive**  expectations pinned to concrete data (`Apple`, `every 30
                    days`) that a `reset`/`bm clean` removes between runs

Plus two checks specific to this pipeline: a flow that declares `screens` but marks no step
with `"screen"` cannot produce edges or a reliable screen identity, and a passing flow that
records no `actual` labels leaves the FN rate unattributable (`docs/ecat-validation-layers`
critique #5).

Nothing here touches a device: it reads the spec (and optionally a T2 report) only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ACTION_STEPS = ("tap", "tap_type", "tap_xy", "input")
ASSERT_PREFIX = "expect"

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Finding:
    flow: str
    check: str
    severity: str
    detail: str
    fix: str

    def to_dict(self) -> dict[str, str]:
        return {
            "flow": self.flow,
            "check": self.check,
            "severity": self.severity,
            "detail": self.detail,
            "fix": self.fix,
        }


@dataclass
class AuditResult:
    flows: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def by_severity(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out

    @property
    def ok(self) -> bool:
        return not any(f.severity == "high" for f in self.findings)

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.flow, f.check))

    def to_dict(self) -> dict[str, Any]:
        return {
            "flows": self.flows,
            "ok": self.ok,
            "by_severity": self.by_severity,
            "findings": [f.to_dict() for f in self.sorted_findings()],
        }

    def to_markdown(self) -> str:
        lines = [
            "# Flow audit — 这些 flow 能不能说真话",
            "",
            f"- 审计 {self.flows} 条 flow · 高危 **{self.by_severity.get('high', 0)}** · "
            f"中 **{self.by_severity.get('medium', 0)}** · 低 **{self.by_severity.get('low', 0)}**",
            f"- 结论：{'通过' if self.ok and not self.by_severity.get('medium') else ('通过（有中危警告）' if self.ok else '有高危问题——全绿也不代表端口没丢页')}",
            "",
            "| severity | flow | check | 说明 | 改法 |",
            "|---|---|---|---|---|",
        ]
        for f in self.sorted_findings():
            lines.append(f"| {f.severity} | `{f.flow}` | {f.check} | {f.detail} | {f.fix} |")
        return "\n".join(lines) + "\n"

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        path.with_suffix(".md").write_text(self.to_markdown(), encoding="utf-8")
        return path


def _step_names(steps: list[dict[str, Any]]) -> list[str]:
    return [next((k for k in s if k != "note"), "?") for s in steps]


def audit_flows(spec: dict[str, Any], report: dict[str, Any] | None = None) -> AuditResult:
    flows = spec.get("flows") or []
    result = AuditResult(flows=len(flows))

    for flow in flows:
        fid = str(flow.get("id") or "flow")
        steps = flow.get("steps") or []
        names = _step_names(steps)
        has_assertion = any(n.startswith(ASSERT_PREFIX) for n in names)
        has_action = any(n in ACTION_STEPS for n in names)
        has_nav_assertion = "expect_page_changed" in names or "expect_visible" in names

        if not has_assertion:
            result.findings.append(
                Finding(fid, "no_assertion", "high",
                        "整条 flow 没有任何 expect_*，跑完什么都能说成通过",
                        "至少加一条 expect_visible，导航后加 expect_page_changed")
            )
        elif has_action and not has_nav_assertion:
            result.findings.append(
                Finding(fid, "no_navigation_assertion", "high",
                        "有点击/输入但没有 expect_page_changed / expect_visible",
                        "每次导航后加 expect_page_changed，并断言目标屏上的关键文本")
            )

        # A flow explicitly marked as the seeder may reset and assert: it is the one that
        # creates the data the other flows rely on.
        if "reset" in names and has_assertion and not flow.get("seed"):
            result.findings.append(
                Finding(fid, "reset_vs_expectation", "medium",
                        "第一步 reset（bm clean 清数据）却又期望具体内容——数据被自己清掉了",
                        "去掉 reset，或先跑一条 seed flow 把数据造出来再断言")
            )

        if flow.get("screens") and not any(s.get("screen") for s in steps):
            result.findings.append(
                Finding(fid, "no_screen_markers", "medium",
                        f"声明了 screens {flow.get('screens')}，但没有任何步骤带 screen 标记",
                        '在动作步骤上加 "screen": "<页面.ets>"，否则页面身份和边都只能靠声明猜')
            )

    if report is not None:
        passed = [f for f in (report.get("flows") or []) if str(f.get("status")).lower() == "pass"]
        silent = [f for f in passed if not f.get("actual") and not f.get("trace")]
        if silent:
            result.findings.append(
                Finding("*", "pass_without_evidence", "low",
                        f"{len(silent)} 条通过的 flow 没记录 actual/trace——FN 无法归因到具体屏",
                        "让 runner 在通过时也记录 actual/trace（本仓库 ui_runner 已记 trace）")
            )
    return result


def audit_states(screen_spec: Any, flow_spec: dict[str, Any]) -> dict[str, list[Finding]]:
    """Cross-check the states a spec declares against the states the flows prove.

    A state is only provable when a flow step lands on that screen and says
    `"state": "<id>"`. Declaring states without touching the flows makes every one of them count
    as missing (`state FN = 100%`), which looks like a port defect and is really a spec/flow
    mismatch — the kind of trap that makes people stop declaring states at all.
    """
    rows = _screen_rows(screen_spec)
    declared: dict[str, set[str]] = {}
    for row in rows:
        port = str((row.get("port") or {}).get("screen") or "")
        for state in row.get("states") or []:
            sid = str((state.get("port") or {}).get("state") or state.get("id") or "").strip().lower()
            if port and sid:
                declared.setdefault(_stem(port), set()).add(sid)
    if not declared:
        return {"states": []}

    evidenced: dict[str, set[str]] = {}
    for flow in flow_spec.get("flows") or []:
        for step in flow.get("steps") or []:
            screen, state = str(step.get("screen") or ""), str(step.get("state") or "")
            if screen and state:
                evidenced.setdefault(_stem(screen), set()).add(state.strip().lower())

    findings: list[Finding] = []
    if not evidenced:
        findings.append(Finding(
            "*", "states_not_evidenced", "high",
            f"spec 声明了 {sum(len(v) for v in declared.values())} 个状态，但没有任何 flow step 写 `state` "
            "——这些状态会全部算缺失（state FN=100%）",
            '在落到该屏的步骤上加 "state": "<id>"，否则删掉 spec 里的 states',
        ))
        return {"states": findings}

    texts: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        port = str((row.get("port") or {}).get("screen") or "")
        for state in row.get("states") or []:
            words = _tokens(str((state.get("android") or {}).get("text") or ""))
            sid = str((state.get("port") or {}).get("state") or state.get("id") or "").strip().lower()
            if port and sid and words:
                texts.setdefault(_stem(port), {})[sid] = words
    for screen, per_state in sorted(texts.items()):
        ids = sorted(per_state)
        for i, a_id in enumerate(ids):
            for b_id in ids[i + 1:]:
                if per_state[a_id] <= per_state[b_id] or per_state[b_id] <= per_state[a_id]:
                    findings.append(Finding(
                        screen, "state_ambiguous", "medium",
                        f"状态 `{a_id}` 与 `{b_id}` 的 android.text 互相包含，靠文本证据分不开",
                        "给两个状态写互不包含的文本（例如各自独有的标签）",
                    ))

    for screen, states in sorted(declared.items()):
        for state in sorted(states):
            if state not in evidenced.get(screen, set()):
                findings.append(Finding(
                    screen, "state_uncovered", "medium",
                    f"状态 `{state}` 在 spec 里声明了，但没有 flow 在这一屏证明它",
                    f'加一步 {{"tap": "…", "screen": "{screen}", "state": "{state}"}}',
                ))
    return {"states": findings}


def _tokens(text: str) -> set[str]:
    import re as _re

    spaced = _re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {t for t in _re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t}


def _stem(value: str) -> str:
    name = value.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[: -len(".ets")] if name.endswith(".ets") else name


def _screen_rows(spec: Any) -> list[dict[str, Any]]:
    """Accept a raw spec payload or a loaded `UiflowSpec`."""
    if hasattr(spec, "screens"):
        return [s.to_dict() for s in spec.screens]
    return list(spec.get("screens") or []) if isinstance(spec, dict) else []


def load_spec(path: Path | str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("flows"), list):
        raise ValueError(f"{path}: not a flow spec (expected {{'flows': [...]}})")
    return payload
