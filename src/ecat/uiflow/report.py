"""Markdown renderers for the uiflow layers (kept out of the CLI so tests can call them)."""

from __future__ import annotations

from typing import Any

from ecat.uiflow.models import PageGraph, short

VERDICT_NOTE = {
    "reached": "直跳后仍在栈顶",
    "reached_trampoline": "跳板页，交棒给后继页",
    "finished": "自己 finish 了（多为缺前置参数/数据）",
    "redirect": "在栈里但被别的页盖住",
    "blocked_dialog": "被系统/权限弹窗挡住",
    "dialog": "弹窗/权限页，不计入分母",
    "empty_frame": "屏幕上没有任何文本（启动白帧），不是页面，不计入分母",
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
    lines += ["", "> A 只回答“这页存在且能渲染”。FN 的分母来自发现结果或 functional gate 的 screens，不用 A。"]
    return "\n".join(lines) + "\n"


def discover_markdown(graph: PageGraph, framework: dict[str, Any], out_dir: str) -> str:
    """Layer B — pages found by an exploration engine (no hand-written flows)."""
    lines = [
        "# Layer B（发现）— 自动遍历出来的页面集合",
        "",
        f"- 目标 `{graph.target}` · 设备 `{graph.device}` · engine `{graph.engine}`",
        f"- 产物 `{out_dir}` · 框架统计：{framework.get('num_nodes')} 状态 / "
        f"{framework.get('num_edges')} 边 / 触达 {framework.get('num_reached_activities')} 个页面组件 / "
        f"{float(framework.get('time_spent') or 0):.0f}s",
        f"- 本图收录 App 内页面 **{len(graph.countable())}** · 跳转边 **{len(graph.edges)}** · "
        f"判定分布 {graph.verdict_counts()}",
        "",
        "> 自动遍历只回答“有哪些页面存在”。探索带启发式，页面集合会随轮次浮动；",
        "> 需要稳定口径时用 functional gate 的 screens 或同一份 spec 复跑。",
        "",
        "## 页面",
        "",
        "| page | 组件 |",
        "|---|---|",
    ]
    for page in graph.pages:
        lines.append(f"| `{page.label}` | `{short(page.component)}` |")
    if graph.edges:
        lines += ["", "## 跳转边", "", "| from | action | to |", "|---|---|---|"]
        by_key = {p.key: p for p in graph.pages}
        for e in graph.edges:
            src = by_key[e.src].label if e.src in by_key else e.src[:12]
            dst = by_key[e.dst].label if e.dst in by_key else e.dst[:12]
            lines.append(f"| `{src}` | {e.action} | `{dst}` |")
    return "\n".join(lines) + "\n"
