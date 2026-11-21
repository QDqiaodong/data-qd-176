#!/bin/sh
# 可直接运行的 soil-water-rain-audit 命令行示例。
#
# 在仓库根目录执行：
#   sh examples/rain-audit-cli/00-run.sh
#
# 演示契约强制的三日算例：前两日合计 4mm，三天候选依次 [0,4]、[0,4]、[0,2]。
# 报告在临时目录原子新建；重复运行会换新临时目录，不会覆盖既有报告。
set -eu

HERE=$(dirname "$0")
if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
else
  PY="python3"
fi
# 允许在未安装包的仓库根目录直接运行（已安装时 PYTHONPATH 为空也不影响）。
export PYTHONPATH="${HERE}/../../src${PYTHONPATH:+:$PYTHONPATH}"

WORK=$(mktemp -d)
echo "工作目录: $WORK"

echo
echo "## 三日候选雨量：前两日合计 4mm"
"$PY" -m irrigation.rain_audit_cli \
  --source "$HERE/input.json" --output "$WORK/rain-audit-report.json"
"$PY" - "$WORK/rain-audit-report.json" <<'PYEOF'
import json, sys
r = json.load(open(sys.argv[1], encoding="utf-8"))
print("状态:", r["status"], " 历史数:", r["history_count"])
for row in r["rain_daily"]:
    print(row["date"], "可能雨量=", row["possible_rain_mm"])
for row in r["next_advice"]:
    print(row["field_id"], "建议可能值=", row["possible_gross_mm"],
          "签发=", row["released_gross_mm"])
f = r["follow_up"]
if f is None:
    print("追查单: 无")
else:
    print("追查日期:", f["date"], "最坏/最好待签发=",
          f["worst_withheld_count"], f["best_withheld_count"])
    for o in f["outcomes"]:
        print("  查到", o["rain_mm"], "mm -> 历史", o["history_count"],
              "建议", [(a["field_id"], a["possible_gross_mm"], a["released_gross_mm"])
                       for a in o["next_advice"]])
PYEOF

echo
echo "报告保留在: $WORK"
