# UI flow coverage — page reachability and FN rate

Answers one question with numbers: **did the port keep every screen a user can actually reach?**
It trusts neither class names, nor the manifest alone, nor "the launch returned 0".

    A  ecat uiflow inventory    patched build, one parameterised `am start` per activity
                                -> capability ceiling: the page exists and renders
    B  ecat uiflow discover     exploration on the original build (DroidBot)
                                -> page set a user can reach, including pages nobody modelled
    C  ecat uiflow import-gate  the port side, taken from the existing functional gate report
                                -> what the port actually renders
       ecat uiflow diff         |B \ C| / |B|, both directions reported

Flows, screenshots and per-screen assertions stay where they already live in ECAT (the
`ui_flows` runner and the functional gate). This package adds no second flow format: it
reuses the gate's `gate.json` for the port side and only adds discovery for the Android
side.

`ecat uiflow diff` refuses an `amstart` graph as the denominator unless `--force` is
passed: direct entry skips the real entry, so misses would be measured against a bar no
user can clear.

## 1. Layer A — inventory (capability ceiling)

`am start` is refused for an activity declared `android:exported="false"`
(`Permission Denial: ... not exported from uid ...`), and the check runs before any extra
is parsed, so no `-e key value` gets past it. Probe a test build whose components are all
exported; producing that build is the caller's job (decode with apktool, set
`exported="true"`, rebuild, sign with the debug key, uninstall + install).

    ecat uiflow inventory \
        --apk <exported-test.apk> --serial <adb-serial> \
        --params <page-params.json> --out reports/ui-flow/table-A.json

`--params` maps an activity's short name to `extras` / `int_extras` / `bool_extras`
(`--es` / `--ei` / `--ez`) plus `raw_args` (see `examples/uiflow/params.template.json`);
`pre_state` documents data a page needs but the probe cannot synthesise. Without
`--params` every activity is launched bare.

| verdict | meaning | counts as reached |
|---|---|---|
| `reached` | still focused after `--settle` seconds | yes |
| `reached_trampoline` | finished by design and handed off to its successor | yes |
| `finished` | launched, then called `finish()` (missing parameter or data) | no |
| `redirect` | in the stack but covered by something else | no |
| `blocked_dialog` | a permission/system dialog is on top | no |
| `crashed_abi` | missing `.so` for this device ABI (environment, not an app bug) | no |
| `crashed_app` | FATAL attributed to this activity | no |
| `rejected` | `am start` refused (not exported / not found) | no |

Reachability is read from `dumpsys window mCurrentFocus` **after** a settle delay, never
from `am start`'s own status: a page can render, log `Displayed ... +897ms`, throw
nothing, and still finish itself a moment later.

## 2. Layer B — discovery (DroidBot)

Hand-written flows only cover pages someone already knew about. `discover` explores the
original APK and converts DroidBot's UI transition graph into the same `PageGraph`:

    ecat uiflow discover \
        --apk <original.apk> \
        --droidbot-home <droidbot-checkout> --droidbot-python <python-with-deps> \
        --count 25 --timeout 300 \
        --out reports/ui-flow/table-B.json

    ecat uiflow discover --from-utg <run-dir>/utg.js --out reports/ui-flow/table-B.json

Graphs are tagged `engine=droidbot`; screens belonging to other apps are dropped unless
`--include-external` is passed.

Traps handled in code, not in a footnote:

* a PyPI install cannot run (`droidbot/__init__.py` does `import start`) — a checkout is
  required and `start.py` runs with the checkout as cwd;
* setuptools >= 81 dropped `pkg_resources`, which DroidBot imports — checked up front,
  the fix is printed instead of a mid-run traceback;
* DroidBot's accessibility bootstrap loops forever on newer Android — the secure setting
  is written here and verified through `dumpsys activity services`;
* one transient adb failure ends a DroidBot run — the device is pre-flighted and the
  whole exploration retried.

Discovery is heuristic: two runs can explore different states. Use it to find pages, and
keep a spec-driven list (the gate's screens) as the stable denominator when you need
comparable numbers across iterations.

## 3. Layer C — the port side, from the functional gate

    ecat uiflow import-gate --from <gate.json> --bundle <port.bundle> \
        --out reports/ui-flow/table-C.json [--only-pass]

Every screen in the gate report is a screen the port renders (there was a port
screenshot to score); the gate's own verdict (pass / suspect / fail), score, `missing`
list and evidence paths are kept in each page's `extra`. `--only-pass` narrows the port
set to screens that also pass the visual rubric.

Then:

    ecat uiflow diff --base reports/ui-flow/table-B.json --port reports/ui-flow/table-C.json \
        --mapping <android-to-port.json> --out reports/ui-flow/diff.json

    B=3 C=2 matched=2 missing=1 extra=0 FN=33.3%
      missing <page label> (<component>)

* `B\C` — missing page: the port has no such screen → **FN**
* `C\B` — extra page: merged / split / added structure → reported, not FN

Matching order: explicit `--mapping`, then component / short name, then label, then the
visible texts a page carries. Nothing about either side's naming is assumed; `--mapping`
is how you declare that one side's page is the other side's.

## Two rules that decide whether the number means anything

1. **The denominator is the Android side's reachable page set**, never an `amstart`
   capability table — the command enforces it.
2. **B and C must see equivalent data.** No media, empty database, no logged-in account:
   the port can only show an empty screen, so every page the original reached reports as
   missing. Seed both sides before quoting an FN rate.
