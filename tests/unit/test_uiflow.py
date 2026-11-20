"""Unit tests for ecat.uiflow — pure data/maths, no device."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET

import pytest

from ecat.uiflow.diff import diff_graphs
from ecat.uiflow.models import COUNTABLE_VERDICTS, Edge, Page, PageGraph, fingerprint, norm, short

HERE = __import__("pathlib").Path(__file__).parent


def _tree(*texts: str, activity_ids: tuple[str, ...] = ()) -> ET.Element:
    nodes = "".join(
        f'<node text="{t}" resource-id="{rid}" content-desc="" bounds="[0,0][10,10]" clickable="true"/>'
        for t, rid in zip(texts, activity_ids or tuple("" for _ in texts), strict=False)
    )
    return ET.fromstring(f'<hierarchy rotation="0">{nodes}</hierarchy>')


# ── models ────────────────────────────────────────────────────────────────────
def test_fingerprint_tracks_data_not_just_activity() -> None:
    a, label_a = fingerprint(_tree("FolderOne"), "pkg/.activities.GridActivity")
    b, _ = fingerprint(_tree("FolderTwo"), "pkg/.activities.GridActivity")
    assert a != b, "same activity with different data must be a different page"
    assert "GridActivity" in label_a


def test_fingerprint_is_order_insensitive() -> None:
    x, _ = fingerprint(_tree("b", "a"), "c/.X")
    y, _ = fingerprint(_tree("a", "b"), "c/.X")
    assert x == y


def test_short_and_norm() -> None:
    assert short("com.x/.activities.GridActivity") == "GridActivity"
    assert short("com.x.activities.GridActivity") == "GridActivity"
    assert norm('GridActivity[$ab12] "FolderOne"') == "gridactivityab12folderone"


def test_countable_excludes_environment_failures() -> None:
    pages = [
        Page(key="1", label="A", component="p/.A", verdict="reached"),
        Page(key="2", label="B", component="p/.B", verdict="reached_trampoline"),
        Page(key="3", label="C", component="p/.C", verdict="crashed_abi"),
        Page(key="4", label="D", component="p/.D", verdict="finished"),
    ]
    g = PageGraph.from_pages(pages, method="click")
    assert [p.label for p in g.countable()] == ["A", "B"]
    assert len(g.countable()) == 2
    assert {"reached", "reached_trampoline"} == COUNTABLE_VERDICTS


def test_dialog_pages_are_not_countable() -> None:
    pages = [
        Page(key="1", label="Main", component="p/.Main", verdict="reached"),
        Page(key="2", label="promo", component="p/.Main", verdict="dialog"),
    ]
    g = PageGraph.from_pages(pages, method="click")
    assert [p.label for p in g.countable()] == ["Main"]
    assert g.summary()["pages"] == 2 and g.summary()["countable"] == 1


def test_graph_roundtrip(tmp_path) -> None:
    g = PageGraph(side="android", method="click", target="pkg", device="emulator-1")
    g.add_page(Page(key="k1", label="Main", component="p/.Main"))
    g.add_page(Page(key="k1", label="dup", component="p/.Main"))  # dedupe by key
    g.add_edge(Edge(src="k1", dst="k2", action="tap(Item)"))
    g.add_edge(Edge(src="k1", dst="k2", action="tap(Item)"))  # dedupe identical edge
    path = g.save(tmp_path / "g.json")
    back = PageGraph.load(path)
    assert len(back.pages) == 1
    assert len(back.edges) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1


# ── diff ──────────────────────────────────────────────────────────────────────
def _graph(labels: list[str], method: str = "click", side: str = "android") -> PageGraph:
    g = PageGraph(side=side, method=method)
    for lbl in labels:
        g.add_page(Page(key=f"{side}:{lbl}", label=f"{lbl}[$aa]", component=f"pkg/.{lbl}"))
    return g


def test_diff_missing_and_extra_both_directions() -> None:
    base = _graph(["Main", "Media", "Search"])
    port = _graph(["Main", "Media", "Settings"], side="oh")
    res = diff_graphs(base, port)
    assert [m["component"] for m in res.missing] == ["pkg/.Search"]
    assert [m["component"] for m in res.extra] == ["pkg/.Settings"]
    assert len(res.matched) == 2
    assert res.base_count == 3
    assert res.fn_rate == pytest.approx(1 / 3, rel=1e-4)
    assert res.coverage == pytest.approx(2 / 3, rel=1e-4)


def test_diff_refuses_amstart_denominator() -> None:
    base = _graph(["Main", "Media"], method="amstart")
    port = _graph(["Main"], side="oh")
    with pytest.raises(ValueError, match="amstart"):
        diff_graphs(base, port)
    forced = diff_graphs(base, port, force=True)
    assert forced.fn_rate == pytest.approx(0.5)


def test_diff_uses_explicit_mapping() -> None:
    base = _graph(["Media"])
    port = PageGraph(side="oh", method="click")
    port.add_page(Page(key="oh:1", label="MediaPage", component="com.oh/.MediaPage"))
    assert len(diff_graphs(base, port).missing) == 1, "labels differ, so no default match"
    mapped = diff_graphs(base, port, mapping={"Media": "MediaPage"})
    assert not mapped.missing
    assert mapped.matched[0]["by"] == "mapping"


def test_visible_text_alone_never_creates_a_match() -> None:
    """Removed on purpose: identical copy is not identity (a copy defect is a T3 finding)."""
    base = PageGraph(side="android", method="click")
    base.add_page(Page(key="a1", label='GridActivity[$aa] "FolderOne"', component="pkg/.activities.GridActivity"))
    port = PageGraph(side="oh", method="flows")
    port.add_page(Page(key="flow:Folder.ets", label="Folder.ets", component="pkg#Folder.ets"))
    res = diff_graphs(base, port)
    assert res.missing and not res.matched, "no fuzzy match: declare the identity in the spec or a mapping"


def test_diff_empty_base_warns() -> None:
    res = diff_graphs(_graph([]), _graph(["Main"]))
    assert res.fn_rate == 0.0
    assert res.warnings


def test_diff_report_notes_denominator_rule() -> None:
    res = diff_graphs(_graph(["Main", "Media"]), _graph(["Main"]))
    md = res.to_markdown()
    assert "B vs C" in md or "FN" in md
    assert "Search" not in md


# ── state level: a tab is not a page, but it can still be missing ────────────
def test_states_are_matched_on_android_text_and_flow_evidence() -> None:
    from ecat.uiflow.flows_report import graph_from_flows_report
    from ecat.uiflow.models import Page, PageGraph
    from ecat.uiflow.spec import ScreenSpec, StateSpec, UiflowSpec, diff_against_spec

    base = PageGraph(side="android", method="click")
    base.pages = [
        Page(key="a1", label='Garden[$aa] "Add plant/My garden"', component="c", extra={"structure": "s1"}),
        Page(key="a2", label='Garden[$bb] "Apple/Beet"', component="c", extra={"structure": "s1"}),
    ]
    port = graph_from_flows_report({"flows": [{
        "id": "tabs", "status": "pass", "screens": ["Garden.ets"],
        "trace": [
            {"step": 0, "action": "launch", "screen": "Garden.ets", "state": "my_garden"},
            {"step": 1, "action": "tap(Plant list)", "screen": "Garden.ets", "state": "plant_list"},
        ]}]})
    spec = UiflowSpec(screens=[ScreenSpec(
        screen="garden", android_structure="s1", port_screen="Garden.ets",
        states=[StateSpec(id="my_garden", android_text="My garden"),
                StateSpec(id="plant_list", android_text="Beet"),
                StateSpec(id="calendar", android_text="Calendar")],
    )])
    result = diff_against_spec(base, port, spec)
    rows = {r.state: r for r in result.state_rows}
    assert rows["my_garden"].android_seen and rows["my_garden"].port_seen
    assert rows["plant_list"].port_seen, "the flow proved this tab"
    assert not rows["calendar"].android_seen, "no Android evidence -> not counted as missing"
    assert result.state_fn_rate == 0.0
    assert result.states_total == 3


def test_a_state_only_in_android_counts_as_state_fn() -> None:
    from ecat.uiflow.flows_report import graph_from_flows_report
    from ecat.uiflow.models import Page, PageGraph
    from ecat.uiflow.spec import ScreenSpec, StateSpec, UiflowSpec, diff_against_spec

    base = PageGraph(side="android", method="click")
    base.pages = [Page(key="a", label='Garden[$aa] "My garden/Filters"', component="c",
                       extra={"structure": "s1"})]
    port = graph_from_flows_report({"flows": [{
        "id": "home", "status": "pass", "screens": ["Garden.ets"],
        "trace": [{"step": 0, "action": "launch", "screen": "Garden.ets", "state": "my_garden"}]}]})
    spec = UiflowSpec(screens=[ScreenSpec(
        screen="garden", android_structure="s1", port_screen="Garden.ets",
        states=[StateSpec(id="my_garden", android_text="My garden"),
                StateSpec(id="filters", android_text="Filters")])])
    result = diff_against_spec(base, port, spec)
    assert [r.state for r in result.states_missing] == ["filters"]
    assert result.state_fn_rate == 0.5


def test_state_evidence_survives_the_importer_merge() -> None:
    from ecat.uiflow.flows_report import graph_from_flows_report

    g = graph_from_flows_report(_flows_report(
        {"id": "a", "status": "pass", "screens": ["Garden.ets"],
         "trace": [{"step": 0, "action": "launch", "screen": "Garden.ets", "state": "my_garden"}]},
        {"id": "b", "status": "pass", "screens": ["Garden.ets"],
         "trace": [{"step": 0, "action": "launch", "screen": "Garden.ets", "state": "plant_list"}]},
    ))
    page = next(p for p in g.pages if p.label == "Garden.ets")
    assert page.extra["states"] == ["my_garden", "plant_list"], "states from both flows are unioned"


def test_state_type_histograms_read_a_droidbot_run(tmp_path) -> None:
    from ecat.uiflow.discover import state_type_histograms

    states = tmp_path / "states"
    states.mkdir()
    payload = {
        "state_str": "abc123",
        "views": [
            {"class": "android.widget.TextView", "visible": True},
            {"class": "android.widget.TextView", "visible": True},
            {"class": "android.widget.Button", "visible": True},
            {"class": "android.widget.Button", "visible": False},  # hidden: not part of the screen
            {"class": "", "visible": True},
        ],
    }
    (states / "state_1.json").write_text(json.dumps(payload), encoding="utf-8")
    hist = state_type_histograms(tmp_path)
    assert hist["abc123"] == {"TextView": 2, "Button": 1}


def test_state_type_histograms_tolerates_a_missing_run_dir(tmp_path) -> None:
    from ecat.uiflow.discover import state_type_histograms

    assert state_type_histograms(tmp_path / "nope") == {}


# ── diagnosis: portfolio of defects, not a pass/fail verdict ────────────────
def _verdict(**over):
    from ecat.uiflow.verdict import Verdict

    v = Verdict(bundle="com.demo", generated="t", status="FAIL")
    v.checks = over.get("checks") or []
    v.missing_detail = over.get("missing_detail") or []
    v.dead_pages = over.get("dead_pages") or []
    v.reasons = over.get("reasons") or []
    v.numbers = {"app": "com.demo v1"}
    return v


def test_a_missing_page_becomes_a_work_item_with_a_file() -> None:
    from ecat.uiflow.workitems import to_work_items

    payload = to_work_items(_verdict(missing_detail=[
        {"screen": "settings", "kind": "missing_page", "action": "补这一页"}]), )
    item = payload["work_list"][0]
    assert item["severity"] == "high"
    assert item["file"].endswith("entry/src/main/ets/pages/Settings.ets")
    assert "端口没有这一页" in item["issues"][0]


def test_a_broken_entry_is_not_reported_as_a_missing_page() -> None:
    from ecat.uiflow.workitems import to_work_items

    payload = to_work_items(_verdict(missing_detail=[
        {"screen": "media", "kind": "entry_broken", "action": "查入口"}]))
    item = payload["work_list"][0]
    assert "页面存在但用户到不了" in item["issues"][0]


def test_tooling_mistakes_are_kept_out_of_the_port_work_list() -> None:
    from ecat.uiflow.verdict import Check
    from ecat.uiflow.workitems import to_work_items

    payload = to_work_items(_verdict(
        missing_detail=[{"screen": "x", "kind": "unmapped", "action": "补 spec"}],
        checks=[Check("flow-audit", "fail", "no_assertion")],
    ))
    assert payload["work_list"] == [], "a broken measuring stick is not a port defect"
    assert {t["screen"] for t in payload["tooling"]} == {"x", "*"}


def test_dead_pages_are_low_severity_cleanup() -> None:
    from ecat.uiflow.workitems import to_markdown, to_work_items

    payload = to_work_items(_verdict(dead_pages=["Index.ets"]))
    assert payload["work_list"][0]["severity"] == "low"
    assert "Index.ets" in to_markdown(payload)


# ── structure: types, not text, are comparable across platforms ─────────────
def test_types_are_bucketed_before_comparing() -> None:
    """The same dialog reads as FrameLayout/ScrollView on Android and Text/Button on ArkUI."""
    from ecat.uiflow.structure import canonical_type, cosine

    assert canonical_type("android.widget.FrameLayout") == "container"
    assert canonical_type("Column") == "container"
    assert canonical_type("TextView") == "text"
    assert canonical_type("Text") == "text"
    assert canonical_type("androidx.recyclerview.widget.RecyclerView") == "list"
    assert canonical_type("SymbolGlyph") == "image"

    android_dialog = {"FrameLayout": 6, "LinearLayout": 2, "ScrollView": 2, "TextView": 1, "Button": 1}
    port_dialog = {"Text": 4, "Button": 1}
    assert cosine(android_dialog, port_dialog) > 0.7, "bucketed, these are the same shape"


def test_cosine_compares_type_histograms_not_sizes() -> None:
    from ecat.uiflow.structure import cosine

    small = {"Text": 3, "Button": 1}
    big = {"Text": 30, "Button": 10}          # same shape, ten times the content
    other = {"Image": 8}
    assert cosine(small, big) == 1.0, "normalisation must ignore screen size"
    assert cosine(small, other) == 0.0
    # proportions matter, counts do not: swapping 3:1 into 1:3 must drop the score clearly
    assert 0.5 <= cosine(small, {"Text": 1, "Button": 3}) < 0.7


def test_cosine_says_nothing_when_a_side_has_no_types() -> None:
    from ecat.uiflow.structure import cosine

    assert cosine(None, {"Text": 1}) is None
    assert cosine({"Text": 1}, {}) is None


def test_position_slots_tell_a_grid_from_a_single_button() -> None:
    from ecat.uiflow.structure import jaccard, slots

    grid = {"size": [300, 300],
            "elements": [["ImageView", 20, 20], ["ImageView", 280, 20],
                         ["ImageView", 20, 280], ["ImageView", 280, 280]]}
    dialog = {"size": [300, 300], "elements": [["Button", 150, 260]]}
    assert len(slots(grid)) == 4 and len(slots(dialog)) == 1
    assert jaccard(slots(grid), slots(dialog)) == 0.0, "a 2x2 grid is not a centred button"
    assert jaccard(slots(grid), slots(grid)) == 1.0


def test_compare_prefers_geometry_over_role_counts() -> None:
    from ecat.uiflow.models import Page, PageGraph
    from ecat.uiflow.spec import SpecDiffResult, SpecRow
    from ecat.uiflow.structure import compare_screens

    base = PageGraph(side="android", method="click")
    base.pages = [Page(key="1", label="Media", component="c", extra={
        "types": {"ImageView": 4, "Text": 1},
        "geometry": {"size": [300, 300], "elements": [["ImageView", 20, 20], ["ImageView", 280, 20],
                                                      ["ImageView", 20, 280], ["ImageView", 280, 280]]}})]
    port = PageGraph(side="oh", method="gate")
    port.pages = [Page(key="2", label="Main.ets", component="c", extra={
        "types": {"Text": 4, "Button": 1},
        "geometry": {"size": [300, 300], "elements": [["Button", 150, 260]]}})]
    result = SpecDiffResult(rows=[SpecRow(screen="media", android_reached=True, port_present=True,
                                          android_page="Media", port_page="Main.ets")])
    row = compare_screens(result, base, port)[0]
    assert row["basis"] == "geometry" and row["verdict"] == "suspicious"


def test_compare_screens_flags_a_pairing_that_does_not_look_alike() -> None:
    from ecat.uiflow.models import Page, PageGraph
    from ecat.uiflow.spec import SpecDiffResult, SpecRow
    from ecat.uiflow.structure import compare_screens

    base = PageGraph(side="android", method="click")
    base.pages = [Page(key="1", label="Media", component="c",
                       extra={"types": {"Image": 20, "Text": 2}})]
    port = PageGraph(side="oh", method="gate")
    port.pages = [Page(key="2", label="Settings.ets", component="c",
                       extra={"types": {"Switch": 9, "Text": 12}})]
    result = SpecDiffResult(rows=[SpecRow(screen="media", android_reached=True, port_present=True,
                                          android_page="Media", port_page="Settings.ets")])
    rows = compare_screens(result, base, port)
    assert rows[0]["verdict"] == "suspicious" and rows[0]["similarity"] < 0.55


def test_compare_screens_reports_missing_data_instead_of_scoring_zero() -> None:
    from ecat.uiflow.models import Page, PageGraph
    from ecat.uiflow.spec import SpecDiffResult, SpecRow
    from ecat.uiflow.structure import compare_screens

    base = PageGraph(side="android", method="click")
    base.pages = [Page(key="1", label="Media", component="c")]
    port = PageGraph(side="oh", method="gate")
    port.pages = [Page(key="2", label="Media.ets", component="c")]
    result = SpecDiffResult(rows=[SpecRow(screen="media", android_reached=True, port_present=True,
                                          android_page="Media", port_page="Media.ets")])
    assert compare_screens(result, base, port)[0]["verdict"] == "no-structure-data"


# ── suggestions, data fingerprints, and why a page is missing ───────────────
def test_suggest_assertions_returns_only_labels_new_on_this_screen() -> None:
    from ecat.uiflow.flows_report import suggest_assertions

    payload = _flows_report({"id": "f", "status": "pass", "screens": ["A.ets", "B.ets"], "trace": [
        {"step": 0, "action": "launch", "screen": "A.ets", "sig": "apple|beet"},
        {"step": 1, "action": "tap(Beet)", "screen": "B.ets", "sig": "apple|watering needs|every 30 days"},
    ]})
    out = suggest_assertions(payload)
    assert "apple" not in out.get("B.ets", []), "apple was already on the previous screen"
    assert "watering needs" in out["B.ets"]


def test_pages_carry_a_data_fingerprint() -> None:
    from ecat.uiflow.flows_report import graph_from_flows_report

    g = graph_from_flows_report(_flows_report({"id": "f", "status": "pass", "screens": ["A.ets"],
        "trace": [{"step": 0, "action": "launch", "screen": "A.ets", "sig": "a|b|c"}]}))
    page = next(p for p in g.pages if p.label == "A.ets")
    assert page.extra["data"]["labels"] == 3


def test_data_parity_warns_when_the_port_renders_far_less() -> None:
    from ecat.uiflow.models import Page, PageGraph
    from ecat.uiflow.spec import SpecDiffResult, SpecRow
    from ecat.uiflow.verdict import data_parity

    port = PageGraph(side="oh", method="gate")
    port.pages = [Page(key="1", label="Media.ets", component="c",
                       extra={"data": {"labels": 1, "sample": ["no media files have been found"]}})]
    result = SpecDiffResult(rows=[SpecRow(screen="media", android_reached=True, port_present=True,
                                          android_page='Media[$ab] "06.11.2026/09.11.2026/a.png"',
                                          port_page="Media.ets")])
    notes = data_parity(result, port)
    assert notes and "数据可能不对等" in notes[0]


def test_data_parity_is_quiet_when_both_sides_show_content() -> None:
    from ecat.uiflow.models import Page, PageGraph
    from ecat.uiflow.spec import SpecDiffResult, SpecRow
    from ecat.uiflow.verdict import data_parity

    port = PageGraph(side="oh", method="gate")
    port.pages = [Page(key="1", label="Media.ets", component="c",
                       extra={"data": {"labels": 5, "sample": []}})]
    result = SpecDiffResult(rows=[SpecRow(screen="media", android_reached=True, port_present=True,
                                          android_page='Media[$ab] "a/b"', port_page="Media.ets")])
    assert data_parity(result, port) == []


def test_missing_pages_are_classified_by_what_is_actually_wrong() -> None:
    from ecat.uiflow.spec import SpecDiffResult, SpecRow
    from ecat.uiflow.verdict import classify_missing

    result = SpecDiffResult(rows=[
        SpecRow(screen="gone", android_reached=True, port_present=False, port_page="Ghost.ets"),
        SpecRow(screen="stuck", android_reached=True, port_present=False, port_page="Known.ets"),
        SpecRow(screen="blank", android_reached=True, port_present=False, port_page=""),
    ])
    kinds = {row["screen"]: row["kind"] for row in classify_missing(result, {"Known"})}
    assert kinds == {"gone": "missing_page", "stuck": "entry_broken", "blank": "unmapped"}


# ── a weak assertion is not evidence of arrival ─────────────────────────────
def test_weak_assertion_does_not_credit_the_screen() -> None:
    """The label was already on the previous screen: presence proves nothing about navigation."""
    from ecat.uiflow.flows_report import graph_from_flows_report

    payload = _flows_report({
        "id": "detail", "status": "pass", "screens": ["Garden.ets", "PlantDetail.ets"],
        "trace": [
            {"step": 0, "action": "launch", "screen": "Garden.ets", "sig": "garden"},
            {"step": 1, "action": "tap(Apple)", "screen": "PlantDetail.ets", "sig": "garden"},
            # the assertion passed only because Garden already shows that text
            {"step": 2, "action": "expect_visible", "screen": "PlantDetail.ets", "sig": "garden",
             "weak": True},
        ]})
    g = graph_from_flows_report(payload)
    reached = {p.label for p in g.pages if p.countable}
    assert reached == {"Garden.ets"}, f"PlantDetail must not count: {reached}"
    assert g.stats["weak_assertions"] == 1


def test_a_marker_without_page_change_is_not_arrival() -> None:
    """A dead route: the step claims a screen but the signature never moved."""
    from ecat.uiflow.flows_report import graph_from_flows_report

    payload = _flows_report({
        "id": "dead_route", "status": "fail", "step": 2, "step_name": "expect_page_changed",
        "screens": ["Garden.ets", "PlantDetail.ets"],
        "trace": [
            {"step": 0, "action": "launch", "screen": "Garden.ets", "sig": "same"},
            {"step": 1, "action": "tap(Apple)", "screen": "PlantDetail.ets", "sig": "same"},
            {"step": 2, "action": "expect_page_changed", "screen": "PlantDetail.ets", "sig": "same"},
        ]})
    g = graph_from_flows_report(payload)
    assert {p.label for p in g.pages if p.countable} == {"Garden.ets"}


# ── states vs flows: declaring states without proving them is a trap ────────
def test_audit_says_states_are_unproven_when_no_flow_marks_them() -> None:
    from ecat.uiflow.audit import audit_states

    spec = {"screens": [{"screen": "garden", "port": {"screen": "Garden.ets"},
                         "states": [{"id": "my_garden", "port": {"state": "my_garden"}}]}]}
    flows = {"flows": [{"id": "home", "steps": [{"launch": True, "screen": "Garden.ets"}]}]}
    findings = audit_states(spec, flows)["states"]
    assert [f.check for f in findings] == ["states_not_evidenced"]
    assert findings[0].severity == "high"


def test_audit_names_the_state_a_flow_forgot_to_prove() -> None:
    from ecat.uiflow.audit import audit_states

    spec = {"screens": [{"screen": "garden", "port": {"screen": "Garden.ets"},
                         "states": [{"id": "my_garden", "port": {"state": "my_garden"}},
                                    {"id": "plant_list", "port": {"state": "plant_list"}}]}]}
    flows = {"flows": [{"id": "home", "steps": [
        {"launch": True, "screen": "Garden.ets", "state": "my_garden"}]}]}
    findings = audit_states(spec, flows)["states"]
    assert [f.check for f in findings] == ["state_uncovered"]
    assert findings[0].detail.startswith("状态 `plant_list`")


def test_audit_is_silent_when_every_state_is_proven() -> None:
    from ecat.uiflow.audit import audit_states

    spec = {"screens": [{"screen": "garden", "port": {"screen": "Garden.ets"},
                         "states": [{"id": "my_garden", "port": {"state": "my_garden"}}]}]}
    flows = {"flows": [{"id": "home", "steps": [
        {"launch": True, "screen": "Garden.ets", "state": "my_garden"}]}]}
    assert audit_states(spec, flows)["states"] == []


def test_history_tells_two_builds_apart_by_sha_not_version() -> None:
    from ecat.uiflow.history import Entry, delta

    prev = Entry(bundle="b", version="1.0.0", generated="t1", status="PASS", fn_rate=0.0,
                 port_pages=2, android_reached="2/2", sha256="aaaa11112222")
    cur = Entry(bundle="b", version="1.0.0", generated="t2", status="PASS", fn_rate=0.0,
                port_pages=2, android_reached="2/2", sha256="bbbb33334444")
    changes = delta(prev, cur)["changes"]
    assert any("构建 aaaa11112222 → bbbb33334444" in c for c in changes), changes
    assert not any(c.startswith("版本") for c in changes), "same versionName, different build"


# ── spec candidates: propose, never pretend to know ──────────────────────────
def test_candidate_scoring_prefers_the_same_name() -> None:
    from ecat.uiflow.spec import score_candidate

    assert score_candidate("Garden", "Garden") == 1.0
    assert score_candidate("Plant list", "PlantDetail") < 0.4, "partial token overlap is not a pairing"
    assert score_candidate("Plant Detail", "PlantDetail") >= 0.6
    assert score_candidate("Settings", "Garden") == 0.0


def test_spec_from_graph_proposes_one_port_screen_per_android_page() -> None:
    from ecat.uiflow.models import Page, PageGraph
    from ecat.uiflow.spec import spec_from_graph

    g = PageGraph(side="android", method="click")
    g.pages = [
        Page(key="a", label="Garden", component="c", extra={"structure": "s1", "activity": ".GardenActivity"}),
        Page(key="b", label="Plant Detail", component="c", extra={"structure": "s2", "activity": ".PlantDetailActivity"}),
    ]
    spec = spec_from_graph(g, port_pages=["Garden", "PlantDetail", "Index"])
    mapping = {row.screen.split("@")[0]: row.port_screen for row in spec.screens}
    assert mapping["Garden"] == "Garden"
    assert mapping["Plant Detail"] == "PlantDetail", "Index must not be handed out twice or by mistake"
    assert len({row.port_screen for row in spec.screens}) == 2, "one port page, one owner"
    assert all(row.auto for row in spec.screens), "proposals are marked as unconfirmed"


def test_spec_matches_ets_suffix_and_bare_stem(tmp_path) -> None:
    """A spec filled from the page registry says `Garden`, flows say `Garden.ets` — same screen."""
    from ecat.uiflow.flows_report import graph_from_flows_report
    from ecat.uiflow.models import Page, PageGraph
    from ecat.uiflow.spec import ScreenSpec, UiflowSpec, diff_against_spec

    base = PageGraph(side="android", method="click")
    base.pages = [Page(key="a", label="Garden", component="c", extra={"structure": "s1"})]
    port = graph_from_flows_report({"flows": [
        {"id": "home", "status": "pass", "screens": ["Garden.ets"],
         "trace": [{"step": 0, "action": "launch", "screen": "Garden.ets"}]}]})
    spec = UiflowSpec(screens=[ScreenSpec(screen="garden", android_structure="s1", port_screen="Garden")])
    result = diff_against_spec(base, port, spec)
    assert result.present, "bare stem must match the .ets page"
    assert result.fn_rate == 0.0


def test_confirming_a_spec_clears_the_auto_flag(tmp_path) -> None:
    from ecat.uiflow.spec import ScreenSpec, UiflowSpec

    spec = UiflowSpec(screens=[ScreenSpec(screen="garden", android_structure="s1",
                                          port_screen="Garden", note="AUTO 候选 Garden (score 1.00)", auto=True)])
    path = spec.save(tmp_path / "spec.json")
    reloaded = UiflowSpec.load(path)
    assert len(reloaded.unconfirmed()) == 1
    for row in reloaded.screens:
        row.auto = False
    reloaded.save(path)
    assert UiflowSpec.load(path).unconfirmed() == []


def test_verdict_says_out_loud_when_the_spec_was_guessed(tmp_path) -> None:
    from ecat.uiflow.verdict import build_verdict

    base, report, spec, flows, port_dir = _verdict_fixtures(tmp_path)
    path = __import__("pathlib").Path(spec)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["screens"][0]["auto"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")

    v = build_verdict(base=base, port_report=report, spec=spec, flows=flows, port_dir=port_dir)
    reviewed = next(c for c in v.checks if c.name == "spec-reviewed")
    assert reviewed.status == "warn"
    assert v.numbers["unconfirmed_spec_rows"] == 1
    assert any("启发式配对" in r for r in v.reasons)


# ── history: a number means nothing without the previous one ─────────────────
def _entry(bundle="demo", version="1.0.0", status="PASS", fn=0.0, missing=(), dead=()):
    from ecat.uiflow.history import Entry

    return Entry(bundle=bundle, version=version, generated="2026-11-18 10:00:00", status=status,
                 fn_rate=fn, port_pages=2, android_reached="2/2",
                 missing_pages=list(missing), dead_pages=list(dead))


def test_history_keeps_the_file_bounded(tmp_path) -> None:
    from ecat.uiflow.history import append_entry, read_history

    for i in range(6):
        append_entry(tmp_path, _entry(fn=i / 100), keep=3)
    entries = read_history(tmp_path, "demo")
    assert len(entries) == 3 and entries[-1].fn_rate == 0.05


def test_ambiguous_state_texts_are_reported(tmp_path) -> None:
    from ecat.uiflow.audit import audit_states

    spec = {"screens": [{"screen": "garden", "port": {"screen": "Garden.ets"},
                         "states": [{"id": "a", "android": {"text": "Garden"},
                                     "port": {"state": "a"}},
                                    {"id": "b", "android": {"text": "My Garden"},
                                     "port": {"state": "b"}}]}]}
    flows = {"flows": [{"id": "f", "steps": [
        {"launch": True, "screen": "Garden.ets", "state": "a"},
        {"tap": "x", "screen": "Garden.ets", "state": "b"}]}]}
    checks = [f.check for f in audit_states(spec, flows)["states"]]
    assert "state_ambiguous" in checks


def test_droidbot_cmd_carries_the_seed_activity_only_when_asked(tmp_path) -> None:
    from ecat.uiflow.discover import droidbot_cmd

    apk, out = tmp_path / "a.apk", tmp_path / "out"
    plain = droidbot_cmd("python", apk, out, "emulator-5630")
    assert "-start_activity" not in plain, "blind rounds must not carry a seed"

    seeded = droidbot_cmd("python", apk, out, "emulator-5630", start_activity="com.x/.Settings")
    idx = seeded.index("-start_activity")
    assert seeded[idx + 1] == "com.x/.Settings"
    assert seeded.count("-keep_app") == 1


def test_merge_graphs_unions_pages_by_structure_and_edges_by_action() -> None:
    from ecat.uiflow.discover import merge_graphs
    from ecat.uiflow.models import Edge, Page, PageGraph

    a = PageGraph(side="android", method="click")
    a.pages = [Page(key="a1", label='Main "A"', component="c", extra={"structure": "S1"})]
    a.edges = [Edge(src="a1", dst="a2", action="tap(X)", source="trace")]
    b = PageGraph(side="android", method="click")
    b.pages = [
        Page(key="b1", label='Main "B"', component="c", extra={"structure": "S1"}),  # same layout
        Page(key="b2", label="Detail", component="c", extra={"structure": "S2"}),
    ]
    b.edges = [Edge(src="b1", dst="b2", action="tap(Folder)", source="trace")]
    b.stats = {"app_sha256": "deadbeef"}

    merged = merge_graphs([a, b])
    assert len(merged.pages) == 2, "the same structure is one page, whatever data it showed"
    assert merged.stats["app_sha256"] == "deadbeef" and merged.stats["rounds"] == 2
    assert {e.action for e in merged.edges} == {"tap(X)", "tap(Folder)"}


def test_discovery_coverage_names_the_activities_b_never_reached() -> None:
    from ecat.uiflow.discover import activity_coverage
    from ecat.uiflow.models import Page, PageGraph

    graph = PageGraph(side="android", method="click")
    graph.pages = [Page(key="1", label="Main", component="com.demo/.MainActivity",
                        extra={"activity": ".MainActivity"})]
    acts = [{"name": "com.demo.MainActivity", "simple": "MainActivity"},
            {"name": "com.demo.MediaActivity", "simple": "MediaActivity"}]
    result = activity_coverage(graph, acts)
    assert result["activities_total"] == 2 and result["activities_seen"] == 1
    assert result["never_reached"] == ["com.demo.MediaActivity"]
    assert result["coverage"] == 0.5


def test_history_is_per_bundle_and_round_trips(tmp_path) -> None:
    from ecat.uiflow.history import append_entry, read_history

    append_entry(tmp_path, _entry(bundle="a.app", fn=0.0))
    append_entry(tmp_path, _entry(bundle="b.app", fn=0.25))
    assert [e.bundle for e in read_history(tmp_path, "a.app")] == ["a.app"]
    assert [e.fn_rate for e in read_history(tmp_path, "b.app")] == [0.25]


def test_delta_names_a_regression_and_an_improvement() -> None:
    from ecat.uiflow.history import delta

    worse = delta(_entry(fn=0.0), _entry(status="FAIL", fn=0.083, missing=["detail"]))
    assert worse["verdict"] == "regression"
    assert worse["regressed_pages"] == ["detail"]
    assert any("变差" in c for c in worse["changes"])

    better = delta(_entry(status="FAIL", fn=0.083, missing=["detail"]), _entry(fn=0.0))
    assert better["verdict"] == "fixed"
    assert better["fixed_pages"] == ["detail"]


def test_delta_separates_worse_from_unmeasurable() -> None:
    from ecat.uiflow.history import delta

    blocked = delta(_entry(status="PASS", fn=0.0), _entry(status="BLOCKED", fn=None))
    assert any("PASS → BLOCKED" in c for c in blocked["changes"])
    assert any("未出数" in c for c in blocked["changes"])
    assert blocked["verdict"] != "regression", "an unmeasurable port is not a worse port"


def test_dead_pages_are_not_compared_when_one_run_skipped_port_check() -> None:
    from ecat.uiflow.history import delta

    checked = _entry(dead=["Index.ets"])
    unchecked = _entry(dead=[])
    unchecked.port_checked = False
    d = delta(checked, unchecked)
    assert d["new_dead_pages"] == [], "a run without --port-dir cannot claim a dead page was fixed"
    assert not any("死页" in c for c in d["changes"])


def test_first_run_has_no_baseline() -> None:
    from ecat.uiflow.history import delta, to_markdown

    first = delta(None, _entry())
    assert first["first_run"] is True
    assert "没有可比基线" in to_markdown(first)


# ── one verdict artifact: all gates, one status, one exit code ───────────────
def _verdict_fixtures(tmp_path, *, port_screen="Garden.ets", flows_steps=None, port_flows=None):
    from ecat.uiflow.models import Page, PageGraph

    base = PageGraph(side="android", method="click", engine="test")
    base.pages = [Page(key="and:1", label="Garden", component="GardenActivity",
                       extra={"structure": "aaa", "activity": ".GardenActivity"})]
    base_path = tmp_path / "B.json"
    PageGraph.save(base, base_path)

    spec = {"schema_version": 1, "screens": [
        {"screen": "garden", "android": {"structure": "aaa"}, "port": {"screen": port_screen}}]}
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    report = {"flows": port_flows if port_flows is not None else [
        {"id": "home", "status": "pass", "screens": ["Garden.ets"],
         "trace": [{"step": 0, "action": "launch", "screen": "Garden.ets"}]}], "summary": {}}
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    flows = {"flows": [{"id": "home", "screens": ["Garden.ets"],
                        "steps": flows_steps or [{"launch": True, "screen": "Garden.ets"},
                                                 {"expect_visible": ["Sunflower"], "screen": "Garden.ets"}]}]}
    flows_path = tmp_path / "flows.json"
    flows_path.write_text(json.dumps(flows), encoding="utf-8")

    prof = tmp_path / "entry/src/main/resources/base/profile"
    prof.mkdir(parents=True)
    (prof / "main_pages.json").write_text(json.dumps({"src": ["pages/Garden"]}), encoding="utf-8")
    ets = tmp_path / "entry/src/main/ets/entryability"
    ets.mkdir(parents=True)
    (ets / "EntryAbility.ets").write_text("windowStage.loadContent('pages/Garden');", encoding="utf-8")
    # give the port a router route so the check runs in blocking mode
    (ets.parent / "Garden.ets").write_text(
        "router.pushUrl({ url: 'pages/PlantDetail' });", encoding="utf-8")
    return base_path, report_path, spec_path, flows_path, tmp_path


def test_verdict_passes_and_carries_exactly_one_fn_number(tmp_path) -> None:
    from ecat.uiflow.verdict import PASS, build_verdict

    base, report, spec, flows, port_dir = _verdict_fixtures(tmp_path)
    v = build_verdict(base=base, port_report=report, spec=spec, flows=flows, port_dir=port_dir, bundle="demo")
    assert v.status == PASS and v.exit_code == 0
    assert v.fn_rate == 0.0
    names = [c.name for c in v.checks]
    assert names[0] == "port-check" and names[-1] == "diff"
    assert {"flow-audit", "port-measured", "spec-reviewed", "state-coverage", "data-parity"} <= set(names)
    assert v.to_markdown().count("FN 率") == 1
    assert v.to_dict()["schema"] == "ecat.uiflow.verdict/1"


def test_verdict_fails_when_the_spec_names_a_screen_the_port_lacks(tmp_path) -> None:
    from ecat.uiflow.verdict import FAIL, build_verdict

    base, report, spec, flows, port_dir = _verdict_fixtures(tmp_path, port_screen="Ghost.ets")
    v = build_verdict(base=base, port_report=report, spec=spec, flows=flows, port_dir=port_dir)
    assert v.status == FAIL and v.exit_code == 1
    assert any("Ghost.ets" in r for r in v.reasons)


def test_verdict_fails_on_a_dishonest_flow(tmp_path) -> None:
    from ecat.uiflow.verdict import FAIL, build_verdict

    base, report, spec, flows, port_dir = _verdict_fixtures(
        tmp_path, flows_steps=[{"launch": True}, {"tap": "Apple"}])
    v = build_verdict(base=base, port_report=report, spec=spec, flows=flows, port_dir=port_dir)
    assert v.status == FAIL and v.exit_code == 1
    assert any("no_assertion" in r for r in v.reasons)


def test_verdict_blocks_the_number_when_flows_never_ran(tmp_path) -> None:
    from ecat.uiflow.verdict import BLOCKED, build_verdict

    base, report, spec, flows, port_dir = _verdict_fixtures(tmp_path, port_flows=[
        {"id": "seed", "status": "error", "step_name": "launch", "screens": ["Garden.ets"]},
        {"id": "home", "status": "skip", "step_name": "dependency", "screens": ["Garden.ets"],
         "message": "blocked_by: seed"}])
    v = build_verdict(base=base, port_report=report, spec=spec, flows=flows, port_dir=port_dir)
    assert v.status == BLOCKED and v.exit_code == 4
    assert v.fn_rate is None, "no number may leak out of a blocked run"
    assert any("依赖失败" in r for r in v.reasons), v.reasons


# ── dependency skips must not be read as missing screens ─────────────────────
def test_dependency_skips_are_counted_but_never_credit_screens() -> None:
    from ecat.uiflow.flows_report import graph_from_flows_report

    payload = _flows_report(
        {"id": "seed", "status": "fail", "step": 2, "step_name": "expect_visible",
         "screens": ["Home.ets"], "trace": [{"step": 0, "action": "launch", "screen": "Home.ets"}]},
        {"id": "detail", "status": "skip", "step_name": "dependency", "screens": ["Home.ets", "Detail.ets"],
         "message": "blocked_by: seed"},
    )
    g = graph_from_flows_report(payload)
    assert g.stats["dependency_skips"] == 1
    assert {p.label for p in g.pages if p.countable} == {"Home.ets"}, "a flow that never ran proves nothing"


def test_degraded_graph_blocks_the_headline_number(tmp_path) -> None:
    from ecat.uiflow.flows_report import graph_from_flows_report
    from ecat.uiflow.models import PageGraph

    payload = _flows_report(
        {"id": "seed", "status": "fail", "step": 1, "step_name": "tap", "screens": ["Home.ets"]},
        {"id": "detail", "status": "skip", "step_name": "dependency", "screens": ["Detail.ets"]},
    )
    g = graph_from_flows_report(payload)
    path = tmp_path / "C.json"
    PageGraph.save(g, path)

    import importlib
    cli = importlib.import_module("ecat.cli")
    notes = cli._degraded_notes(path)
    assert any("blocked by a failed dependency" in n for n in notes)
    assert cli._degraded_notes.__doc__

    clean = graph_from_flows_report(_flows_report(
        {"id": "ok", "status": "pass", "screens": ["Home.ets"],
         "trace": [{"step": 0, "action": "launch", "screen": "Home.ets"}]}))
    clean_path = tmp_path / "C_clean.json"
    PageGraph.save(clean, clean_path)
    assert cli._degraded_notes(clean_path) == []


# ── port check: a spec may name screens the port does not ship ───────────────
def _port_tree(tmp_path, pages, entry="pages/Garden", pushes=(("Garden", "PlantDetail"),)):
    """A port that uses the router model: main_pages + literal pushUrl."""
    prof = tmp_path / "entry/src/main/resources/base/profile"
    prof.mkdir(parents=True)
    (prof / "main_pages.json").write_text(json.dumps({"src": list(pages)}), encoding="utf-8")
    ets = tmp_path / "entry/src/main/ets"
    (ets / "entryability").mkdir(parents=True)
    (ets / "entryability/EntryAbility.ets").write_text(
        f"windowStage.loadContent('{entry}', (err) => {{}});", encoding="utf-8")
    for name, target in pushes:
        (ets / f"pages/{name}.ets").parent.mkdir(parents=True, exist_ok=True)
        (ets / f"pages/{name}.ets").write_text(
            f"router.pushUrl({{ url: 'pages/{target}', params: {{}} }});", encoding="utf-8")
    return tmp_path


def _nav_port_tree(tmp_path, registered=("pages/Index",), entry="pages/Index"):
    """A port the way ECAT generates it: one registered page, screens via NavPathStack constants."""
    prof = tmp_path / "entry/src/main/resources/base/profile"
    prof.mkdir(parents=True)
    (prof / "main_pages.json").write_text(json.dumps({"src": list(registered)}), encoding="utf-8")
    ets = tmp_path / "entry/src/main/ets"
    (ets / "entryability").mkdir(parents=True)
    (ets / "common").mkdir(parents=True, exist_ok=True)
    (ets / "entryability/EntryAbility.ets").write_text(
        f"windowStage.loadContent('{entry}', (err) => {{}});", encoding="utf-8")
    (ets / "common/Constants.ets").write_text(
        "export const PAGE_MEDIA: string = 'MediaActivity';\n"
        "export const PAGE_VIEWER: string = 'ViewerActivity';\n", encoding="utf-8")
    (ets / "MainActivity.ets").write_text(
        "pageStack: NavPathStack = new NavPathStack();\n"
        "this.pageStack.pushPathByName(PAGE_MEDIA, param);\n"
        "this.pageStack.pushPathByName('SettingsActivity', {});\n", encoding="utf-8")
    return tmp_path


def _spec_payload(*rows) -> dict:
    return {"schema_version": 1, "screens": [
        {"screen": name, "android": {"structure": "x"}, "port": {"screen": port}} for name, port in rows
    ]}


def test_port_check_flags_a_screen_name_the_port_does_not_have(tmp_path) -> None:
    from ecat.uiflow.portcheck import check_spec

    root = _port_tree(tmp_path, ["pages/Garden", "pages/PlantDetail"])
    res = check_spec(_spec_payload(("garden", "Garden.ets"), ("ghost", "Index.ets")), root)
    unknown = [f for f in res.findings if f.check == "unknown_screen"]
    assert [f.screen for f in unknown] == ["Index.ets"]
    assert res.errors, "a typo in the spec must block the diff, not become a missing page"
    assert {"Garden", "PlantDetail"} == set(res.pages.registered) and res.pages.entry == "Garden"


def test_port_check_exposes_a_registered_page_nothing_navigates_to(tmp_path) -> None:
    from ecat.uiflow.portcheck import check_spec

    root = _port_tree(tmp_path, ["pages/Garden", "pages/PlantDetail", "pages/Index"],
                      pushes=[("Garden", "PlantDetail")])
    res = check_spec(_spec_payload(("garden", "Garden.ets"), ("detail", "PlantDetail.ets")), root)
    orphans = [f.screen for f in res.findings if f.check == "orphan_page"]
    assert orphans == ["Index.ets"], "registered, no loadContent, no pushUrl -> unreachable"


def test_navigation_port_does_not_report_its_screens_as_missing(tmp_path) -> None:
    """ECAT-generated ports use NavPathStack: one registered page, screens named by constants."""
    from ecat.uiflow.portcheck import check_spec

    root = _nav_port_tree(tmp_path)
    res = check_spec(_spec_payload(("gallery", "Index.ets"), ("media", "MediaActivity.ets"),
                                   ("settings", "SettingsActivity.ets")), root)
    assert res.pages.model == "navigation"
    assert {"MediaActivity", "SettingsActivity"} <= res.pages.known_screens
    assert not res.errors, [f.detail for f in res.errors]
    assert "MediaActivity" in res.pages.nav_targets, "the constant was resolved to its value"
    assert "ViewerActivity" not in res.pages.nav_targets, "declared but never pushed: not a route"


def test_export_layout_finds_sources_under_ets_without_entry_prefix(tmp_path) -> None:
    """Snapshots keep only `ets/` — guessing the DevEco path made the whole check say 'unknown'."""
    from ecat.uiflow.portcheck import check_spec

    ets = tmp_path / "ets"
    (ets / "common").mkdir(parents=True)
    prof = tmp_path / "resources/base/profile"
    prof.mkdir(parents=True)
    (prof / "main_pages.json").write_text(json.dumps({"src": ["pages/Index"]}), encoding="utf-8")
    (ets / "entryability").mkdir(parents=True)
    (ets / "entryability/EntryAbility.ets").write_text("windowStage.loadContent('pages/Index');",
                                                      encoding="utf-8")
    (ets / "common/Constants.ets").write_text(
        "export const PAGE_MEDIA: string = 'MediaActivity';", encoding="utf-8")
    (ets / "MainActivity.ets").write_text(
        "this.pageStack.pushPathByName(PAGE_MEDIA, param);", encoding="utf-8")

    res = check_spec(_spec_payload(("main", "Index.ets"), ("media", "MediaActivity.ets")), tmp_path)
    assert res.pages.model == "navigation"
    assert res.pages.entry == "Index"
    assert "MediaActivity" in res.pages.known_screens
    assert not res.errors


def test_unknown_routing_model_downgrades_the_gate_to_a_warning(tmp_path) -> None:
    from ecat.uiflow.portcheck import check_spec

    root = _port_tree(tmp_path, ["pages/Garden"], pushes=())  # no pushUrl anywhere
    res = check_spec(_spec_payload(("garden", "Garden.ets"), ("ghost", "Ghost.ets")), root)
    assert res.pages.model == "unknown"
    assert not res.errors, "an unparsed routing model must not block the diff"
    assert [f.check for f in res.findings if f.severity == "warning"] and any(
        f.check == "unknown_screen_unverified" for f in res.findings)


def test_router_port_still_blocks_a_screen_that_does_not_exist(tmp_path) -> None:
    from ecat.uiflow.portcheck import check_spec

    root = _port_tree(tmp_path, ["pages/Garden", "pages/PlantDetail"])
    res = check_spec(_spec_payload(("garden", "Garden.ets"), ("ghost", "Ghost.ets")), root)
    assert res.pages.model == "router"
    assert [f.screen for f in res.errors] == ["Ghost.ets"]


def test_port_check_warns_when_the_entry_page_is_never_asserted(tmp_path) -> None:
    from ecat.uiflow.portcheck import check_spec

    root = _port_tree(tmp_path, ["pages/Garden", "pages/PlantDetail"])
    res = check_spec(_spec_payload(("detail", "PlantDetail.ets")), root)
    assert [f.screen for f in res.findings if f.check == "entry_uncovered"] == ["Garden.ets"]


def test_port_check_flags_an_unfilled_port_screen(tmp_path) -> None:
    from ecat.uiflow.portcheck import check_spec

    root = _port_tree(tmp_path, ["pages/Garden"])
    res = check_spec(_spec_payload(("garden", "")), root)
    assert [f.check for f in res.findings if f.severity == "error"] == ["port_screen_missing"]


# ── flow audit: flows that cannot tell the truth ─────────────────────────────
def _spec(*flows: dict) -> dict:
    return {"flows": list(flows)}


def test_audit_flags_a_flow_without_assertions() -> None:
    from ecat.uiflow.audit import audit_flows

    res = audit_flows(_spec({"id": "walk", "steps": [{"launch": True}, {"tap": "A"}]}))
    assert [f.check for f in res.findings] == ["no_assertion"]
    assert res.ok is False, "a flow with no assertion can never fail"


def test_audit_flags_taps_without_a_navigation_assertion() -> None:
    from ecat.uiflow.audit import audit_flows

    res = audit_flows(_spec({"id": "tap_only", "steps": [
        {"launch": True}, {"tap": "Settings"}, {"expect_visible": ["Settings"]},
    ]}))
    assert "no_navigation_assertion" not in [f.check for f in res.findings], "expect_visible counts"
    res2 = audit_flows(_spec({"id": "tap_only", "steps": [
        {"launch": True}, {"tap": "Settings"}, {"expect_absent": ["Error"]},
    ]}))
    checks2 = [f.check for f in res2.findings]
    assert "no_navigation_assertion" in checks2, "an assertion that is not about navigation"


def test_audit_flags_reset_that_wipes_the_data_it_expects() -> None:
    from ecat.uiflow.audit import audit_flows

    res = audit_flows(_spec({"id": "seeded", "steps": [
        {"reset": True}, {"launch": True}, {"expect_visible": ["Apple"]},
    ]}))
    checks = [f.check for f in res.findings]
    assert "reset_vs_expectation" in checks


def test_audit_flags_declared_screens_without_markers() -> None:
    from ecat.uiflow.audit import audit_flows

    res = audit_flows(_spec({"id": "s", "screens": ["A.ets", "B.ets"],
                             "steps": [{"launch": True}, {"tap": "B"}, {"expect_visible": ["B"]}]}))
    assert "no_screen_markers" in [f.check for f in res.findings]

    marked = audit_flows(_spec({"id": "s", "screens": ["A.ets", "B.ets"], "steps": [
        {"launch": True, "screen": "A.ets"}, {"tap": "B", "screen": "B.ets"},
        {"expect_visible": ["B"], "screen": "B.ets"},
    ]}))
    assert "no_screen_markers" not in [f.check for f in marked.findings]


def test_audit_cross_checks_a_report_for_silent_passes() -> None:
    from ecat.uiflow.audit import audit_flows

    spec = _spec({"id": "ok", "steps": [{"launch": True}, {"expect_visible": ["A"]}]})
    silent = audit_flows(spec, {"flows": [{"id": "ok", "status": "pass"}]})
    assert any(f.check == "pass_without_evidence" for f in silent.findings)
    traced = audit_flows(spec, {"flows": [{"id": "ok", "status": "pass",
                                           "trace": [{"step": 0, "action": "launch"}]}]})
    assert not any(f.check == "pass_without_evidence" for f in traced.findings)


# ── guards: refuse to print a number that means nothing ──────────────────────
def test_set_diff_is_invalid_when_the_port_side_is_empty() -> None:
    base = _graph(["Main", "Media"])
    result = diff_graphs(base, PageGraph(side="oh", method="flows"))
    assert result.valid is False
    assert "C has no pages" in result.invalid_reason
    assert result.fn_rate == 0.0, "an invalid result must not advertise 100%"


def test_set_diff_is_invalid_when_the_base_side_is_empty() -> None:
    result = diff_graphs(_graph([]), _graph(["Main"]))
    assert result.valid is False and "B has no reachable pages" in result.invalid_reason


def test_spec_diff_is_invalid_when_nothing_matches() -> None:
    from ecat.uiflow.spec import ScreenSpec, UiflowSpec, diff_against_spec

    base = _discovery_graph([("aaaa1111", "Garden")])
    spec = UiflowSpec(screens=[ScreenSpec(screen="garden", android_structure="deadbeef")])
    result = diff_against_spec(base, PageGraph(side="oh", method="flows"), spec)
    assert result.valid is False and result.fn_rate == 0.0
    assert "no spec screen matched" in result.invalid_reason


def test_a_failed_flow_still_credits_screens_the_trace_reached() -> None:
    """A tap that fails later must not erase a screen the walk already visited."""
    from ecat.uiflow.flows_report import graph_from_flows_report

    payload = _flows_report({
        "id": "halfway",
        "status": "fail",
        "step": 2,
        "step_name": "tap",
        "message": "element not found: Save",
        "screens": ["Index.ets", "Detail.ets", "Garden.ets"],
        "trace": [
            {"step": 0, "action": "launch", "screen": "Index.ets"},
            {"step": 1, "action": "tap(Item)", "screen": "Detail.ets"},
            {"step": 2, "action": "tap(Save)"},
            {"step": 3, "action": "tap(Garden)", "screen": "Garden.ets"},  # never ran
        ],
    })
    g = graph_from_flows_report(payload)
    reached = {p.label: p for p in g.pages if p.countable}
    assert set(reached) == {"Index.ets", "Detail.ets"}, "only markers at or before the failure count"
    assert reached["Detail.ets"].extra["reached_via_trace"] is True
    assert g.stats["partial"] == 1


def test_trace_markers_give_edges_even_when_the_flow_fails() -> None:
    from ecat.uiflow.flows_report import graph_from_flows_report

    payload = _flows_report({
        "id": "halfway", "status": "fail", "step": 1, "step_name": "tap",
        "screens": ["Index.ets", "Detail.ets"],
        "trace": [
            {"step": 0, "action": "launch", "screen": "Index.ets"},
            {"step": 1, "action": "tap(Item)", "screen": "Detail.ets"},
        ],
    })
    g = graph_from_flows_report(payload)
    assert [(e.action, e.source) for e in g.edges] == [("tap(Item)", "trace")]


def test_trace_gives_action_level_edges() -> None:
    from ecat.uiflow.flows_report import graph_from_flows_report

    payload = _flows_report({
        "id": "journey",
        "status": "pass",
        "screens": ["Index.ets", "Settings.ets"],
        "trace": [
            {"step": 0, "action": "launch", "screen": "Index.ets"},
            {"step": 1, "action": "tap(Settings)", "screen": "Settings.ets"},
            {"step": 2, "action": "back", "screen": "Index.ets"},
        ],
    })
    g = graph_from_flows_report(payload)
    assert [(e.src, e.action, e.dst) for e in g.edges] == [
        ("flow:Index.ets", "tap(Settings)", "flow:Settings.ets"),
        ("flow:Settings.ets", "back", "flow:Index.ets"),
    ]
    assert all(e.source == "trace" for e in g.edges)
    assert g.stats["edges"] == "trace (action-level)"


def test_trace_screen_not_declared_still_gets_a_page() -> None:
    from ecat.uiflow.flows_report import graph_from_flows_report

    payload = _flows_report({
        "id": "journey", "status": "pass", "screens": ["Index.ets"],
        "trace": [
            {"step": 0, "action": "launch", "screen": "Index.ets"},
            {"step": 1, "action": "tap(About)", "screen": "About.ets"},
        ],
    })
    g = graph_from_flows_report(payload)
    assert sorted(p.label for p in g.pages) == ["About.ets", "Index.ets"]
    assert g.pages[[p.label for p in g.pages].index("About.ets")].extra["from_trace"] is True


def test_flow_order_edges_come_only_from_passing_flows() -> None:
    from ecat.uiflow.flows_report import graph_from_flows_report

    payload = _flows_report(
        {"id": "journey", "status": "pass", "screens": ["Index.ets", "Detail.ets"]},
        {"id": "broken", "status": "fail", "screens": ["A.ets", "B.ets"], "step_name": "tap"},
    )
    g = graph_from_flows_report(payload)
    assert [(e.src, e.dst) for e in g.edges] == [("flow:Index.ets", "flow:Detail.ets")]
    assert g.edges[0].source == "flow-order"
    assert g.stats["edges"] == "flow-order (coarse)"


# ── baselines (pin an exploration to one APK build) ──────────────────────────
def test_baseline_roundtrip_is_keyed_by_apk_sha(tmp_path) -> None:
    from ecat.uiflow import discover

    utg = tmp_path / "utg.js"
    utg.write_text("var utg =\n" + json.dumps({"app_package": "pkg", "num_nodes": 1}), encoding="utf-8")
    sha = "a" * 64

    assert discover.load_baseline(tmp_path / "baselines", sha) is None
    stored = discover.store_baseline(utg, tmp_path / "baselines", sha)
    assert stored.name == f"{sha}.utg.js" and stored.is_file()
    assert discover.load_baseline(tmp_path / "baselines", sha) == stored
    assert discover.load_baseline(tmp_path / "baselines", "b" * 64) is None, "another build must not reuse it"


def test_graph_carries_the_build_identity() -> None:
    from ecat.uiflow.discover import graph_from_utg

    fw = _utg_framework()
    fw["app_sha256"] = "c" * 64
    g = graph_from_utg(fw)
    assert g.stats["app_sha256"] == "c" * 64


def test_spec_records_the_build_and_warns_when_it_is_stale() -> None:
    from ecat.uiflow.models import PageGraph
    from ecat.uiflow.spec import diff_against_spec, spec_from_graph

    base = _discovery_graph([("aaaa1111", "Garden")])
    base.stats["app_sha256"] = "d" * 64
    spec = spec_from_graph(base)
    assert spec.android_sha256 == "d" * 64

    spec.android_sha256 = "e" * 64  # regenerate-worthy: different build
    result = diff_against_spec(base, PageGraph(side="oh", method="flows"), spec)
    assert any("different build" in w for w in result.warnings)


# ── spec mode (identity declared once, no fuzzy matching) ────────────────────
def _discovery_graph(structures: list[tuple[str, str]]) -> PageGraph:
    """(structure, label) pairs -> a discovery-style graph, one page per structure."""
    g = PageGraph(side="android", method="click", engine="droidbot")
    for idx, (structure, label) in enumerate(structures):
        g.add_page(
            Page(
                key=f"state-{structure}-{idx}",
                label=f"{label}[${structure[:6]}]",
                component="pkg/.GardenActivity",
                verdict="reached",
                extra={"structure": structure, "activity": ".GardenActivity"},
            )
        )
    return g


def test_spec_from_graph_emits_one_screen_per_page() -> None:
    from ecat.uiflow.spec import spec_from_graph

    spec = spec_from_graph(_discovery_graph([("aaaa1111", "Garden"), ("bbbb2222", "Detail")]))
    assert [s.screen for s in spec.screens] == ["Garden@aaaa11", "Detail@bbbb22"]
    assert all(not s.port_key for s in spec.screens), "port names are a human step"
    assert len(spec.missing_port_screens()) == 2


def test_spec_diff_matches_android_by_structure_not_by_text() -> None:
    from ecat.uiflow.spec import ScreenSpec, UiflowSpec, diff_against_spec

    base = _discovery_graph([("aaaa1111", "Garden"), ("bbbb2222", "Detail")])
    port = PageGraph(side="oh", method="flows")
    # same visible text as the Android page, different identity: must NOT match
    port.add_page(Page(key="flow:Garden.ets", label="Garden.ets", component="p#Garden.ets"))
    spec = UiflowSpec(screens=[ScreenSpec(screen="garden", android_structure="aaaa1111",
                                         port_screen="Garden.ets")])
    result = diff_against_spec(base, port, spec)
    row = result.rows[0]
    assert row.android_reached and row.port_present
    assert result.fn_rate == 0.0


def test_spec_can_split_two_screens_that_share_one_layout() -> None:
    """Same content-free layout can host two screens; the spec declares the split by text."""
    from ecat.uiflow.spec import ScreenSpec, UiflowSpec, diff_against_spec

    base = _discovery_graph([("aaaa1111", "Garden"), ("aaaa1111", "Plant list")])
    port = PageGraph(side="oh", method="flows")
    for screen in ("Garden.ets", "Index.ets"):
        port.add_page(Page(key=f"flow:{screen}", label=screen, component=f"p#{screen}"))
    spec = UiflowSpec(screens=[
        ScreenSpec(screen="garden", android_structure="aaaa1111", android_text="Garden",
                   port_screen="Garden.ets"),
        ScreenSpec(screen="list", android_structure="aaaa1111", android_text="list",
                   port_screen="Index.ets"),
    ])
    result = diff_against_spec(base, port, spec)
    assert result.android_reached == 2 and not result.missing
    assert [r.android_page for r in result.rows] == ["Garden[$aaaa11]", "Plant list[$aaaa11]"]


def test_spec_diff_counts_missing_and_unlisted_android_pages() -> None:
    from ecat.uiflow.spec import ScreenSpec, UiflowSpec, diff_against_spec

    base = _discovery_graph([("aaaa1111", "Garden"), ("bbbb2222", "Detail"), ("cccc3333", "Settings")])
    port = PageGraph(side="oh", method="flows")
    port.add_page(Page(key="flow:Garden.ets", label="Garden.ets", component="p#Garden.ets"))
    spec = UiflowSpec(screens=[
        ScreenSpec(screen="garden", android_structure="aaaa1111", port_screen="Garden.ets"),
        ScreenSpec(screen="detail", android_structure="bbbb2222", port_screen="Detail.ets"),
    ])
    result = diff_against_spec(base, port, spec)
    assert [r.screen for r in result.missing] == ["detail"]
    assert result.fn_rate == pytest.approx(0.5)
    assert result.unlisted_android == ["Settings[$cccc33]"], "spec coverage gap is a number, not prose"
    assert result.spec_coverage == pytest.approx(0.6667, abs=1e-4)
    assert any("not in the spec" in w for w in result.warnings)


def test_spec_diff_warns_on_stale_structures_and_unfilled_screens() -> None:
    from ecat.uiflow.spec import ScreenSpec, UiflowSpec, diff_against_spec

    base = _discovery_graph([("aaaa1111", "Garden")])
    spec = UiflowSpec(screens=[ScreenSpec(screen="garden", android_structure="deadbeef")])
    result = diff_against_spec(base, PageGraph(side="oh", method="flows"), spec)
    assert result.android_reached == 0
    assert any("stale spec" in w for w in result.warnings)
    assert any("no port screen declared" in w for w in result.warnings)


def test_spec_diff_refuses_amstart_denominator() -> None:
    from ecat.uiflow.spec import UiflowSpec, diff_against_spec

    base = PageGraph(side="android", method="amstart")
    with pytest.raises(ValueError, match="discovery"):
        diff_against_spec(base, PageGraph(side="oh", method="flows"), UiflowSpec())


# ── T2 functional gate (ui_runner) import ────────────────────────────────────
def _flows_report(*flows: dict) -> dict:
    return {
        "runner": "hypium",
        "device": "device-1",
        "bundle": "com.example.port",
        "flows": list(flows),
        "summary": {"total": len(flows), "passed": 0, "failed": 0, "errored": 0, "skipped": 0},
    }


def test_flows_report_rejects_other_json(tmp_path) -> None:
    from ecat.uiflow.flows_report import FlowsReportError, load_flows_report

    bad = tmp_path / "notaflows.json"
    bad.write_text('{"screens": []}', encoding="utf-8")
    with pytest.raises(FlowsReportError, match="flows"):
        load_flows_report(bad)


def test_passing_flows_yield_one_page_per_screen() -> None:
    from ecat.uiflow.flows_report import graph_from_flows_report

    payload = _flows_report(
        {"id": "a", "status": "pass", "screens": ["Index.ets"]},
        {"id": "b", "status": "pass", "screens": ["Index.ets", "Detail.ets"]},
    )
    g = graph_from_flows_report(payload)
    assert g.side == "oh" and g.method == "flows"
    assert g.engine == "functional-gate:hypium"
    assert sorted(p.label for p in g.pages) == ["Detail.ets", "Index.ets"], "screens deduped"
    assert all(p.countable for p in g.pages)
    assert g.stats["pass"] == 2


def test_assertion_failure_still_means_the_screen_was_reached() -> None:
    from ecat.uiflow.flows_report import graph_from_flows_report

    payload = _flows_report(
        {"id": "a", "status": "fail", "screens": ["Garden.ets"], "step_name": "expect_visible",
         "message": "missing ['Water every 30 days']"},
    )
    g = graph_from_flows_report(payload)
    page = g.pages[0]
    assert page.countable, "navigation happened, only the assertion failed"
    assert page.extra["assert_failed"] is True

    strict = graph_from_flows_report(payload, include_failed_assertions=False)
    assert not strict.pages[0].countable
    assert strict.stats["fail_assert"] == 1


def test_action_failure_means_the_screen_was_not_reached() -> None:
    from ecat.uiflow.flows_report import graph_from_flows_report

    payload = _flows_report(
        {"id": "a", "status": "fail", "screens": ["Garden.ets"], "step_name": "tap",
         "message": "element not found"},
    )
    g = graph_from_flows_report(payload)
    assert g.pages[0].verdict == "finished" and not g.pages[0].countable


def test_a_passing_flow_outranks_a_failing_one_for_the_same_screen() -> None:
    from ecat.uiflow.flows_report import graph_from_flows_report

    payload = _flows_report(
        {"id": "late", "status": "fail", "screens": ["Index.ets"], "step_name": "tap"},
        {"id": "early", "status": "pass", "screens": ["Index.ets"]},
    )
    g = graph_from_flows_report(payload)
    assert len(g.pages) == 1 and g.pages[0].countable


def test_skipped_and_errored_flows_are_recorded_but_not_counted() -> None:
    from ecat.uiflow.flows_report import graph_from_flows_report

    payload = _flows_report(
        {"id": "s", "status": "skip", "screens": ["A.ets"]},
        {"id": "e", "status": "error", "screens": ["B.ets"]},
        {"id": "n", "status": "pass", "screens": []},
    )
    g = graph_from_flows_report(payload)
    assert sorted(p.label for p in g.pages) == ["A.ets", "B.ets"]
    assert not any(p.countable for p in g.pages)
    assert g.stats["skip"] == 1 and g.stats["error"] == 1 and g.stats["no_screens"] == 1


# ── discovery (DroidBot) ──────────────────────────────────────────────────────
def _utg_framework() -> dict:
    return {
        "app_package": "pkg",
        "app_main_activity": ".Main",
        "device_serial": "emulator-1",
        "test_date": "2026-01-01 00:00:00",
        "time_spent": 12.0,
        "num_nodes": 3,
        "num_edges": 2,
        "num_reached_activities": 2,
        "nodes": [
            {"id": "n1", "label": "Main", "package": "pkg", "activity": ".Main",
             "structure_str": "aaaa1111", "image": "states/a.png",
             "content": "pkg\n.Main\naaaa1111\nandroid:id/content,Main screen,Open detail"},
            {"id": "n2", "label": "Detail", "package": "pkg", "activity": ".Detail",
             "structure_str": "bbbb2222", "image": "states/b.png",
             "content": "pkg\n.Detail\nbbbb2222\nandroid:id/content,Detail screen,Back"},
            {"id": "n3", "label": "Launcher", "package": "com.other", "activity": ".Home",
             "structure_str": "cccc3333", "image": "states/c.png",
             "content": "com.other\n.Home\ncccc3333\nandroid:id/content,Launcher,All apps"},
        ],
        "edges": [
            {"from": "n1", "to": "n2",
             "events": [{"event_type": "touch", "event_str": "TouchEvent(view=Button)"}]},
            {"from": "n3", "to": "n1",
             "events": [{"event_type": "intent", "event_str": "IntentEvent(am start)"}]},
        ],
    }


def test_parse_utg_strips_js_assignment(tmp_path) -> None:
    from ecat.uiflow.discover import parse_utg

    f = tmp_path / "utg.js"
    f.write_text("var utg =\n" + json.dumps(_utg_framework()), encoding="utf-8")
    fw = parse_utg(f)
    assert fw["app_package"] == "pkg"
    assert len(fw["nodes"]) == 3


def test_graph_from_utg_drops_other_apps() -> None:
    from ecat.uiflow.discover import graph_from_utg

    g = graph_from_utg(_utg_framework())
    assert g.engine == "droidbot"
    assert g.method == "click"
    assert [p.component for p in g.pages] == ["pkg/.Main", "pkg/.Detail"], "other apps dropped"
    assert len(g.edges) == 1 and g.edges[0].src == g.pages[0].key
    assert "Main" in g.pages[0].label


def test_graph_from_utg_can_keep_external() -> None:
    from ecat.uiflow.discover import graph_from_utg

    g = graph_from_utg(_utg_framework(), include_external=True)
    assert len(g.pages) == 3
    assert any(p.extra.get("external") for p in g.pages)


def test_find_checkout_explains_the_pypi_trap(tmp_path, monkeypatch) -> None:
    from ecat.uiflow import discover

    monkeypatch.setattr(discover, "_fallback_homes", lambda: [])
    monkeypatch.delenv("ECAT_DROIDBOT_HOME", raising=False)
    with pytest.raises(discover.DroidBotError, match="start.py|checkout"):
        discover.find_checkout(tmp_path / "not-a-checkout")


def test_discover_retries_after_a_transient_droidbot_failure(tmp_path, monkeypatch) -> None:
    """DroidBot dies on one adb blip: the runner must start a fresh attempt, not give up."""
    from ecat.uiflow import discover

    checkout = tmp_path / "droidbot"
    (checkout / "droidbot").mkdir(parents=True)
    (checkout / "droidbot" / "__init__.py").write_text("", encoding="utf-8")
    (checkout / "start.py").write_text(
        "import json, pathlib, sys\n"
        "m = pathlib.Path('attempts.txt')\n"
        "n = int(m.read_text()) + 1 if m.exists() else 1\n"
        "m.write_text(str(n))\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "if n == 1:\n"
        "    print(\"AttributeError: 'NoneType' object has no attribute 'tear_down'\")\n"
        "    sys.exit(1)\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "utg = {'app_package': 'pkg', 'num_nodes': 1, 'num_edges': 0,\n"
        "       'nodes': [{'id': 'n1', 'package': 'pkg', 'activity': '.Main', 'structure_str': 'aaaa',\n"
        "                  'content': 'pkg\\\\n.Main\\\\naaaa\\\\nandroid:id/content,Main screen'}],\n"
        "       'edges': []}\n"
        "(out / 'utg.js').write_text('var utg =\\n' + json.dumps(utg))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(discover, "_check_interpreter", lambda *a, **k: None)

    class _StubAdb:
        serial = "stub"

        def shell(self, *a, **k):
            return "ok"

        def run(self, *a, **k):
            return ""

        def wake(self):
            return None

    run = discover.run_droidbot(
        tmp_path / "app.apk", "stub", tmp_path / "out",
        home=checkout, python="python", tries=2, adb=_StubAdb(),  # type: ignore[arg-type]
    )
    assert run.attempts == 2, "one transient failure must cost a retry, not the run"
    assert (checkout / "attempts.txt").read_text() == "2"
    assert run.graph.countable()[0].label.startswith("Main")


# ── discovery: DroidBot UTG -> PageGraph ─────────────────────────────────────
from ecat.uiflow.discover import graph_from_utg, visible_texts  # noqa: E402

PKG = "com.example.app"


def _content(nid: str, *texts: str) -> str:
    """DroidBot's `content` field: pkg, activity, state hash, then one block per view."""
    state = hashlib.md5(nid.encode("utf-8")).hexdigest()
    header = f"{PKG}\n.MainActivity\n{state}"
    if not texts:
        return header + "\n"
    return header + "\nandroid:id/content," + ",".join(texts)


def _node(nid: str, *texts: str, structure: str = "same") -> dict:
    return {"id": nid, "state_str": hashlib.md5(nid.encode("utf-8")).hexdigest(),
            "package": PKG, "activity": ".MainActivity",
            "structure_str": structure, "content": _content(nid, *texts),
            "image": f"states/screen_{nid}.png"}


def _utg(nodes: list[dict], edges: list[tuple[str, str]]) -> dict:
    return {"app_package": PKG, "app_main_activity": ".MainActivity", "device_serial": "emulator-1",
            "test_date": "", "nodes": nodes, "edges": [{"from": a, "to": b, "events": []} for a, b in edges]}


def test_visible_texts_drops_view_ids_and_header() -> None:
    node = _node("a", "Home", "Settings")
    assert visible_texts(node) == ["Home", "Settings"]
    assert all(":id/" not in t for t in visible_texts(node))


def test_utg_state_without_text_is_not_a_page() -> None:
    g = graph_from_utg(_utg([_node("blank"), _node("home", "Home", "Settings")], []))
    blank = next(p for p in g.pages if p.verdict == "empty_frame")
    assert not blank.countable, "a start-up frame with no text must not enter the denominator"
    assert len(g.countable()) == 1


def test_utg_identity_is_text_not_structure() -> None:
    """Same structure, different text -> two pages; repeated visit -> one page."""
    g = graph_from_utg(_utg([
        _node("d1", "Item one", "Watering needs"),
        _node("d2", "Item two", "Watering needs"),
        _node("d3", "Item one", "Watering needs"),  # same content as d1, other node id
    ], [("d1", "d2")],))
    assert len(g.pages) == 2, "structure is shared by both detail screens, text is not"
    assert len(g.countable()) == 2
    labels = [p.label for p in g.pages]
    assert any("Item one" in lbl for lbl in labels) and any("Item two" in lbl for lbl in labels)
    assert sum(1 for p in g.pages if "Item one" in p.label) == 1, "a revisit must not add a page"


def test_utg_edges_follow_deduped_pages() -> None:
    g = graph_from_utg(_utg([
        _node("a", "Home"), _node("b", "List"), _node("b2", "List"),
    ], [("a", "b"), ("a", "b2")]))
    assert len(g.pages) == 2
    assert len(g.edges) == 1, "edges to the same page collapse with it"
    assert g.edges[0].src == g.pages[0].key


def test_flows_port_diff_warns_about_spec_coverage() -> None:
    from ecat.uiflow.flows_report import graph_from_flows_report

    base = graph_from_utg(_utg([_node("a", "Home"), _node("b", "List"), _node("c", "Item one", "Detail")], []))
    port = graph_from_flows_report(_flows_report({"id": "home", "status": "pass", "screens": ["Home.ets"]}))
    res = diff_graphs(base, port)
    assert any("spec" in w for w in res.warnings), "a flow-backed port set must say so"


def test_label_is_stable_when_the_dump_reorders_the_same_texts() -> None:
    """Two dumps of one screen list its texts in a different order; the label must not move."""
    from ecat.uiflow.models import fingerprint_texts

    a = fingerprint_texts(["Plant list", "My garden", "Your garden is empty"], "pkg/.A")
    b = fingerprint_texts(["Your garden is empty", "My garden", "Plant list"], "pkg/.A")
    assert a[0] == b[0] and a[1] == b[1], "page identity must survive a reordered dump"


def test_parse_utg_accepts_its_own_snapshot(tmp_path) -> None:
    """`--from-utg` must read the `*.utg.json` snapshot ECAT itself writes."""
    from ecat.uiflow.discover import parse_utg

    fw = {"app_package": "pkg", "nodes": [{"id": "n1", "content": "pkg\n.A\nh\nandroid:id/content,a=b"}]}
    plain = tmp_path / "snap.utg.json"
    plain.write_text(json.dumps(fw), encoding="utf-8")
    wrapped = tmp_path / "utg.js"
    wrapped.write_text("var utg =\n" + json.dumps(fw), encoding="utf-8")
    assert parse_utg(plain) == fw, "a plain JSON snapshot must not be truncated at the first '='"
    assert parse_utg(wrapped) == fw
