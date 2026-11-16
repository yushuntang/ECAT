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
    apk: Annotated[Path | None, typer.Option("--apk", help="original APK to explore (not the patched one)")] = None,
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
) -> None:
    """Layer B（发现）— 用 DroidBot 自动遍历出页面集合与跳转边（无需手写流程）。"""
    import json as _json

    from ecat.uiflow.discover import (
        DroidBotError,
        graph_from_utg,
        parse_utg,
        run_droidbot,
        summary_line,
    )
    from ecat.uiflow.report import discover_markdown

    try:
        if from_utg is not None:
            framework = parse_utg(from_utg)
            graph = graph_from_utg(framework, include_external=include_external)
            out_dir = str(from_utg.parent)
            console.print(f"[yellow]imported existing UTG {from_utg}[/yellow]")
        else:
            if apk is None or not apk.exists():
                console.print(f"[red]APK not found: {apk} (or pass --from-utg)[/red]")
                raise typer.Exit(1)
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
                log=lambda m: console.print(m, markup=False, highlight=False),
            )
            graph = graph_from_utg(run.framework, include_external=include_external)
            framework, out_dir = run.framework, str(run.out_dir)
            console.print(summary_line(run))
    except DroidBotError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    path = graph.save(out)
    path.with_suffix(".md").write_text(discover_markdown(graph, framework, out_dir), encoding="utf-8")
    path.with_suffix(".utg.json").write_text(
        _json.dumps(framework, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for page in graph.pages:
        console.print(f"  {page.label:32s} {page.component.rsplit('/', 1)[-1]}")
    console.print(
        f"[green]discover -> {path}[/green] (+ .md, + .utg.json) · "
        f"pages={len(graph.countable())} edges={len(graph.edges)}"
    )


@uiflow_app.command("import-gate")
def uiflow_import_gate(
    report: Annotated[Path, typer.Option("--from", help="functional-gate report (gate.json)")],
    out: Annotated[Path, typer.Option("--out", help="output graph JSON")] = Path("reports/ui-flow/table-C.json"),
    bundle: Annotated[str, typer.Option("--bundle", help="port bundle name (label only)")] = "",
    device: Annotated[str, typer.Option("--device", help="device that produced the report")] = "",
    only_pass: Annotated[bool, typer.Option("--only-pass", help="count only screens the gate passed")] = False,
) -> None:
    """Layer C — 直接吃现有 functional gate 的报告，不另起一套流程/截图逻辑。"""
    from ecat.uiflow.gate import (
        GateReportError,
        gate_summary,
        graph_from_gate_report,
        load_gate_report,
    )

    if not report.exists():
        console.print(f"[red]gate report not found: {report}[/red]")
        raise typer.Exit(1)
    try:
        payload = load_gate_report(report)
    except (GateReportError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    graph = graph_from_gate_report(payload, bundle=bundle, device=device, only_pass=only_pass)
    path = graph.save(out)
    console.print(gate_summary(payload))
    console.print(
        f"[green]C graph -> {path}[/green] · pages={len(graph.countable())}"
        + ("  [yellow](only-pass)[/yellow]" if only_pass else "")
    )


@uiflow_app.command("diff")
def uiflow_diff(
    base: Annotated[Path, typer.Option("--base", help="Layer B graph (denominator)")],
    port: Annotated[Path, typer.Option("--port", help="Layer C graph (numerator)")],
    mapping: Annotated[Path | None, typer.Option("--mapping", help="base->port page mapping JSON")] = None,
    out: Annotated[Path, typer.Option("--out", help="output diff JSON")] = Path("reports/ui-flow/diff.json"),
    force: Annotated[bool, typer.Option("--force", help="allow an amstart graph as denominator")] = False,
) -> None:
    """验收 — diff(B, C)，算 FN 率（A 不能当分母）。"""
    import json as _json

    from ecat.uiflow.diff import diff_graphs
    from ecat.uiflow.models import PageGraph

    for p in (base, port):
        if not p.exists():
            console.print(f"[red]graph not found: {p}[/red]")
            raise typer.Exit(1)
    mapping_data = _json.loads(mapping.read_text(encoding="utf-8")) if mapping else None
    try:
        result = diff_graphs(PageGraph.load(base), PageGraph.load(port), mapping=mapping_data, force=force)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    path = result.save(out)
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
