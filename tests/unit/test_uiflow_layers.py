"""Layer A tests + one end-to-end chain test.

Layer A (`inventory`) was the only module in uiflow with zero tests, because it looked like it needed
a device. It does not: `Adb` is just an object with nine tiny methods, so a fake can replay exactly
what a phone would have said — including the awkward cases (`am start` refused, activity finishes
itself, crash with a missing ABI, adb dying mid-sweep).

The second half goes the whole way: A-graph (amstart) is refused as a denominator, B-graph (click) +
C-graph (flow report) + spec produce an FN, and the verdict carries it. That is the integration test
the individual modules never had.
"""

from __future__ import annotations

import json

import pytest

from ecat.uiflow import inventory as inv
from ecat.uiflow.device import AdbError, Crash

# ── a phone that answers from a script ───────────────────────────────────────

class FakeAdb:
    serial = "fake-1"

    def __init__(self, responses: dict[str, dict] | None = None, fail_on: set[str] | None = None):
        self.responses = responses or {}
        self.fail_on = fail_on or set()
        self.started: list[tuple[str, list[str]]] = []
        self.stopped: list[str] = []

    def wake(self) -> None:
        return None

    def pre_grant(self, package: str) -> list[str]:
        return ["android.permission.READ_MEDIA_IMAGES"]

    def logcat_clear(self) -> None:
        return None

    def force_stop(self, package: str) -> None:
        self.stopped.append(package)

    def start_activity(self, component: str, args: list[str] | None = None) -> tuple[bool, str]:
        self.started.append((component, list(args or [])))
        simple = component.split("/")[-1].rsplit(".", 1)[-1]
        if simple in self.fail_on:
            raise AdbError("device offline")
        spec = self.responses.get(simple, {"top": "", "rejected": False})
        return bool(spec.get("rejected")), ""

    def settle_focus(self, settle: float = 3.0) -> str:
        return self.responses.get(
            self.started[-1][0].split("/")[-1].rsplit(".", 1)[-1], {}
        ).get("top", "")

    def crash(self, package: str) -> Crash | None:
        return self.responses.get(
            self.started[-1][0].split("/")[-1].rsplit(".", 1)[-1], {}
        ).get("crash")

    def is_dialog(self) -> bool:
        return False

    def in_stack(self, needle: str) -> bool:
        return False


def _activities(*names: str) -> list[dict[str, str]]:
    return [
        {"name": f"{n}", "simple": n.rsplit(".", 1)[-1], "exported": "true"} for n in names
    ]


# ── A layer: typed verdicts, one sweep, no device ────────────────────────────
def test_classify_keeps_failure_kinds_apart() -> None:
    adb = FakeAdb()
    assert inv.classify("com.x/.Main", "Main", False, None, adb, set())[0] == "reached"
    assert inv.classify("com.x/.Other", "Main", True, None, adb, set())[0] == "rejected"
    assert inv.classify("com.x/.Other", "Main", False, None, adb, {"Main"})[0] == "reached_trampoline"
    assert inv.classify("", "Main", False, None, adb, set())[0] == "unknown"

    abi_crash = Crash(component="Main", exception="UnsatisfiedLinkError", abi=True, caused_by=[])
    plain_crash = Crash(component="Main", exception="NPE", abi=False, caused_by=[])
    assert inv.classify("com.x/.Other", "Main", False, abi_crash, adb, set())[0] == "crashed_abi"
    assert inv.classify("com.x/.Other", "Main", False, plain_crash, adb, set())[0] == "crashed_app"


def test_inventory_marks_rejected_and_finished_pages_without_aborting(monkeypatch) -> None:
    monkeypatch.setattr(inv, "enumerate_activities", lambda apk: ("com.demo", _activities("com.demo.Main", "com.demo.Hidden", "com.demo.Dead")))
    adb = FakeAdb(
        responses={
            "Main": {"top": "com.demo/.Main"},
            "Hidden": {"top": "", "rejected": True},
            "Dead": {"top": "com.other/.Home"},
        },
        fail_on={"Dead"},
    )
    graph = inv.inventory(apk=__import__("pathlib").Path("demo.apk"), adb=adb, settle=0)

    assert graph.method == "amstart"
    by_label = {p.label: p for p in graph.pages}
    assert by_label["Main"].verdict == "reached"
    assert by_label["Hidden"].verdict == "rejected"
    assert by_label["Dead"].verdict == "unknown", "one dead row must not end the sweep"
    assert len(adb.started) == 3, "every activity was attempted"
    assert graph.countable() == [by_label["Main"]], "only reached pages count as pages"


def test_build_params_turns_params_into_am_start_args() -> None:
    args, notes = inv.build_params(
        {"Media": {"extras": {"directory": "/sdcard/Pictures"}, "bool_extras": {"deep": True},
                   "raw_args": ["--activity-clear-task"], "pre_state": "needs a folder"}},
        "Media",
    )
    assert args == [
        "--es", "directory", "/sdcard/Pictures",
        "--ez", "deep", "true",
        "--activity-clear-task",
    ]
    assert notes == ["needs a folder"]


def test_apk_abis_surfaces_a_missing_abi_early(tmp_path) -> None:
    import zipfile

    apk = tmp_path / "demo.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("lib/arm64-v8a/libdemo.so", b"x")
        z.writestr("lib/x86_64/libdemo.so", b"x")
        z.writestr("classes.dex", b"x")
    assert inv.apk_abis(apk) == ["arm64-v8a", "x86_64"]

    broken = tmp_path / "broken.apk"
    broken.write_bytes(b"not a zip")
    assert inv.apk_abis(broken) == [], "a corrupt APK must not crash the sweep"


# ── the whole chain, one test ────────────────────────────────────────────────
def test_chain_a_refuses_to_be_a_denominator_and_b_c_diff_produce_the_number(monkeypatch, tmp_path) -> None:
    from ecat.uiflow.diff import diff_graphs
    from ecat.uiflow.flows_report import graph_from_flows_report
    from ecat.uiflow.models import Page, PageGraph
    from ecat.uiflow.spec import ScreenSpec, UiflowSpec, diff_against_spec
    from ecat.uiflow.verdict import PASS, build_verdict

    # A — what am start can reach; a capability ceiling, not a denominator
    a_graph = PageGraph(side="android", method="amstart", target="com.demo")
    a_graph.pages = [Page(key="com.demo/.Main", label="Main", component="com.demo/.Main")]
    with pytest.raises(ValueError, match="denominator"):
        diff_graphs(a_graph, a_graph)

    # B — what a user can actually reach (discovery)
    b_graph = PageGraph(side="android", method="click", target="com.demo")
    b_graph.pages = [
        Page(key="1", label="Garden", component="Garden", extra={"structure": "s1"}),
        Page(key="2", label="Detail", component="Detail", extra={"structure": "s2"}),
    ]

    # C — what the port demonstrably reaches
    report = {"bundle": "com.demo", "app": {"bundle": "com.demo", "version": "2.0.0"}, "flows": [
        {"id": "seed", "status": "pass", "screens": ["Garden.ets", "Detail.ets"],
         "trace": [{"step": 0, "action": "launch", "screen": "Garden.ets", "state": "home"},
                   {"step": 1, "action": "tap(Item)", "screen": "Detail.ets", "state": "item"}]}]}
    c_graph = graph_from_flows_report(report)

    spec_obj = UiflowSpec(screens=[
        ScreenSpec(screen="garden", android_structure="s1", port_screen="Garden.ets",
                   states=[__import__("ecat.uiflow.spec", fromlist=["StateSpec"]).StateSpec(
                       id="home", android_text="Garden", port_state="home")]),
        ScreenSpec(screen="detail", android_structure="s2", port_screen="Detail.ets"),
    ])

    result = diff_against_spec(b_graph, c_graph, spec_obj)
    assert result.fn_rate == 0.0 and result.state_fn_rate == 0.0
    assert [(e.action) for e in c_graph.edges] == ["tap(Item)"]

    b_path, r_path, s_path = tmp_path / "B.json", tmp_path / "c.json", tmp_path / "spec.json"
    PageGraph.save(b_graph, b_path)
    r_path.write_text(json.dumps(report), encoding="utf-8")
    spec_obj.save(s_path)

    verdict = build_verdict(base=b_path, port_report=r_path, spec=s_path, bundle="com.demo")
    assert verdict.status == PASS
    assert verdict.fn_rate == 0.0
    assert verdict.numbers["app"] == "com.demo v2.0.0"
    assert verdict.numbers["states"] == "1/1"


# ── manifest backends: layer A must not need build-tools ────────────────────
MANIFEST_XML = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.demo">
  <application>
    <activity android:name=".Main" android:exported="true"/>
    <activity android:name="com.demo.Hidden" android:exported="false"/>
    <activity-alias android:name=".Main.Splash" android:targetActivity=".Main"/>
    <activity android:name=".Main.Splash.Orange"/>
  </application>
</manifest>
"""


def test_manifest_xml_backend_reads_activities_without_aapt() -> None:
    pkg, acts = inv.activities_from_xml(MANIFEST_XML)
    assert pkg == "com.demo"
    by_simple = {a["simple"]: a for a in acts}
    assert by_simple["Main"]["exported"] is True
    assert by_simple["Hidden"]["exported"] is False
    assert "Orange" not in by_simple, "alias theme variants collapse into their target"


def test_manifest_xml_backend_refuses_non_xml() -> None:
    from ecat.uiflow.inventory import ToolsMissing

    with pytest.raises(ToolsMissing):
        inv.activities_from_xml("binary junk")


def test_enumerate_falls_back_when_aapt_is_missing(monkeypatch) -> None:
    def boom(apk):
        from ecat.uiflow.inventory import ToolsMissing

        raise ToolsMissing("no aapt")

    monkeypatch.setattr(inv, "_enumerate_with_aapt", boom)
    monkeypatch.setattr(inv, "_enumerate_with_apkanalyzer",
                        lambda apk: inv.activities_from_xml(MANIFEST_XML))
    pkg, acts = inv.enumerate_activities(__import__("pathlib").Path("demo.apk"))
    assert pkg == "com.demo" and acts


def test_enumerate_reports_every_missing_backend(monkeypatch) -> None:
    from ecat.uiflow.inventory import ToolsMissing

    def boom(apk):
        raise ToolsMissing("missing")

    for name in ("_enumerate_with_aapt", "_enumerate_with_apkanalyzer", "_enumerate_with_androguard"):
        monkeypatch.setattr(inv, name, boom)
    with pytest.raises(ToolsMissing, match="apkanalyzer"):
        inv.enumerate_activities(__import__("pathlib").Path("demo.apk"))
