#!/bin/sh
# 可直接运行的 soil-water-ledger 命令行示例。
#
# 在仓库根目录执行：
#   sh examples/ledger-cli/00-run.sh
#
# 它在临时目录建库，依次演示录入 -> 结算首日 -> 两次更正 -> 幂等重发 ->
# 结算次日 -> 报告，结束后打印临时数据库路径（可自行打开核对）。
set -eu

HERE=$(dirname "$0")
if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
else
  PY="python3"
fi

WORK=$(mktemp -d)
DB="$WORK/ledger.db"
OUT="$WORK/report.json"
echo "工作目录: $WORK"

"$PY" -m irrigation.ledger_cli create --db "$DB" --config "$HERE/config.json"
"$PY" -m irrigation.ledger_cli apply  --db "$DB" --source "$HERE/01-apply-initial.json"
"$PY" -m irrigation.ledger_cli close  --db "$DB" --source "$HERE/02-close-day1.json"
"$PY" -m irrigation.ledger_cli apply  --db "$DB" --source "$HERE/03-correction-rev2.json"
"$PY" -m irrigation.ledger_cli apply  --db "$DB" --source "$HERE/04-correction-rev3.json"

echo
echo "## 幂等重发第一次更正（原 expected_generation=2）：返回原回执，generation 不前进"
"$PY" -m irrigation.ledger_cli apply  --db "$DB" --source "$HERE/03-correction-rev2.json"

"$PY" -m irrigation.ledger_cli close  --db "$DB" --source "$HERE/05-close-day2.json"
"$PY" -m irrigation.ledger_cli report --db "$DB" --output "$OUT"

echo
echo "## 报告财务汇总（原结算 57 ＋ 净差额 3 ＝ 财务确认 60）"
"$PY" - "$OUT" <<'PYEOF'
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
print(json.dumps(report["financial_summary"], ensure_ascii=False, indent=2))
for row in report["daily"]:
    wb = row["water_balance"]
    print(row["field_id"], row["date"], "升数=", row["gross_litres"],
          "gross_mm=", row["gross_mm"],
          "未满足ET=", wb["unmet_et_mm"], "期末储水=", wb["closing_mm"])
PYEOF
echo
echo "数据库与报告保留在: $WORK"
