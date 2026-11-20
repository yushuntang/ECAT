"""ECAT command-line interface — typer-based entry point."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from ecat.prompts import load_prompt

if TYPE_CHECKING:
    from ecat.config import AgentConfig, EcatConfig
    from ecat.entropy.base import EntropyReport
    from ecat.runner.base import AgentRunner

app = typer.Typer(
    name="ecat",
    help="ECAT — Entropy-based Code Adversarial Translation",
    no_args_is_help=True,
)
console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(console=console, show_time=False, show_path=False, markup=True)],
    force=True,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path("ecat.config.yaml")

_RICH_TAG_RE = re.compile(r"\[/?[a-z][a-z0-9 _]*\]", re.IGNORECASE)


class _StripMarkupFormatter(logging.Formatter):
    """Logging formatter that strips Rich markup tags for plain-text output."""

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        return _RICH_TAG_RE.sub("", msg)


@app.callback()
def _main(
    workdir: Annotated[
        Path | None,
        typer.Option("-C", "--workdir", help="Change to this directory before running"),
    ] = None,
) -> None:
    if workdir is not None:
        target = workdir.resolve()
        if not target.is_dir():
            console.print(f"[red]Directory not found: {workdir}[/red]")
            raise typer.Exit(1)
        os.chdir(target)


# ── init ─────────────────────────────────────────────────────────────────────


@app.command()
def init(
    target: Annotated[Path | None, typer.Argument(help="Target directory (default: current dir)")] = None,
    force: Annotated[bool, typer.Option("--force", help="Wipe memory/ and runs/")] = False,
    config: Annotated[Path, typer.Option(help="Config file path")] = _DEFAULT_CONFIG,
) -> None:
    """Initialise (or reset) the .ecat/ workspace skeleton."""
    if target is not None:
        resolved = target.resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        os.chdir(resolved)

    from ecat.workspace import Workspace

    ws = Workspace(Path("."))
    ws.init(force=force)

    if not config.exists():
        from ecat.config import SAMPLE_CONFIG

        config.write_text(SAMPLE_CONFIG, encoding="utf-8")
        console.print(f"[green]Created:[/green] {config}")

    env_file = Path(".env")
    if env_file.exists():
        console.print(f"[green]Created:[/green] {env_file} (fill in your API keys)")

    console.print("[green]Workspace initialised.[/green]")


# ── migrate ───────────────────────────────────────────────────────────────────


@app.command()
def migrate(
    config: Annotated[Path, typer.Option(help="Config file")] = _DEFAULT_CONFIG,
    prompt: Annotated[str | None, typer.Option("--prompt", "-p", help="Custom instruction (replaces default migration prompt)")] = None,
    prompt_file: Annotated[Path | None, typer.Option("--prompt-file", help="File containing custom instruction")] = None,
) -> None:
    """Run Generator once with a user-provided instruction."""
    cfg = _load_cfg(config)

    _attach_file_log(Path(".ecat") / "migrate.log")

    user_instructions = _resolve_prompt(prompt, prompt_file)
    if not user_instructions:
        console.print(
            "[yellow]No instruction provided. Use --prompt or --prompt-file to tell the Generator what to do.[/yellow]\n"
            "\n"
            "Examples:\n"
            '  ecat migrate --prompt "把 PhotoActivity 翻译成 PhotoPage.ets"\n'
            '  ecat migrate --prompt "fix all hvigorw build errors"\n'
            "  ecat migrate --prompt-file instructions.txt\n"
        )
        raise typer.Exit(1)

    runner = _make_runner(cfg.generator)
    env = _build_env(cfg, "generator")

    gen_prompt = (
        f"You are the ECAT Generator.\n"
        f"Android source: {cfg.paths.android}\n"
        f"HarmonyOS target: {cfg.paths.harmony}\n"
        f"Spec directory: {cfg.paths.spec}\n\n"
        f"{user_instructions}"
    )

    console.print("[cyan]Running Generator…[/cyan]")
    result = runner.run(
        prompt=gen_prompt,
        model=cfg.generator.model,
        env=env,
        cwd=Path(cfg.paths.harmony),
        role="generator",
    )
    if result.success:
        console.print("[green]Done.[/green]")
    else:
        console.print(f"[red]Generator failed (exit {result.returncode}).[/red]")
        raise typer.Exit(1)


# ── refine ────────────────────────────────────────────────────────────────────


@app.command()
def refine(
    config: Annotated[Path, typer.Option(help="Config file")] = _DEFAULT_CONFIG,
    max_iter: Annotated[int | None, typer.Option("--max-iter")] = None,
    score_only: Annotated[
        bool, typer.Option("--score-only", help="Evaluate entropy without running loop")
    ] = False,
    pause: Annotated[
        bool, typer.Option("--pause", help="Pause after each iteration for user confirmation")
    ] = False,
    prompt: Annotated[str | None, typer.Option("--prompt", "-p", help="Additional instructions appended to Generator prompt each iteration")] = None,
    prompt_file: Annotated[Path | None, typer.Option("--prompt-file", help="File containing additional instructions for Generator")] = None,
    start_iter: Annotated[
        int, typer.Option("--start-iter", help="Continue numbering from this iteration; code progress already lives in the harmony git checkout")
    ] = 0,
) -> None:
    """Run the adversarial Generator ↔ Discriminator refinement loop."""
    import ecat.entropy.plugins.a2h_verify  # noqa: F401 — register plugins

    cfg = _load_cfg(config)
    ws_path = Path(".")

    from ecat.workspace import Workspace

    ws = Workspace(ws_path)
    runs_dir = ws.create_runs_dir()
    charts_dir = runs_dir / "charts"

    _attach_file_log(runs_dir / "run.log")

    harmony_dir = Path(cfg.paths.harmony)
    ws.ensure_harmony_git(harmony_dir)

    # Banner identifying which codebase this run will modify and which models
    # the agents/judges will use. Helps diagnose multi-config setups (e.g.
    # ecat.config_claude.yaml vs ecat.config.yaml writing to different harmony dirs).
    try:
        rel_harmony = harmony_dir.relative_to(ws_path)
    except ValueError:
        rel_harmony = harmony_dir
    log.info("Modifying harmony codebase: [bold]%s[/]", rel_harmony)
    log.info(
        "Models: generator=%s/%s | discriminator=%s/%s",
        cfg.generator.provider, cfg.generator.model,
        cfg.discriminator.provider, cfg.discriminator.model,
    )

    from ecat.entropy.engine import EntropyEngine
    from ecat.report.charts import ChartRenderer
    from ecat.report.timeseries import EntropyTimeseries

    disc_runner = _make_runner(
        cfg.discriminator,
        add_dirs=[*cfg.discriminator.evolution_write_paths, cfg.paths.android, cfg.paths.spec],
    )

    # JudgeClient is kept as an optional prose-extraction fallback for agentic
    # judges that fail to output structured JSON.  No plugin uses judge_mode="llm"
    # anymore, so this is best-effort only — skip silently if unconfigured.
    judge = None
    jm = cfg.entropy.judge_model
    if jm.api_key_env:
        try:
            from ecat.llm.judge import JudgeClient
            judge = JudgeClient(
                runtime=jm.runtime,
                model=jm.model,
                api_key_env=jm.api_key_env,
                base_url=jm.base_url,
            )
        except Exception as exc:
            log.debug("JudgeClient unavailable (prose fallback disabled): %s", exc)

    engine = EntropyEngine(
        cfg.entropy.plugin_set,
        cfg.entropy.weights,
        android_dir=Path(cfg.paths.android),
        spec_dir=Path(cfg.paths.spec),
        judge_client=judge,
        agentic_runner=disc_runner,
        judge_model=cfg.discriminator.model,
        agentic_env=_build_env(cfg, "discriminator"),
        # Defer agentic judges to GradientBuilder so they run as parallel
        # subagents inside the discriminator session — saves 3 separate
        # Claude/opencode subprocesses per iter and lets the discriminator see
        # all verdicts before writing the gradient.
        defer_agentic_judges=True,
        deveco_studio_path=_resolve_deveco_path(cfg.loop),
        runs_dir=runs_dir,
        judge_max_issues=cfg.entropy.judge_max_issues,
        dynamic_render_interval=cfg.entropy.dynamic_render_interval,
        feature_checklist=cfg.paths.feature_checklist,
    )

    if score_only:
        report = engine.evaluate(Path(cfg.paths.harmony), iteration=0)
        _print_entropy_report(report)
        return

    history_file = runs_dir / "entropy_history.jsonl"
    timeseries = EntropyTimeseries(history_file)
    charts = ChartRenderer(charts_dir, fmt=cfg.reporting.chart_format)

    gen_runner = _make_runner(
        cfg.generator,
        add_dirs=[cfg.paths.android, cfg.paths.spec],
    )
    disc_runner = _make_runner(
        cfg.discriminator,
        add_dirs=[*cfg.discriminator.evolution_write_paths, cfg.paths.android, cfg.paths.spec],
    )

    from ecat.gradient.builder import GradientBuilder
    from ecat.loop.driver import LoopDriver
    from ecat.loop.policies import OscillationPolicy, RegressionPolicy

    _skill_root = ws_path / ".ecat" / "agents" / "generator" / "skills"
    if not _skill_root.exists():
        # Fall back to ECAT seed directory (development installs)
        _skill_root = Path(__file__).parent / "seed" / "agents" / "generator" / "skills"
    _catalog_path = ws_path / ".ecat" / "skill_catalog.json"

    # Subagent dispatch tool name is runtime-specific:
    #   claude_code → "Agent" or "Task" (depends on the installed Claude Code version; probed)
    #   opencode    → "task"    (opencode's tool name)
    # Generator and discriminator can use different runtimes, so compute each.
    from ecat.runner.claude_code import detect_subagent_tool

    def _subagent_tool(agent_cfg: AgentConfig) -> str:
        if agent_cfg.runtime in ("opencode", "deveco_code"):
            return "task"
        config_dir = str(agent_cfg.config_dir.resolve()) if agent_cfg.config_dir else None
        return detect_subagent_tool(config_dir)

    gen_subagent_tool = _subagent_tool(cfg.generator)
    disc_subagent_tool = _subagent_tool(cfg.discriminator)
    log.info("Subagent tool: generator=%s | discriminator=%s", gen_subagent_tool, disc_subagent_tool)

    _memory_dir = ws_path / ".ecat" / "agents" / "generator" / "memory"
    # Bake the run-constant Android source path into the discriminator prompt up
    # front — it's the ground-truth reference for the whole run, not a
    # per-iteration field, so it doesn't go through GradientBuilder.format().
    # The HarmonyOS repo is the runner's cwd, so it needs no path token.
    disc_prompt_template = load_prompt("discriminator").replace(
        "{android_dir}", Path(cfg.paths.android).resolve().as_posix()
    )
    gradient_gen = GradientBuilder(
        runner=disc_runner,
        model=cfg.discriminator.model,
        plugins=engine.plugins,
        env=_build_env(cfg, "discriminator"),
        cwd=Path(cfg.paths.harmony),
        prompt_template=disc_prompt_template,
        skill_root=_skill_root if _skill_root.exists() else None,
        catalog_path=_catalog_path,
        weights=cfg.entropy.weights,
        subagent_tool=disc_subagent_tool,
        memory_evolution=cfg.features.memory_evolution,
        memory_dir=_memory_dir if cfg.features.memory_evolution else None,
        judge_max_issues=cfg.entropy.judge_max_issues,
        max_items_per_iter=cfg.loop.max_items_per_iter,
    )

    user_instructions = _resolve_prompt(prompt, prompt_file)
    gen_prompt_template = load_prompt("generator")
    if user_instructions:
        gen_prompt_template += f"\n\nAdditional instructions from the user:\n{user_instructions}"

    subagent_tool = gen_subagent_tool

    osc = cfg.loop.oscillation_guard
    deveco_path = _resolve_deveco_path(cfg.loop)
    driver = LoopDriver(
        harmony_dir=Path(cfg.paths.harmony),
        android_dir=Path(cfg.paths.android),
        spec_dir=Path(cfg.paths.spec),
        runs_dir=runs_dir,
        entropy_engine=engine,
        gradient_builder=gradient_gen,
        generator_runner=gen_runner,
        generator_model=cfg.generator.model,
        generator_env=_build_env(cfg, "generator"),
        max_iterations=max_iter or cfg.loop.max_iterations,
        entropy_threshold=cfg.loop.entropy_threshold,
        regression_policy=RegressionPolicy(
            cfg.loop.regression_tolerance, cfg.loop.revert_on_regression
        ),
        oscillation_policy=OscillationPolicy(
            osc.window, osc.min_delta, osc.enabled
        ),
        timeseries=timeseries,
        generator_prompt_template=gen_prompt_template,
        pause_callback=_pause_callback if pause else None,
        subagent_tool=subagent_tool,
        deveco_studio_path=deveco_path,
        generator_compact_interval=cfg.loop.generator_compact_interval,
        memory_dir=_memory_dir if cfg.features.memory_evolution else None,
        max_items_per_iter=cfg.loop.max_items_per_iter,
        chart_renderer=charts,
        stable_iters_to_stop=cfg.loop.stable_iters_to_stop,
        resource_porter=cfg.features.resource_porter,
        required_skills=cfg.skills.require,
        auto_fetch_skills=cfg.skills.auto_fetch,
        skills_dir=_skill_root,
        ui_flows_spec=(cfg.ui_flows.spec if cfg.ui_flows.enabled else None),
        ui_flows_cmd=([cfg.ui_flows.python, cfg.ui_flows.runner] if cfg.ui_flows.enabled else []),
        ui_flows_bundle=cfg.ui_flows.bundle,
        ui_flows_device=cfg.ui_flows.device,
        ui_flows_timeout=cfg.ui_flows.timeout_s,
    )

    reports = driver.run(start_iteration=max(0, start_iter))

    # Re-render charts
    iters = [r.iteration for r in reports]
    hs = [r.total_H for r in reports]
    bds = [r.breakdown for r in reports]
    if iters:
        charts.render_trend(iters, hs, epsilon=cfg.loop.entropy_threshold)
        charts.render_breakdown(iters, bds)

    # Generate report
    from ecat.report.summary import ReportSummary

    summary = ReportSummary(runs_dir=runs_dir, charts_dir=charts_dir)
    report_path = summary.write(reports)

    console.print(f"[green]Refine complete. {len(reports)} iterations.[/green]")
    if reports:
        console.print(f"Final H = {reports[-1].total_H:.4f}")
    console.print(f"Report: {report_path}")


# ── status ────────────────────────────────────────────────────────────────────


@app.command()
def status(
    config: Annotated[Path, typer.Option(help="Config file")] = _DEFAULT_CONFIG,
) -> None:
    """Print current H, Δ, and iteration count."""
    from ecat.errors import WorkspaceError
    from ecat.workspace import Workspace

    ws = Workspace(Path("."))
    try:
        runs_dir = ws.current_runs_dir()
    except WorkspaceError:
        console.print("[yellow]No runs found. Run `ecat refine` first.[/yellow]")
        return
    history_file = runs_dir / "entropy_history.jsonl"
    if not history_file.exists():
        console.print("[yellow]No entropy history found. Run `ecat refine` first.[/yellow]")
        return

    from ecat.report.timeseries import EntropyTimeseries

    ts = EntropyTimeseries(history_file)
    records = ts.load()
    if not records:
        console.print("[yellow]History file is empty.[/yellow]")
        return

    last = records[-1]
    prev = records[-2] if len(records) >= 2 else None
    delta = (last.total_H - prev.total_H) if prev else 0.0

    console.print(
        f"iter {last.iteration} | H = {last.total_H:.4f} ({delta:+.4f}) | "
        f"iterations: {len(records)}"
    )

    # Cost summary
    cost_file = runs_dir / "cost_history.jsonl"
    if cost_file.exists():
        import contextlib

        total_cost = 0.0
        for line in cost_file.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(Exception):
                total_cost += json.loads(line).get("est_cost_usd", 0.0)
        console.print(f"Estimated cost: ${total_cost:.4f}")


# ── diff ──────────────────────────────────────────────────────────────────────


@app.command()
def diff(
    iteration: Annotated[int, typer.Argument(help="Iteration number to inspect")],
) -> None:
    """Show entropy breakdown + text gradient + diff for a given iteration."""
    from ecat.errors import WorkspaceError
    from ecat.workspace import Workspace

    ws = Workspace(Path("."))
    try:
        runs_dir = ws.current_runs_dir()
    except WorkspaceError:
        console.print("[red]No runs found. Run `ecat refine` first.[/red]")
        raise typer.Exit(1) from None
    iter_dir = runs_dir / str(iteration)
    if not iter_dir.exists():
        console.print(f"[red]Iteration {iteration} not found at {iter_dir}[/red]")
        raise typer.Exit(1)

    for fname in ("gradient.md", "gradient.json", "diff.patch"):
        f = iter_dir / fname
        if f.exists():
            console.rule(fname)
            console.print(f.read_text(encoding="utf-8"))


# ── report ────────────────────────────────────────────────────────────────────


@app.command()
def report() -> None:
    """Generate a markdown run report (including charts)."""
    from ecat.errors import WorkspaceError
    from ecat.workspace import Workspace

    ws = Workspace(Path("."))
    try:
        runs_dir = ws.current_runs_dir()
    except WorkspaceError:
        console.print("[yellow]No runs found. Run `ecat refine` first.[/yellow]")
        return
    history_file = runs_dir / "entropy_history.jsonl"
    if not history_file.exists():
        console.print("[yellow]No history found.[/yellow]")
        return

    from ecat.report.summary import ReportSummary
    from ecat.report.timeseries import EntropyTimeseries

    ts = EntropyTimeseries(history_file)
    records = ts.load()
    charts_dir = runs_dir / "charts"

    summary = ReportSummary(runs_dir=runs_dir, charts_dir=charts_dir)
    out = summary.write(records)
    console.print(f"[green]Report written to {out}[/green]")


# ── uiflow ────────────────────────────────────────────────────────────────────

uiflow_app = typer.Typer(
    help="UI flow coverage: A 直跳清单 / B 点击遍历 / diff 算 FN 率",
    no_args_is_help=True,
)
app.add_typer(uiflow_app, name="uiflow")


@uiflow_app.command("inventory")
def uiflow_inventory(
    apk: Annotated[Path, typer.Option("--apk", help="Patched APK whose manifest is the page source")],
    serial: Annotated[str, typer.Option("--serial", help="adb serial")] = "emulator-5630",
    params: Annotated[Path | None, typer.Option("--params", help="page entry params JSON")] = None,
    out: Annotated[Path, typer.Option("--out", help="output graph JSON (a .md sits next to it)")] = Path(
        "reports/ui-flow/table-A.json"
    ),
    settle: Annotated[float, typer.Option("--settle", help="seconds before the reach assertion")] = 3.0,
    only: Annotated[str, typer.Option("--only", help="comma-separated simple activity names")] = "",
    trampolines: Annotated[str, typer.Option("--trampolines", help="hand-off activities")] = "SplashActivity",
    batch_size: Annotated[int, typer.Option("--batch-size", help="pages per batch before a breather")] = 8,
) -> None:
    """Layer A — 每个 manifest activity 一条参数化 am start，落地断言后出清单。"""
    import json as _json

    from ecat.uiflow.device import Adb, AdbError
    from ecat.uiflow.inventory import inventory
    from ecat.uiflow.report import inventory_markdown

    if not apk.exists():
        console.print(f"[red]APK not found: {apk}[/red]")
        raise typer.Exit(1)
    entry_params = _json.loads(params.read_text(encoding="utf-8")) if params else {}
    adb = Adb(serial)
    try:
        graph = inventory(
            apk,
            adb,
            params=entry_params,
            settle=settle,
            trampolines={x.strip() for x in trampolines.split(",") if x.strip()},
            only={x.strip() for x in only.split(",") if x.strip()} or None,
            batch_size=batch_size,
            log=lambda m: console.print(m, markup=False, highlight=False),
        )
    except (AdbError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    path = graph.save(out)
    path.with_suffix(".md").write_text(inventory_markdown(graph, settle=settle), encoding="utf-8")

    table = Table(title=f"Layer A — {graph.summary()['countable']}/{len(graph.pages)} 页可直跳")
    table.add_column("page")
    table.add_column("verdict")
    table.add_column("detail")
    for page in graph.pages:
        style = "green" if page.countable else "yellow"
        table.add_row(page.label, f"[{style}]{page.verdict}[/{style}]", page.detail[:52])
    console.print(table)
    console.print(f"[green]A graph -> {path}[/green] (+ .md)")


@uiflow_app.command("discover")
def uiflow_discover(
    apk: Annotated[Path | None, typer.Option("--apk", "-a", help="original APK to explore (not the patched one)")] = None,
    serial: Annotated[str, typer.Option("--serial", help="adb serial")] = "emulator-5630",
    out: Annotated[Path, typer.Option("--out", help="output graph JSON (a .md sits next to it)")] = Path(
        "reports/ui-flow/table-B.json"
    ),
    home: Annotated[str, typer.Option("--droidbot-home", help="DroidBot checkout (or ECAT_DROIDBOT_HOME)")] = "",
    python: Annotated[str, typer.Option("--droidbot-python", help="interpreter with DroidBot deps")] = "",
    policy: Annotated[str, typer.Option("--policy", help="droidbot exploration policy")] = "bfs_greedy",
    count: Annotated[int, typer.Option("--count", help="max events")] = 30,
    timeout: Annotated[int, typer.Option("--timeout", help="exploration seconds")] = 300,
    tries: Annotated[int, typer.Option("--tries", help="retries on transient adb failures")] = 3,
    include_external: Annotated[bool, typer.Option("--include-external", help="keep other apps' screens")] = False,
    from_utg: Annotated[Path | None, typer.Option("--from-utg", help="import an existing utg.js instead of running")] = None,
    baseline_dir: Annotated[Path, typer.Option("--baseline-dir", help="where explorations are pinned by APK sha")] = Path(
        "reports/ui-flow/baselines"
    ),
    refresh: Annotated[bool, typer.Option("--refresh", help="explore again even if a baseline exists for this build")] = False,
    start_activity: Annotated[
        str,
        typer.Option("--start-activity", help="seed exploration on this activity (guided coverage of a page the entry never reaches)"),
    ] = "",
    allow_sha_mismatch: Annotated[
        bool, typer.Option("--allow-sha-mismatch", help="accept a UTG that was explored from another APK build")
    ] = False,
) -> None:
    """Layer B（发现）— 用 DroidBot 自动遍历；同一个 APK sha 默认复用已固定的基线。"""
    import json as _json

    from ecat.uiflow.discover import (
        DroidBotError,
        file_sha256,
        graph_from_utg,
        load_baseline,
        parse_utg,
        run_droidbot,
        store_baseline,
        summary_line,
    )
    from ecat.uiflow.report import discover_markdown

    framework: dict = {}
    out_dir = ""
    mode = ""
    sha = file_sha256(apk) if apk is not None and apk.exists() else ""

    try:
        if from_utg is not None:
            if not from_utg.exists():
                console.print(f"[red]UTG not found: {from_utg}[/red]")
                raise typer.Exit(1)
            framework = parse_utg(from_utg)
            out_dir, mode = str(from_utg.parent), "imported"
            utgs_sha = str(framework.get("app_sha256") or "")
            if apk is not None and apk.exists() and utgs_sha:
                cur_sha = file_sha256(apk)
                if utgs_sha != cur_sha and not allow_sha_mismatch:
                    console.print(
                        f"[red]this UTG was explored from build {utgs_sha[:12]}…, not {cur_sha[:12]}…[/red]\n"
                        "[yellow]Layer B is a baseline: a stale one silently changes the FN denominator.[/yellow] "
                        "Re-explore, or pass --allow-sha-mismatch if you know the UI is unchanged."
                    )
                    raise typer.Exit(2)
                if utgs_sha != cur_sha:
                    console.print("[yellow]sha mismatch allowed: denominator may not describe this build[/yellow]")
            console.print(f"[yellow]imported existing UTG {from_utg}[/yellow]")
        else:
            if apk is None or not apk.exists():
                console.print(f"[red]APK not found: {apk} (or pass --from-utg)[/red]")
                raise typer.Exit(1)
            sha = file_sha256(apk)
            pinned = None if refresh else load_baseline(baseline_dir, sha)
            if pinned is not None:
                framework = parse_utg(pinned)
                out_dir, mode = str(pinned.parent), "reused"
                console.print(f"[cyan]reusing baseline for sha {sha[:12]}… ({pinned.name})[/cyan]")
            else:
                run = run_droidbot(
                    apk,
                    serial,
                    out.parent / "droidbot-run",
                    home=home or None,
                    python=python or None,
                    policy=policy,
                    count=count,
                    timeout=timeout,
                    tries=tries,
                    start_activity=start_activity,
                    log=lambda m: console.print(m, markup=False, highlight=False),
                )
                framework, out_dir, mode = run.framework, str(run.out_dir), "captured"
                console.print(summary_line(run))

        sha = sha or str(framework.get("app_sha256") or "")
        if sha and mode != "reused":
            utg_path = from_utg if from_utg is not None else Path(out_dir) / "utg.js"
            if utg_path.is_file():
                stored = store_baseline(utg_path, baseline_dir, sha)
                console.print(f"[dim]baseline pinned: {stored}[/dim]")
        from ecat.uiflow.discover import state_geometry, state_type_histograms

        run_dir = Path(out_dir) if out_dir else (Path(from_utg).parent if from_utg else Path("."))
        graph = graph_from_utg(
            framework,
            include_external=include_external,
            types_by_state=state_type_histograms(run_dir),
            geometry_by_state=state_geometry(run_dir),
        )
    except DroidBotError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    graph.stats["baseline"] = mode
    graph.stats["baseline_sha"] = sha[:12]
    path = graph.save(out)
    path.with_suffix(".md").write_text(discover_markdown(graph, framework, out_dir), encoding="utf-8")
    path.with_suffix(".utg.json").write_text(
        _json.dumps(framework, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for page in graph.pages:
        console.print(f"  {page.label:32s} {page.component.rsplit('/', 1)[-1]}")
    console.print(
        f"[green]discover -> {path}[/green] (+ .md, + .utg.json) · "
        f"pages={len(graph.countable())} edges={len(graph.edges)} · baseline={mode} sha={sha[:12]}"
    )


@uiflow_app.command("suggest-assertions")
def uiflow_suggest_assertions(
    report: Annotated[Path, typer.Option("--report", help="T2 ui_runner report")],
    out: Annotated[Path, typer.Option("--out", help="suggestions JSON")] = Path(
        "reports/ui-flow/assertion-suggestions.json"
    ),
) -> None:
    """从报告里挑出“只在这一屏出现”的标签，作为能证明到达的断言。"""
    from ecat.uiflow.flows_report import suggest_assertions

    if not report.exists():
        console.print(f"[red]report not found: {report}[/red]")
        raise typer.Exit(1)
    payload = json.loads(report.read_text(encoding="utf-8"))
    suggestions = suggest_assertions(payload)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(suggestions, ensure_ascii=False, indent=2), encoding="utf-8")
    if not suggestions:
        console.print("[yellow]no discriminating labels found (same signature on every screen?)[/yellow]")
        raise typer.Exit(0)
    for screen, labels in suggestions.items():
        console.print(f"  {screen}: " + ", ".join(f'"{lab}"' for lab in labels))
    console.print(f"[green]suggestions -> {out}[/green]")


@uiflow_app.command("audit-discovery")
def uiflow_audit_discovery(
    graph: Annotated[Path, typer.Option("--graph", help="layer B graph (discovery output)")],
    apk: Annotated[Path, typer.Option("--apk", "-a", help="the APK the graph describes")],
    out: Annotated[Path, typer.Option("--out", help="coverage JSON")] = Path(
        "reports/ui-flow/discovery-coverage.json"
    ),
) -> None:
    """B 的分母完整吗：manifest 里的 activity 有多少根本没被探索到。"""
    from ecat.uiflow.discover import activity_coverage
    from ecat.uiflow.inventory import enumerate_activities
    from ecat.uiflow.models import PageGraph

    for label, path in (("graph", graph), ("apk", apk)):
        if not path.exists():
            console.print(f"[red]{label} not found: {path}[/red]")
            raise typer.Exit(1)
    try:
        pkg, acts = enumerate_activities(apk)
    except Exception as exc:  # noqa: BLE001 - a missing manifest backend is an input problem
        console.print(f"[red]cannot read the manifest: {exc}[/red]")
        raise typer.Exit(2) from exc

    result = activity_coverage(PageGraph.load(graph), acts, package=pkg)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(
        f"B 覆盖 {result['app_activities_seen']}/{result['app_activities_total']} 个本 app 的 activity"
        f"（{result['app_coverage']:.1%}）· 另含库页面 {result['library_activities']} -> {out}"
    )
    for name in result["never_reached"][:12]:
        console.print(f"  [yellow]never reached[/yellow] {name}")
    if result["never_reached"]:
        console.print(
            "[yellow]分母可能偏小：FN 会因此偏低。加大 --count/--timeout 或把这些页补进探索路径。[/yellow]"
        )


@uiflow_app.command("cover-gaps")
def uiflow_cover_gaps(
    apk: Annotated[Path, typer.Option("--apk", "-a", help="original APK")],
    graph: Annotated[Path, typer.Option("--graph", help="current layer-B graph to extend")],
    out: Annotated[Path, typer.Option("--out", help="merged graph JSON")] = Path(
        "reports/ui-flow/table-B-covered.json"
    ),
    serial: Annotated[str, typer.Option("--serial", help="adb serial")] = "emulator-5630",
    targets: Annotated[int, typer.Option("--targets", help="how many uncovered activities to seed")] = 4,
    count: Annotated[int, typer.Option("--count", help="events per seeded round")] = 30,
    timeout: Annotated[int, typer.Option("--timeout", help="seconds per seeded round")] = 180,
    home: Annotated[str, typer.Option("--droidbot-home")] = "",
    python: Annotated[str, typer.Option("--droidbot-python")] = "",
) -> None:
    """用 A 的清单给 B 补探：对未覆盖的 activity 逐条 seed 一轮探索，再合并进分母。"""
    from ecat.uiflow.discover import activity_coverage, merge_graphs, run_droidbot, summary_line
    from ecat.uiflow.inventory import enumerate_activities
    from ecat.uiflow.models import PageGraph

    for label, path in (("apk", apk), ("graph", graph)):
        if not path.exists():
            console.print(f"[red]{label} not found: {path}[/red]")
            raise typer.Exit(1)
    pkg, acts = enumerate_activities(apk)
    graphs = [PageGraph.load(graph)]
    before = activity_coverage(graphs[0], acts, package=pkg)
    console.print(
        f"起点：{before['app_activities_seen']}/{before['app_activities_total']} "
        f"({before['app_coverage']:.1%}) · 未覆盖 {len(before['never_reached'])}"
    )

    pending = [name for name in before["never_reached"] if name.startswith(pkg)][: max(targets, 0)]
    for i, activity in enumerate(pending, 1):
        console.print(f"  [{i}/{len(pending)}] seed on {activity.rsplit('.', 1)[-1]}")
        try:
            run = run_droidbot(
                apk,
                serial,
                out.parent / f"seeded-{activity.rsplit('.', 1)[-1]}",
                home=home or None,
                python=python or None,
                count=count,
                timeout=timeout,
                start_activity=activity,
                log=lambda m: console.print(m, markup=False, highlight=False),
            )
        except Exception as exc:  # noqa: BLE001 - one dead seed must not end the sweep
            console.print(f"[yellow]  seed failed: {exc}[/yellow]")
            continue
        from ecat.uiflow.discover import graph_from_utg, state_geometry, state_type_histograms

        graphs.append(
            graph_from_utg(
                run.framework,
                types_by_state=state_type_histograms(run.out_dir),
                geometry_by_state=state_geometry(run.out_dir),
            )
        )
        console.print(f"    {summary_line(run)}")

    merged = merge_graphs(graphs)
    path = merged.save(out)
    after = activity_coverage(merged, acts, package=pkg)
    console.print(
        f"[green]{before['app_coverage']:.1%} -> {after['app_coverage']:.1%}[/green] "
        f"({after['app_activities_seen']}/{after['app_activities_total']}) · "
        f"pages={len(merged.pages)} edges={len(merged.edges)} -> {path}"
    )


@uiflow_app.command("diagnose")
def uiflow_diagnose(
    base: Annotated[Path, typer.Option("--base", help="layer B graph (what users can reach)")],
    port_report: Annotated[Path, typer.Option("--port-report", help="T2 ui_runner report")],
    spec: Annotated[Path, typer.Option("--spec", help="spec file (the join key)")],
    out: Annotated[Path, typer.Option("--out", help="work items JSON (a .md sits next to it)")] = Path(
        "reports/ui-flow/diagnosis.json"
    ),
    flows: Annotated[Path | None, typer.Option("--flows", help="flow spec, enables the flow audit")] = None,
    port_dir: Annotated[Path | None, typer.Option("--port-dir", help="harmony project, enables port check")] = None,
    bundle: Annotated[str, typer.Option("--bundle", help="bundle name")] = "",
    strict: Annotated[bool, typer.Option("--strict", help="exit 1 when port defects were found")] = False,
) -> None:
    """把 uiflow 当缺陷发现器：跑一遍诊断，产出 ECAT 可吃的 work item（不判验收通过与否）。"""
    from ecat.uiflow.verdict import build_verdict
    from ecat.uiflow.workitems import save, to_work_items

    for label, path in (("base", base), ("port-report", port_report), ("spec", spec)):
        if not path.exists():
            console.print(f"[red]{label} not found: {path}[/red]")
            raise typer.Exit(2)
    try:
        verdict = build_verdict(base=base, port_report=port_report, spec=spec, flows=flows,
                                port_dir=port_dir, bundle=bundle, history_dir=None)
    except (ValueError, json.JSONDecodeError) as exc:
        console.print(f"[red]diagnosis failed: {exc}[/red]")
        raise typer.Exit(2) from exc

    payload = to_work_items(verdict, port_dir=port_dir, spec=spec)
    path = save(payload, out)
    defects = payload.get("work_list") or []
    tooling = payload.get("tooling") or []
    for item in defects:
        console.print(f"  [{'red' if item['severity'] == 'high' else 'yellow'}]{item['severity']:5s}[/] "
                      f"{item['file']}")
        console.print(f"        {item['issues'][0]}")
    for item in tooling:
        console.print(f"  [dim]tooling {item.get('screen')}: {item.get('issue')[:90]}[/dim]")
    console.print(
        f"[green]诊断：{len(defects)} 个端口缺陷 · {len(tooling)} 个测量问题 -> {path}[/green] (+ .md)"
    )
    if strict and defects:
        raise typer.Exit(1)


@uiflow_app.command("compare-structure")
def uiflow_compare_structure(
    base: Annotated[Path, typer.Option("--base", help="layer B graph")],
    port: Annotated[Path, typer.Option("--port", help="layer C graph")],
    spec: Annotated[Path, typer.Option("--spec", help="spec file (the pairing to check)")],
    out: Annotated[Path, typer.Option("--out", help="comparison JSON (a .md sits next to it)")] = Path(
        "reports/ui-flow/structure-compare.json"
    ),
) -> None:
    """按元素类型直方图核对 spec 的配对是否合理（只提示，不改页面身份）。"""
    from ecat.uiflow.models import PageGraph
    from ecat.uiflow.spec import UiflowSpec, diff_against_spec
    from ecat.uiflow.structure import compare_screens, to_markdown

    for label, path in (("base", base), ("port", port), ("spec", spec)):
        if not path.exists():
            console.print(f"[red]{label} not found: {path}[/red]")
            raise typer.Exit(1)
    base_graph, port_graph = PageGraph.load(base), PageGraph.load(port)
    result = diff_against_spec(base_graph, port_graph, UiflowSpec.load(spec))
    rows = compare_screens(result, base_graph, port_graph)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    out.with_suffix(".md").write_text(to_markdown(rows), encoding="utf-8")
    for row in rows:
        score = "—" if row["similarity"] is None else f"{row['similarity']:.2f}"
        colour = {"plausible": "green", "suspicious": "red"}.get(row["verdict"], "dim")
        console.print(f"  [{colour}]{row['verdict']:16s}[/{colour}] {row['screen']:24s} {score}")
    console.print(f"[green]compare -> {out}[/green] (+ .md)")


@uiflow_app.command("merge-graphs")
def uiflow_merge_graphs(
    graphs: Annotated[list[Path], typer.Argument(help="B graphs to union (2 or more)")],
    out: Annotated[Path, typer.Option("--out", help="merged graph JSON")] = Path(
        "reports/ui-flow/table-B-merged.json"
    ),
    apk: Annotated[Path | None, typer.Option("--apk", help="report the coverage of the merged graph")] = None,
) -> None:
    """把多轮探索并成一个分母，并可选地报告它覆盖了 manifest 里多少 activity。"""
    from ecat.uiflow.discover import activity_coverage, merge_graphs
    from ecat.uiflow.models import PageGraph

    loaded = []
    for path in graphs:
        if not path.exists():
            console.print(f"[red]graph not found: {path}[/red]")
            raise typer.Exit(1)
        loaded.append(PageGraph.load(path))

    merged = merge_graphs(loaded)
    path = merged.save(out)
    console.print(
        f"[green]merged {len(loaded)} graph(s) -> {path}[/green] · "
        f"pages={len(merged.pages)} edges={len(merged.edges)}"
    )
    if apk is not None and apk.exists():
        from ecat.uiflow.inventory import enumerate_activities

        pkg, acts = enumerate_activities(apk)
        cov = activity_coverage(merged, acts, package=pkg)
        console.print(
            f"覆盖 {cov['app_activities_seen']}/{cov['app_activities_total']} 个本 app activity"
            f"（{cov['app_coverage']:.1%}）· 仍有 {len(cov['never_reached'])} 个没到"
        )


@uiflow_app.command("import-flows")
def uiflow_import_flows(
    report: Annotated[Path, typer.Option("--from", help="T2 functional-gate report (ui_runner JSON)")],
    out: Annotated[Path, typer.Option("--out", help="output graph JSON")] = Path("reports/ui-flow/table-C.json"),
    bundle: Annotated[str, typer.Option("--bundle", help="port bundle name (label only)")] = "",
    device: Annotated[str, typer.Option("--device", help="device that produced the report")] = "",
    no_failed_assertions: Annotated[
        bool,
        typer.Option("--no-failed-assertions", help="do not count screens whose flow failed on an assertion"),
    ] = False,
) -> None:
    """Layer C（到达）— 吃 T2 functional gate 的 ui_runner 报告，一屏一页。"""
    from ecat.uiflow.flows_report import (
        FlowsReportError,
        flows_summary,
        graph_from_flows_report,
        load_flows_report,
    )

    if not report.exists():
        console.print(f"[red]flows report not found: {report}[/red]")
        raise typer.Exit(1)
    try:
        payload = load_flows_report(report)
    except (FlowsReportError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    graph = graph_from_flows_report(
        payload,
        bundle=bundle,
        device=device,
        include_failed_assertions=not no_failed_assertions,
    )
    path = graph.save(out)
    console.print(flows_summary(payload))
    console.print(f"  stats: {graph.stats}")
    console.print(f"[green]C graph -> {path}[/green] · pages={len(graph.countable())}")


@uiflow_app.command("spec-from-discovery")
def uiflow_spec_from_discovery(
    graph: Annotated[Path, typer.Option("--graph", help="Layer B graph (discovery output)")],
    out: Annotated[Path, typer.Option("--out", help="spec JSON to write")] = Path("specs/ui-flow.spec.json"),
    prefix: Annotated[str, typer.Option("--prefix", help="prefix for generated screen names")] = "",
    port_dir: Annotated[
        Path | None, typer.Option("--port-dir", help="harmony project: propose port.screen candidates from its page registry")
    ] = None,
) -> None:
    """从发现结果生成 spec 骨架，并用端口注册页给出候选配对（人只做二选一确认）。"""
    from ecat.uiflow.models import PageGraph
    from ecat.uiflow.portcheck import read_port_pages
    from ecat.uiflow.spec import spec_from_graph

    if not graph.exists():
        console.print(f"[red]graph not found: {graph}[/red]")
        raise typer.Exit(1)
    port_pages = []
    if port_dir is not None:
        if not port_dir.exists():
            console.print(f"[red]port dir not found: {port_dir}[/red]")
            raise typer.Exit(1)
        pages = read_port_pages(port_dir)
        port_pages = list(pages.registered)
        if not port_pages:
            console.print(f"[yellow]no main_pages.json under {port_dir} — no candidates proposed[/yellow]")

    spec = spec_from_graph(PageGraph.load(graph), name_prefix=prefix, port_pages=port_pages or None)
    path = spec.save(out)
    console.print(
        f"[green]spec -> {path}[/green] · {len(spec.screens)} screen(s)"
        f" · 自动配对 {len(spec.unconfirmed())} · 仍空 {len(spec.missing_port_screens())}"
    )
    for row in spec.screens:
        if row.auto:
            console.print(f"  [yellow]?[/yellow] {row.screen} -> [bold]{row.port_screen}[/bold]  [dim]{row.note}[/dim]")
        elif not row.port_key:
            console.print(f"  [red]·[/red] {row.screen} -> (待填)  [dim]{row.note}[/dim]")
    claimed = {row.port_screen for row in spec.screens if row.port_screen}
    left = [p for p in port_pages if p not in claimed]
    if left:
        console.print(f"  [dim]端口还有 {len(left)} 个页没被认领，可人工挑：{', '.join(left[:8])}[/dim]")
    if spec.unconfirmed():
        console.print(
            f"[yellow]{len(spec.unconfirmed())} 屏是启发式猜的配对：改完确认后跑 "
            f"`ecat uiflow spec-confirm --spec {path}`[/yellow]"
        )


@uiflow_app.command("spec-confirm")
def uiflow_spec_confirm(
    spec: Annotated[Path, typer.Option("--spec", help="spec whose AUTO rows a human has reviewed")],
    only: Annotated[str, typer.Option("--only", help="comma separated screen names to confirm (default: all)")] = "",
) -> None:
    """人看过之后清掉 AUTO 标记——之后 report 不再提示“未确认”。"""
    from ecat.uiflow.spec import UiflowSpec

    if not spec.exists():
        console.print(f"[red]spec not found: {spec}[/red]")
        raise typer.Exit(1)
    spec_obj = UiflowSpec.load(spec)
    wanted = {s.strip() for s in only.split(",") if s.strip()}
    confirmed = 0
    for row in spec_obj.screens:
        if row.auto and (not wanted or row.screen in wanted):
            row.auto = False
            row.note = row.note.replace("AUTO 候选", "确认")
            confirmed += 1
    path = spec_obj.save(spec)
    console.print(
        f"[green]confirmed {confirmed} row(s)[/green] · 剩余未确认 {len(spec_obj.unconfirmed())} -> {path}"
    )


@uiflow_app.command("audit-flows")
def uiflow_audit_flows(
    flows: Annotated[Path, typer.Option("--flows", help="flow spec to audit")],
    out: Annotated[Path, typer.Option("--out", help="audit JSON (a .md sits next to it)")] = Path(
        "reports/ui-flow/flow-audit.json"
    ),
    report: Annotated[Path | None, typer.Option("--report", help="optional T2 report to cross-check")] = None,
    screen_spec: Annotated[
        Path | None,
        typer.Option("--screen-spec", help="spec file: also check that every declared state is proven by a flow step"),
    ] = None,
) -> None:
    """审计 flow：哪些永远绿、哪些随数据漂、哪些没记证据（不碰设备）。"""
    from ecat.uiflow.audit import audit_flows, audit_states, load_spec

    if not flows.exists():
        console.print(f"[red]spec not found: {flows}[/red]")
        raise typer.Exit(1)
    try:
        flow_payload = load_spec(flows)
        result = audit_flows(flow_payload, load_spec(report) if report else None)
        if screen_spec is not None:
            from ecat.uiflow.spec import UiflowSpec

            if not screen_spec.exists():
                console.print(f"[red]screen spec not found: {screen_spec}[/red]")
                raise typer.Exit(1)
            # load through the spec class so a malformed file fails here instead of auditing nothing
            extra = audit_states(UiflowSpec.load(screen_spec), flow_payload)
            result.findings.extend(extra.get("states") or [])
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    path = result.save(out)
    for finding in result.sorted_findings():
        colour = {"high": "red", "medium": "yellow", "low": "dim"}[finding.severity]
        console.print(f"  [{colour}]{finding.severity:6s}[/{colour}] {finding.flow:24s} {finding.check}")
        console.print(f"          {finding.detail}")
    console.print(
        f"[{'green' if result.ok else 'red'}]{'audit passed' if result.ok else 'high-severity findings'}[/{'green' if result.ok else 'red'}]"
        f" · flows={result.flows} {result.by_severity} -> {path}"
    )
    if not result.ok:
        raise typer.Exit(1)


@uiflow_app.command("check-port")
def uiflow_check_port(
    spec: Annotated[Path, typer.Option("--spec", help="spec file whose port.screen names are checked")],
    port_dir: Annotated[Path, typer.Option("--port-dir", help="OpenHarmony project root")],
    out: Annotated[Path, typer.Option("--out", help="check JSON (a .md sits next to it)")] = Path(
        "reports/ui-flow/port-check.json"
    ),
) -> None:
    """校验 spec 的屏名在端口真实存在（main_pages.json + loadContent，不碰设备）。"""
    from ecat.uiflow.portcheck import check_spec
    from ecat.uiflow.spec import UiflowSpec

    if not spec.exists():
        console.print(f"[red]spec not found: {spec}[/red]")
        raise typer.Exit(1)
    if not port_dir.exists():
        console.print(f"[red]port dir not found: {port_dir}[/red]")
        raise typer.Exit(1)
    result = check_spec(UiflowSpec.load(spec), port_dir)
    path = result.save(out)
    for finding in result.sorted_findings():
        colour = {"error": "red", "warning": "yellow", "info": "dim"}[finding.severity]
        console.print(f"  [{colour}]{finding.severity:7s}[/{colour}] {finding.check:18s} {finding.detail}")
    console.print(
        f"[{'green' if not result.errors else 'red'}]"
        f"{'port check passed' if not result.errors else f'{len(result.errors)} error(s)'}[/]"
        f" · registered={len(result.pages.registered)} entry={result.pages.entry} -> {path}"
    )
    if result.errors:
        raise typer.Exit(1)


@uiflow_app.command("report")
def uiflow_report(
    base: Annotated[Path, typer.Option("--base", help="Layer B graph (denominator)")],
    port_report: Annotated[Path, typer.Option("--port-report", help="T2 ui_runner report (source of C)")],
    spec: Annotated[Path, typer.Option("--spec", help="spec file (the join key)")],
    out: Annotated[Path, typer.Option("--out", help="verdict JSON (a .md sits next to it)")] = Path(
        "reports/ui-flow/verdict.json"
    ),
    flows: Annotated[Path | None, typer.Option("--flows", help="flow spec, enables the flow audit")] = None,
    port_dir: Annotated[Path | None, typer.Option("--port-dir", help="harmony project, enables port check")] = None,
    bundle: Annotated[str, typer.Option("--bundle", help="bundle name, for the header")] = "",
    history_dir: Annotated[
        Path, typer.Option("--history-dir", help="where per-bundle verdict history is appended")
    ] = Path("reports/ui-flow/history"),
    no_history: Annotated[bool, typer.Option("--no-history", help="do not record this run")] = False,
) -> None:
    """一条命令出验收结论：port check + flow audit + C 覆盖 + FN + 与上一版对比，单一退出码。"""
    from ecat.uiflow.verdict import BLOCKED, PASS, build_verdict

    for label, path in (("base", base), ("port-report", port_report), ("spec", spec)):
        if not path.exists():
            console.print(f"[red]{label} not found: {path}[/red]")
            raise typer.Exit(2)
    for label, path in (("flows", flows), ("port-dir", port_dir)):
        if path is not None and not path.exists():
            console.print(f"[red]{label} not found: {path}[/red]")
            raise typer.Exit(2)

    try:
        verdict = build_verdict(base=base, port_report=port_report, spec=spec,
                                flows=flows, port_dir=port_dir, bundle=bundle,
                                history_dir=None if no_history else history_dir)
    except (ValueError, json.JSONDecodeError) as exc:
        console.print(f"[red]cannot build a verdict: {exc}[/red]")
        raise typer.Exit(2) from exc

    verdict.artifacts = {
        "base (B)": str(base),
        "port report (C source)": str(port_report),
        "spec": str(spec),
        **({"flows": str(flows)} if flows else {}),
        **({"port dir": str(port_dir)} if port_dir else {}),
        "verdict": str(out),
        **({} if no_history else {"history": str(history_dir)}),
    }
    path = verdict.save(out)
    colour = "green" if verdict.status == PASS else ("yellow" if verdict.status == BLOCKED else "red")
    for check in verdict.checks:
        tick = {"pass": "green", "fail": "red", "blocked": "yellow", "warn": "yellow", "skip": "dim"}.get(
            check.status, "dim"
        )
        console.print(f"  [{tick}]{check.status:7s}[/{tick}] {check.name:14s} {check.detail}")
    if verdict.fn_rate is not None:
        console.print(f"[{colour}]{verdict.status}[/{colour}] · [bold]FN={verdict.fn_rate:.1%}[/bold] -> {path} (+ .md)")
    else:
        console.print(f"[{colour}]{verdict.status}[/{colour}] · FN 未出数 -> {path} (+ .md)")
    for reason in verdict.reasons:
        console.print(f"  [yellow]![/yellow] {reason}")
    if verdict.history:
        if verdict.history.get("first_run"):
            console.print("  [dim]history: 首次记录，没有可比基线[/dim]")
        else:
            word = verdict.history.get("verdict")
            colour_h = "red" if word == "regression" else ("green" if word == "fixed" else "dim")
            console.print(f"  [{colour_h}]history: {word}[/{colour_h}] " +
                          "; ".join(verdict.history.get("changes") or ["无变化"]))
    raise typer.Exit(verdict.exit_code)


@uiflow_app.command("history")
def uiflow_history(
    bundle: Annotated[str, typer.Option("--bundle", help="bundle name")],
    history_dir: Annotated[Path, typer.Option("--history-dir")] = Path("reports/ui-flow/history"),
    limit: Annotated[int, typer.Option("--limit", help="how many runs to show")] = 10,
) -> None:
    """列出某个包历次验收：状态、FN、缺失页。"""
    from ecat.uiflow.history import read_history

    entries = read_history(history_dir, bundle)
    if not entries:
        console.print(f"[dim]no history for {bundle} under {history_dir}[/dim]")
        return
    for entry in entries[-limit:]:
        fn = "未出数" if entry.fn_rate is None else f"{entry.fn_rate:.1%}"
        console.print(
            f"  {entry.generated:20s} v{entry.version or '?':10s} {entry.status:8s} FN={fn:8s} "
            f"pages={entry.port_pages} missing={len(entry.missing_pages)} dead={len(entry.dead_pages)}"
        )


@uiflow_app.command("diff")
def uiflow_diff(
    base: Annotated[Path, typer.Option("--base", help="Layer B graph (denominator)")],
    port: Annotated[Path, typer.Option("--port", help="Layer C graph (numerator)")],
    mapping: Annotated[Path | None, typer.Option("--mapping", help="base->port page mapping JSON")] = None,
    out: Annotated[Path, typer.Option("--out", help="output diff JSON")] = Path("reports/ui-flow/diff.json"),
    force: Annotated[bool, typer.Option("--force", help="allow an amstart graph as denominator")] = False,
    spec: Annotated[Path | None, typer.Option("--spec", help="spec file: per-screen two columns, no name matching")] = None,
    port_dir: Annotated[
        Path | None,
        typer.Option("--port-dir", help="validate spec screen names against the port's page registry first"),
    ] = None,
    allow_degraded: Annotated[
        bool,
        typer.Option("--allow-degraded", help="print an FN number even when flows were blocked by a failed dependency"),
    ] = False,
    apk: Annotated[
        Path | None, typer.Option("--apk", help="APK the denominator should describe; warns if the B graph disagrees")
    ] = None,
) -> None:
    """验收 — diff(B, C)，算 FN 率（A 不能当分母）。"""
    import json as _json

    from ecat.uiflow.diff import diff_graphs
    from ecat.uiflow.models import PageGraph

    for p in (base, port):
        if not p.exists():
            console.print(f"[red]graph not found: {p}[/red]")
            raise typer.Exit(1)
    if spec is not None and port_dir is not None:
        from ecat.uiflow.portcheck import check_spec
        from ecat.uiflow.spec import UiflowSpec as _UiflowSpec

        check = check_spec(_UiflowSpec.load(spec), port_dir)
        for finding in check.errors:
            console.print(f"[red]spec screen does not exist:[/red] {finding.detail}")
        if check.errors:
            console.print(
                "[red]refusing to diff[/red] — a spec that names screens the port does not ship "
                "turns a typo into a missing-page number. Fix the spec (or drop --port-dir)."
            )
            raise typer.Exit(2)
        if check.findings:
            console.print(f"[dim]port check: {check.to_dict()['by_severity']} (no blocking errors)[/dim]")

    if apk is not None and apk.exists():
        from ecat.uiflow.discover import file_sha256

        base_graph = PageGraph.load(base)
        base_sha = str((base_graph.stats or {}).get("app_sha256") or "")
        apk_sha = file_sha256(apk)
        if base_sha and base_sha != apk_sha:
            console.print(
                f"[yellow]![/yellow] B 图来自 {base_sha[:12]}…，手上的 APK 是 {apk_sha[:12]}… "
                "——分母可能描述的不是这个构建"
            )

    degraded = _degraded_notes(port)
    if degraded and not allow_degraded:
        console.print("[yellow]port graph is degraded:[/yellow] " + "; ".join(degraded))
        console.print(
            "[red]refusing to report an FN number[/red] — flows that never ran cannot prove a screen "
            "is missing, so FN would be overstated. Re-run the seed (or pass --allow-degraded)."
        )
        raise typer.Exit(4)

    if spec is not None:
        from ecat.uiflow.spec import UiflowSpec, diff_against_spec

        if not spec.exists():
            console.print(f"[red]spec not found: {spec}[/red]")
            raise typer.Exit(1)
        try:
            result_spec = diff_against_spec(PageGraph.load(base), PageGraph.load(port), UiflowSpec.load(spec))
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc
        path = result_spec.save(out)
        if not result_spec.valid:
            console.print(f"[red]refusing to report an FN number: {result_spec.invalid_reason}[/red]")
            console.print(f"[yellow]artifact kept for inspection: {path}[/yellow]")
            raise typer.Exit(2)
        if result_spec.state_fn_rate is not None:
            miss_states = [f"{r.screen}/{r.state}" for r in result_spec.states_missing]
            console.print(
                f"状态 {result_spec.states_total - len(miss_states)}/{result_spec.states_total}"
                f" · 状态 FN={result_spec.state_fn_rate:.1%}"
                + (f" · 缺 {', '.join(miss_states[:4])}" if miss_states else "")
            )
        console.print(
            f"spec 屏 {len(result_spec.rows)} · Android 到达 {result_spec.android_reached}/{result_spec.android_pages}"
            f"（覆盖 {result_spec.spec_coverage:.1%}）· 端口 {len(result_spec.present)}/{result_spec.port_pages}"
            f" · [bold]FN={result_spec.fn_rate:.1%}[/bold]"
        )
        for row in result_spec.missing:
            console.print(f"  [red]missing[/red] {row.screen}")
        if result_spec.unlisted_android:
            console.print(f"  [yellow]{len(result_spec.unlisted_android)} Android page(s) not in spec[/yellow]")
        for warning in result_spec.warnings:
            console.print(f"  [yellow]![/yellow] {warning}")
        console.print(f"[green]diff -> {path}[/green] (+ .md)")
        return

    mapping_data = _json.loads(mapping.read_text(encoding="utf-8")) if mapping else None
    try:
        result = diff_graphs(PageGraph.load(base), PageGraph.load(port), mapping=mapping_data, force=force)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    path = result.save(out)
    if not result.valid:
        console.print(f"[red]refusing to report an FN number: {result.invalid_reason}[/red]")
        console.print(f"[yellow]artifact kept for inspection: {path}[/yellow]")
        raise typer.Exit(2)
    console.print(
        f"B={result.base_count} C={result.port_count} matched={len(result.matched)} "
        f"missing={len(result.missing)} extra={len(result.extra)} "
        f"[bold]FN={result.fn_rate:.1%}[/bold]"
    )
    for item in result.missing:
        console.print(f"  [red]missing[/red] {item['label']}")
    for item in result.extra:
        console.print(f"  [yellow]extra[/yellow]   {item['label']}")
    console.print(f"[green]diff -> {path}[/green] (+ .md)")


# ── helpers ───────────────────────────────────────────────────────────────────


def _degraded_notes(graph_path: Path) -> list[str]:
    """Why this port graph cannot support a headline FN number."""
    from ecat.uiflow.models import PageGraph

    try:
        stats = PageGraph.load(graph_path).stats or {}
    except Exception:  # noqa: BLE001 - a broken graph is diff's problem, not this guard's
        return []
    notes: list[str] = []
    blocked = int(stats.get("dependency_skips") or 0)
    if blocked:
        notes.append(f"{blocked} flow(s) blocked by a failed dependency (never ran)")
    if int(stats.get("pass") or 0) == 0:
        notes.append("no flow passed")
    return notes


def _attach_file_log(log_path: Path) -> None:
    """Add a FileHandler to the root logger that writes plain text (no Rich markup)."""
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(_StripMarkupFormatter("%(asctime)s %(levelname)-7s %(message)s"))
    logging.getLogger().addHandler(fh)


def _load_cfg(config: Path) -> EcatConfig:
    """Load config, print errors nicely, exit on failure."""
    from ecat.config import load_config
    from ecat.errors import ConfigError, RunFailed

    if not config.exists():
        console.print(f"[red]Config file not found: {config}[/red]")
        console.print("Run `ecat init` to create a sample config.")
        raise typer.Exit(1)

    try:
        return load_config(config)
    except (RunFailed, ConfigError, ValueError) as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(1) from exc


def _make_runner(
    agent_cfg: AgentConfig,
    add_dirs: list[str] | list[Path] | None = None,
    transcript_dir: Path | None = None,
) -> AgentRunner:
    from ecat.runner.claude_code import ClaudeCodeRunner
    from ecat.runner.deveco import DevecoCodeRunner
    from ecat.runner.opencode import OpencodeRunner

    config_dir = Path(agent_cfg.config_dir)
    timeout_s = agent_cfg.timeout_s
    extra_args = agent_cfg.extra_args
    dirs = [Path(d) for d in (add_dirs or [])]
    if agent_cfg.runtime == "claude_code":
        return ClaudeCodeRunner(
            config_dir=config_dir, timeout_s=timeout_s, extra_args=extra_args, add_dirs=dirs,
            transcript_dir=transcript_dir,
            resume_session=agent_cfg.resume_session,
        )
    if agent_cfg.runtime == "deveco_code":
        return DevecoCodeRunner(
            config_dir=config_dir, timeout_s=timeout_s, extra_args=extra_args, add_dirs=dirs,
            provider=agent_cfg.provider,
            transcript_dir=transcript_dir,
            base_url=agent_cfg.base_url,
            api_key_env=agent_cfg.api_key_env,
            resume_session=agent_cfg.resume_session,
        )
    return OpencodeRunner(
        config_dir=config_dir, timeout_s=timeout_s, extra_args=extra_args, add_dirs=dirs,
        provider=agent_cfg.provider,
        transcript_dir=transcript_dir,
        base_url=agent_cfg.base_url,
        api_key_env=agent_cfg.api_key_env,
        resume_session=agent_cfg.resume_session,
    )


def _stage_claude_credentials(target_config_dir: str | None) -> None:
    """Copy ~/.claude/.credentials.json into the per-role CLAUDE_CONFIG_DIR.

    Under a Claude.ai subscription the OAuth tokens live in the user-global
    ``~/.claude/.credentials.json``. ECAT overrides ``CLAUDE_CONFIG_DIR`` to a
    workspace-local dir for per-role skills and subagent isolation, which makes
    `claude` look for the credentials in the wrong place. Copy them so both
    requirements are satisfied: per-role config + global subscription auth.
    No-op when the source file is missing (e.g. user not logged in) or when
    the destination already has a fresh copy.
    """
    if not target_config_dir:
        return
    src = Path.home() / ".claude" / ".credentials.json"
    if not src.exists():
        return
    dst = Path(target_config_dir) / ".credentials.json"
    try:
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            return  # already up to date
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    except OSError:
        pass


def _build_env(cfg: EcatConfig, role: str) -> dict[str, str]:
    import os

    from ecat.workspace import Workspace

    ws = Workspace(Path("."))
    env = ws.agent_env(role)
    agent = getattr(cfg, role)
    key_name = agent.api_key_env
    # Empty api_key_env means "no API key needed" — claude_code runtime under
    # a Claude.ai subscription. Strip any leaked auth env vars from the parent
    # process so the `claude` CLI does not try to use a stale API key.
    # CLAUDE_CONFIG_DIR is kept (it points at the per-role workspace dir for
    # skills/subagents); we just copy the OAuth credentials from ~/.claude/
    # into that dir so `claude` finds them at login-restore time.
    if not key_name:
        for k in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_API_BASE",
        ):
            env.pop(k, None)
        _stage_claude_credentials(env.get("CLAUDE_CONFIG_DIR"))
        # macOS keeps the subscription login in the Keychain, not in
        # ~/.claude/.credentials.json, so there is nothing to copy. The `claude`
        # CLI also accepts a long-lived token from `claude setup-token` via
        # CLAUDE_CODE_OAUTH_TOKEN (read from .env), which works on every OS.
        if (
            agent.runtime == "claude_code"
            and not env.get("CLAUDE_CODE_OAUTH_TOKEN")
            and not (Path.home() / ".claude" / ".credentials.json").exists()
        ):
            log.warning(
                "%s: subscription mode, but no ~/.claude/.credentials.json (macOS keeps "
                "it in the Keychain). Run `claude setup-token` and put the token in .env "
                "as CLAUDE_CODE_OAUTH_TOKEN=..., or set api_key_env.", role,
            )
    else:
        key_val = os.environ.get(key_name, "")
        env[key_name] = key_val

        if agent.base_url:
            env["ANTHROPIC_BASE_URL"] = agent.base_url
            env["OPENAI_BASE_URL"] = agent.base_url
            env["OPENAI_API_BASE"] = agent.base_url

        if agent.runtime in ("opencode", "deveco_code") and key_name != "OPENAI_API_KEY":
            env["OPENAI_API_KEY"] = key_val

        if agent.runtime == "claude_code" and agent.base_url and key_name:
            # The `claude` CLI authenticates via ANTHROPIC_AUTH_TOKEN /
            # ANTHROPIC_API_KEY — it never reads a custom var like GLM52_API_KEY.
            # Map the resolved key onto ANTHROPIC_AUTH_TOKEN and strip any
            # ANTHROPIC_API_KEY leaked from the parent env / .env so a stale
            # Anthropic key can't pre-empt the third-party endpoint's token.
            env["ANTHROPIC_AUTH_TOKEN"] = key_val
            env.pop("ANTHROPIC_API_KEY", None)
            # Pin all model aliases so Claude Code subagents don't try to resolve
            # Anthropic model names (e.g. claude-sonnet-4-6) against the
            # third-party endpoint.
            env["ANTHROPIC_MODEL"] = agent.model
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = agent.model
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = agent.model
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = agent.model
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = agent.model

    if agent.runtime == "claude_code":
        # `claude --print` 在模型结束回合后只等挂起任务 600s 就终止它们。判别器派发
        # agentic judge 后有时会先收尾（"I'll assemble the verdict once the judge
        # returns"），judge 在 926 条清单上要跑十几分钟 —— 于是被杀，最终消息是自然语言
        # 而非 JSON，verdict 整个丢失。实测 runs_20260811_1933 的 iter1/iter3 与
        # runs_20260811_2152 的 iter0 都是这样挂的。0 = 不设上限；真正的超时护栏是
        # runner 自己的 timeout_s（判别器 3600s），它会杀掉整棵进程树。
        env.setdefault("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "0")

    _inject_deveco_env(env, cfg.loop.deveco_studio_path)

    return env


def _resolve_deveco_path(loop_cfg: object) -> str | None:
    """Return DevEco Studio root for build_verify, or None if disabled/unavailable."""
    import os

    from ecat.config import LoopConfig
    if not isinstance(loop_cfg, LoopConfig) or not loop_cfg.build_verify:
        return None
    if loop_cfg.deveco_studio_path:
        return loop_cfg.deveco_studio_path
    from ecat import deveco
    root = deveco.root_from_java_home(os.environ.get("JAVA_HOME")) or deveco.default_root()
    if root:
        return str(root)
    log.warning(
        "loop.build_verify=true but deveco_studio_path not set and "
        "JAVA_HOME does not point to a DevEco installation — build verify disabled"
    )
    return None


def _inject_deveco_env(env: dict[str, str], deveco_path: str | None = None) -> None:
    """Put DevEco's JDK, node and SDK on the env of agent subprocesses.

    The DevEco root comes from config, else from JAVA_HOME, else the default
    macOS install location. An existing JAVA_HOME / DEVECO_SDK_HOME is kept.
    """
    import os

    from ecat import deveco

    root = (
        Path(deveco_path) if deveco_path
        else deveco.root_from_java_home(env.get("JAVA_HOME")) or deveco.default_root()
    )
    if root is None or not deveco.is_deveco_root(root):
        java_home = env.get("JAVA_HOME", "")
        if java_home:
            java_bin = Path(java_home) / "bin"
            if java_bin.is_dir() and str(java_bin) not in env.get("PATH", ""):
                env["PATH"] = str(java_bin) + os.pathsep + env.get("PATH", "")
        return

    jdk = deveco.jdk_home(root)
    if jdk and "JAVA_HOME" not in env:
        env["JAVA_HOME"] = str(jdk)
    for extra in (Path(env["JAVA_HOME"]) / "bin" if env.get("JAVA_HOME") else None,
                  deveco.node_bin_dir(root)):
        if extra and extra.is_dir() and str(extra) not in env.get("PATH", ""):
            env["PATH"] = str(extra) + os.pathsep + env.get("PATH", "")
    sdk_dir = root / "sdk"
    if sdk_dir.is_dir() and "DEVECO_SDK_HOME" not in env:
        env["DEVECO_SDK_HOME"] = str(sdk_dir)


def _print_entropy_report(report: EntropyReport) -> None:
    table = Table(title=f"Entropy Report — iter {report.iteration}")
    table.add_column("Dimension")
    table.add_column("Score", justify="right")
    for name, score in sorted(report.breakdown.items(), key=lambda x: -x[1]):
        style = "red" if score > 0.7 else "yellow" if score > 0.3 else "green"
        table.add_row(name, f"[{style}]{score:.4f}[/{style}]")
    console.print(table)
    console.print(f"\nTotal H = {report.total_H:.4f}")


def _resolve_prompt(prompt: str | None, prompt_file: Path | None) -> str:
    """Resolve user prompt from --prompt or --prompt-file."""
    if prompt and prompt_file:
        console.print("[red]Cannot specify both --prompt and --prompt-file[/red]")
        raise typer.Exit(1)
    if prompt_file:
        if not prompt_file.exists():
            console.print(f"[red]Prompt file not found: {prompt_file}[/red]")
            raise typer.Exit(1)
        return prompt_file.read_text(encoding="utf-8").strip()
    return prompt or ""


def _pause_callback(iteration: int, total_h: float) -> bool:
    """Called after each iteration when --pause is active. Returns False to abort."""
    console.print(
        f"\n[yellow]Iteration {iteration} complete (H = {total_h:.4f}). "
        "Press Enter to continue, or type 'q' to stop:[/yellow]"
    )
    try:
        response = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return response not in ("q", "quit", "stop", "exit")




if __name__ == "__main__":
    app()
