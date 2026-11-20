#!/usr/bin/env bash
# One CI entry point for the UI coverage gate.
#
#   BUNDLE=com.example.app DEVICE=<serial> HAP=path/to/app.hap \
#   FLOWS=flows.json SPEC=spec.json PORT_DIR=port/ BASE=table-B.json \
#     eval/ecat/scripts/uiflow_gate.sh
#
# Extra env: CHANGED=screens (run only flows touching them), OUT_DIR=<dir> (default reports/ui-flow).
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

# shellcheck source=scripts/_uiflow_common.sh
source "$(dirname "$0")/_uiflow_common.sh"
uiflow_prepare

VERDICT_JSON="$OUT_DIR/verdict.json"

uiflow_run_flows || exit 2
uiflow_import_graph || exit 2

echo "== verdict =="
"$ECAT" uiflow report \
  --base "$BASE" \
  --port-report "$REPORT_JSON" \
  --spec "$SPEC" \
  --flows "$FLOWS" \
  --port-dir "$PORT_DIR" \
  --bundle "$BUNDLE" \
  --history-dir "$OUT_DIR/history" \
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
