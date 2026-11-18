"""UI flow coverage — page sets per side, and the acceptance diff.

Three inputs, three different claims:

    A  inventory      method="amstart"  capability ceiling ("the page exists and renders")
    B  discover       method="click"    pages a user can reach on the original build
                                        (DroidBot, pinned by APK sha) -> FN denominator
    C  import-flows   method="flows"    screens the T2 functional gate proves the port
                                        reaches -> numerator

    FN rate = |B \\ C| / |B|      (an amstart graph is never a denominator)
    spec mode: identity is declared once (spec.py) — Android by layout structure, port by
    screen name — so no fuzzy name or text matching is involved.

Visual fidelity (T3) is a separate column and never enters the FN number.
"""

from ecat.uiflow.diff import DiffResult, diff_graphs
from ecat.uiflow.models import COUNTABLE_VERDICTS, Edge, Page, PageGraph, fingerprint, norm
from ecat.uiflow.spec import ScreenSpec, SpecDiffResult, UiflowSpec, diff_against_spec

__all__ = [
    "COUNTABLE_VERDICTS",
    "DiffResult",
    "Edge",
    "Page",
    "PageGraph",
    "ScreenSpec",
    "SpecDiffResult",
    "UiflowSpec",
    "diff_against_spec",
    "diff_graphs",
    "fingerprint",
    "norm",
]
