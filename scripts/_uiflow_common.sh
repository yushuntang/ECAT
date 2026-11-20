#!/usr/bin/env bash
# Shared steps for the two uiflow entry points: the acceptance gate (`uiflow_gate.sh`) and the
# discovery instrument (`uiflow_diagnose.sh`). Both must run the *same* measurement, otherwise the
# work items and the verdict can describe different builds.
#
# Callers set: BUNDLE DEVICE FLOWS SPEC PORT_DIR BASE [HAP] [OUT_DIR] [CHANGED] and then call
# uiflow_run_flows / uiflow_import_graph. Paths are absolutised once here, because the ecat CLI is
# often a wrapper that changes directory.

uiflow_abspath() {
  local p="$1"
  case "$p" in
    [A-Za-z]:[\\/]*) printf '%s\n' "${p//\\//}" ;;
    *)
      local full="$p"
      case "$p" in
        /*) ;;
        *) full="$PWD/$p" ;;
      esac
      if command -v cygpath >/dev/null 2>&1; then
        cygpath -m "$full"   # git-bash: hand native tools a path they can actually open
      else
        printf '%s\n' "$full"
      fi
      ;;
  esac
}

uiflow_prepare() {
  PY="${PY:-python}"
  RUNNER="${RUNNER:-tools/ui_runner.py}"
  ECAT="${ECAT:-ecat}"
  OUT_DIR="$(uiflow_abspath "${OUT_DIR:-reports/ui-flow}")"
  FLOWS="$(uiflow_abspath "$FLOWS")"
  SPEC="$(uiflow_abspath "$SPEC")"
  PORT_DIR="$(uiflow_abspath "$PORT_DIR")"
  BASE="$(uiflow_abspath "$BASE")"
  HAP="${HAP:-}"
  [[ -n "$HAP" ]] && HAP="$(uiflow_abspath "$HAP")"
  REPORT_JSON="$OUT_DIR/port-report.json"
  C_JSON="$OUT_DIR/graph-C.json"
  mkdir -p "$OUT_DIR"
}

uiflow_run_flows() {
  # REPORT=<existing report> re-uses a measurement instead of touching the device — needed when the
  # device is unavailable (locked, busy) and you still want the diagnosis for the last run.
  if [[ -n "${REPORT:-}" ]]; then
    echo "== flows: re-using $REPORT =="
    cp "$(uiflow_abspath "$REPORT")" "$REPORT_JSON" || return 2
    return 0
  fi
  echo "== flows on $DEVICE (bundle $BUNDLE) =="
  local args=(--flows "$FLOWS" --out "$REPORT_JSON" --bundle "$BUNDLE" --device "$DEVICE" --no-hypium)
  [[ -n "${HAP:-}" ]] && args+=(--hap "$HAP")
  [[ -n "${CHANGED:-}" ]] && args+=(--changed "$CHANGED")
  "$PY" "$RUNNER" "${args[@]}" > "$OUT_DIR/runner.log" 2>&1
  local rc=$?
  if [[ ! -f "$REPORT_JSON" ]]; then
    echo "runner produced no report (exit $rc) — see $OUT_DIR/runner.log" >&2
    tail -20 "$OUT_DIR/runner.log" >&2
    return 2
  fi
  return 0
}

uiflow_import_graph() {
  echo "== port graph =="
  "$ECAT" uiflow import-flows --from "$REPORT_JSON" --bundle "$BUNDLE" --out "$C_JSON" || {
    echo "import-flows failed" >&2
    return 2
  }
  return 0
}
