#!/usr/bin/env bash
# Fix-and-verify loop: feed a work item to ECAT, rebuild, re-run the same flows, compare verdicts.
#
#   PROJECT=eval/sunflower-harmony \
#   WORK_ITEM=reports/ui-flow/work-item-add-to-garden.json \
#   BUNDLE=com.simplemobiletools.gallery.pro DEVICE=<serial> \
#   FLOWS=eval/sunflower-harmony/shots/sunflower-flows.json \
#   SPEC=eval/sunflower-harmony/shots/sunflower.spec.json \
#   PORT_DIR=eval/sunflower-harmony BASE=<B graph> \
#     scripts/uiflow_fix_verify.sh
#
# Steps: work item -> prompt -> `ecat refine` (spends model budget) -> build -> install -> same flows
# -> verdict -> history delta. The point is that the *same* flows judge the fix; nothing here
# re-writes the expectations after a failure.
set -uo pipefail

: "${PROJECT:?PROJECT (harmony project dir) is required}"
: "${WORK_ITEM:?WORK_ITEM (work item JSON) is required}"
: "${BUNDLE:?BUNDLE is required}"
: "${DEVICE:?DEVICE is required}"
: "${FLOWS:?FLOWS is required}"
: "${SPEC:?SPEC is required}"
: "${PORT_DIR:?PORT_DIR is required}"
: "${BASE:?BASE is required}"

OUT_DIR="${OUT_DIR:-reports/ui-flow/fix-verify}"
MAX_ITER="${MAX_ITER:-2}"
ECAT="${ECAT:-ecat}"
PY="${PY:-python}"
node_bin="${NODE_BIN:-/c/Program Files/Huawei/DevEco Studio/tools/node}"
hv="${HVIGOR:-C:/Program Files/Huawei/DevEco Studio/tools/hvigor/bin/hvigorw.js}"

mkdir -p "$OUT_DIR"
prompt="$OUT_DIR/prompt.md"

echo "== 1/4 work item -> prompt =="
"$PY" - "$WORK_ITEM" "$prompt" <<'PY'
import json, sys, pathlib
item = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
lines = [f"# Fix list: {item.get('summary','')}", ""]
for w in item.get("work_list") or []:
    lines.append(f"## {w['file']}  (severity {w.get('severity','')})")
    for issue in w.get("issues") or []:
        lines.append(f"- {issue}")
    if w.get("verify"):
        lines.append("")
        lines.append("Device check that must pass afterwards:")
        for step in w["verify"]:
            lines.append(f"  {json.dumps(step, ensure_ascii=False)}")
    lines.append("")
pathlib.Path(sys.argv[2]).write_text("\n".join(lines), encoding="utf-8")
print(f"prompt -> {sys.argv[2]}")
PY

echo "== 2/4 ecat refine (max $MAX_ITER iterations; this spends model budget) =="
( cd "$PROJECT" && "$ECAT" refine --max-iter "$MAX_ITER" --prompt-file "$(cd "$(dirname "$prompt")" && pwd)/$(basename "$prompt")" ) \
  | tee "$OUT_DIR/refine.log"
rc=${PIPESTATUS[0]}
if [[ "$rc" -ne 0 ]]; then
  echo "refine failed ($rc) — see $OUT_DIR/refine.log" >&2
  exit 2
fi

echo "== 3/4 build + install =="
( cd "$PROJECT" \
  && PATH="$node_bin:$PATH" JAVA_HOME="${JAVA_HOME:-C:/Program Files/Huawei/DevEco Studio/jbr}" \
     DEVECO_SDK_HOME="${DEVECO_SDK_HOME:-C:/Program Files/Huawei/DevEco Studio/sdk}" \
     node "$hv" --mode module -p product=default assembleHap --no-daemon ) > "$OUT_DIR/build.log" 2>&1 \
  || { echo "build failed — see $OUT_DIR/build.log" >&2; exit 2; }
hap=$(ls -t "$PROJECT"/entry/build/default/outputs/default/*signed.hap 2>/dev/null | head -1)
[[ -n "$hap" ]] || { echo "no signed HAP after build" >&2; exit 2; }
hdc -t "$DEVICE" uninstall "$BUNDLE" >/dev/null 2>&1
( cd "$(dirname "$hap")" && hdc -t "$DEVICE" install "$(basename "$hap")" >/dev/null 2>&1 ) \
  || { echo "install failed: $hap" >&2; exit 2; }

echo "== 4/4 same flows, same spec, verdict =="
HAP="$hap" BUNDLE="$BUNDLE" DEVICE="$DEVICE" FLOWS="$FLOWS" SPEC="$SPEC" \
  PORT_DIR="$PORT_DIR" BASE="$BASE" OUT_DIR="$OUT_DIR" bash "$(dirname "$0")/uiflow_gate.sh"
rc=$?

echo
echo "history:"
"$ECAT" uiflow history --bundle "$BUNDLE" --history-dir "$OUT_DIR/history" --limit 5 || true
echo
case "$rc" in
  0) echo "FIXED — the same flows now pass; see $OUT_DIR/verdict.md" ;;
  1) echo "STILL FAILING — read $OUT_DIR/verdict.md reasons (do not edit the flows to make it green)" ;;
  4) echo "BLOCKED — the port was not measurable this round" ;;
  *) echo "ERROR($rc)" ;;
esac
exit "$rc"
