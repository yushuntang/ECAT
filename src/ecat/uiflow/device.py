"""Thin, fault-tolerant adb wrapper.

Lessons baked in (each cost a run):

* **Retry transient adb failures.** One `getprop` hiccup is enough to kill a
  DroidBot run with `AttributeError: 'NoneType' object has no attribute
  'tear_down'`; every helper here retries 3x with backoff.
* **Assert on `dumpsys window mCurrentFocus`, not `dumpsys activity top`.**
  After a hard emulator kill the latter reports a stale `TASK null id=2`
  (launcher) while the app is genuinely on screen -> every row becomes a false
  "not reached".
* **Settle before deciding.** An activity can render and then finish itself — it logs
  `Displayed ... +897ms`, throws nothing, and leaves the launcher on top — so "on top"
  is only meaningful a few seconds later.
"""

from __future__ import annotations

import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

#: substrings that mean "the adb connection blipped", not "the command is wrong"
TRANSIENT = (
    "device offline",
    "device not found",
    "protocol fault",
    "connection reset",
    "error: closed",
    "no devices/emulators found",
    "adb server",
    "cannot connect",
)

DIALOG_HINTS = ("permissioncontroller", "GrantPermissions", "com.android.systemui", "com.google.android.permissioncontroller")


class DeviceError(RuntimeError):
    """A device transport failed in a way retrying did not fix."""


class AdbError(DeviceError):
    """adb failed in a way retrying did not fix."""


@dataclass
class Crash:
    component: str
    exception: str
    abi: bool
    caused_by: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "component": self.component,
            "exception": self.exception,
            "caused_by": self.caused_by,
            "abi": self.abi,
        }


class Adb:
    def __init__(self, serial: str, retries: int = 3, delay: float = 1.5, timeout: int = 60):
        self.serial = serial
        self.retries = retries
        self.delay = delay
        self.timeout = timeout

    # ── plumbing ──────────────────────────────────────────────────────────────
    def run(self, *args: str, timeout: int | None = None, check: bool = True) -> str:
        cmd = ["adb", "-s", self.serial, *args]
        last = ""
        for attempt in range(self.retries):
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout or self.timeout,
                )
            except subprocess.TimeoutExpired:
                last = f"<timeout after {timeout or self.timeout}s>: {' '.join(args[:3])}"
                time.sleep(self.delay * (attempt + 1))
                continue
            out = (proc.stdout or "") + (proc.stderr or "")
            if proc.returncode == 0:
                return out.rstrip("\r\n")
            last = out.strip() or f"exit {proc.returncode}"
            if not any(t in last.lower() for t in TRANSIENT):
                break
            time.sleep(self.delay * (attempt + 1))
        if check:
            raise AdbError(f"adb {' '.join(args[:4])} failed: {last[:300]}")
        return last

    def shell(self, *args: str, timeout: int | None = None, check: bool = True) -> str:
        return self.run("shell", *args, timeout=timeout, check=check)

    # ── state ─────────────────────────────────────────────────────────────────
    def focus(self) -> str:
        """`pkg/activity` currently focused, or "" if nothing readable."""
        out = self.shell("dumpsys", "window", timeout=30, check=False)
        m = re.search(r"mCurrentFocus=Window\{\S+ u\d+ ([\w.]+)/([\w.$]+)", out)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
        out = self.shell("dumpsys", "activity", "activities", timeout=40, check=False)
        for line in out.splitlines():
            if "topResumedActivity" in line or "mResumedActivity" in line:
                m = re.search(r"(?:topResumedActivity|mResumedActivity)=ActivityRecord\{\S+ u\d+ ([\w./]+)", line)
                if m:
                    return m.group(1)
        return ""

    def in_stack(self, needle: str) -> bool:
        out = self.shell("dumpsys", "activity", "activities", timeout=40, check=False)
        return needle in out

    def settle_focus(self, settle: float = 3.0) -> str:
        """Focus read *after* `settle` seconds — the only honest reachability check."""
        time.sleep(settle)
        top = self.focus()
        for _ in range(3):
            if top:
                break
            time.sleep(1.0)
            top = self.focus()
        return top

    # ── control ───────────────────────────────────────────────────────────────
    def wake(self) -> None:
        self.shell("input", "keyevent", "KEYCODE_WAKEUP", check=False)
        self.shell("wm", "dismiss-keyguard", check=False)

    def force_stop(self, package: str) -> None:
        self.shell("am", "force-stop", package, timeout=45, check=False)

    def start_activity(
        self, component: str, extra_args: list[str] | None = None, wait: bool = True
    ) -> tuple[bool, str]:
        """(rejected?, raw output). Rejected == permission/exported refusal."""
        args = ["am", "start"]
        if wait:
            args.append("-W")
        args += ["-n", component, *(extra_args or [])]
        out = self.shell(*args, timeout=60, check=False)
        rejected = any(
            k in out for k in ("Permission Denial", "not exported", "Error type", "does not exist")
        )
        return rejected, out

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(x), str(y), check=False)

    def tap_node(self, node: ET.Element) -> None:
        x1, y1, x2, y2 = self._bounds(node)
        self.tap((x1 + x2) // 2, (y1 + y2) // 2)

    def back(self) -> None:
        self.shell("input", "keyevent", "KEYCODE_BACK", check=False)

    # ── ui tree ───────────────────────────────────────────────────────────────
    def dump_root(self) -> ET.Element:
        """Current UI hierarchy. Dump to the device, pull it, parse it."""
        last = ""
        for _ in range(3):
            self.shell("uiautomator", "dump", "--compressed", "/sdcard/ecat_ui.xml", check=False)
            xml = self.run("exec-out", "cat", "/sdcard/ecat_ui.xml", check=False)
            if xml.lstrip().startswith("<"):
                try:
                    return ET.fromstring(xml)
                except ET.ParseError as exc:  # truncated pull
                    last = str(exc)
            time.sleep(0.7)
        raise AdbError(f"uiautomator dump unusable: {last}")

    @staticmethod
    def _bounds(node: ET.Element) -> tuple[int, int, int, int]:
        nums = [int(x) for x in re.findall(r"-?\d+", node.get("bounds", "[0,0][0,0]"))]
        return (nums + [0, 0, 0, 0])[:4]  # type: ignore[return-value]

    @classmethod
    def area(cls, node: ET.Element) -> int:
        x1, y1, x2, y2 = cls._bounds(node)
        return max(0, x2 - x1) * max(0, y2 - y1)

    @staticmethod
    def node_text(node: ET.Element) -> str:
        for attr in ("text", "content-desc", "resource-id"):
            v = (node.get(attr) or "").strip()
            if v:
                return v
        return ""

    def find_node(self, label: str, package: str | None = None) -> ET.Element | None:
        """First node whose text/content-desc equals (or contains) `label`."""
        root = self.dump_root()
        target = norm_label(label)
        contains = None
        for node in root.iter("node"):
            if package and node.get("package") not in (None, package):
                continue
            text = self.node_text(node)
            if not text:
                continue
            if norm_label(text) == target:
                return node
            if contains is None and target and target in norm_label(text):
                contains = node
        return contains

    # ── diagnostics ───────────────────────────────────────────────────────────
    def logcat_clear(self) -> None:
        self.shell("logcat", "-c", check=False)

    def recent_starts(self) -> list[str]:
        """`cmp=` targets of every `ActivityTaskManager: START` in the buffer."""
        out = self.shell("logcat", "-d", "-t", "4000", timeout=40, check=False)
        found: list[str] = []
        for m in re.finditer(r"START u0 \{[^}]*cmp=([^\s}]+)", out):
            comp = m.group(1)
            if not found or found[-1] != comp:
                found.append(comp)
        return found

    def crash(self, package: str) -> Crash | None:
        """Last FATAL EXCEPTION attributed to this package (crash buffer)."""
        out = self.run("logcat", "-d", "-b", "crash", "-t", "120", check=False)
        if "FATAL EXCEPTION" not in out:
            return None
        tail = "FATAL EXCEPTION" + out.split("FATAL EXCEPTION")[-1]
        if package not in tail:
            return None
        m = re.search(r"ComponentInfo\{[\w.]+/([\w.$]+)\}", tail)
        lines = [
            line.split("AndroidRuntime: ", 1)[-1]
            for line in tail.splitlines()
            if "AndroidRuntime" in line
        ]
        exc = next((line.strip() for line in lines if re.match(r"^[\w.]*(Exception|Error)", line.strip())), "")
        caused = [line.strip() for line in lines if line.strip().startswith("Caused by:")][:2]
        abi = any("UnsatisfiedLinkError" in line or 'library "' in line for line in lines)
        return Crash(component=m.group(1) if m else "", exception=exc, abi=abi, caused_by=caused)

    def pre_grant(self, package: str) -> list[str]:
        """Grant every declared-but-ungranted runtime permission.

        Skip this and the first probe lands on GrantPermissionsActivity, which
        looks like an unreachable page but is really a harness gap.
        """
        out = self.shell("dumpsys", "package", package, timeout=60, check=False)
        perms = sorted(set(re.findall(r"^\s+(android\.permission\.\w+): granted=false", out, re.M)))
        for perm in perms:
            self.shell("pm", "grant", package, perm, check=False)
        return perms

    def current_package(self) -> str:
        comp = self.focus()
        return comp.split("/", 1)[0] if "/" in comp else ""

    def is_dialog(self) -> bool:
        comp = self.focus()
        return any(h in comp for h in DIALOG_HINTS)


def norm_label(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())
