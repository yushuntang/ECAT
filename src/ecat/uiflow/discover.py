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

import hashlib
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
from ecat.uiflow.models import Edge, Page, PageGraph, fingerprint_texts, short

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
    required = ("pkg_resources", "networkx", "androguard")
    probe = subprocess.run(
        [python, "-c", "import importlib.util, json, sys\n"
         "missing = [m for m in sys.argv[1:] if not importlib.util.find_spec(m)]\n"
         "print(json.dumps(missing))", *required],
        capture_output=True,
        text=True,
        cwd=str(checkout),
    )
    if probe.returncode == 0 and not (probe.stdout or "").strip().strip("[]"):
        return
    err = (probe.stderr or probe.stdout or "").strip()
    if "androguard" in err:
        raise DroidBotError(
            f"'{python}' is missing androguard, which DroidBot imports to read the APK "
            "(a run would fail after three attempts):\n"
            f"  {python} -m pip install -r {checkout / 'requirements.txt'}"
        )
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
def droidbot_cmd(
    interpreter: str,
    apk: Path,
    out_dir: Path,
    serial: str,
    *,
    policy: str = "bfs_greedy",
    count: int = 30,
    timeout: int = 300,
    start_activity: str = "",
    extra_args: tuple[str, ...] = (),
) -> list[str]:
    """The DroidBot command line, isolated so the flag plumbing can be tested without a device."""
    return [
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
        *(["-start_activity", start_activity] if start_activity else []),
        *extra_args,
    ]


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
    start_activity: str = "",
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
        cmd = droidbot_cmd(interpreter, apk, out_dir, serial, policy=policy, count=count,
                           timeout=timeout, start_activity=start_activity, extra_args=extra_args)
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
            framework.setdefault("policy", policy)
            framework.setdefault("count", count)
            if Path(apk).is_file():
                framework.setdefault("app_sha256", file_sha256(apk))
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
def file_sha256(path: Path | str) -> str:
    """Content hash of the APK — the identity a baseline is pinned to."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def merge_graphs(graphs: list[Any], *, side: str = "android") -> Any:
    """Union several explorations into one denominator.

    One DroidBot round is a sample: 30 events reached 2 of 18 activities here, and a second round
    reached a different page. Pages are keyed by the content-free structure (so the same screen
    showing another row is still one page); edges are keyed by (src, dst, action).
    """
    from ecat.uiflow.models import Edge, Page, PageGraph

    merged = PageGraph(side=side, method="click", engine="discovery-union")
    sha, target = "", ""
    for graph in graphs:
        sha = sha or str((graph.stats or {}).get("app_sha256") or "")
        target = target or str(graph.target or "")
        for page in graph.pages:
            structure = str((page.extra or {}).get("structure") or "").strip().lower()
            key = f"structure:{structure}" if structure else page.key
            existing = next((p for p in merged.pages if p.key == key), None)
            if existing is None:
                merged.pages.append(Page(
                    key=key,
                    label=page.label,
                    component=page.component,
                    verdict=page.verdict,
                    detail=page.detail,
                    side=page.side,
                    extra=dict(page.extra or {}),
                ))
            else:
                # keep the first label, but remember every state we saw of this layout
                seen = set(existing.extra.get("labels") or [])
                seen.add(page.label)
                existing.extra["labels"] = sorted(seen)

    edge_keys = {(e.src, e.dst, e.action) for e in merged.edges}
    for graph in graphs:
        for edge in graph.edges:
            key = (edge.src, edge.dst, edge.action)
            if key not in edge_keys:
                edge_keys.add(key)
                merged.edges.append(Edge(src=edge.src, dst=edge.dst, action=edge.action,
                                         source=edge.source))

    merged.target = target
    merged.stats = {
        "app_sha256": sha,
        "rounds": len(graphs),
        "pages": len(merged.pages),
        "edges": len(merged.edges),
    }
    return merged


def activity_coverage(
    graph: Any, activities: list[dict[str, Any]], package: str = ""
) -> dict[str, Any]:
    """How much of the manifest's activity table layer B actually reached.

    The FN denominator is B's page set, so an exploration that never reached half the app produces
    a small denominator and therefore a flattering FN. This turns that into a number instead of a
    suspicion: activities the manifest has that no discovered page maps to.
    """
    def _simple(value: str) -> str:
        """`com.demo.MainActivity`, `.MainActivity` and `com.demo/.MainActivity` are one name."""
        tail = value.strip().replace("\\", "/").rsplit("/", 1)[-1]
        return tail.rsplit(".", 1)[-1].lower()

    seen: set[str] = set()
    for page in graph.pages:
        extra = page.extra or {}
        for key in ("activity", "component"):
            value = str(extra.get(key) or "")
            if value:
                seen.add(_simple(value))
        seen.add(_simple(str(page.component or "")))
        seen.add(_simple(str(page.label or "").split("[", 1)[0]))

    wanted = {
        str(a.get("simple") or "").lower(): str(a.get("name") or "")
        for a in activities
        if a.get("simple")
    }
    missing = sorted(simple for simple in wanted if simple and simple not in seen)

    # library activities (ExoPlayer, commons' License/ManageBlockedNumbers…) are pages that exist
    # but are not product flow: counting them makes coverage look catastrophic for the wrong reason
    in_pkg = {s: n for s, n in wanted.items() if not package or n.startswith(package)}
    lib = {s: n for s, n in wanted.items() if s not in in_pkg}
    base = in_pkg or wanted
    seen_base = [s for s in base if s in seen]
    return {
        "activities_total": len(wanted),
        "activities_seen": len(wanted) - len(missing),
        "coverage": round((len(wanted) - len(missing)) / len(wanted), 4) if wanted else 0.0,
        "app_activities_total": len(base),
        "app_activities_seen": len(seen_base),
        "app_coverage": round(len(seen_base) / len(base), 4) if base else 0.0,
        "library_activities": len(lib),
        "never_reached": [base[s] for s in sorted(base) if s not in seen],
        "never_reached_library": [lib[s] for s in sorted(lib) if s not in seen],
    }


def baseline_path(baseline_dir: Path | str, app_sha256: str) -> Path:
    return Path(baseline_dir) / f"{app_sha256}.utg.js"


def load_baseline(baseline_dir: Path | str | None, app_sha256: str) -> Path | None:
    """An exploration pinned to this exact APK build, if one was stored before."""
    if not baseline_dir or not app_sha256:
        return None
    candidate = baseline_path(baseline_dir, app_sha256)
    return candidate if candidate.is_file() else None


def store_baseline(utg_path: Path, baseline_dir: Path | str, app_sha256: str) -> Path:
    """Freeze this exploration so the next run on the same build is reproducible."""
    target = baseline_path(baseline_dir, app_sha256)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(utg_path, target)
    return target


def parse_utg(path: Path) -> dict[str, Any]:
    """`utg.js` is `var utg = {…}`; ECAT's own `*.utg.json` snapshot is plain JSON.

    Both forms are accepted. The JS prefix is stripped only when it is really there —
    splitting on the first `=` in a plain JSON file truncates it (any `=` inside a
    captured text used to do exactly that on our own snapshots).
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace").lstrip()
    body = text.split("=", 1)[1].strip() if text.startswith("var utg") and "=" in text else text
    parsed: dict[str, Any] = json.loads(body)
    return parsed


#: a view id, e.g. `android:id/content` or `com.x:id/list` — never a user-visible label
_RESOURCE_ID = re.compile(r"[\w.]+:id/")

#: the state hash DroidBot prints as the third header line when `state_str` is absent
_STATE_HASH = re.compile(r"^[0-9a-f]{8,}$")


def visible_texts(node: dict[str, Any]) -> list[str]:
    """The texts DroidBot recorded for one state, from the UTG's own `content` field.

    `content` = `<package>\n<activity>\n<state hash>` followed by one comma-joined
    block per view. The header lines are recognised from the node's own fields (not
    from a fixed line count), so an empty state really comes back empty instead of
    reporting its own package name as a label. Engine-agnostic by construction: it
    reads DroidBot's output format, so no app needs to be known here.
    """
    lines = str(node.get("content") or "").splitlines()
    header = {
        str(node.get(field) or "").strip()
        for field in ("package", "activity", "state_str", "id")
    }
    while lines and (lines[0].strip() in header or _STATE_HASH.match(lines[0].strip())):
        lines.pop(0)
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        for part in line.split(","):
            text = part.strip()
            if not text or _RESOURCE_ID.search(text) or text in seen:
                continue
            seen.add(text)
            out.append(text)
    return out


def _bounds_center(bounds: Any) -> tuple[int, int] | None:
    """Centre of a view's bounds — DroidBot stores `[[x1,y1],[x2,y2]]`, uitest uses "[x1,y1][x2,y2]"."""
    if isinstance(bounds, (list, tuple)) and len(bounds) >= 2:
        try:
            (x1, y1), (x2, y2) = bounds[0], bounds[1]
            x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        except (TypeError, ValueError):
            return None
    elif isinstance(bounds, str):
        import re as _re

        nums = _re.findall(r"-?\d+", bounds)
        if len(nums) < 4:
            return None
        x1, y1, x2, y2 = (int(n) for n in nums[:4])
    else:
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1 + x2) // 2, (y1 + y2) // 2


def state_geometry(run_dir: Path | str) -> dict[str, dict[str, Any]]:
    """`state_str -> {size: [w,h], elements: [[class, x, y], …]}` from a DroidBot run."""
    states_dir = Path(run_dir) / "states"
    out: dict[str, dict[str, Any]] = {}
    if not states_dir.is_dir():
        return out
    for path in sorted(states_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        state = str(payload.get("state_str") or "")
        views = payload.get("views") or []
        width, height = payload.get("width"), payload.get("height")
        if not state or not isinstance(views, list) or not width or not height:
            continue
        elements: list[list[Any]] = []
        for view in views:
            if not isinstance(view, dict) or view.get("visible") is False:
                continue
            centre = _bounds_center(view.get("bounds"))
            name = str(view.get("class") or "").rsplit(".", 1)[-1].strip()
            if centre and name:
                elements.append([name, centre[0], centre[1]])
        if elements:
            out[state] = {"size": [int(width), int(height)], "elements": elements[:80]}
    return out


def state_type_histograms(run_dir: Path | str) -> dict[str, dict[str, int]]:
    """`state_str -> {component class: count}` from a DroidBot run's saved states.

    The UTG only carries ids and hashes, so the component classes live here. Short class names
    ("TextView", "Button") are used because the platforms name them differently at the prefix level
    (android.widget.TextView vs Text) and any structural comparison has to survive that.
    """
    states_dir = Path(run_dir) / "states"
    out: dict[str, dict[str, int]] = {}
    if not states_dir.is_dir():
        return out
    for path in sorted(states_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        state = str(payload.get("state_str") or "")
        views = payload.get("views") or []
        if not state or not isinstance(views, list):
            continue
        counts: dict[str, int] = {}
        for view in views:
            if not isinstance(view, dict) or view.get("visible") is False:
                continue
            name = str(view.get("class") or "").rsplit(".", 1)[-1].strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
        if counts:
            out[state] = dict(sorted(counts.items(), key=lambda kv: -kv[1])[:12])
    return out


def graph_from_utg(
    framework: dict[str, Any],
    *,
    include_external: bool = False,
    types_by_state: dict[str, dict[str, int]] | None = None,
    geometry_by_state: dict[str, dict[str, Any]] | None = None,
) -> PageGraph:
    """UTG -> PageGraph. States outside the app under test are dropped by default.

    Page identity comes from the **visible texts** (`fingerprint_texts`), not from
    DroidBot's `structure_str`: structure ignores text, so every detail page of a
    list app collapses into one hash while a screen visited twice keeps two nodes.
    A state with no text at all is recorded as `empty_frame` — the start-up frame
    between two screens is not a page, and counting it inflates the denominator.
    """
    app_package = framework.get("app_package") or ""
    graph = PageGraph(
        side="android",
        method="click",  # DroidBot only moves the UI with real input events
        target=f"{app_package} (@{framework.get('app_main_activity', '')})",
        device=framework.get("device_serial", ""),
        generated=framework.get("test_date", ""),
        engine="droidbot",
    )
    graph.stats.update(
        {
            "app_sha256": str(framework.get("app_sha256") or ""),
            "policy": str(framework.get("policy") or ""),
            "count": framework.get("count"),
            "explored_at": str(framework.get("test_date") or ""),
            "num_nodes": framework.get("num_nodes"),
            "num_edges": framework.get("num_edges"),
        }
    )

    node_key: dict[str, str] = {}
    for node in framework.get("nodes") or []:
        package = node.get("package") or ""
        if not include_external and app_package and package != app_package:
            continue
        activity = str(node.get("activity") or "")
        component = f"{package}/{activity}" if package else activity
        texts = visible_texts(node)
        if texts:
            digest, label = fingerprint_texts(texts, activity)
            verdict, detail = "reached", ""
        else:
            digest = str(node.get("structure_str") or node.get("id") or "empty")[:10]
            label = f"{short(activity) or 'Screen'}[${digest}]"
            verdict, detail = "empty_frame", "no visible text — start-up frame, not a page"
        page = graph.add_page(
            Page(
                key=f"{component}#{digest}",
                label=label,
                component=component,
                verdict=verdict,
                detail=detail,
                side="android",
                extra={
                    "activity": activity,
                    "structure": str(node.get("structure_str") or ""),
                    "screenshot": node.get("image"),
                    "external": bool(app_package and package != app_package),
                    "texts": texts[:8],
                    **(
                        {"types": (types_by_state or {})[str(node.get("state_str"))]}
                        if (types_by_state or {}).get(str(node.get("state_str")))
                        else {}
                    ),
                    **(
                        {"geometry": (geometry_by_state or {})[str(node.get("state_str"))]}
                        if (geometry_by_state or {}).get(str(node.get("state_str")))
                        else {}
                    ),
                },
            )
        )
        node_key[str(node.get("id"))] = page.key

    for edge in framework.get("edges") or []:
        src = node_key.get(str(edge.get("from")))
        dst = node_key.get(str(edge.get("to")))
        if not src or not dst:
            continue
        actions = [
            str((ev or {}).get("event_str") or (ev or {}).get("event_type") or "event")
            for ev in edge.get("events") or []
        ]
        graph.add_edge(Edge(src=src, dst=dst, action=" | ".join(actions)[:160], source="click"))
    return graph


def summary_line(run: DroidBotRun) -> str:
    g = graph_from_utg(run.framework, types_by_state=state_type_histograms(run.out_dir))
    fw = run.framework
    return (
        f"droidbot: {g.summary()['countable']} in-app page(s), {len(g.edges)} edge(s), "
        f"{fw.get('num_reached_activities')} activit(y/ies) reached, "
        f"{fw.get('time_spent', 0):.0f}s, attempt {run.attempts}"
    )
