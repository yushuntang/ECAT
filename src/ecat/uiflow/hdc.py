"""OpenHarmony driver (hdc + uitest) behind the same surface as `Adb`.

Same three jobs as the Android side, different transports:

    perceive   `uitest dumpLayout -p <remote>` -> `hdc file recv` -> JSON -> ElementTree
    identify   `aa dump -a` -> `Mission ... [#bundle:entry:EntryAbility]`
    act        `uitest uiInput click|keyEvent`, `aa start`, `aa force-stop`

The tree is materialised with uiautomator's attribute names (`text`,
`content-desc`, `bounds`, `clickable`, `package`) so fingerprinting, node search
and the click walk are shared code, not a second implementation.

Windows trap: `hdc file recv` resolves a native absolute path against its own
cwd and fails with `Error opening file`; run it with cwd=scratch and a bare
filename instead.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from ecat.uiflow.device import DeviceError

TRANSIENT = ("offline", "connect", "timeout", "Failure", "not found", "closed")

#: lock-screen markers: none of these belong to an app under test
LOCK_MARKERS = (
    "ScreenLockRootComponent",
    "Double-tap to activate",
    "Enter your password",
    "Enter PIN",
    "Enter the PIN",
    "Slide to unlock",
    "Draw pattern",
)

#: OH system chrome pollutes the label set; mirrors tools/ui_runner.py
SYS_PREFIX = ("StatusBar", "NavBar", "SCBDesktop", "SwiperPage", "FormItem", "PluginRoot")
_JUNK = re.compile(r"^[\s\d,:.%/-]*$")


class HdcError(DeviceError):
    """hdc failed in a way retrying did not fix."""


def _is_ui_label(text: str) -> bool:
    t = text.strip()
    if not t or len(t) > 60 or _JUNK.match(t):
        return False
    return not any(t.startswith(p) for p in SYS_PREFIX)


class Hdc:
    def __init__(
        self,
        target: str | None = None,
        exe: str | None = None,
        retries: int = 3,
        delay: float = 1.5,
        scratch: Path | None = None,
    ):
        self.exe = exe or os.environ.get("HDC_EXE") or shutil.which("hdc") or "hdc"
        self.retries = retries
        self.delay = delay
        self.scratch = Path(scratch or Path(os.environ.get("TMPDIR", "/tmp"))) / "ecat_uiflow"
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.target = target or self._first_target()
        self.serial = self.target  # parity with Adb.serial

    # ── plumbing ──────────────────────────────────────────────────────────────
    def _first_target(self) -> str:
        out = self._raw(["list", "targets"])
        for line in out.splitlines():
            line = line.strip()
            if line and not line.startswith("["):
                return line
        raise HdcError("no HarmonyOS device — `hdc list targets` is empty")

    def _raw(self, args: list[str], timeout: int = 60, cwd: str | None = None) -> str:
        proc = subprocess.run(
            [self.exe, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd,
        )
        return (proc.stdout or "") + (proc.stderr or "")

    def run(self, *args: str, timeout: int = 60, check: bool = True) -> str:
        last = ""
        for attempt in range(self.retries):
            try:
                out = self._raw(["-t", self.target, *args], timeout=timeout)
            except subprocess.TimeoutExpired:
                last = f"<timeout {timeout}s>"
                time.sleep(self.delay * (attempt + 1))
                continue
            if "[Fail]" not in out and "error:" not in out.lower():
                return out.rstrip("\r\n")
            last = out.strip()
            if not any(t.lower() in out.lower() for t in TRANSIENT):
                break
            time.sleep(self.delay * (attempt + 1))
        if check:
            raise HdcError(f"hdc {' '.join(args[:3])} failed: {last[:300]}")
        return last

    def shell(self, *args: str, timeout: int = 60, check: bool = True) -> str:
        return self.run("shell", *args, timeout=timeout, check=check)

    # ── state ─────────────────────────────────────────────────────────────────
    def focus(self) -> str:
        """`bundle:entry:Ability` of the top mission, "" when unreadable."""
        out = self.shell("aa", "dump", "-a", check=False)
        m = re.search(r"mission name #\[#([^\]]+)\]", out)
        return m.group(1) if m else ""

    def current_package(self) -> str:
        comp = self.focus()
        return comp.split(":", 1)[0] if comp else ""

    def settle_focus(self, settle: float = 3.0) -> str:
        time.sleep(settle)
        top = self.focus()
        for _ in range(3):
            if top:
                break
            time.sleep(1.0)
            top = self.focus()
        return top

    def in_stack(self, needle: str) -> bool:
        return bool(needle) and needle in self.focus()

    # ── control ───────────────────────────────────────────────────────────────
    def wake(self) -> None:
        """Wake and unlock, or fail loudly.

        `uitest keyEvent WakeUp` alone leaves the lock screen up on HarmonyOS hardware;
        `power-shell wakeup` actually lights the panel. If a lock screen is still there
        afterwards (PIN / pattern), stop with a clear message — silently continuing
        "measures" the clock face as if it were a page of the app.
        """
        self.shell("power-shell", "wakeup", check=False)
        time.sleep(0.8)
        if not self._locked():
            return
        self.shell("uitest", "uiInput", "swipe", "640", "2400", "640", "700", "400", check=False)
        time.sleep(1.2)
        if self._locked():
            raise HdcError(
                "OpenHarmony device is locked (PIN/pattern/password). Unlock it on the device "
                "and run again — a walk on a lock screen would record the clock as a page."
            )

    def _locked(self) -> bool:
        try:
            root = self.dump_root()
        except HdcError:
            return False
        labels = {(node.get("text") or node.get("content-desc") or "").strip() for node in root.iter("node")}
        return any(marker in label for label in labels for marker in LOCK_MARKERS)

    def force_stop(self, package: str) -> None:
        self.shell("aa", "force-stop", package, check=False)
        time.sleep(0.4)

    def start_activity(
        self, component: str, extra_args: list[str] | None = None, wait: bool = True
    ) -> tuple[bool, str]:
        """`bundle:entry:Ability` -> `aa start -b bundle -a Ability`."""
        bundle, _, ability = component.partition(":")
        ability = ability.rsplit(":", 1)[-1] or "EntryAbility"
        out = self.shell("aa", "start", "-b", bundle, "-a", ability, check=False)
        rejected = "not found" in out.lower() or "fail" in out.lower()
        return rejected, out

    def tap(self, x: int, y: int) -> None:
        self.shell("uitest", "uiInput", "click", str(int(x)), str(int(y)), check=False)

    def tap_node(self, node: ET.Element) -> None:
        x1, y1, x2, y2 = self._bounds(node)
        self.tap((x1 + x2) // 2, (y1 + y2) // 2)

    def back(self) -> None:
        self.shell("uitest", "uiInput", "keyEvent", "Back", check=False)
        time.sleep(0.4)

    # ── ui tree ───────────────────────────────────────────────────────────────
    def dump_root(self) -> ET.Element:
        remote = "/data/local/tmp/ecat_ui.json"
        local_name = "ecat_ui.json"
        local = self.scratch / local_name
        last = ""
        for _ in range(3):
            self.shell("uitest", "dumpLayout", "-p", remote, check=False)
            local.unlink(missing_ok=True)
            # hdc mangles native absolute paths: run in scratch with a bare name
            self._raw(["-t", self.target, "file", "recv", remote, local_name], cwd=str(self.scratch))
            if local.exists():
                text = local.read_text(encoding="utf-8", errors="ignore")
                if text.lstrip().startswith(("{", "[")):
                    try:
                        return self._json_to_tree(json.loads(text), self.current_package())
                    except json.JSONDecodeError as exc:
                        last = str(exc)
            last = last or "dump not received"
            time.sleep(0.7)
        raise HdcError(f"uitest dumpLayout unusable: {last}")

    def _json_to_tree(self, data: object, package: str) -> ET.Element:
        root = ET.Element("hierarchy")

        def walk(node: object, parent: ET.Element) -> None:
            if isinstance(node, list):
                for item in node:
                    walk(item, parent)
                return
            if not isinstance(node, dict):
                return
            attrs = node.get("attributes") if isinstance(node.get("attributes"), dict) else node
            el = None
            if isinstance(attrs, dict):
                text = str(attrs.get("text") or "").strip()
                desc = str(attrs.get("description") or attrs.get("accessibilityText") or "").strip()
                label = text or desc
                if label and _is_ui_label(label):
                    el = ET.SubElement(
                        parent,
                        "node",
                        {
                            "text": text,
                            "content-desc": desc,
                            "resource-id": str(attrs.get("id") or ""),
                            "bounds": str(attrs.get("bounds") or "[0,0][0,0]"),
                            "clickable": str(attrs.get("clickable")).lower(),
                            "package": str(attrs.get("bundleName") or package),
                            "class": str(attrs.get("type") or ""),
                        },
                    )
            for key in ("children", "childNodes", "nodes"):
                walk(node.get(key), el if el is not None else parent)

        walk(data, root)
        return root

    @staticmethod
    def _bounds(node: ET.Element) -> tuple[int, int, int, int]:
        nums = [int(x) for x in re.findall(r"-?\d+", node.get("bounds", "[0,0][0,0]"))]
        return (nums + [0, 0, 0, 0])[:4]  # type: ignore[return-value]

    @staticmethod
    def area(node: ET.Element) -> int:
        x1, y1, x2, y2 = Hdc._bounds(node)
        return max(0, x2 - x1) * max(0, y2 - y1)

    @staticmethod
    def node_text(node: ET.Element) -> str:
        for attr in ("text", "content-desc", "resource-id"):
            v = (node.get(attr) or "").strip()
            if v:
                return v
        return ""

    def find_node(self, label: str, package: str | None = None) -> ET.Element | None:
        root = self.dump_root()
        target = _norm(label)
        contains = None
        for node in root.iter("node"):
            text = self.node_text(node)
            if not text:
                continue
            if _norm(text) == target:
                return node
            if contains is None and target and target in _norm(text):
                contains = node
        return contains

    # ── diagnostics ───────────────────────────────────────────────────────────
    def logcat_clear(self) -> None:
        return None

    def recent_starts(self) -> list[str]:
        """Not implemented for OH (no ActivityTaskManager equivalent) — edges come from actions."""
        return []

    def crash(self, package: str) -> None:
        """OH crashes are reported through FaultLoggerd/hilog; not wired up yet."""
        return None

    def pre_grant(self, package: str) -> list[str]:
        return []

    def is_dialog(self) -> bool:
        return False


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (text or "").lower())
