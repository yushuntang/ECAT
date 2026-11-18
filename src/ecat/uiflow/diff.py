"""Acceptance math — diff the reachable page sets of two sides.

    FN rate = |B \\ C| / |B|

where **B is the click-walk of the original app** and **C is the click-walk of the
port target**. `am_start` (Layer A) may never be the denominator: direct entry
bypasses the real entry, so misses would be counted against a bar no user can
clear. `diff_graphs` refuses that mistake unless `force=True`.

Both directions are reported, because they mean different things:

    missing (B\\C)  the port lost a page the user could reach   -> FN
    extra   (C\\B)  the port merged/split/added pages           -> structure change
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ecat.uiflow.models import Page, PageGraph, norm, short


def _label_norm(text: str) -> str:
    """Label without fingerprint hash/data — `SomeScreen[$ab12] "x"` -> `somescreen`."""
    head = (text or "").split("[", 1)[0]
    head = re.split(r"\s\"", head, maxsplit=1)[0]
    return norm(head)


@dataclass
class DiffResult:
    base_side: str
    port_side: str
    base_method: str
    port_method: str
    base_count: int
    port_count: int
    base_engine: str = ""
    port_engine: str = ""
    matched: list[dict[str, str]] = field(default_factory=list)
    missing: list[dict[str, str]] = field(default_factory=list)
    extra: list[dict[str, str]] = field(default_factory=list)
    mapping_source: str = "none"
    warnings: list[str] = field(default_factory=list)
    #: False when the numbers must not be quoted (one side is empty)
    valid: bool = True
    invalid_reason: str = ""

    @property
    def fn_rate(self) -> float:
        if not self.valid:
            return 0.0
        return round(len(self.missing) / self.base_count, 4) if self.base_count else 0.0

    @property
    def coverage(self) -> float:
        return round(len(self.matched) / self.base_count, 4) if self.base_count else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_side": self.base_side,
            "port_side": self.port_side,
            "base_method": self.base_method,
            "port_method": self.port_method,
            "base_count": self.base_count,
            "port_count": self.port_count,
            "matched": len(self.matched),
            "missing": len(self.missing),
            "extra": len(self.extra),
            "fn_rate": self.fn_rate,
            "coverage": self.coverage,
            "mapping_source": self.mapping_source,
            "valid": self.valid,
            "invalid_reason": self.invalid_reason,
            "warnings": self.warnings,
            "detail": {"matched": self.matched, "missing": self.missing, "extra": self.extra},
        }

    def to_markdown(self) -> str:
        lines = [
            "# UI flow parity — B vs C（FN 率）",
            "",
            f"- 分母 B：`{self.base_side}`（method=`{self.base_method}`"
            + (f" · engine=`{self.base_engine}`" if self.base_engine else "")
            + f"）可点击到达 **{self.base_count}** 页",
            f"- 分子 C：`{self.port_side}`（method=`{self.port_method}`"
            + (f" · engine=`{self.port_engine}`" if self.port_engine else "")
            + f"）可点击到达 **{self.port_count}** 页",
            f"- 匹配 **{len(self.matched)}** · 缺失 **{len(self.missing)}** · 多出 **{len(self.extra)}**",
            f"- **FN 率 = {len(self.missing)}/{self.base_count} = {self.fn_rate:.1%}** · 覆盖率 {self.coverage:.1%}",
            f"- 匹配方式：`{self.mapping_source}`",
        ]
        if self.warnings:
            lines += ["", "## 警告", *[f"- {w}" for w in self.warnings]]
        if self.missing:
            lines += ["", "## 缺失页（B\\C，计入 FN）", "", "| B 页面 | 组件 |", "|---|---|"]
            lines += [f"| `{m['label']}` | `{m['component']}` |" for m in self.missing]
        if self.extra:
            lines += ["", "## 多出页（C\\B，结构变化，不计 FN）", "", "| C 页面 | 组件 |", "|---|---|"]
            lines += [f"| `{m['label']}` | `{m['component']}` |" for m in self.extra]
        if self.matched:
            lines += ["", "## 匹配明细", "", "| B 页面 | C 页面 | 依据 |", "|---|---|---|"]
            lines += [
                f"| `{m['base_label']}` | `{m['port_label']}` | {m['by']} |" for m in self.matched
            ]
        return "\n".join(lines) + "\n"

    def save(self, path: Path | str) -> Path:
        import json

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        path.with_suffix(".md").write_text(self.to_markdown(), encoding="utf-8")
        return path


def _index(graph: PageGraph) -> dict[str, list[Page]]:
    idx: dict[str, list[Page]] = {}
    for page in graph.countable():
        for key in {page.key, page.component, short(page.component), _label_norm(page.label)}:
            if key:
                idx.setdefault(norm(str(key)), []).append(page)
    return idx


def diff_graphs(
    base: PageGraph,
    port: PageGraph,
    mapping: dict[str, str] | None = None,
    force: bool = False,
) -> DiffResult:
    """Compare page sets; raises ValueError when `base` is an am_start capability table."""
    if base.method != "click" and not force:
        raise ValueError(
            "refusing to diff against an amstart graph: direct entry bypasses the real entry, "
            "so FN would be measured against a bar users cannot clear. Use the click-walk (Layer B) "
            "as the denominator, or pass force=True for a deliberate capability comparison."
        )

    base_pages = base.countable()
    port_pages = port.countable()
    result = DiffResult(
        base_side=base.side,
        port_side=port.side,
        base_method=base.method,
        port_method=port.method,
        base_engine=base.engine,
        port_engine=port.engine,
        base_count=len(base_pages),
        port_count=len(port_pages),
        mapping_source="mapping file" if mapping else "component/label normalisation",
    )
    if not base_pages:
        result.warnings.append("B has no countable pages — run the click walk on the original APK first")
    if mapping:
        result.mapping_source = "mapping file"

    port_idx = _index(port)
    used: set[str] = set()
    base_idx = _index(base) if mapping else {}

    for page in base_pages:
        hit = None
        by = ""
        if mapping:
            for probe in (page.key, page.component, short(page.component), _label_norm(page.label)):
                target = mapping.get(str(probe)) or mapping.get(norm(str(probe)))
                if not target:
                    continue
                for cand in port_idx.get(norm(target), []):
                    if cand.key not in used:
                        hit, by = cand, "mapping"
                        break
                if hit:
                    break
        if hit is None:
            for probe in (page.component, short(page.component), _label_norm(page.label)):
                for cand in port_idx.get(norm(str(probe)), []):
                    if cand.key not in used:
                        hit, by = cand, probe.split(".")[-1] if "." in str(probe) else "label"
                        break
                if hit:
                    break
        if hit is not None:
            used.add(hit.key)
            result.matched.append(
                {"base_label": page.label, "port_label": hit.label, "by": by,
                 "base_component": page.component, "port_component": hit.component}
            )
        else:
            result.missing.append({"label": page.label, "component": page.component, "key": page.key})

    for page in port_pages:
        if page.key not in used:
            result.extra.append({"label": page.label, "component": page.component, "key": page.key})

    if not base_pages:
        result.valid = False
        result.invalid_reason = "B has no reachable pages — nothing to measure against"
    elif not port_pages:
        result.valid = False
        result.invalid_reason = "C has no pages — the port side report is empty or was not imported"

    if base_idx and len(result.missing) == len(base_pages):
        result.warnings.append("mapping matched nothing — check the keys (component/short name/label)")
    if port.method == "flows" and base_pages:
        result.warnings.append(
            f"C comes from {len(port_pages)} screen(s) named by the T2 flow spec, B is discovery "
            f"({len(base_pages)} page(s)): a page no flow mentions reports as missing even when the "
            "port renders it, so FN here is bounded by the spec's coverage, not by the port alone"
        )
    return result
