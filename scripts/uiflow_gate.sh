#!/usr/bin/env bash
# One CI entry point for the UI coverage gate.
#
#   BUNDLE=com.example.app DEVICE=<serial> HAP=path/to/app.hap \
#   FLOWS=flows.json SPEC=spec.json PORT_DIR=port/ BASE=table-B.json \
#     eval/ecat/scripts/uiflow_gate.sh
#
# Exit codes are the verdict's own: 0 PASS, 1 FAIL, 4 BLOCKED (not measurable), 2 bad input.
# Nothing here decides anything — it only stops the chain at the first refusal, so a CI job that
# looks at $? cannot read a number the gates did not vouch for.
set -uo pipefail

: "${BUNDLE:?BUNDLE is required}"
: "${DEVICE:?DEVICE (device serial) is required}"
: "${FLOWS:?FLOWS (flow spec) is required}"
: "${SPEC:?SPEC (screen spec) is required}"
: "${PORT_DIR:?PORT_DIR (harmony project) is required}"
: "${BASE:?BASE (layer B graph) is required}"

HAP="${HAP:-}"
OUT_DIR="${OUT_DIR:-reports/ui-flow}"
PY="${PY:-python}"
RUNNER="${RUNNER:-tools/ui_runner.py}"
ECAT="${ECAT:-ecat}"

# Everything below may run from a different working directory (the ecat CLI is often a wrapper),
# so resolve inputs once, here.
abspath() {
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
        # git-bash: hand native tools a mixed path they can actually open
        cygpath -m "$full"
      else
        printf '%s\n' "$full"
      fi
      ;;
  esac
}
FLOWS=$(abspath "$FLOWS")
SPEC=$(abspath "$SPEC")
PORT_DIR=$(abspath "$PORT_DIR")
BASE=$(abspath "$BASE")
[[ -n "$HAP" ]] && HAP=$(abspath "$HAP")
OUT_DIR=$(abspath "$OUT_DIR")
REPORT_JSON="$OUT_DIR/port-report.json"
C_JSON="$OUT_DIR/graph-C.json"
VERDICT_JSON="$OUT_DIR/verdict.json"

mkdir -p "$OUT_DIR"
echo "== 1/3 flows on $DEVICE (bundle $BUNDLE) =="
runner_args=(--flows "$FLOWS" --out "$REPORT_JSON" --bundle "$BUNDLE" --device "$DEVICE" --no-hypium)
[[ -n "$HAP" ]] && runner_args+=(--hap "$HAP")
"$PY" "$RUNNER" "${runner_args[@]}" > "$OUT_DIR/runner.log" 2>&1
runner_rc=$?
if [[ ! -f "$REPORT_JSON" ]]; then
  echo "runner produced no report (exit $runner_rc) — see $OUT_DIR/runner.log" >&2
  tail -20 "$OUT_DIR/runner.log" >&2
  exit 2
fi

echo "== 2/3 port graph =="
"$ECAT" uiflow import-flows --from "$REPORT_JSON" --bundle "$BUNDLE" --out "$C_JSON" \
  || { echo "import-flows failed" >&2; exit 2; }

echo "== 3/3 verdict =="
"$ECAT" uiflow report \
  --base "$BASE" \
  --port-report "$REPORT_JSON" \
  --spec "$SPEC" \
  --flows "$FLOWS" \
  --port-dir "$PORT_DIR" \
  --bundle "$BUNDLE" \
  --out "$VERDICT_JSON"
rc=$?

echo
echo "verdict: $VERDICT_JSON (+ .md)"
case "$rc" in
  0) echo "PASS — FN 已出，闸门全过" ;;
  1) echo "FAIL — 有闸门拒绝或端口缺页，看 verdict.md 的 reasons" ;;
  4) echo "BLOCKED — 端口没被量到（依赖失败/无通过 flow），FN 不可用" ;;
  *) echo "ERROR($rc) — 输入或环境问题" ;;
esac
exit "$rc"
