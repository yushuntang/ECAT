"""Layer B discovery — DroidBot as the page-discovery engine, with its traps fixed.

DroidBot is the only crawler in the comparison that exports a UI transition graph
without app instrumentation, so it backs the "which pages exist at all" half of
Layer B. Hand-written flows stay the reproducible *denominator*; this module is for
finding the pages nobody thought to write a flow for.

Three failures were hit for real while wiring it up, each handled here:

1. **`import start` fails** when DroidBot is installed from PyPI (`droidbot/__init__.py`
   line 1). Fixed by running the *checkout* (`python start.py`) with `cwd=<checkout>`,
   never the installed console script.
2. **`ModuleNotFoundError: pkg_resources`** — setuptools >= 81 dropped it. Detected up
   front with an actionable message instead of a traceback in the middle of a run.
3. **"Please enable accessibility for DroidBot app manually."** forever: DroidBot's own
   `-accessibility_auto` writes the secure setting but its readiness check parses
   `dumpsys activity services`, which on newer Android only lists the service once it
   is genuinely bound. The secure setting is written here, explicitly, then verified.

Also on retry duty: DroidBot has zero tolerance for a transient adb failure (one
`getprop` blip ends the run with `AttributeError: 'NoneType' object has no attribute
'tear_down'`), so the device is pre-flighted and the whole exploration is retried.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ecat.uiflow.device import Adb, DeviceError
from ecat.uiflow.models import Edge, Page, PageGraph, short

#: package DroidBot installs on the device to expose the view tree
DROIDBOT_APP_PACKAGES = ("io.github.ylimit.droidbotapp", "io.github.privacystreams.droidbot")

TRANSIENT_LOG = (
    "tear_down",
    "returned non-zero exit status",
    "device offline",
    "protocol fault",
    "connection reset",
)


class DroidBotError(RuntimeError):
    """DroidBot could not be located, prepared, or completed a run."""


@dataclass
class DroidBotRun:
    out_dir: Path
    framework: dict[str, Any]
    attempts: int
    log_tail: str

    @property
    def graph(self) -> PageGraph:
        return graph_from_utg(self.framework)


# ── locating a checkout ───────────────────────────────────────────────────────
def _fallback_homes() -> list[Path]:
    """Where a checkout is looked for when neither --droidbot-home nor the env var is set."""
    return [
        Path.cwd() / "third_party" / "droidbot",
        Path.cwd() / "droidbot",
        Path.home() / ".cache" / "ecat" / "droidbot",
    ]


def find_checkout(home: str | Path | None = None) -> Path:
    """A DroidBot checkout (not a pip install) — `start.py` must exist beside `droidbot/`."""
    candidates: list[Path] = []
    if home:
        candidates.append(Path(home))
    env = os.environ.get("ECAT_DROIDBOT_HOME")
    if env:
        candidates.append(Path(env))
    candidates += _fallback_homes()
    for path in candidates:
        if (path / "start.py").is_file() and (path / "droidbot" / "__init__.py").is_file():
            return path
    raise DroidBotError(
        "DroidBot checkout not found. Point --droidbot-home (or ECAT_DROIDBOT_HOME) at one:\n"
        "  git clone --depth 1 https://github.com/honeynet/droidbot <path>\n"
        "  <python> -m pip install -r <path>/requirements.txt  # any interpreter works\n"
        "A PyPI install is not enough: `droidbot/__init__.py` does `import start`, which only "
        "resolves when the checkout root is the working directory."
    )


def _check_interpreter(python: str, checkout: Path) -> None:
    """Fail with the fix, not with a traceback 40 s into a run."""
    probe = subprocess.run(
        [python, "-c", "import pkg_resources, networkx"],
        capture_output=True,
        text=True,
        cwd=str(checkout),
    )
    if probe.returncode == 0:
        return
    err = (probe.stderr or probe.stdout or "").strip()
    if "pkg_resources" in err:
        raise DroidBotError(
            f"'{python}' has setuptools>=81, which dropped pkg_resources (DroidBot imports it).\n"
            f"  {python} -m pip install 'setuptools<81'"
        )
    raise DroidBotError(
        f"'{python}' is missing DroidBot's dependencies ({err.splitlines()[-1] if err else 'unknown'}).\n"
        f"  {python} -m pip install -r {checkout / 'requirements.txt'}"
    )


# ── device preparation ────────────────────────────────────────────────────────
def enable_accessibility(adb: Adb, app_apk: Path | None = None) -> str | None:
    """Enable DroidBot's accessibility service and wait until it is really bound.

    The install happens here, before the secure setting is written: DroidBot's own
    ordering (install inside set_up, then enable) is what left the service unbound on
    newer Android. Returns None only when the app is neither installed nor installable.
    """
    package = ""
    for candidate in DROIDBOT_APP_PACKAGES:
        if adb.shell("pm", "list", "packages", candidate, check=False).strip():
            package = candidate
            break
    if not package and app_apk is not None and app_apk.is_file():
        adb.run("install", "-r", str(app_apk), timeout=180, check=False)
        for candidate in DROIDBOT_APP_PACKAGES:
            if adb.shell("pm", "list", "packages", candidate, check=False).strip():
                package = candidate
                break
    if not package:
        return None

    dump = adb.shell("dumpsys", "package", package, check=False)
    services = re.findall(rf"({re.escape(package)}/[\w.$]*[Aa]ccessibility[\w.$]*)", dump)
    if not services:
        return None
    component = services[0]

    adb.shell("settings", "put", "secure", "enabled_accessibility_services", component, check=False)
    adb.shell("settings", "put", "secure", "accessibility_enabled", "1", check=False)

    for _ in range(10):  # the service only shows up in dumpsys once the system binds it
        bound = adb.shell("dumpsys", "activity", "services", check=False)
        if component.split("/")[-1] in bound:
            return str(component)
        time.sleep(1.0)
    return None


def _preflight(adb: Adb) -> None:
    """A single adb blip is enough to kill DroidBot mid-run: prove the device answers first."""
    for attempt in range(3):
        try:
            if adb.shell("echo", "ok", check=False).strip() == "ok":
                adb.wake()
                return
        except DeviceError:
            pass
        time.sleep(1.5 * (attempt + 1))
    raise DroidBotError(f"device {adb.serial} is not answering adb")


# ── running ───────────────────────────────────────────────────────────────────
def run_droidbot(
    apk: Path,
    serial: str,
    out_dir: Path,
    *,
    home: str | Path | None = None,
    python: str | None = None,
    policy: str = "bfs_greedy",
    count: int = 30,
    timeout: int = 300,
    tries: int = 3,
    extra_args: tuple[str, ...] = (),
    log: Any = None,
    adb: Adb | None = None,
) -> DroidBotRun:
    checkout = find_checkout(home)
    interpreter = python or os.environ.get("ECAT_DROIDBOT_PYTHON") or sys.executable
    _check_interpreter(interpreter, checkout)

    adb = adb or Adb(serial)
    _preflight(adb)
    component = enable_accessibility(adb, checkout / "droidbot" / "resources" / "droidbotApp.apk")
    if log:
        log(f"[discover] droidbot checkout={checkout}")
        log(f"[discover] accessibility={'enabled ' + component if component else 'not installed yet'}")

    last_log = ""
    for attempt in range(1, tries + 1):
        if out_dir.exists():
            shutil.rmtree(out_dir)
        cmd = [
            interpreter,
            "start.py",
            "-a",
            str(apk.resolve()),
            "-o",
            str(out_dir.resolve()),
            "-d",
            serial,
            "-policy",
            policy,
            "-count",
            str(count),
            "-timeout",
            str(timeout),
            "-grant_perm",
            "-is_emulator",
            "-keep_app",
            *extra_args,
        ]
        if log:
            log(f"[discover] attempt {attempt}/{tries}: {' '.join(cmd[1:6])} …")
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(checkout),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout + 180,
            )
        except subprocess.TimeoutExpired:
            last_log = "subprocess timeout"
            continue
        last_log = (proc.stdout or "") + (proc.stderr or "")
        utg_path = out_dir / "utg.js"
        if utg_path.is_file():
            framework = parse_utg(utg_path)
            if framework.get("num_nodes"):
                return DroidBotRun(out_dir=out_dir, framework=framework, attempts=attempt,
                                   log_tail=last_log[-2000:])
        if not any(marker in last_log for marker in TRANSIENT_LOG):
            break
        if log:
            log(f"[discover] attempt {attempt} died on a transient fault, retrying")
        time.sleep(2.0 * attempt)

    raise DroidBotError(
        f"DroidBot produced no graph in {tries} attempt(s). Tail:\n{last_log[-800:]}"
    )


# ── parsing ───────────────────────────────────────────────────────────────────
def parse_utg(path: Path) -> dict[str, Any]:
    """`utg.js` is `var utg = {…}` — a JSON object behind a JS assignment."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    body = text.split("var utg", 1)[-1]
    body = body.split("=", 1)[-1].strip() if "=" in body else body.strip()
    if body.startswith("="):
        body = body[1:]
    parsed: dict[str, Any] = json.loads(body)
    return parsed


def graph_from_utg(framework: dict[str, Any], *, include_external: bool = False) -> PageGraph:
    """UTG -> PageGraph. States outside the app under test are dropped by default."""
    app_package = framework.get("app_package") or ""
    graph = PageGraph(
        side="android",
        method="click",  # DroidBot only moves the UI with real input events
        target=f"{app_package} (@{framework.get('app_main_activity', '')})",
        device=framework.get("device_serial", ""),
        generated=framework.get("test_date", ""),
        engine="droidbot",
    )

    kept: set[str] = set()
    for node in framework.get("nodes") or []:
        package = node.get("package") or ""
        if not include_external and app_package and package != app_package:
            continue
        activity = str(node.get("activity") or "")
        structure = str(node.get("structure_str") or "")
        component = f"{package}/{activity}" if package else activity
        page = Page(
            key=str(node.get("id")),
            label=f"{short(activity) or 'Screen'}[${structure[:6]}]",
            component=component,
            verdict="reached",
            side="android",
            extra={
                "activity": activity,
                "structure": structure,
                "screenshot": node.get("image"),
                "external": bool(app_package and package != app_package),
            },
        )
        graph.add_page(page)
        kept.add(page.key)

    for edge in framework.get("edges") or []:
        src, dst = str(edge.get("from")), str(edge.get("to"))
        if src not in kept or dst not in kept:
            continue
        actions = [
            str((ev or {}).get("event_str") or (ev or {}).get("event_type") or "event")
            for ev in edge.get("events") or []
        ]
        graph.add_edge(Edge(src=src, dst=dst, action=" | ".join(actions)[:160], source="click"))
    return graph


def summary_line(run: DroidBotRun) -> str:
    g = graph_from_utg(run.framework)
    fw = run.framework
    return (
        f"droidbot: {g.summary()['countable']} in-app page(s), {len(g.edges)} edge(s), "
        f"{fw.get('num_reached_activities')} activit(y/ies) reached, "
        f"{fw.get('time_spent', 0):.0f}s, attempt {run.attempts}"
    )
