<p align="center">
  <img src="docs/logo.png" alt="ECAT" width="220">
</p>

# ECAT — Entropy-based Code Adversarial Translation

> A multi-agent, adversarial framework for Android → HarmonyOS migration.

**English** | [简体中文](README.zh-CN.md)

ECAT models Android-to-HarmonyOS migration as a **Generator ↔ Discriminator** adversarial loop:
the Generator performs the migration and applies fixes; the Discriminator computes
**Code Entropy (H)** and emits a text gradient (a work_list plus improvement suggestions),
driving the Generator toward lower entropy — i.e. higher code quality.

📄 **Paper**: [Entropy-based Code Adversarial Translation for Real-world Repository Migration](https://arxiv.org/abs/2608.09273)

📊 **Benchmark**: [A2H-RepoBench](https://github.com/yushuntang/ECAT/tree/benchmark)

> **Note on reproducibility**: the experimental results in the paper were produced mainly with
> **DeepSeek-V4-Pro (20260424-preview)**. The bundled skill library and subagents were tuned for
> that model and may not carry over to newer models. This repository therefore ships a reduced
> set; you can add your own back.

## TL;DR

1. `uv tool install --editable ./ECAT` — install the CLI (editable = changes take effect immediately)
2. `cd your-workspace/ && ecat init` — generate `.ecat/`, the config file, and agent templates
3. Edit `ecat.config.yaml` (model, provider, paths) and `.env` (API keys)
4. `ecat refine` — start the adversarial loop; each round the Discriminator scores multiple
   dimensions, the Generator fixes, until H converges

Workspace layout:
```
your-workspace/
├── android_app/         # Android source (read-only input)
├── harmony_app/         # HarmonyOS target repo (what the Generator edits)
├── .ecat/               # ECAT runtime data + agent config
├── .env
└── ecat.config.yaml
```

---

## Installation

### Prerequisites

- **Python ≥3.11 + uv** ([install](https://docs.astral.sh/uv/getting-started/installation/)) — runs ecat; Python dependencies are installed automatically
- **Node.js ≥18 + npm** ([install](https://nodejs.org/)) — runtime host for any of the agent CLIs below
- **At least one agent CLI** (pick one; the Generator and Discriminator may use different ones):

  | runtime | CLI | Install | Notes |
  |---|---|---|---|
  | `claude_code` | `claude` | `npm install -g @anthropic-ai/claude-code` | Most complete. Subscription mode needs `claude login`; with an API key, set `api_key_env` in the config |
  | `opencode` | `opencode` | see [opencode docs](https://opencode.ai/docs/) | OpenAI-compatible endpoints, lower cost |
  | `deveco_code` | `deveco` | `npm install -g @deveco/deveco-code` | A fork of opencode; same config schema |

  `ecat migrate` / `ecat refine` verify that the corresponding CLI is on `PATH` and fail with a
  clear error if it is missing.

  **Capability differences**: `generator_compact_interval` (automatic long-session compaction)
  is only implemented for `claude_code`; it has no effect under opencode / deveco_code.
  Everything else (subagent dispatch, skill loading, session resumption) works on all three.

- **DevEco Studio SDK + `hvigorw`** ([download](https://developer.huawei.com/consumer/cn/deveco-studio/)) — required.
  The `compile` dimension builds through it, and `build_verify` (on by default) makes the
  Generator run the `harmonyos-build-fix` skill at the end of every round to repair compile
  errors. Without it, `compile` scores maximum entropy (1.0) and the loop loses its most
  important convergence signal.
  After installing, set `loop.deveco_studio_path` in `ecat.config.yaml` (e.g. `D:/DevEco Studio`
  on Windows, `/Applications/DevEco-Studio.app/Contents` on macOS). If it is not set, ECAT infers
  the location from `JAVA_HOME` (DevEco's bundled `jbr`; on macOS `jbr/Contents/Home`), and on
  macOS it also finds a default install under `/Applications`.
- Optional: `adb` (ships with [Android Studio](https://developer.android.com/studio)) — needed
  only by the `ui_align` visual-alignment dimension, which is skipped when absent (the macOS
  default location `~/Library/Android/sdk` is found automatically).

### Install

```bash
git clone https://github.com/<your-org>/ECAT.git
uv tool install --editable ./ECAT          # ecat becomes available in any directory
```

`--editable` is required: `ecat` then points at the source tree, so **edits take effect without
reinstalling**.

To run ECAT's own unit tests (this checks your dev environment, not a migration):
`uv run --extra dev pytest tests/ -q` from the repository root.

---

## Quick Start

### 1. Prepare a workspace

```
your-workspace/
├── myapp_android/         # Android source repo
└── myapp_harmonyos/       # HarmonyOS target repo (DevEco "Hello World" template)
```

The HarmonyOS repo only needs a Hello World project: create an Empty Ability project in DevEco Studio, or copy
DevEco's bundled template from the command line.

**Windows (Git Bash)**

```bash
DEVECO="/d/Program Files/Huawei/DevEco Studio"
TARGET=/path/to/MyHelloApp

# 1. Copy the template
mkdir -p "$TARGET"
cp -r "$DEVECO/plugins/codegenie-plugin/previewProjectTemplate/." "$TARGET/"

# 2. Set bundleName / app name
cd "$TARGET"
sed -i 's/com\.example\.myapplication/com.yourco.hello/' AppScope/app.json5
sed -i 's/"MyApplication"/"MyHello"/' AppScope/resources/base/element/string.json

# 3. Point the start page to pages/Index (the template registers the preview placeholder generated/Index; without this the app opens to a blank screen)
sed -i 's#"generated/Index"#"pages/Index"#' entry/src/main/resources/base/profile/main_pages.json
```

**macOS**

```bash
DEVECO="/Applications/DevEco-Studio.app/Contents"
TARGET=/path/to/MyHelloApp

# 1. Copy the template
mkdir -p "$TARGET"
cp -R "$DEVECO/plugins/codegenie-plugin/previewProjectTemplate/." "$TARGET/"

# 2. Set bundleName / app name (macOS sed needs an empty string after -i)
cd "$TARGET"
sed -i '' 's/com\.example\.myapplication/com.yourco.hello/' AppScope/app.json5
sed -i '' 's/"MyApplication"/"MyHello"/' AppScope/resources/base/element/string.json

# 3. Point the start page to pages/Index (the template registers the preview placeholder generated/Index; without this the app opens to a blank screen)
sed -i '' 's#"generated/Index"#"pages/Index"#' entry/src/main/resources/base/profile/main_pages.json
```

The template targets SDK 5.0.0(12); raise `compatibleSdkVersion` / `targetSdkVersion` in `build-profile.json5`
if you need newer APIs.

### 2. Initialize

```bash
cd your-workspace/
ecat init                     # creates .ecat/, ecat.config.yaml, .env
```

`ecat init` copies the agent templates bundled in the ECAT package (CLAUDE.md, skills, agents,
prompts) into `.ecat/agents/`. Existing files are never overwritten; `--force` resets them.

### 3. Configure

**`ecat.config.yaml`** (minimal working config):

```yaml
paths:
  android: ./myapp_android
  harmony: ./myapp_harmonyos

generator:
  runtime: opencode               # claude_code | opencode | deveco_code
  model: deepseek-chat
  provider: deepseek
  base_url: https://api.deepseek.com/v1
  api_key_env: DEEPSEEK_API_KEY

discriminator:
  runtime: claude_code
  model: claude-sonnet-4-6
  api_key_env: ""                 # empty string → use the `claude login` subscription

entropy:
  weights:
    compile: 0.25
    feature_check: 0.20
    entry_nav: 0.10
    page_audit: 0.10
    static_lint: 0.10
    source_parity: 0.05         # Android↔Harmony source parity
    app_identity: 0.05
    placeholder: 0.05
    style: 0.05
    ui_align: 0.00              # needs on-device screenshots; off by default
```

**`.env`** (secrets only — `model` and `base_url` live in the yaml):

```dotenv
DEEPSEEK_API_KEY=sk-xxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxx
```

> **Subscription mode**: leave `api_key_env: ""` empty and the `claude` CLI falls back to the
> OAuth subscription maintained by `claude login`. ECAT copies `~/.claude/.credentials.json`
> into each role's config dir automatically.
> On macOS the login lives in the Keychain and that file does not exist: run `claude setup-token`
> once and put the printed token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN=...` (this works on every OS).

### 4. Run

```bash
ecat refine                             # uses the default ecat.config.yaml
ecat refine --max-iter 5                # cap the number of iterations
ecat refine --config my.config.yaml     # use another config (switch providers)
```

Multiple configs can coexist, each pointing at a different `paths.harmony`:

```
your-workspace/
├── ecat.config.yaml              # default (e.g. opencode + DeepSeek)
├── ecat.config_claude.yaml       # Claude Code subscription
└── ecat.config_glm.yaml          # GLM via DashScope
```

Each round the Discriminator emits a per-dimension sub_entropy plus a work_list; the Generator
fixes the code and the next round re-evaluates. The loop stops once H stays below
`entropy_threshold` for `stable_iters_to_stop` consecutive rounds (default 2), or when
`max_iterations` is reached.

See [CLI Reference](#cli-reference) for all flags.

---

## Evaluation Plugins

Each plugin outputs `sub_entropy ∈ [0,1]` (0 = perfect, 1 = worst); the total H is the
`entropy.weights`-weighted sum.

> The table below lists every available plugin. **Which dimensions actually count depends on
> your configuration**: the `entropy.version` preset (`flash` / `pro` / `custom`) replaces
> `weights` wholesale, and any plugin absent from `weights` has weight 0. Different setups
> therefore enable a different number of dimensions.

| CHECK | Plugin | Type | What it checks |
|---|---|---|---|
| 1 | **compile** | hard | Builds with hvigorw and scores linearly by compile-error count; no toolchain → 1.0 (max entropy), errors → `min(1, 0.5 + errors×0.1)` |
| 2 | **static_lint** | hard | Greps for Android leftovers (`findViewById` / `setOnClickListener` / `R.id.*` / `android.` packages / `androidx.` imports, etc.); any hit costs points |
| 3 | **placeholder** | hard | Two-pass scan: ① when a spec exists, count pending/deferred/backfillable entries in `spec/placeholder-registry.md` (this repository ships no spec generator; without a spec this step is skipped); ② grep TODO/FIXME/`// stub`/"Hello World"/"NOT IMPLEMENTED" (comment lines only, to avoid false hits on the ArkTS `placeholder:` prop); score = `backfillable×3 + pending×1 + unregistered×0.5 + grep_hit×0.1` |
| 4 | **app_identity** | hard | 6 checks: bundleName must not contain `com.example`, vendor ≠ `"example"`, versionName ≠ template default, label must not be `EntryAbility`, app icon > 1KB, non-empty description |
| 5 | **page_audit** | hard+agentic | With a spec, groups pages by ui-manifest P0/P1/P2 and verifies each .ets exists with `@Entry`/`build()` (without a spec it only checks that pages exist under `pages/` and have `@Entry`); `completeness = 0.4×line_ratio + 0.6×density ≥ 0.55` is required to count as verified; missing P0 pages immediately produce a high-priority WorkItem; the agentic judge reads unverified pages in depth |
| 6 | **feature_check** | agentic | Three layers — spec-driven (when a spec exists) + grep fallback + agentic judge — checking V1 feature implementation; `hard_weight=0.0`, so the agentic score fully replaces the hard check |
| 7 | **ui_align** | agentic (device) | Visual alignment: rebuilds and installs the HarmonyOS app, screenshots the same page on the HarmonyOS and Android emulators, stitches a side-by-side image, and the `visual-judge` subagent scores it; needs both emulators online, the original app pre-installed on the Android side, and a discriminator model that can read images; this repository ships no page-navigation scripts, so only the home page is compared for now; weight 0 by default |
| 8 | **style** | hard | ArkTS style violations: leftover `console.log`, explicit `px` units, non-PascalCase components, the `var` keyword, bare string color values |
| 9 | **entry_nav** | hard+agentic | Four categories, 13 checks: EntryAbility→main_pages.json consistency, registered `router.pushUrl` targets, Tabs/TabContent pairing, nav icons that are not launcher icons / emoji / 1×1 pixel placeholders |
| 10 | **source_parity** | hard+agentic | Android↔Harmony source parity: ① match by stem whether every Android source file has an ArkTS / .cpp / shader counterpart, classifying gaps as `copy` / `translate` / `header-swap` / `port`; ② reachability gate — walking relative and cross-HSP/HAR imports from pages/, *ability/, workers/ and module `Index.ets`, flagging ArkTS counterparts that exist but are never imported as `wire`; ③ ETS↔.so↔registration triangle — ETS calls native symbols with no `.cpp` registration (`napi_unregistered`), or registration exists but the body says `not (yet) implemented` (`native_stub_funcs`); vendor directories (cryptopp890 / boost / openssl, …) are auto-detected via `LICENSE` / `Doxyfile` / version suffixes and skipped, as are `samples` / `demo` / `example` |
| 11 | **data_layer_parity** | hard | Data-layer reconciliation: maps Android DB tables/entities plus SharedPreferences/DataStore keys to Harmony one by one using loose substring matching (prefers misses over false positives); skipped when Android has no data layer |
| 12 | **permission_parity** | hard | Permission/capability reconciliation: maps each Android `<uses-permission>` to its Harmony equivalent and reports gaps |
| 13 | **runtime_liveness** | hard (device) | On-device cold-launch liveness plus main-page smoke taps: install HAP → launch → probe (crash/blank/force-close) → tap main-page elements; device-driven, weight 0 by default (needs hdc + a running emulator) |
| 14 | **hallucination** | hard+agentic | Reverse check Harmony→Android: finds features invented on the Harmony side with no origin in the Android prototype; weight 0.10 by default |
| 15 | **crash_risk** | hard | Static click-crash risk: ArkUI event contracts, resource references, global exception fallback |
| 16 | **feature_checklist** | hard+agentic | Checks a human-authored feature checklist (xlsx / csv / md) item by item; requires `paths.feature_checklist`, skipped when unset; weight 0 by default |

**Judge types**:
- **hard**: pure Python static analysis (milliseconds)
- **agentic**: an agent reads the code files directly (minutes; the most accurate option)

### Signal density — more robust than line counts

`feature_check` and `page_audit` share `_signal_density.py`, which computes, for each ArkTS file,
the ratio of six classes of language idiom against their Android counterparts:

| Signal | Android | ArkTS |
|---|---|---|
| click_handler | `setOnClickListener` | `.onClick()` / `.onChange()` |
| state_var | `private var` / `mutableStateOf` | `@State` / `@Prop` / `@Link` |
| list_or_menu_item | `addPreference` / menu xml | `ListItem` / `@Builder` / `MenuItem` |
| string_resource | `R.string.*` | `$r('app.string.*')` |
| media_resource | `R.drawable.*` / `R.mipmap.*` | `$r('app.media.*')` |
| navigation_jump | `Intent(...)` / `startActivity` | `router.pushUrl` / `Navigation.push` |

`completeness = 0.4 × line_ratio + 0.6 × density` — ArkTS declarative UI is inherently compact,
so a pure line count mistakes a genuinely finished migration for a stub; signal density anchors
the measure to functional surface.

---

## Architecture

```
ECAT Orchestrator
  ├── ConfigLoader          Pydantic v2 + YAML + .env
  ├── LoopDriver            adversarial loop controller
  │     ├── EntropyEngine   a2h_verify plugin set
  │     │     ├── 9 hard plugins (parallel ThreadPoolExecutor)
  │     │     └── 7 agentic judges (deferred to Discriminator session)
  │     ├── GradientBuilder runs Discriminator → text gradient
  │     │     └── dispatches entropy-judge subagents (Agent/task)
  │     └── AgentRunner     Generator subprocess (claude/opencode/deveco)
  ├── Workspace             .ecat/ state management
  └── ReportWriter          charts + markdown + entropy_history.jsonl
```

### Subagents inside the Discriminator session

ECAT no longer starts a separate subprocess for every agentic judge. All agentic evaluation is
**centralized inside the Discriminator session**, dispatched concurrently through the generic
`entropy-judge` subagent:

```
1. Engine runs the hard checks (Python side, seconds)
2. Discriminator subprocess starts
   → collects all hard verdicts
   → dispatches 7 entropy-judge subagents (parallel via Agent/task)
   → collects all agentic verdicts
   → writes gradient.json (summary + work_list + suggested skills + insights)
3. Generator subprocess starts and fixes the code per the work_list
```

The gain: 7 fewer subprocess cold starts, a synthesized gradient written only after the
Discriminator has seen every verdict (cross-dimension reasoning), and a precise work_list with
suggested_skills for the Generator.

### The Generator reads Android source automatically

The `generator.txt` prompt gives the Generator the Android source root (absolute path).
Whenever a work_list issue contains a quantitative description such as "port progress N%" /
"missing N/N" / "stub" / "PARTIAL", the Generator **must Read the Android source first**,
compare 1:1, and then implement what is missing.

If `paths.spec` is configured, `spec/baseline/source-mapping.md` (ArkTS→Android file mapping)
is attached as a cross-reference; without spec it matches by file name.

### Parallel subagent dispatch (Generator side)

When the work_list contains ≥2 independent file items, the Generator prompt insists on issuing
N `task()` / `Agent()` calls **within a single response** (the runtime-specific tool name is
injected by the driver), letting the runtime execute them in parallel — 5-10× faster than
serial execution.

---

## Configuration Reference

### Required

| Key | Description |
|---|---|
| `paths.android` | Relative path to the Android source repo |
| `paths.harmony` | Relative path to the HarmonyOS target repo (the Generator edits this) |
| `generator.runtime` | `claude_code`, `opencode`, or `deveco_code` |
| `generator.model` | Any model name (`deepseek-chat`, `claude-opus-4-7`, ...) |
| `generator.api_key_env` | Name of the API-key variable in `.env`; **empty string = subscription** |
| `discriminator.*` | Same as generator; may use a different runtime/model |
| `entropy.weights` | Per-dimension weights; unlisted plugins fall back to `default_weight`. Which dimensions actually count depends on the active preset and config |

### Optional / advanced

| Key | Default | Description |
|---|---|---|
| `loop.max_iterations` | `100` | Maximum number of iterations |
| `loop.entropy_threshold` | `0.01` | Early stop when H falls below this |
| `loop.regression_tolerance` | `0.1` | An H increase beyond this counts as a regression and triggers grace fixing |
| `loop.oscillation_guard.window` | `5` | H changes all below min_delta within the window count as oscillation → break |
| `generator.timeout_s` | `3600` | Generator subprocess timeout; psutil kills the process tree at the deadline |
| `discriminator.timeout_s` | `2400` | Discriminator subprocess timeout |
| `retry.max_attempts` | `3` | Retries on subprocess failure |
| `reporting.chart_format` | `png` | `png` or `html` |

### Multi-provider examples

Generator / Discriminator / Judge can each use a different runtime and provider. Three common
combinations:

**A. All Claude subscription**:
```yaml
generator:     {runtime: claude_code, model: claude-opus-4-7, api_key_env: ""}
discriminator: {runtime: claude_code, model: claude-opus-4-7, api_key_env: ""}
```

**B. All opencode + DeepSeek** (cheaper, slower):
```yaml
generator:
  runtime: opencode
  provider: deepseek
  model: deepseek-chat
  base_url: https://api.deepseek.com/v1
  api_key_env: DEEPSEEK_API_KEY
# discriminator: same
```

**C. A custom OpenAI-compatible aggregator (DashScope / GLM)**:
```yaml
generator:
  runtime: opencode
  provider: dashscope-proxy           # custom name, avoids opencode's built-in openai special path
  model: glm-5.1
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key_env: GLM_API_KEY
```

---

## CLI Reference

| Command | Description |
|---|---|
| `ecat init [TARGET]` | Initialize .ecat/, ecat.config.yaml, .env and seed the agent config |
| `ecat init --force` | Force reset (overwrites the agent config, clears memory/ and runs/) |
| `ecat migrate --prompt "..."` | Run the Generator once with the given instruction |
| `ecat refine` | The adversarial refinement loop (needs an initial migration in harmony) |
| `ecat status` | Current entropy and iteration count |
| `ecat diff <N>` | Entropy breakdown, gradient and diff for iteration N |
| `ecat report` | Generate a markdown report plus charts |
| `ecat uiflow inventory` | Layer A: parameterised `am start` over every manifest activity → reachability table |
| `ecat uiflow discover` | Layer B: DroidBot exploration → the page set a user can reach |
| `ecat uiflow import-flows` | Layer C: import the T2 (ui_runner) report as the port's page set |
| `ecat uiflow spec-from-discovery` | Generate the spec skeleton (identity join key) from a discovery graph |
| `ecat uiflow diff` | Acceptance: `diff(B, C)` → FN rate, both directions (`docs/ui-flow.md`) |
| `ecat uiflow audit-flows` | Audit a flow spec for flows that cannot fail / lie / drift with data |
| `ecat uiflow check-port` | Validate spec screen names against the port's `main_pages.json` + routes; flags unreachable pages |
| `ecat uiflow report` | One verdict artifact (JSON + Markdown) for all gates, single exit code 0/1/4 |
| `ecat uiflow history` | Per-bundle run history; `report` also prints the delta (regression/fixed/unchanged) |
| `ecat uiflow spec-confirm` | Accept the auto-proposed screen pairings once a human has reviewed them |
| state coverage | `states` in a spec row + `"state"` on flow steps → `state FN` for tabs/filters/panels inside a page |
| `uiflow diagnose` | run the measurement and emit work items (`work_list` = port defects, `tooling` = bad measuring stick) |
| `scripts/uiflow_gate.sh` | CI entry point: flows → C graph → verdict, exits with the verdict's code |
| `scripts/uiflow_diagnose.sh` | discovery entry point: same measurement → work items today can be fixed, never fails the build unless unmeasurable |
| `ecat uiflow audit-discovery` | Is B's denominator complete? Manifest activities vs discovered pages, app vs library |
| `ecat uiflow suggest-assertions` | Which labels can prove arrival (present here, absent on the previous screen) |
| `scripts/uiflow_fix_verify.sh` | work item → refine → build → same flows → verdict → history |

Common examples for each subcommand follow.

### `ecat init` — initialize a workspace

```bash
ecat init                            # initialize in the current directory
ecat init /path/to/workspace         # target directory (created if missing)
ecat init --force                    # force reset (overwrites .ecat/agents/, clears memory/ runs/)
ecat init --config my.config.yaml    # use a custom config filename
```

Creates `.ecat/`, `ecat.config.yaml`, `.env` and copies the package's agent templates into
`.ecat/agents/`. Existing files are never overwritten; only `--force` clears them.

### `ecat migrate` — run the Generator once

```bash
# Run the Generator once with a natural-language instruction
ecat migrate -p "translate PhotoActivity into PhotoPage.ets"

# Read a long instruction from a file
ecat migrate --prompt-file instructions.txt

# Switch config (multi-provider testing)
ecat migrate --config ecat.config_claude.yaml -p "fix all hvigorw build errors"
```

Runs the Generator once, following the prompt freely, without entering the refine loop. Useful
for manual control or for debugging a single skill.

### `ecat refine` — the adversarial refinement loop (core command)

```bash
ecat refine                                     # uses the default ecat.config.yaml
ecat refine --max-iter 5                        # at most 5 iterations
ecat refine --pause                             # pause for Enter after each iteration
ecat refine --score-only                        # evaluate once without looping (inspect current H)
ecat refine -p "fix compile errors first; do not rewrite pages that already pass"   # extra instruction
```

Each round the Discriminator scores multiple dimensions → writes the text gradient → the
Generator fixes code per the work_list, until H < `entropy_threshold` or `max_iterations` is
reached.

### `ecat status` / `diff` / `report`

```bash
ecat status                  # current H, ΔH, iteration count
ecat diff 3                  # iteration 3's gradient.json + diff.patch
ecat report                  # write report.md + trend charts into the newest runs directory
```

`diff` prints the strategist summary written by the Discriminator, the structured work_list and
the Generator's actual changes; `report` summarizes the H trajectory across iterations, the
dimension breakdown and code statistics — handy for a PR or a status update.

### Global options

```bash
ecat -C /path/to/workspace <command>   # run any subcommand after changing directory
ecat <command> --help                  # subcommand help
```

Every other setting (models, iteration count, thresholds, …) lives in `ecat.config.yaml`; use
`--config` to switch between several config files.

---

## Troubleshooting

### The Generator times out frequently

DeepSeek / GLM-class models can exhaust the timeout on a single iteration with a large
work_list. Remedies:

1. **Shrink the work_list**: lower `loop.max_items_per_iter` (default 25; 3-5 works well)
2. **Raise the timeout**: `generator.timeout_s` (default 3600)
3. **Switch to a faster model**: Claude Sonnet is typically 5-10× faster than DeepSeek in tool loops

### H suddenly jumps above 0.3 after iteration X

Usually the Generator broke the build. The driver automatically grants a `regression_grace`
iteration to fix it; if it breaks repeatedly, roll back to the last stable commit:
`cd harmony_dir && git reset --hard HEAD~N`.

### "Invalid API key" from the Generator/Discriminator

In Claude Code subscription mode, ECAT copies `~/.claude/.credentials.json` into the role
config dir. If the error persists:

- `claude /status` to confirm the subscription has not expired
- Remove any bogus `ANTHROPIC_API_KEY` from the environment
- macOS: make sure `.env` has `CLAUDE_CODE_OAUTH_TOKEN` (see subscription mode above), otherwise you get "Not logged in"
- ECAT already strips fake keys in `cli.py:_build_env()`, so this should not happen

### The Generator does not Read the Android source

Check whether the path after `Android source root:` in `runs_*/iter/generator_prompt.md` is
absolute (it should be an absolute path, not something relative like `./android_app`). If not,
confirm you are on a driver.py version that includes the absolute-path fix (merged into main).

### Subagent dispatch fails (opencode)

OpencodeRunner copies `<config_dir>/agents/*.md` into a tmp_dir and rewrites the frontmatter
(Claude Code style → opencode style). If the frontmatter still errors, check whether
`_rewrite_agent_frontmatter()` handles the new fields.

### The Discriminator scores by itself without dispatching judges

Symptom: an iteration's `discriminator.stream.jsonl` has no `Task`/`Agent` calls, and H suddenly drops to 0 or
shows an absurdly high score. Cause: the Discriminator searches for `Task` with ToolSearch and does not find it
(`Task` is already loaded; ToolSearch only finds deferred tools), concludes there is no subagent tool, and scores
from the hard-check results itself. Fix: disable ToolSearch for the Discriminator:

```yaml
discriminator:
  extra_args: ["--disallowedTools", "ToolSearch"]
```

### ui_align captures something other than the original Android app

- Install the original app on the Android emulator and grant its permissions beforehand (ECAT does not build or
  install the Android side), e.g. `adb shell pm grant <package> android.permission.READ_MEDIA_IMAGES`; for
  all-files access use `adb shell appops set <package> MANAGE_EXTERNAL_STORAGE allow`.
- ECAT launches the Android app with `monkey`. On an emulator without hardware keys monkey prints
  `SYS_KEYS has no physical keys` and exits: set `hw.keyboard` to `yes` in `~/.android/avd/<name>.avd/config.ini`
  and restart the emulator.
- The HarmonyOS emulator holds port 5555, so `adb devices` shows an extra `emulator-5554 offline`; this is harmless.
  Pinning devices with the `ANDROID_SERIAL` and `HDC_TARGET` env vars is more robust.
- Put the same test images on both sides: `mediatool` cannot import on the HarmonyOS emulator; `hdc file send` them
  to Downloads, then use Files → Share → Save to Gallery.

### Refining already-converged code stops before the dynamic dimensions run

By default the dynamic dimensions run every 5 iterations and count as 0 in H otherwise, so converged code can hit
early stop before the next dynamic evaluation. To keep them in play, set `entropy.dynamic_render_interval: 1`
(runs every iteration, a few extra minutes each).

### Token usage looks too high

With `resume_session: true`, Claude Code reports the resumed session's cumulative usage each iteration and ECAT
adds those up, counting the same tokens again; iterations killed by a timeout report no usage at all. For exact
numbers, use the session transcripts under `.ecat/agents/<role>/projects/`.

### Stopping a run

Do not press Ctrl+C: on an interrupt ECAT resets the harmony directory to the last commit and drops uncommitted
changes. To keep them, end the ECAT main process with `kill <ECAT pid>` (SIGTERM), then end the `claude`
processes it spawned.

---

## Development

```bash
uv run --extra dev pytest tests/ -q                                  # tests
uv run --extra dev ruff check src/ tests/ --fix                      # lint
uv run --extra dev mypy src/ecat/ --ignore-missing-imports           # type check
```

### Adding an evaluation plugin

Plugins are named `check_<name>.py` and subclass `EntropyPlugin`:

```python
# src/ecat/entropy/plugins/a2h_verify/check_my_check.py
from pathlib import Path
from ecat.entropy.base import EntropyPlugin, should_skip_path
from ecat.gradient.schema import WorkItem

class MyCheckPlugin(EntropyPlugin):
    name = "my_check"        # the key in entropy.weights
    default_weight = 0.05    # weight when the config omits it
    judge_mode = "hard"      # "hard" | "agentic"
    hard_weight = 0.4        # share of the hard score in agentic mode

    def evaluate(self, harmony_dir: Path, *, android_dir=None,
                 spec_dir=None, config=None) -> tuple[float, dict]:
        """Return (score, details): score ∈ [0,1], 0 = perfect, 1 = worst."""
        hits = [
            {"file": str(ets)}
            for ets in harmony_dir.rglob("*.ets")
            if not should_skip_path(ets, harmony_dir)
            and "some_bad_pattern" in ets.read_text(encoding="utf-8", errors="ignore")
        ]
        return min(1.0, len(hits) / 10), {"hits": hits}

    def extract_work_items(self, score, details) -> list[WorkItem]:
        """Turn details into work_list items; the base class returns [] by default."""
        return [
            WorkItem(file=Path(h["file"]), issues=["found bad pattern"],
                     suggested_skills=["harmonyos-build-fix"], severity="medium")
            for h in details.get("hits", [])
        ]
```

| Class attribute | Default | Description |
|---|---|---|
| `name` | `""` | Must be unique; the key under `entropy.weights` |
| `default_weight` | `0.1` | Weight when the config omits this key |
| `judge_mode` | `"hard"` | `hard` runs only `evaluate()`; `agentic` adds an LLM-judge score on top |
| `hard_weight` | `0.4` | Share of the hard score in agentic mode; `0.0` means the judge score alone |
| `judge_prompt` | `""` | Required in agentic mode |

`WorkItem` requires `file` / `issues` / `suggested_skills` / `severity`, and optionally accepts
`lines` (problem line numbers).

Three steps to register: put the file under `plugins/a2h_verify/` → add an instance to
`_PLUGINS` in `__init__.py` → give it a weight under `entropy.weights`. Use
`should_skip_path()` to skip directories; the line-count-plus-signal-density scorer lives in
`_signal_density.density_score()` / `combined_completeness()`.

---

## License

MIT — see the `LICENSE` file for the full text.
