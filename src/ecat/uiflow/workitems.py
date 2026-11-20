"""Turn a diagnosis into work items the refinement loop can act on.

As an acceptance metric uiflow is not trustworthy yet (the denominator covers three activities of
eighteen, and cross-platform element comparison is blocked by the two dump formats). As a *discovery
instrument* it is already useful: it names the page that is missing, the entry that is broken, the
flow that cannot tell the truth, and the spec row that is a typo. This module is that sentence turned
into data — a `TextGradient`-shaped payload (the same shape the discriminator produces) plus a
markdown list a human can read.

Only real port defects become work items. Tooling mistakes (a spec typo, an unproven assertion) are
reported in a separate section, because "fix the port" is the wrong response to a broken measuring
stick.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PAGE_DIR = "entry/src/main/ets/pages"


def _page_file(port_screen: str, port_dir: Path | None) -> str:
    """Where the screen probably lives, so the work item points at a file rather than a name."""
    stem = (port_screen or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if stem.endswith(".ets"):
        stem = stem[: -len(".ets")]
    if not stem:
        return ""
    stem = stem[:1].upper() + stem[1:]  # pages are PascalCase by convention; the glob below corrects it
    guess = f"{PAGE_DIR}/{stem}.ets"
    if port_dir is not None and port_dir.exists() and not (port_dir / guess).exists():
        for candidate in port_dir.glob(f"**/{stem}.ets"):
            return str(candidate.relative_to(port_dir)).replace("\\", "/")
    return guess


def to_work_items(
    verdict: Any,
    *,
    port_dir: Path | str | None = None,
    spec: Path | str | None = None,
) -> dict[str, Any]:
    """`TextGradient`-shaped payload: summary + work_list + insights, plus a tooling section."""
    root = Path(port_dir) if port_dir else None
    port_screens = {}
    if spec is not None and Path(spec).exists():
        try:
            payload = json.loads(Path(spec).read_text(encoding="utf-8"))
            for row in payload.get("screens") or []:
                port_screens[str(row.get("screen") or "")] = str((row.get("port") or {}).get("screen") or "")
        except (json.JSONDecodeError, OSError):
            pass

    work_list: list[dict[str, Any]] = []
    tooling: list[dict[str, str]] = []

    for row in getattr(verdict, "missing_detail", []) or []:
        screen = row.get("screen", "")
        kind = row.get("kind", "")
        # when the spec row is silent, the spec screen name is still a better guess than nothing
        port_screen = port_screens.get(screen) or screen
        if kind == "missing_page":
            work_list.append({
                "file": _page_file(port_screen, root) or screen,
                "issues": [
                    f"端口没有这一页：spec 屏 `{screen}` 在 Android 侧可达，端口侧 `{port_screen}` 不存在",
                    "补实现该页面，或在 spec 中确认它确实不该存在（后者要写明理由）",
                ],
                "suggested_skills": ["hmos-arkui-develop-skill", "hmos-arkui-mvvm-pattern"],
                "severity": "high",
                "verify": [],
            })
        elif kind == "entry_broken":
            work_list.append({
                "file": _page_file(port_screen, root) or screen,
                "issues": [
                    f"页面存在但用户到不了：{port_screen} 在包里，没有任何 flow 证明能从入口走到它",
                    "检查入口（按钮/菜单/路由注册/main_pages），确认点击路径真实存在",
                ],
                "suggested_skills": ["hmos-arkui-develop-skill"],
                "severity": "high",
                "verify": [],
            })
        elif kind == "unmapped":
            tooling.append({
                "screen": screen,
                "issue": "spec 这一行没有填 port.screen，测量本身没定义",
                "action": "补 spec，不要在端口里找 bug",
            })

    for page in getattr(verdict, "dead_pages", []) or []:
        work_list.append({
            "file": _page_file(page, root) or page,
            "issues": [
                f"`{page}` 注册在 main_pages 里，但 loadContent 和 pushUrl 都没指向它——用户永远到不了",
                "确认是残留就删掉；要保留就补入口",
            ],
            "suggested_skills": ["hmos-arkui-develop-skill"],
            "severity": "low",
            "verify": [],
        })

    for check in getattr(verdict, "checks", []) or []:
        if check.name == "flow-audit" and check.status == "fail":
            tooling.append({
                "screen": "*",
                "issue": f"flow 说不了真话：{check.detail}",
                "action": "改 flow（加断言/加 screen 标记），不要改端口",
            })
        if check.name == "state-coverage" and check.status == "fail":
            tooling.append({
                "screen": "*",
                "issue": f"状态级覆盖不完整：{check.detail}",
                "action": "要么在 flow 里补该状态的证据，要么确认端口确实缺这个状态（缺就补页面）",
            })

    summary = (
        f"uiflow 诊断：{len(work_list)} 个端口缺陷，{len(tooling)} 个测量问题 "
        f"（{getattr(verdict, 'bundle', '') or 'bundle 未知'}）"
    )
    insights = list(getattr(verdict, "reasons", []) or [])[:6]
    return {
        "summary": summary,
        "work_list": work_list,
        "skill_evolution": [],
        "insights": insights,
        "tooling": tooling,
        "verdict": {
            "status": getattr(verdict, "status", ""),
            "fn_rate": getattr(verdict, "fn_rate", None),
            "numbers": getattr(verdict, "numbers", {}),
        },
    }


def to_markdown(payload: dict[str, Any]) -> str:
    lines = [f"# {payload.get('summary', 'uiflow 诊断')}", ""]
    work = payload.get("work_list") or []
    if work:
        lines += ["## 端口缺陷（进 refine 循环）", ""]
        for item in work:
            lines.append(f"### {item['file']} · {item['severity']}")
            lines += [f"- {issue}" for issue in item.get("issues") or []]
            lines.append("")
    else:
        lines += ["## 端口缺陷", "", "没有发现。", ""]
    tooling = payload.get("tooling") or []
    if tooling:
        lines += ["## 测量问题（不要当端口 bug 修）", ""]
        for item in tooling:
            lines.append(f"- `{item.get('screen')}`：{item.get('issue')} → {item.get('action')}")
        lines.append("")
    if payload.get("insights"):
        lines += ["## 上下文", ""] + [f"- {note}" for note in payload["insights"]]
    return "\n".join(lines) + "\n"


def save(payload: dict[str, Any], path: Path | str) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out.with_suffix(".md").write_text(to_markdown(payload), encoding="utf-8")
    return out
