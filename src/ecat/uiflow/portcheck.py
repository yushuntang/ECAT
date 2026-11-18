"""Check a spec against the port's real page registry.

A spec is a hand-written join key, so it can name screens that do not exist. When that
happens nothing complains: `diff` reports the port as "missing" the screen, or as having an
"unlisted" one, and the number looks like a port defect while it is a typo. This module reads
what the port actually ships — `main_pages.json` plus the entry ability's `loadContent` — and
reports three things:

* **unknown screen**  the spec names a port screen that is not registered (typo → FN noise)
* **entry uncovered** the page the ability loads on start is never asserted by a flow
* **orphan page**     a registered page that nothing navigates to (neither the entry page nor
                      any `router.pushUrl`), i.e. dead surface the user can never reach

Read-only: parses source, never launches anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOAD_CONTENT = re.compile(r"loadContent\(\s*['\"]([^'\"]+)['\"]")
PUSH_URL = re.compile(r"(?:router\.)?pushUrl\(\s*\{[^}]*?url\s*:\s*['\"]([^'\"]+)['\"]", re.S)
PUSH_URL_SIMPLE = re.compile(r"pushUrl\(\s*['\"]([^'\"]+)['\"]")

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _page_stem(value: str) -> str:
    """'pages/Garden' -> 'Garden'; 'Garden.ets' -> 'Garden'."""
    name = value.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if name.endswith(".ets"):
        name = name[: -len(".ets")]
    return name


@dataclass
class PortPages:
    root: Path | None = None
    registered: list[str] = field(default_factory=list)  # page stems, e.g. ['Garden', 'Index']
    entry: str | None = None                             # page stem the ability loads
    sources: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.registered)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root) if self.root else None,
            "registered": self.registered,
            "entry": self.entry,
            "sources": self.sources,
        }


@dataclass
class PortFinding:
    check: str
    severity: str
    detail: str
    fix: str
    screen: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"check": self.check, "severity": self.severity, "screen": self.screen,
                "detail": self.detail, "fix": self.fix}


@dataclass
class PortCheck:
    pages: PortPages
    findings: list[PortFinding] = field(default_factory=list)

    @property
    def errors(self) -> list[PortFinding]:
        return [f for f in self.findings if f.severity == "error"]

    def sorted_findings(self) -> list[PortFinding]:
        return sorted(self.findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.screen, f.check))

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": self.pages.to_dict(),
            "ok": not self.errors,
            "by_severity": {s: sum(1 for f in self.findings if f.severity == s)
                            for s in ("error", "warning", "info")},
            "findings": [f.to_dict() for f in self.sorted_findings()],
        }

    def to_markdown(self) -> str:
        lines = [
            "# Port check — spec 里的屏名对得上端口吗",
            "",
            f"- 注册页 {len(self.pages.registered)}：{', '.join(self.pages.registered) or '（没读到）'}",
            f"- 启动页：`{self.pages.entry or '（没读到）'}`",
            f"- 结论：{'通过' if not self.errors else f'{len(self.errors)} 个错误——spec 里有端口不存在的屏名'}",
            "",
            "| severity | screen | check | 说明 | 改法 |",
            "|---|---|---|---|---|",
        ]
        for f in self.sorted_findings():
            lines.append(f"| {f.severity} | `{f.screen}` | {f.check} | {f.detail} | {f.fix} |")
        return "\n".join(lines) + "\n"

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        path.with_suffix(".md").write_text(self.to_markdown(), encoding="utf-8")
        return path


def _find_main_pages(root: Path) -> Path | None:
    direct = root / "entry/src/main/resources/base/profile/main_pages.json"
    if direct.exists():
        return direct
    hits = sorted(root.glob("**/resources/base/profile/main_pages.json"))
    return hits[0] if hits else None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def read_port_pages(port_dir: Path | str) -> PortPages:
    root = Path(port_dir)
    pages = PortPages(root=root if root.exists() else None)

    main = _find_main_pages(root) if root.exists() else None
    if main is not None:
        try:
            payload = json.loads(_read_text(main))
        except json.JSONDecodeError:
            payload = {}
        src = payload.get("src") if isinstance(payload, dict) else None
        if isinstance(src, list):
            pages.registered = [_page_stem(str(s)) for s in src if str(s).strip()]
            pages.sources.append(str(main))

    for ets in sorted((root / "entry/src/main/ets").glob("**/*.ets")) if (root / "entry/src/main/ets").exists() else []:
        text = _read_text(ets)
        hit = LOAD_CONTENT.search(text)
        if hit:
            pages.entry = _page_stem(hit.group(1))
            pages.sources.append(str(ets))
            break
    return pages


def _navigated_pages(root: Path) -> set[str]:
    found: set[str] = set()
    ets_dir = root / "entry/src/main/ets"
    if not ets_dir.exists():
        return found
    for ets in sorted(ets_dir.glob("**/*.ets")):
        text = _read_text(ets)
        for match in PUSH_URL.finditer(text):
            found.add(_page_stem(match.group(1)))
        for match in PUSH_URL_SIMPLE.finditer(text):
            found.add(_page_stem(match.group(1)))
    return found


def _screen_rows(spec: Any) -> list[dict[str, Any]]:
    """Accept a raw spec payload or a loaded `UiflowSpec`."""
    if hasattr(spec, "screens"):
        return [s.to_dict() for s in spec.screens]
    return list(spec.get("screens") or []) if isinstance(spec, dict) else []


def check_spec(spec: Any, port_dir: Path | str) -> PortCheck:
    pages = read_port_pages(port_dir)
    result = PortCheck(pages=pages)
    screens = _screen_rows(spec)
    if not pages.ok:
        result.findings.append(PortFinding(
            "no_registry", "warning",
            f"{port_dir} 里没读到 main_pages.json —— 屏名校验跳过",
            "用 --port-dir 指向鸿蒙工程根目录（含 entry/src/main/resources/base/profile/main_pages.json）",
        ))
        return result

    registered = set(pages.registered)
    declared: dict[str, str] = {}
    for entry in screens:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("screen") or "")
        port_screen = str((entry.get("port") or {}).get("screen") or "")
        if not port_screen:
            result.findings.append(PortFinding(
                "port_screen_missing", "error",
                f"spec 屏 `{name}` 没填 port.screen，C 侧永远对不上",
                '补上 "port": {"screen": "<页面.ets>"}', screen=name,
            ))
            continue
        stem = _page_stem(port_screen)
        declared[stem] = name
        if stem not in registered:
            result.findings.append(PortFinding(
                "unknown_screen", "error",
                f"spec 写了 `{port_screen}`，但端口只注册了 {sorted(registered)}",
                "改 spec 里的屏名（或这是端口真的少了这一页——先确认再改）", screen=port_screen,
            ))

    if pages.entry and pages.entry not in declared:
        result.findings.append(PortFinding(
            "entry_uncovered", "warning",
            f"启动页 `{pages.entry}` 没有被任何 flow 断言",
            "至少一条 flow 从启动页开始并断言它上面的关键文案", screen=f"{pages.entry}.ets",
        ))

    for stem in pages.registered:
        if stem not in declared:
            result.findings.append(PortFinding(
                "port_page_uncovered", "info",
                f"端口注册了 `{stem}`，但 spec 里没有一行指向它——要么这页没测，要么配对被别的屏抢走了",
                "确认后补一行 spec，或删掉这页（真死页）", screen=f"{stem}.ets",
            ))

    if (port_dir_path := Path(port_dir)).exists():
        navigated = _navigated_pages(port_dir_path)
        for stem in pages.registered:
            if stem == pages.entry or stem in navigated or stem in declared:
                continue
            result.findings.append(PortFinding(
                "orphan_page", "info",
                f"`{stem}` 注册在 main_pages.json，但没有 loadContent 也没有 pushUrl 指向它——用户到不了",
                "确认是残留页就删掉；确实要保留就补一条入口，否则 FN 永远看不见它", screen=f"{stem}.ets",
            ))
    return result
