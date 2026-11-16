"""Markdown renderers for the three layers (kept out of the CLI so tests can call them)."""

from __future__ import annotations

from typing import Any

from ecat.uiflow.models import PageGraph, short

VERDICT_NOTE = {
    "reached": "直跳后仍在栈顶",
    "reached_trampoline": "跳板页，交棒给后继页",
    "finished": "自己 finish 了（多为缺前置参数/数据）",
    "redirect": "在栈里但被别的页盖住",
    "blocked_dialog": "被系统/权限弹窗挡住",
    "crashed_abi": "缺 .so，环境限制，不算 App 缺陷",
    "crashed_app": "应用崩溃（FATAL 归属该页）",
    "rejected": "am start 被拒（未导出/不存在）",
    "unknown": "无法判定（adb 异常/无焦点）",
}


def inventory_markdown(graph: PageGraph, *, settle: float = 3.0) -> str:
    reached = graph.countable()
    counts = graph.verdict_counts()
    lines = [
        "# Layer A — 直跳可达清单（能力上限，不是覆盖率）",
        "",
        f"- 目标 `{graph.target}` · 设备 `{graph.device}` · 生成 {graph.generated}",
        f"- 判据：`am start` 后 {settle:g}s，`dumpsys window mCurrentFocus` 仍是目标组件才算到达",
        f"- 到达 **{len(reached)}/{len(graph.pages)}**：{counts}",
        "",
        "| # | page | exported | verdict | detail | 落地后栈顶 | 参数 |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, page in enumerate(graph.pages, 1):
        extra = page.extra or {}
        params = " ".join(extra.get("params") or []) or "—"
        top = short(str(extra.get("top_after") or "")) or "—"
        exported = extra.get("exported")
        exported = "—" if exported is None else ("true" if exported else "false")
        lines.append(
            f"| {i} | `{short(page.component)}` | {exported} | {page.verdict} | "
            f"{page.detail[:60]} | `{top}` | {params} |"
        )
    lines += ["", "> A 只回答“这页存在且能渲染”。FN 率的分母永远取 Layer B 的点击结果。"]
    return "\n".join(lines) + "\n"


def walk_markdown(graph: PageGraph, results: list[Any]) -> str:
    ok = sum(1 for r in results if r.ok)
    failures = [f for r in results for f in r.failures]
    lines = [
        "# Layer B — 点击遍历（用户真实入口能到哪些页）",
        "",
        f"- 目标 `{graph.target}` · 设备 `{graph.device}` · 生成 {graph.generated}",
        f"- 流程 {ok}/{len(results)} 通过 · 抵达页面 **{len(graph.countable())}** · 边 **{len(graph.edges)}**",
    ]
    if failures:
        lines += ["", "## 断言失败", *[f"- {f}" for f in failures]]
    lines += ["", "## 页面", "", "| page | 组件 | 首次出现于流程 |", "|---|---|---|"]
    owner = {key: r.id for r in results for key in r.pages_seen}
    for page in graph.pages:
        lines.append(f"| `{page.label}` | `{short(page.component)}` | {owner.get(page.key, '—')} |")
    if graph.edges:
        lines += ["", "## 跳转边", "", "| from | action | to |", "|---|---|---|"]
        by_key = {p.key: p for p in graph.pages}
        for e in graph.edges:
            src = short(by_key[e.src].component) if e.src in by_key else e.src[-14:]
            dst = short(by_key[e.dst].component) if e.dst in by_key else e.dst[-14:]
            lines.append(f"| `{src}` | {e.action} | `{dst}` |")
    return "\n".join(lines) + "\n"
