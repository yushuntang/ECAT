"""Unit tests for tools/ui_runner.py — the most-run, least-tested file in the chain.

It runs on a device and takes minutes per iteration, so a regression here is expensive to notice.
Most of its logic is pure once you take the phone away: dependency ordering, blocked-dependency
decisions, version parsing, label filtering, the page fingerprint, and dump parsing. None of that
needs hardware, so it is tested here by loading the script as a module.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[4] / "tools" / "ui_runner.py"


def _load_runner():
    if not RUNNER.is_file():
        pytest.skip(f"runner not found at {RUNNER}")
    name = "ui_runner_under_test"
    spec = importlib.util.spec_from_file_location(name, RUNNER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses look the module up by name
    spec.loader.exec_module(mod)
    return mod


ur = _load_runner()


# ── dependency ordering ──────────────────────────────────────────────────────
def test_order_flows_puts_dependencies_first_whatever_the_file_order() -> None:
    flows = [
        {"id": "detail", "needs": ["seed"]},
        {"id": "home"},
        {"id": "seed"},
    ]
    assert [f["id"] for f in ur.order_flows(flows)] == ["home", "seed", "detail"]


def test_order_flows_refuses_an_unknown_dependency() -> None:
    with pytest.raises(ValueError, match="not in the spec"):
        ur.order_flows([{"id": "detail", "needs": ["ghost"]}])


def test_order_flows_refuses_a_cycle() -> None:
    with pytest.raises(ValueError, match="cycle"):
        ur.order_flows([{"id": "a", "needs": ["b"]}, {"id": "b", "needs": ["a"]}])


def test_order_flows_refuses_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        ur.order_flows([{"id": "a"}, {"id": "a"}])


def test_only_a_declared_dependency_can_block_a_flow() -> None:
    statuses = {"seed": "fail", "other": "pass"}
    assert ur.dependency_blockers({"id": "x", "needs": ["seed"]}, statuses) == ["seed"]
    assert ur.dependency_blockers({"id": "x", "needs": ["other"]}, statuses) == []
    assert ur.dependency_blockers({"id": "x"}, statuses) == [], "no needs, no blocking"


# ── preflight parsing ───────────────────────────────────────────────────────
def test_app_version_comes_out_of_the_bm_dump() -> None:
    dump = '{"appId": "x", "versionName": "1.0.0", "versionCode": 1000000}'
    assert ur.app_version_from_dump(dump) == "1.0.0"
    assert ur.app_version_from_dump("no version here") == ""


# ── perception helpers ──────────────────────────────────────────────────────
def test_sig_masks_digits_so_timers_are_not_mistaken_for_navigation() -> None:
    a = ur.ScreenState(labels=["12:34", "Garden"])
    b = ur.ScreenState(labels=["12:35", "Garden"])
    assert a.sig() == b.sig(), "a ticking clock must not look like a page change"
    c = ur.ScreenState(labels=["Plant list"])
    assert c.sig() != a.sig()


def test_is_ui_label_keeps_text_and_drops_ids() -> None:
    assert ur.is_ui_label("My Garden")
    assert ur.is_ui_label("植物列表")
    assert not ur.is_ui_label("")
    assert not ur.is_ui_label("some_snake_case_id")
    assert not ur.is_ui_label("__Common__")
    assert not ur.is_ui_label("x" * 60), "a paragraph is not a control label"


def test_clickables_reads_bounds_and_centres() -> None:
    dump = (
        '{"attributes": {"text": "Settings", "type": "Button", "clickable": "true", '
        '"bounds": "[10,20][110,220]"}}'
    )
    hits = ur.clickables(dump)
    assert hits and hits[0]["type"] == "Button"
    assert (hits[0]["x"], hits[0]["y"]) == (60, 120)


def test_clickables_survives_a_broken_dump() -> None:
    assert ur.clickables("not json") == []


def test_screen_state_has_is_substring_and_case_insensitive() -> None:
    st = ur.ScreenState(labels=["My Garden", "Add plant"])
    assert st.has("garden") and st.has("PLANT")
    assert not st.has("compost")


def test_system_chrome_is_filtered_out_of_the_signature() -> None:
    assert ur.is_system_noise("LiveMetaBallBaseVm", "metaballNode")
    assert ur.is_system_noise("ClockStatusView")
    assert ur.is_system_noise("__Common__")
    assert not ur.is_system_noise("Plant list", "Text")
    assert not ur.is_system_noise("Watering needs")
