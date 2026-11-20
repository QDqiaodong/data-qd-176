#!/bin/sh
# 可直接运行的 soil-water-reuse 命令行示例。
#
# 在仓库根目录执行：
#   sh examples/reuse-cli/00-run.sh
#
# 演示两个算例：
#   1. input.json       契约规定的两日回流-拦截算例（淡水灌溉 -> 回流变咸 -> 拦截）；
#   2. cycle-input.json 同日双池循环输水的完全混合（与数组排列无关的精确分数浓度）。
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
echo "## 算例 1：两日回流与水质拦截"
"$PY" -m irrigation.reuse_cli \
  --source "$HERE/input.json" --output "$WORK/reuse-report.json"
"$PY" - "$WORK/reuse-report.json" <<'PYEOF'
import json, sys
r = json.load(open(sys.argv[1], encoding="utf-8"))
for d in r["deliveries"]:
    print(d["date"], d["field_id"], "来源浓度=", d["source_mg_per_litre"],
          "接受=", d["accepted"], "入田=", d["applied_litres"],
          "拦截=", d["rejected_litres"], d["rejected_salt_mg"], "mg")
print("系统水残差:", r["system_balance"]["water_litres"]["residual"],
      " 系统盐残差:", r["system_balance"]["salt_mg"]["residual"])
PYEOF

echo
echo "## 算例 2：同日循环输水完全混合（浓度与数组顺序无关）"
"$PY" -m irrigation.reuse_cli \
  --source "$HERE/cycle-input.json" --output "$WORK/cycle-report.json"
"$PY" - "$WORK/cycle-report.json" <<'PYEOF'
import json, sys
r = json.load(open(sys.argv[1], encoding="utf-8"))
for row in r["tank_daily"]:
    print(row["tank_id"], "混合水量=", row["mixed_litres"],
          "浓度=", row["concentration_mg_per_litre"], "mg/L",
          "期末盐=", row["closing_salt_mg"], "mg")
PYEOF

echo
echo "报告保留在: $WORK"
