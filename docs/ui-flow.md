# UI flow coverage — page reachability and FN rate

Answers one question with numbers: **did the port keep every screen a user can actually reach?**
It trusts neither class names, nor the manifest alone, nor "the launch returned 0".

    A  ecat uiflow inventory    patched build, one parameterised `am start` per activity
                                -> capability ceiling: the page exists and renders
    B  ecat uiflow discover     exploration on the original build (DroidBot)
                                -> page set a user can reach, including pages nobody modelled
    C  ecat uiflow import-flows  the port side, from the T2 functional gate's report
                                -> screens the port provably reaches
       ecat uiflow diff         |B \ C| / |B|, both directions reported

Flows, screenshots and per-screen assertions stay where they already live in ECAT (the
`ui_flows` runner and the functional gate). This package adds no second flow format: it
reuses the gate's `gate.json` for the port side and only adds discovery for the Android
side.

`ecat uiflow diff` refuses an `amstart` graph as the denominator unless `--force` is
passed: direct entry skips the real entry, so misses would be measured against a bar no
user can clear.

It also refuses to *print* an FN number when the number cannot mean anything — exit code
2, the JSON artifact is still written with `valid: false` and the reason:

* `C has no pages` — the T2 report was empty / not imported;
* `B has no reachable pages` — discovery produced nothing;
* `no spec screen matched an Android page` — the spec is older than the build
  (structures moved), so every screen would look missing.

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
  whole exploration is retried;
* **page identity comes from the visible texts**, read from the UTG's own `content`
  field. DroidBot's `structure_str` ignores text: every detail page of a list app shares
  one hash (six plants, one label), while one screen visited twice keeps two nodes;
* **a state with no text is not a page.** The frame the app shows between the launcher
  tap and the first draw has an empty `content`; it is recorded as `empty_frame` and
  stays out of the denominator, where it would otherwise be an unremovable "missing page".

### Baselines — the same build must give the same number

Discovery is heuristic: two runs can explore different states (9 pages/14 edges vs
9 pages/16 edges on the same build). So an exploration is pinned to the **APK sha256**
(`reports/ui-flow/baselines/<sha>.utg.js` by default):

    ecat uiflow discover --apk app.apk                      # explores once, pins the baseline
    ecat uiflow discover --apk app.apk                      # same sha -> reuses it (no device, ~1 s)
    ecat uiflow discover --apk app.apk --refresh            # deliberately explore again

The graph records `stats.baseline` = `captured` / `reused` / `imported` and
`stats.app_sha256`, and the spec records the sha it was generated from: a spec older than
the build raises a "different build" warning instead of silently comparing structures that
moved. Measured on a real APK: 117 s to explore, 0.8 s to reuse, identical graph.

## 3. Layer C — the port side, from the T2 functional gate

    ecat uiflow import-flows --from <ui_runner-report.json> --bundle <port.bundle> \
        --out reports/ui-flow/table-C.json [--no-failed-assertions]

`docs/ecat-validation-layers.en.md` defines two side channels: **T2 functional** (flows +
control-tree assertions, `tools/ui_runner.py`) and **T3 visual** (screenshot scoring,
`tools/visual_gate.py`). Only T2 feeds page coverage here — a port page exists when a
flow that declares it actually got there:

| flow status | failing step | page | counted |
|---|---|---|---|
| `pass` | — | reached | yes |
| `fail` | `expect_visible` / `expect_absent` / `expect_page_changed` | reached, `assert_failed` in `extra` | yes (disable with `--no-failed-assertions`) |
| `fail` | anything else (tap / input / launch) | `finished`, **unless the trace proves the screen** | traced screens yes, later declared screens no |
| `error` / `skip` | — | recorded with that verdict | no |

A flow that dies on a tap must not erase a screen it already visited: the step trace is
authoritative for what was on screen, so markers at or before the failing step count as
reached (`extra.reached_via_trace`), and their hops still become edges. Without a trace the
old rule applies — a flow that never got there credits nothing.

Flow pass/fail/skip/error counters land in the graph's `stats` (`partial` counts flows that
failed but still proved some screens), so a report can show how much of the number is
assertion failures rather than missing screens.

C also gets **action-level edges**, because `tools/ui_runner.py` now records a step trace
(`trace: [{step, action, screen?, sig?}]`) on every result, passing or failing. A flow step
may carry a `screen` marker — "after this action we are on that screen":

    {"tap": "Add folder", "screen": "Picker.ets"}

Consecutive markers become labelled edges (`source=trace`):

    Home.ets --tap(Add folder)--> Picker.ets

Verified on a real device: one flow, three steps, one edge. Reports produced before the
trace existed (or flows without `screen` markers) fall back to the declared screen order
(`source=flow-order`), which only says "related", not "by which action".

Visual fidelity is **not** part of this number: it is scored per screenshot pair by T3 and
belongs in its own column, otherwise a screen that exists but looks different gets counted
as a missing page.

## 3b. Spec mode — the join key, so no name or text matching is involved

Set-diff by name cannot work across platforms: the Android side is
`GardenActivity[$hash] "My garden…"`, the port side is `Garden.ets`, and the copy differs
by definition (a copy difference is a T3 finding, not an identity). So identity is
declared once in a spec file:

    ecat uiflow spec-from-discovery --graph reports/ui-flow/table-B.json \
        --out specs/ui-flow.spec.json        # skeleton: one row per content-free layout

    {"schema_version": 1,
     "screens": [
       {"screen": "garden",
        "android": {"structure": "c794bc91ea", "activity": ".GardenActivity"},
        "port": {"screen": "Garden.ets"}},
       {"screen": "detail",                       # same layout as another screen?
        "android": {"structure": "aaaa1111", "text": "Plant list"},   # split it by text
        "port": {"screen": "Index.ets"}}]}

    ecat uiflow diff --base <B.json> --port <C.json> --spec specs/ui-flow.spec.json \
        --out reports/ui-flow/diff.json

Rules the implementation enforces:

* **Android side** matches on the `structure` hash (content-free layout), optionally
  narrowed by an `text` substring — no fuzzy matching, no "similar name" fallback.
* **Port side** matches on the exact screen name the T2 report/flow spec uses.
* "A page" is one layout: data variants (same screen, other item) are states of that page.
  Otherwise adding a row to a list would raise the denominator and lower FN for nothing.
* The report carries numbers, not prose: `spec_coverage` (how much of the discovered
  Android page set the spec claims), `unlisted_android`, `unlisted_port`, `fn_rate`.

Then (set-diff mode, still available when no spec exists):

    ecat uiflow diff --base reports/ui-flow/table-B.json --port reports/ui-flow/table-C.json \
        --mapping <android-to-port.json> --out reports/ui-flow/diff.json

    B=3 C=2 matched=2 missing=1 extra=0 FN=33.3%
      missing <page label> (<component>)

`C` is bounded by the flow spec: a screen no flow mentions cannot appear in it, and the
diff reports that as a warning on the result.

* `B\C` — missing page: the port has no such screen → **FN**
* `C\B` — extra page: merged / split / added structure → reported, not FN

Matching order: explicit `--mapping`, then component / short name, then label, then the
visible texts a page carries. Nothing about either side's naming is assumed; `--mapping`
is how you declare that one side's page is the other side's.

## 2b. Preflight — fail in seconds, not in five minutes

`tools/ui_runner.py` checks the device and the installed build **before** any flow runs, and exits 2
with a readable reason instead of turning a locked screen or a wrong HAP into a batch of fake failures:

| condition | what happens |
|---|---|
| screen locked, no PIN | `wake_and_unlock()` is attempted, then re-checked |
| still locked (PIN) | exit 2 — a human must unlock; the runner never guesses a code |
| bundle not installed | exit 2 with the bundle name |
| `--expect-version` mismatch | exit 2 ("bundle 名相同的两个 app 靠肉眼分不出来") |
| `--bundle` omitted | exit 2 — there is no default, on purpose |

The resolved `{bundle, version}` lands in the report's `app` block, and `uiflow report` copies it into
the verdict, so a stored artifact says which build the number describes.

## 3. One verdict — every gate, one file, one exit code

    ecat uiflow report --base <B.json> --port-report <T2 report> --spec <spec.json> \
        [--flows <flow spec>] [--port-dir <harmony project>] --bundle <bundle> \
        --out reports/ui-flow/verdict.json

Runs the gates in order and writes one JSON + one Markdown with a single status:

| status | exit | meaning |
|---|---|---|
| `PASS` | 0 | port check clean, flows honest, port measured, `fn_rate` present |
| `FAIL` | 1 | a gate rejected the run — reasons listed, no FN |
| `BLOCKED` | 4 | the port was not measured (dependency-blocked / nothing passed) — `fn_rate` null |
| `ERROR` | 2 | inputs missing or unreadable |

`fn_rate` is only ever non-null when the status is `PASS`, so a CI job cannot pick up a number the
gates refused to vouch for. Existing per-layer commands stay for debugging; this is what CI calls.

## 3e. Spec, half-automatic — propose, then confirm

    ecat uiflow spec-from-discovery --graph <B.json> --port-dir <harmony project> --out spec.json
    ecat uiflow spec-confirm --spec spec.json [--only screen1,screen2]

`spec-from-discovery` now pairs each discovered Android layout with a port page from the registry
(`main_pages.json`), scoring names and the visible text discovery recorded:

| score | rule | meaning |
|---|---|---|
| 1.0 | same tokens | `Garden` ↔ `Garden` |
| 0.8 | one contains the other | `GardenActivity` ↔ `Garden`, `DetailActivity` ↔ `PlantDetail` |
| ≤0.5 | partial overlap | weak — below the 0.4 floor it proposes nothing |
| 0.0 | nothing shared | left empty, with the unclaimed port pages printed as a shortlist |

Proposals are written with `"auto": true` and a `AUTO 候选 … (score …)` note. One port page is only
ever claimed once, so two Android layouts cannot be paired to the same screen. `uiflow report` adds a
`spec-reviewed` check (`warn` + a reason while any row is still `auto`) — the FN number is only as
trustworthy as those pairings, so it says so out loud. `spec-confirm` clears the flag once a human
looked.

Suite-side: bare stems and `.ets` names are the same screen (`Garden` == `Garden.ets`), because the
registry says the former and flow specs say the latter.

## 3f. State level — a tab is not a page, but it can still be missing

Page-level FN cannot see a whole view disappearing inside a page (a tab, a filter, a panel): merge
two screens into one file and the page number gets *better*. States close that hole.

```json
{"screen": "garden", "android": {"structure": "c794…"}, "port": {"screen": "Garden.ets"},
 "states": [
   {"id": "my_garden",  "android": {"text": "My garden"}, "port": {"state": "my_garden"}},
   {"id": "plant_list", "android": {"text": "Beet"},      "port": {"state": "plant_list"}}]}
```

Evidence rules — each side must supply its own proof, never a name match:

| side | evidence | where it comes from |
|---|---|---|
| Android | the state's `android.text` tokens appear in a discovered label of that layout | discovery (`structure` + visible text) |
| port | a flow step that lands on the screen declares `"state": "<id>"` | `ui_runner` trace → `Page.extra["states"]` |

A state with no Android evidence is not counted (nothing to prove); a state Android shows but no
flow ever demonstrates becomes **state FN** (`state-coverage` check → `FAIL`, listed by
`screen/state`). The page-level `fn_rate` is unchanged, so the two numbers stay separable — on this
project's sunflower port the page number is `0.0%` while a deliberately removed state shows
`state FN = 33.3%`, which a page-only run reports as a plain pass.

## 3d. History — a number means nothing without the previous one

`uiflow report` appends every verdict to `reports/ui-flow/history/<bundle>.jsonl` (keyed by the
measured build) and prints the delta against the previous run of the same bundle:

| delta verdict | meaning |
|---|---|
| `regression` | a page that used to be present is now missing (`FN` went up) |
| `fixed` | a previously missing page is now covered |
| `unchanged` | same status, same FN, same page set |
| — | `PASS → BLOCKED` is reported as *unmeasurable*, never as *worse* |
| `first_run` | no baseline yet |

    ecat uiflow history --bundle <bundle> [--limit N]    # 历次：状态 / FN / 页面数 / 缺失数 / 死页数

Two honesty rules: a run without `--port-dir` records `port_checked: false`, so dead pages are never
compared against it (no fake "dead page fixed"); and a `BLOCKED` run stores `fn_rate: null`, so it
cannot masquerade as a 0% improvement.

## 3c. Flow audit — a green flow is not evidence

    ecat uiflow audit-flows --flows <spec.json> [--report <T2 report>] --out reports/ui-flow/flow-audit.json

Reads the spec (and optionally a report); no device. Exit 1 on any high-severity finding.

| check | severity | meaning |
|---|---|---|
| `no_assertion` | high | the flow asserts nothing — it can only "pass" |
| `no_navigation_assertion` | high | it taps/inputs but never checks the screen changed |
| `reset_vs_expectation` | medium | step 1 is `reset` (`bm clean`) yet the flow expects content — it wipes the data it asserts. Mark the one flow that *creates* that data with `"seed": true` and it is exempt |
| `no_screen_markers` | medium | `screens` declared but no step carries `"screen"` → no page identity, no edges |
| `pass_without_evidence` | low | passing flows recorded neither `actual` nor `trace` → the FN rate cannot be attributed |

On this project's sunflower spec the audit returns 5 medium `reset_vs_expectation` findings and
no high ones — which matches what the device did: every flow starts by clearing the app data and
then expects `Apple` to be on screen.

## 4. Port check — does the spec name screens the port actually ships?

    ecat uiflow check-port --spec <spec.json> --port-dir <harmony project> --out reports/ui-flow/port-check.json
    ecat uiflow diff ... --spec <spec.json> --port-dir <harmony project>   # gate before diffing

Reads `main_pages.json`, the entry ability's `loadContent`, and the routing code; no device. Two
models are understood, because generated ports use both:

| model | shape | screens come from |
|---|---|---|
| `router` | one `.ets` per screen | `main_pages.json` + `router.pushUrl('pages/X')` |
| `navigation` | one registered page, `NavPathStack` destinations | `pushPathByName(PAGE_X, …)` resolved through `export const PAGE_X: string = 'X'` |
| `unknown` | anything else | nothing — the check degrades to warnings and says so |

Source discovery also tolerates non-DevEco layouts (`ets/` at the root, as exports and snapshots
have). Guessing the source path wrong used to make every port look like `unknown`, which is how the
gate quietly stopped protecting anything.
`diff --port-dir` **refuses to run (exit 2)** when the spec names a page that is not registered —
a typo would otherwise be reported as a page the port is missing.

| check | severity | meaning |
|---|---|---|
| `unknown_screen` | error | spec's `port.screen` is not a screen the port has (see routing models below) |
| `unknown_screen_unverified` | warning | same, but the routing model could not be parsed — does **not** block |
| `routing_unknown` | warning | neither `router.pushUrl` nor `NavPathStack.pushPathByName` found |
| `port_screen_missing` | error | spec row has no `port.screen` at all |
| `entry_uncovered` | warning | the page the ability loads on start is never asserted |
| `orphan_page` | info | a registered page that neither `loadContent` nor any `pushUrl` reaches — dead surface FN can never see |
| `no_registry` | warning | `--port-dir` has no `main_pages.json`, check skipped |

## 5c. Two ways a green flow lies (found by mutating the port)

A marker is a **claim**; the runner also records the screen signature, and those two can disagree.

| what the port did | what an honest run must say |
|---|---|
| route removed, tap still "succeeds" | signature unchanged → the claimed screen is **`declared`**, not reached |
| assertion text also present on the previous screen | the assertion is marked **`weak`** → cannot be evidence of arrival |

Both are now enforced: `observed_markers()` drops weak entries and entries whose signature did not
move, `graph_from_flows_report` counts a screen as reached only from such markers (falling back to
declared screens only for flows with no markers at all), and `stats` carries `weak_assertions` and
`marker_verified`.

This came out of mutating this project's own port: deleting the `router.pushUrl` to the detail page
rebuilt in 24s, and the same five flows that had been "5/5 green" then failed on the detail
assertion. The assertion had been passing on a *different build* — the detail page now renders
`Watering needs` + `every 30 days` as two labels, while the flow still asserted the Android phrasing
`Water every 30 days`. Green meant "the text matched somewhere", not "this screen is right".

## 5a. Raising the denominator: union of exploration rounds

One DroidBot round is a *sample*: with 30 events it reached 2 of the gallery's own 18 activities.
The denominator is that page set, so a thin sample makes FN look good for the wrong reason.

    # three independent rounds, same build (sha-pinned), then union them
    for i in 1 2 3; do
      ecat uiflow discover --apk app.apk --serial emulator-5630 --refresh \
        --count 120 --timeout 600 --out reports/ui-flow/B-$i.json
    done
    ecat uiflow merge-graphs reports/ui-flow/B-*.json --apk app.apk \
        --out reports/ui-flow/table-B.json

**Measured plateau (gallery, emulator-5630).** Three rounds with different policies and larger
budgets, unioned:

| rounds | pages | edges | own activities covered |
|---|---|---|---|
| 1 × count 25 | 5 | 14 | 2/18 (11.1%) |
| + count 120 | 11 | 28 | 3/18 (16.7%) |
| + dfs_greedy count 90 | 13 | 46 | 3/18 (16.7%) |

More pages and edges, no new activity: the remaining 15 sit behind entry points that random label
clicks never find (an icon-only Settings gear, hidden/excluded folders). Seeding a round on a
specific activity was tried four times and never captured the target state (see the commit record);
the accessibility adapter re-foregrounds the app after `am start`, so the seeded screen is replaced
before it is captured.

Consequence for the acceptance number: on an app of this shape, blind exploration gives a denominator
that covers three activities, and FN is only meaningful over those three. Raising it needs either a
curated entry map (human taps, which this project wants to avoid) or a working seed — not more rounds.

`merge-graphs` keys pages by the content-free structure (`structure:<hash>`), so the same layout
showing different rows stays one page, and unions edges by `(src, dst, action)`. `--apk` reports the
resulting coverage, and `--out` is what `uiflow report --base` should point at.

## 5b. Is the denominator complete?

    ecat uiflow audit-discovery --graph <B.json> --apk <app.apk> --out reports/ui-flow/discovery-coverage.json

FN divides by B's page set, so an exploration that never reached half the app produces a small
denominator and a flattering number. This compares the manifest's activity table with what discovery
actually reached and reports the gap as a number, split into the app's own activities and the library
pages it also ships (ExoPlayer, commons' License…), which are pages but not product flow.

Measured on this project: the gallery exploration reached **2 of 18** of the app's own activities
(11%) — i.e. the 30-event / 300-second default was the binding constraint, not the port.

## 5f. Two entry points: the gate and the instrument

The same measurement serves two jobs, and mixing them was a mistake worth naming:

| | `scripts/uiflow_gate.sh` | `scripts/uiflow_diagnose.sh` |
|---|---|---|
| output | verdict (PASS/FAIL/BLOCKED) + FN | `work_list` (port defects) + `tooling` (measurement defects) |
| exit | the verdict's code — CI reads it | 0 unless unmeasurable (`STRICT=1` to fail on defects) |
| when | the denominator is trusted | now, while it is not |

`work_list` entries carry `file`, `issues`, `severity` and an empty `verify`, so
`scripts/uiflow_fix_verify.sh` consumes them unchanged: diagnose → refine → rebuild → **same flows** →
verdict → history. Nothing in that loop rewrites the expectations after a failure.

`tooling` is deliberately kept out of `work_list`: a weak assertion, a spec row without `port.screen`,
or missing state evidence are faults of the measuring stick, and handing them to the port refinement
loop would make the agent "fix" a screen that is fine.

## 5e. Structure comparison — measured, and not yet a gate

`uiflow compare-structure --base --port --spec` scores each paired screen by the mix of component
*roles* (text / button / image / input / list), after bucketing the platform-specific class names
(`android.widget.FrameLayout` and `Column` are both containers) and dropping containers/scrollers,
whose counts only reflect how many layout nodes each platform's dump emits.

Position-aware variant (element role + a 3×3 position bucket, `geometry` in the page extra). Measured
on the same pair, it scores the *correct* pairing at **0.10** — worse than the role mix it replaced.

The reason is not the metric, it is the two dump sources:

| side | what a dump exposes |
|---|---|
| Android (DroidBot accessibility) | the full view hierarchy — 43 views on one dialog, containers included |
| ArkUI (`uitest dumpLayout`) | only nodes carrying text/description — 5 elements on the same dialog |

So a set-overlap score is dominated by *how many nodes each platform exposes*, not by the screen. Until
the OH dump exposes structure (or the comparison moves to screenshots / T3), element-level coverage
across platforms is not measurable. The code stays (it is honest about `no-structure-data`), but it is
not a gate and should not be quoted as one.

Real data, gallery port vs gallery APK:

| pairing | score | verdict |
|---|---|---|
| Main empty state ↔ MainActivity.ets | 0.86 | plausible |
| MediaActivity file list ↔ MainActivity.ets (**deliberately wrong**) | 0.86 | plausible |

So it does **not** discriminate yet: with three role buckets, a file list and an empty dialog look
alike. Keep it as a sanity check (gross mismatches still fall below 0.55), not as a pass/fail gate.
Making it useful needs order/position (a row of N images is not the same as one big image) or
per-row element counts — i.e. the element-level coverage work, not another threshold.

## 5d. What the report tells you to do, and when it cannot be trusted

Four additions, all from failures that actually happened here:

| output | what it answers | command |
|---|---|---|
| per-page missing kind | `missing_page` (port lacks it) vs `entry_broken` (port ships it, nothing reaches it) vs `unmapped` (spec row has no port screen) | `uiflow report` → `missing_detail` |
| discriminating assertions | which labels exist *only* on the new screen — the only ones that can prove arrival | `uiflow suggest-assertions --report <T2 report>` |
| data fingerprint | how much content each screen showed (`labels`, `sample`) | `Page.extra["data"]` in the C graph |
| system chrome filter | HarmonyOS chrome (`LiveMetaBallBaseVm`, `ClockStatusView`, `__Common__`) no longer pollutes signatures | `ui_runner.is_system_noise()` |

Two honesty notes:

* Suggestions are computed from the runner's signature, which used to be truncated at 32 chars.
  Reports produced before that cap was raised still yield fragments (`cloc`, `grow`); re-run the
  flows to get whole labels.
* Element-level coverage is deliberately **not** implemented. Comparing element *text* across
  platforms measures copy differences, not structure (measured Jaccard 0.05–0.50 on the same
  screen); it needs element *types/positions* from both sides, which is a different feature.

## 7. Fix-and-verify loop

    PROJECT=<harmony project> WORK_ITEM=<work item json> BUNDLE=… DEVICE=… FLOWS=… SPEC=… \
    PORT_DIR=… BASE=<B graph> scripts/uiflow_fix_verify.sh

work item → prompt → `ecat refine` → build → install → **the same flows** → verdict → history
delta. The script never rewrites expectations after a failure: a red verdict stays red until the
port changes.

## 6. Running this in CI

    BUNDLE=com.example.app DEVICE=<serial> HAP=app.hap \
    FLOWS=flows.json SPEC=spec.json PORT_DIR=port/ BASE=table-B.json \
      scripts/uiflow_gate.sh

One command, one exit code (`0` PASS / `1` FAIL / `4` BLOCKED / `2` bad input). It runs the flows,
builds the C graph, and stops at the first refusal — the script itself decides nothing, so a CI job
that only looks at `$?` cannot read a number the gates refused to vouch for. Inputs are resolved to
absolute paths up front (the `ecat` CLI is usually a wrapper that changes directory), and with
git-bash `cygpath -m` hands native tools a path they can open.

Extra knobs: `CHANGED=Garden.ets,PlantDetail.ets` runs only flows touching those screens, and the
history is written under `$OUT_DIR/history` so it travels with the job's artifacts (a fresh
workspace would otherwise report `first_run` on every build). History files keep the last 50 runs.

Pass `HAP=` as well: the runner records its sha256, which is what the history keys on — versionName
stays `1.0.0` across rebuilds, so only the sha can tell two builds apart in a regression delta.

## 7. Where the layers get their evidence

| layer | evidence | backend |
|---|---|---|
| A | the manifest's activity table | aapt2/aapt, then `apkanalyzer`, then `androguard` |
| B | clicks in the original APK | DroidBot, pinned by APK sha (`--baseline-dir`) |
| C | screens/edges/states a flow reached | `ui_runner` trace (`screen`, `state`) |
| spec | which Android layout is which port page | human + `spec-from-discovery` candidates |
| port check | which screens the port actually ships | `main_pages.json` + router / NavPathStack |

Every arrow above is checked somewhere: `discover --from-utg` refuses a UTG explored from another
build, `diff --apk` warns when the denominator's sha disagrees, and `report` refuses to print FN
when the port was not measured.

## 5. Tests: layer A and the whole chain

`tests/unit/test_uiflow_layers.py` covers what used to be untested:

* **A layer without a device** — `Adb` is nine small methods, so `FakeAdb` replays a phone:
  reached / `am start` refused / activity finishes itself / crash with a missing ABI / adb dying
  mid-sweep. The sweep must type each row and survive one dead probe.
* **`build_params`** — the parameterised-entry half, including `--es/--ei/--ez` and `raw_args`.
* **the chain** — an `amstart` graph is refused as a denominator, then B (click) + C (flow report) +
  spec produce an FN, edges, state coverage and a verdict in one test.

Writing these found a real bug: `apk_abis` filtered `n.count("/") > 2`, but `lib/<abi>/<lib>.so` has
exactly two slashes — so it returned `[]` for every APK and `crashed_abi` could never be reported.
The real gallery APK ships 7 ABIs and now says so.

## Two rules that decide whether the number means anything

1. **The denominator is the Android side's reachable page set**, never an `amstart`
   capability table — the command enforces it.
2. **B and C must see equivalent data.** No media, empty database, no logged-in account:
   the port can only show an empty screen, so every page the original reached reports as
   missing. Seed both sides before quoting an FN rate.
