"""Layer A — deterministic entry: every manifest activity, launched with params.

What this layer *claims*: the page exists in the build, it can be entered with an
explicit intent, and it renders. It does **not** claim a user can reach it — that
is Layer B's job, and the FN denominator always comes from B.

The activity list is read from the APK manifest (aapt2/aapt xmltree), so it is the
machine's registration table, not a hand-written page model. Aliases are collapsed
into their target activity; library activities stay in the list on purpose (they
are pages that exist, even if they are not product flow).
"""

from __future__ import annotations

import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any

from ecat.uiflow.device import Adb, AdbError
from ecat.uiflow.models import Page, PageGraph, short

#: extra keys -> `am start` flag
PARAM_FLAGS = (("extras", "--es"), ("int_extras", "--ei"), ("bool_extras", "--ez"))


class ToolsMissing(RuntimeError):
    """aapt2/aapt not found; the caller should install build-tools or set ECAT_AAPT."""


def aapt_bin() -> str:
    """Newest available aapt2 (preferred) or aapt."""
    env = os.environ.get("ECAT_AAPT")
    if env and Path(env).is_file():
        return env
    roots = [
        os.environ.get("ANDROID_HOME", ""),
        os.environ.get("ANDROID_SDK_ROOT", ""),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk"),
        str(Path.home() / "AppData/Local/Android/Sdk"),
    ]
    for tool in ("aapt2", "aapt"):
        for root in roots:
            bt = Path(root) / "build-tools"
            if not bt.is_dir():
                continue
            cands = sorted((d for d in bt.iterdir() if d.is_dir()), reverse=True)
            for d in cands:
                for suffix in ("", ".exe"):
                    exe = d / f"{tool}{suffix}"
                    if exe.is_file():
                        return str(exe)
    raise ToolsMissing("aapt2/aapt not found — install Android build-tools or set ECAT_AAPT")


def _run(args: list[str], timeout: int = 180) -> tuple[int, str]:
    import subprocess

    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "<timeout>"


_NAME_RE = re.compile(r'android:name\((?:0x[0-9a-f]+)\)="([^"]+)"')
_EXPORTED_RE = re.compile(r'android:exported\((?:0x[0-9a-f]+)\)=(?:\(type 0x12\))?(?:0x([0-9a-f]+)|(true|false))')


def enumerate_activities(apk: Path) -> tuple[str, list[dict[str, Any]]]:
    """(package, activities) straight from the APK's AndroidManifest.xml."""
    tool = aapt_bin()
    if Path(tool).name.lower().startswith("aapt2"):
        rc, out = _run([tool, "dump", "xmltree", "--file", "AndroidManifest.xml", str(apk)])
    else:
        rc, out = _run([tool, "dump", "xmltree", str(apk), "AndroidManifest.xml"])
    if rc != 0:
        raise ToolsMissing(f"aapt failed: {out[:300]}")

    pkg = ""
    m = re.search(r"package=\"([^\"]+)\"", out)
    if m:
        pkg = m.group(1)

    entries: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("E: activity-alias"):
            cur = {"kind": "alias"}
            entries.append(cur)
            continue
        if s.startswith("E: activity"):
            cur = {"kind": "activity"}
            entries.append(cur)
            continue
        if s.startswith("E: ") and cur is not None:
            cur = None
            continue
        if cur is None:
            continue
        m = _NAME_RE.search(s)
        if m and "name" not in cur:
            cur["name"] = m.group(1)
        m = _EXPORTED_RE.search(s)
        if m:
            cur["exported"] = (m.group(2) == "true") if m.group(2) else (m.group(1) or "0") != "0"

    aliases = [e["name"] for e in entries if e.get("kind") == "alias" and e.get("name")]
    acts: list[dict[str, Any]] = []
    for e in entries:
        if e.get("kind") != "activity" or not e.get("name"):
            continue
        name = e["name"]
        if name.startswith("."):
            name = pkg + name
        if any(name.startswith(a + ".") for a in aliases):
            continue  # alias theme variants all target the same activity
        acts.append({"name": name, "simple": short(name), "exported": e.get("exported")})
    return pkg, acts


def apk_abis(apk: Path) -> list[str]:
    """Native ABIs shipped by the APK — surfaces `crashed_abi` before blaming the app."""
    try:
        with zipfile.ZipFile(apk) as z:
            return sorted({n.split("/")[1] for n in z.namelist() if n.startswith("lib/") and n.count("/") > 2})
    except (zipfile.BadZipFile, OSError):
        return []


def build_params(params: dict[str, Any], simple: str) -> tuple[list[str], list[str]]:
    """`am start` args + human notes for one page (the "parameterisable entry" half)."""
    spec = params.get(simple) or {}
    args: list[str] = []
    for field, flag in PARAM_FLAGS:
        for key, value in (spec.get(field) or {}).items():
            args += [flag, key, str(value).lower() if flag == "--ez" else str(value)]
    for raw in spec.get("raw_args") or []:
        args.append(str(raw))
    notes = [spec["pre_state"]] if spec.get("pre_state") else []
    return args, notes


def classify(top: str, simple: str, rejected: bool, crash: Any, adb: Adb,
             trampolines: set[str]) -> tuple[str, str]:
    """(verdict, reason) — typed, never lumped: each failure means something different."""
    if rejected:
        return "rejected", "am start refused (not exported / not found)"
    if not top:
        return "unknown", "no focus readable"
    if f"/{simple}" in top or top.endswith(f".{simple}"):
        return "reached", "landed"
    if crash is not None:
        return ("crashed_abi", crash.exception) if crash.abi else ("crashed_app", crash.exception)
    if adb.is_dialog():
        return "blocked_dialog", top
    if simple in trampolines:
        return "reached_trampoline", f"handoff -> {short(top)}"
    if adb.in_stack(simple):
        return "redirect", f"on top: {short(top)}"
    return "finished", "activity finished itself"


def inventory(
    apk: Path,
    adb: Adb,
    params: dict[str, Any] | None = None,
    settle: float = 3.0,
    trampolines: set[str] | None = None,
    only: set[str] | None = None,
    batch_size: int = 8,
    batch_pause: float = 2.0,
    log: Any = None,
) -> PageGraph:
    """Probe every activity in the APK; returns a PageGraph with method="amstart"."""
    params = params or {}
    trampolines = trampolines or {"SplashActivity"}
    pkg, acts = enumerate_activities(apk)
    if only:
        acts = [a for a in acts if a["simple"] in only]

    adb.wake()
    granted = adb.pre_grant(pkg)
    if log:
        log(f"[A] {apk.name}: package={pkg} activities={len(acts)} pre_granted={len(granted)}")

    graph = PageGraph(side="android", method="amstart", target=f"{pkg} ({apk.name})",
                      device=adb.serial, generated=time.strftime("%Y-%m-%d %H:%M:%S"))

    for i, act in enumerate(acts, 1):
        name, simple = act["name"], act["simple"]
        args, notes = build_params(params, simple)
        adb.pre_grant(pkg) if i % batch_size == 1 else None  # permissions can reappear
        adb.logcat_clear()
        adb.force_stop(pkg)
        time.sleep(0.3)
        t0 = time.time()
        try:
            rejected, _ = adb.start_activity(f"{pkg}/{name}", args)
            top = adb.settle_focus(settle)
            crash = adb.crash(pkg)
            verdict, reason = classify(top, simple, rejected, crash, adb, trampolines)
        except AdbError as exc:  # keep sweeping; one dead row must not end the run
            verdict, reason, top, crash = "unknown", f"adb error: {exc}"[:120], "", None

        page = Page(
            key=name,
            label=simple,
            component=f"{pkg}/{name}",
            verdict=verdict,
            detail=reason,
            side="android",
            extra={
                "exported": act.get("exported"),
                "params": args or None,
                "notes": notes,
                "top_after": top,
                "launch_ms": int((time.time() - t0) * 1000),
                "crash": crash.to_dict() if crash else None,
            },
        )
        graph.add_page(page)
        if log:
            log(f"[A] {i:02d}/{len(acts)} {simple:30s} {verdict:18s} {reason[:40]}")

        if i % batch_size == 0 and i < len(acts):
            time.sleep(batch_pause)  # this host's emulator wedges after ~20-25 cycles

    return graph
