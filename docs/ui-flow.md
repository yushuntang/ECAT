# UI flow coverage — page reachability and FN rate

Answers one question with numbers: **did the port keep every screen a user can actually reach?**
It trusts neither class names, nor the manifest alone, nor "the launch returned 0".

    A  ecat uiflow inventory   patched build, one parameterised `am start` per activity
                               -> capability ceiling: the page exists and renders
    B  ecat uiflow walk        original build, clicks only, real entry
                               -> FN denominator: what a user can actually get to
    C  ecat uiflow walk        port target, same flows, clicks only
                               -> numerator
       ecat uiflow diff        |B \ C| / |B|, both directions reported

Nothing in this package is app-specific: the app enters at runtime through `--apk`,
`--params`, `--flows`, `--package`, `--ability` and `--mapping`. Templates live in
`examples/uiflow/` and are meant to be copied and filled in.

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
        --apk <exported-test.apk> \
        --serial <adb-serial> \
        --params <page-params.json> \
        --out reports/ui-flow/table-A.json

`--params` maps an activity's short name to `extras` / `int_extras` / `bool_extras`
(`--es` / `--ei` / `--ez`) plus `raw_args` (see `examples/uiflow/params.template.json`);
`pre_state` documents data a page needs but the probe cannot synthesise. Without
`--params` every activity is launched bare.

Verdicts are typed, because each failure means something different downstream:

| verdict | meaning | counts as reached |
|---|---|---|
| `reached` | still focused after `--settle` seconds | yes |
| `reached_trampoline` | finished by design and handed off to its successor | yes |
| `finished` | launched, then called `finish()` (usually a missing parameter or data) | no |
| `redirect` | in the stack but covered by something else | no |
| `blocked_dialog` | a permission/system dialog is on top | no |
| `crashed_abi` | missing `.so` for this device ABI (environment, not an app bug) | no |
| `crashed_app` | FATAL attributed to this activity | no |
| `rejected` | `am start` refused (not exported / not found) | no |

Reachability is read from `dumpsys window mCurrentFocus` **after** a settle delay, never
from `am start`'s own status: a page can render, log `Displayed ... +897ms`, throw
nothing, and still finish itself a moment later.

## 2. Layer B — click walk (denominator)

    ecat uiflow walk \
        --flows <flows.json> \
        --serial <adb-serial> \
        --out reports/ui-flow/table-B.json \
        --settle 2.0 [--edges-from-logcat] [--dismiss-labels "OK,Allow"]

Run it against the **original** APK: every page must be entered by a tap. Flow steps are
`launch`, `am_start`, `tap`, `back`, `expect_visible`, `expect_page_changed`, `sleep`
(see `examples/uiflow/flows.template.json`).

What the walk records:

* **pages** — activity + a fingerprint of visible text/content-desc only (no resource-ids,
  no clock or counter chrome), so badge numbers do not split one screen into several nodes
* **edges** — labelled with the action that produced them (`tap(<label>)`,
  `dismiss(dialog)`, `back`)
* **dialogs** — a first-run or permission dialog is snapshotted as a page with verdict
  `dialog` *before* it is dismissed: the user sees it, but it is not a product page, so it
  never enters the denominator. Which buttons to tap through comes from
  `--dismiss-labels` or the flows file's `dismiss_labels`.

## 2b. Layer B（发现）— exploration, for the pages nobody wrote a flow for

Hand-written flows only cover what their author knew about. `discover` runs an
exploration engine (DroidBot) to find the rest, and converts its UI transition graph
into the same `PageGraph` the diff consumes:

    ecat uiflow discover \
        --apk <original.apk> \
        --droidbot-home <droidbot-checkout> \
        --droidbot-python <python-with-droidbot-deps> \
        --count 25 --timeout 300 \
        --out reports/ui-flow/table-B-discovered.json

    # import a graph from a previous run instead of exploring again
    ecat uiflow discover --from-utg <run-dir>/utg.js --out reports/ui-flow/table-B-discovered.json

The graph is tagged `engine=droidbot`; screens belonging to other apps are dropped
unless `--include-external` is passed.

Three DroidBot traps are handled rather than documented away:

* a PyPI install cannot run (`droidbot/__init__.py` does `import start`) — the command
  insists on a checkout and runs `start.py` with the checkout as cwd;
* setuptools >= 81 dropped `pkg_resources`, which DroidBot imports — detected up front
  with the exact fix printed instead of a traceback mid-run;
* DroidBot's own accessibility bootstrap loops forever on newer Android — the secure
  setting is written here and then verified through `dumpsys activity services`.

It also pre-flights the device and retries the whole exploration on transient adb
failures, because DroidBot dies on a single `getprop` blip
(`AttributeError: 'NoneType' object has no attribute 'tear_down'`).

Discovery is heuristic: two runs can explore different states. Use it to find pages,
not as the FN denominator — keep the click walk as B when you need a stable number.

## 3. Layer C and the diff

Same flow file, executed against the ported app:

    # OpenHarmony target (hdc + uitest + aa), same flow shape
    ecat uiflow walk --driver hdc --flows <flows.json> \
        --package <bundle> --ability <UIAbility> \
        --out reports/ui-flow/table-C.json

    ecat uiflow diff --base reports/ui-flow/table-B.json --port reports/ui-flow/table-C.json \
        --mapping <activities-to-abilities.json> --out reports/ui-flow/diff.json

Diff writes JSON + markdown:

    B=3 C=2 matched=2 missing=1 extra=0 FN=33.3%
      missing <page label> (<component>)

* `B\C` — missing page: the port lost something a user could reach → **FN**
* `C\B` — extra page: merged / split / added structure → reported, not FN

Matching order: explicit `--mapping`, then component / short name, then label, then the
visible texts a page carries. The last one matters on OpenHarmony, where a single
UIAbility hosts every page — the component is identical everywhere and only the texts
distinguish pages, so without a mapping the text fallback is what lines the sides up.

Neither side's naming is assumed: `--mapping` is how you declare that one side's page is
the other side's page when names differ.

## Two rules that decide whether the number means anything

1. **The denominator is the click walk of the original build.** Never an `amstart` graph;
   the command enforces it.
2. **B and C must see equivalent data.** No media, empty database, no logged-in account:
   the port can only show an empty screen, so every page the original reached reports as
   missing. That is a data-parity gap, not a port defect — seed both sides with the same
   fixtures before quoting an FN rate.
