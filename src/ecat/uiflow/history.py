"""Keep every verdict, and show what changed since the last one.

A single FN number says nothing about a port that used to be fine. This module appends each verdict
to a per-bundle history file (JSONL, one line per run, keyed by the build that was measured) and
diffs the newest entry against the one before it:

    FN 8.3% -> 0.0%   fixed: detail
    FN 0.0% -> 8.3%   regression: detail
    PASS -> BLOCKED   the port stopped being measurable (not the same thing as worse)

Nothing here launches a device; it only reads and writes verdict files.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_HISTORY_DIR = Path("reports/ui-flow/history")


def _safe(name: str) -> str:
    keep = [c if (c.isalnum() or c in "-._") else "_" for c in (name or "unknown")]
    return "".join(keep) or "unknown"


def history_path(history_dir: Path | str, bundle: str) -> Path:
    return Path(history_dir) / f"{_safe(bundle)}.jsonl"


@dataclass
class Entry:
    bundle: str
    version: str
    generated: str
    status: str
    fn_rate: float | None
    port_pages: int
    android_reached: str
    missing_pages: list[str] = field(default_factory=list)
    dead_pages: list[str] = field(default_factory=list)
    #: False when the run had no --port-dir, so `dead_pages` is unknown rather than empty
    port_checked: bool = True
    #: sha256 (short) of the installed build — versionName repeats across rebuilds
    sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle": self.bundle,
            "version": self.version,
            "generated": self.generated,
            "status": self.status,
            "fn_rate": self.fn_rate,
            "port_pages": self.port_pages,
            "android_reached": self.android_reached,
            "missing_pages": list(self.missing_pages),
            "dead_pages": list(self.dead_pages),
            "port_checked": self.port_checked,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Entry:
        fn = d.get("fn_rate")
        return cls(
            bundle=str(d.get("bundle") or ""),
            version=str(d.get("version") or ""),
            generated=str(d.get("generated") or ""),
            status=str(d.get("status") or ""),
            fn_rate=float(fn) if isinstance(fn, (int, float)) else None,
            port_pages=int(d.get("port_pages") or 0),
            android_reached=str(d.get("android_reached") or ""),
            missing_pages=[str(x) for x in (d.get("missing_pages") or [])],
            dead_pages=[str(x) for x in (d.get("dead_pages") or [])],
            port_checked=bool(d.get("port_checked", True)),
            sha256=str(d.get("sha256") or ""),
        )

    @classmethod
    def from_verdict(cls, verdict: dict[str, Any]) -> Entry:
        numbers = verdict.get("numbers") or {}
        app = str(numbers.get("app") or "")
        version = app.rsplit(" v", 1)[-1] if " v" in app else ""
        port = numbers.get("port_reached") or numbers.get("port_pages") or 0
        if isinstance(port, str) and "/" in port:
            port = int(port.split("/", 1)[0])
        return cls(
            bundle=str(verdict.get("bundle") or ""),
            version=version,
            generated=str(verdict.get("generated") or time.strftime("%Y-%m-%d %H:%M:%S")),
            status=str(verdict.get("status") or ""),
            fn_rate=verdict.get("fn_rate") if isinstance(verdict.get("fn_rate"), (int, float)) else None,
            port_pages=int(port or 0),
            android_reached=str(numbers.get("android_reached") or ""),
            missing_pages=[str(x) for x in (verdict.get("missing_pages") or [])],
            dead_pages=[str(x) for x in (verdict.get("dead_pages") or [])],
            port_checked=any(
                c.get("check") == "port-check" and c.get("status") != "skip"
                for c in (verdict.get("checks") or [])
            ),
            sha256=str(numbers.get("app_sha256") or ""),
        )


def read_history(history_dir: Path | str, bundle: str) -> list[Entry]:
    path = history_path(history_dir, bundle)
    if not path.exists():
        return []
    entries: list[Entry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(Entry.from_dict(json.loads(line)))
        except (json.JSONDecodeError, ValueError):
            continue
    return entries


def append_entry(history_dir: Path | str, entry: Entry, *, keep: int = 50) -> Path:
    """Append one run and keep the file bounded (a delta only ever needs the previous line)."""
    path = history_path(history_dir, entry.bundle)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if path.exists():
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    lines.append(json.dumps(entry.to_dict(), ensure_ascii=False))
    if keep > 0:
        lines = lines[-keep:]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def delta(previous: Entry | None, current: Entry) -> dict[str, Any]:
    """What changed between two runs of the same bundle."""
    if previous is None:
        return {"first_run": True}

    def pct(value: float | None) -> str:
        return "未出数" if value is None else f"{value:.1%}"

    changes: list[str] = []
    verdict_word = "unchanged"

    if previous.status != current.status:
        changes.append(f"状态 {previous.status} → {current.status}")
    if previous.version != current.version:
        changes.append(f"版本 {previous.version or '?'} → {current.version or '?'}")
    if previous.sha256 != current.sha256 and previous.sha256 and current.sha256:
        # same versionName, different build: this is the case version numbers cannot see
        changes.append(f"构建 {previous.sha256[:12]} → {current.sha256[:12]}")
    if previous.fn_rate != current.fn_rate:
        if previous.fn_rate is None or current.fn_rate is None:
            changes.append(f"FN {pct(previous.fn_rate)} → {pct(current.fn_rate)}")
        elif current.fn_rate > previous.fn_rate:
            changes.append(f"FN {pct(previous.fn_rate)} → {pct(current.fn_rate)}（变差）")
        else:
            changes.append(f"FN {pct(previous.fn_rate)} → {pct(current.fn_rate)}（变好）")
    if previous.port_pages != current.port_pages:
        changes.append(f"端口可见页 {previous.port_pages} → {current.port_pages}")

    fixed = sorted(set(previous.missing_pages) - set(current.missing_pages))
    regressed = sorted(set(current.missing_pages) - set(previous.missing_pages))
    new_dead = (
        sorted(set(current.dead_pages) - set(previous.dead_pages))
        if (previous.port_checked and current.port_checked)
        else []  # dead pages unknown on one side: claiming a change would be a guess
    )

    if regressed:
        verdict_word = "regression"
        changes += [f"新增缺失：{p}" for p in regressed]
    elif fixed:
        verdict_word = "fixed"
        changes += [f"补上了：{p}" for p in fixed]
    changes += [f"新增死页：{p}" for p in new_dead]

    return {
        "first_run": False,
        "previous": previous.to_dict(),
        "verdict": verdict_word,
        "changes": changes,
        "fixed_pages": fixed,
        "regressed_pages": regressed,
        "new_dead_pages": new_dead,
    }


def to_markdown(d: dict[str, Any]) -> str:
    if d.get("first_run"):
        return "## 与上一版对比\n\n首次记录，没有可比基线。\n"
    prev = d.get("previous") or {}
    lines = [
        "## 与上一版对比（{} → {}）".format(prev.get("version") or "?", "当前"),
        "",
        f"- 上次：{prev.get('status')} · FN {prev.get('fn_rate')} · {prev.get('generated')}",
        f"- 判定：**{d.get('verdict')}**",
    ]
    lines += [f"- {c}" for c in d.get("changes") or []] or ["- 无变化"]
    return "\n".join(lines) + "\n"
