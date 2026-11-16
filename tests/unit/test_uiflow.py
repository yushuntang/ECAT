"""Unit tests for ecat.uiflow — pure data/maths, no device."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from ecat.uiflow.diff import diff_graphs
from ecat.uiflow.models import COUNTABLE_VERDICTS, Edge, Page, PageGraph, fingerprint, norm, short
from ecat.uiflow.walk import load_flows

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


# ── flows ─────────────────────────────────────────────────────────────────────
def test_load_flows_both_shapes(tmp_path) -> None:
    wrapped = tmp_path / "a.json"
    wrapped.write_text(
        json.dumps({"package": "pkg", "flows": [{"id": "f1", "steps": [{"launch": True}]}]}),
        encoding="utf-8",
    )
    pkg, flows = load_flows(wrapped)
    assert pkg == "pkg" and flows[0]["id"] == "f1"

    bare = tmp_path / "b.json"
    bare.write_text(json.dumps([{"id": "f2", "steps": []}]), encoding="utf-8")
    pkg2, flows2 = load_flows(bare)
    assert pkg2 is None and flows2[0]["id"] == "f2"
