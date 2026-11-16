"""UI flow coverage — page inventory (A), click walk (B), parity diff (C vs B).

Three layers, three different claims:

    A  method="amstart"  every manifest activity, launched with explicit params
       -> capability ceiling ("the page exists and renders")
    B  method="click"    reached only by user-reachable clicks on the *original*
       APK -> the FN denominator ("what a user can actually get to")
    C  method="click"    same walk against the port target (OpenHarmony)
       -> the numerator

    FN rate = |B \\ C| / |B|

A must never be the denominator: `am_start` bypasses the real entry, so using it
would understate misses.
"""

from ecat.uiflow.diff import DiffResult, diff_graphs
from ecat.uiflow.models import COUNTABLE_VERDICTS, Edge, Page, PageGraph, fingerprint, norm

__all__ = [
    "COUNTABLE_VERDICTS",
    "DiffResult",
    "Edge",
    "Page",
    "PageGraph",
    "diff_graphs",
    "fingerprint",
    "norm",
]
