#!/usr/bin/env bash
# The discovery instrument: same measurement as the gate, but the output is work the port can act on
# instead of a pass/fail number.
#
#   BUNDLE=com.example.app DEVICE=<serial> HAP=path/to/app.hap \
#   FLOWS=flows.json SPEC=spec.json PORT_DIR=port/ BASE=table-B.json \
#     eval/ecat/scripts/uiflow_diagnose.sh
#
# Extra env: CHANGED=screens, OUT_DIR=<dir> (default reports/ui-flow), STRICT=1 (exit 1 when port
# defects were found — do this only once the denominator is trusted).
#
# Exit codes: 0 diagnosis written, 1 defects found and STRICT=1, 2 bad input / no measurement.
# The whole point of separating this from the gate is that a broken measuring stick (weak flow, spec
# typo, missing state evidence) must not block the build while still being impossible to ignore:
# it lands in `tooling`, not in `work_list`, and the fix-verify loop only consumes `work_list`.
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

WORK_ITEMS="$OUT_DIR/diagnosis.json"

uiflow_run_flows || exit 2
uiflow_import_graph || exit 2

echo "== diagnosis =="
strict_flag=()
[[ -n "${STRICT:-}" ]] && strict_flag+=(--strict)
"$ECAT" uiflow diagnose \
  --base "$BASE" \
  --port-report "$REPORT_JSON" \
  --spec "$SPEC" \
  --flows "$FLOWS" \
  --port-dir "$PORT_DIR" \
  --bundle "$BUNDLE" \
  --out "$WORK_ITEMS" \
  "${strict_flag[@]}"
rc=$?

if [[ "$rc" -eq 2 ]]; then
  echo "diagnosis could not be produced — inputs or measurement are broken" >&2
  exit 2
fi

echo
echo "work items: $WORK_ITEMS (+ .md)"
echo "hand them to the fix loop:"
echo "  PROJECT=$PORT_DIR WORK_ITEM=$WORK_ITEMS BUNDLE=$BUNDLE DEVICE=$DEVICE \\"
echo "  FLOWS=$FLOWS SPEC=$SPEC PORT_DIR=$PORT_DIR BASE=$BASE \\"
echo "    $(dirname "$0")/uiflow_fix_verify.sh"
exit "$rc"
