"""Unit tests for ecat.uiflow — pure data/maths, no device."""

from __future__ import annotations

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


def test_diff_matches_oh_page_by_visible_text() -> None:
    """OH: one UIAbility hosts every page, so identity must come from the texts."""
    base = PageGraph(side="android", method="click")
    base.add_page(Page(key="a1", label='GridActivity[$aa] "FolderOne"', component="pkg/.activities.GridActivity"))
    port = PageGraph(side="oh", method="click")
    port.add_page(Page(key="c1", label='EntryAbility[$bb] "FolderOne"', component="pkg:entry:EntryAbility"))
    res = diff_graphs(base, port)
    assert not res.missing and not res.extra
    assert res.matched[0]["by"] == "visible text"


def test_diff_empty_base_warns() -> None:
    res = diff_graphs(_graph([]), _graph(["Main"]))
    assert res.fn_rate == 0.0
    assert res.warnings


def test_diff_report_notes_denominator_rule() -> None:
    res = diff_graphs(_graph(["Main", "Media"]), _graph(["Main"]))
    md = res.to_markdown()
    assert "B vs C" in md or "FN" in md
    assert "Search" not in md


# ── functional gate import ───────────────────────────────────────────────────
def _gate_payload() -> dict:
    return {
        "rubric": {"weights": {}, "floors": {}},
        "summary": {"screens": 2, "pass": 1, "suspect": 0, "fail": 1},
        "screens": [
            {"screen": "Home", "verdict": "pass", "score": 0.93, "missing": []},
            {"screen": "Settings", "verdict": "fail", "score": 0.41,
             "reason": "floor: block_match<0.85", "missing": ["Dark mode"]},
        ],
    }


def test_gate_report_rejects_other_json(tmp_path) -> None:
    from ecat.uiflow.gate import GateReportError, load_gate_report

    bad = tmp_path / "notgate.json"
    bad.write_text('{"nodes": []}', encoding="utf-8")
    with pytest.raises(GateReportError, match="screens"):
        load_gate_report(bad)


def test_gate_report_becomes_port_page_set() -> None:
    from ecat.uiflow.gate import graph_from_gate_report

    g = graph_from_gate_report(_gate_payload(), bundle="com.example.port")
    assert g.side == "oh" and g.engine == "functional-gate" and g.method == "gate"
    assert [p.label for p in g.pages] == ["Home", "Settings"]
    assert all(p.countable for p in g.pages), "the port renders both screens; fidelity is extra"
    assert g.pages[1].extra["gate_verdict"] == "fail"
    assert g.pages[1].extra["missing"] == ["Dark mode"]


def test_gate_only_pass_filters_screens() -> None:
    from ecat.uiflow.gate import graph_from_gate_report

    g = graph_from_gate_report(_gate_payload(), only_pass=True)
    assert [p.label for p in g.pages] == ["Home"]


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
             "structure_str": "aaaa1111", "image": "states/a.png"},
            {"id": "n2", "label": "Detail", "package": "pkg", "activity": ".Detail",
             "structure_str": "bbbb2222", "image": "states/b.png"},
            {"id": "n3", "label": "Launcher", "package": "com.other", "activity": ".Home",
             "structure_str": "cccc3333", "image": "states/c.png"},
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
    assert len(g.edges) == 1 and g.edges[0].src == "n1"
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
        "       'nodes': [{'id': 'n1', 'package': 'pkg', 'activity': '.Main', 'structure_str': 'aaaa'}],\n"
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
