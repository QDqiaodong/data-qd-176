#!/bin/sh
# 可直接运行的 soil-water-land 命令行示例。
#
# 在仓库根目录执行：
#   sh examples/land-cli/00-run.sh
#
# 演示契约规定的三日拆并接续算例（a/b 退出 -> c/d 各接管一半 -> 并入 e）：
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
echo "## 三日拆并接续与用水来源审计"
"$PY" -m irrigation.land_water_cli \
  --source "$HERE/../../fixtures/land-input.json" \
  --output "$WORK/land-report.json"
"$PY" - "$WORK/land-report.json" <<'PYEOF'
import json, sys
from fractions import Fraction as F
r = json.load(open(sys.argv[1], encoding="utf-8"))
print("交接边:")
for t in r["land_transfers"]:
    print(" ", t["date"], t["from_field_id"], "->", t["to_field_id"],
          t["area_m2"], "m²", t["water_litres"], "L")
print("来源审计:")
for row in r["audit_sources"]:
    ident = row["identity"]
    print(" ", ident["date"], ident["field_id"], ident["kind"],
          "进水", row["water_litres"], "L")
    for s in row["sinks"]:
        i = s["identity"]
        print("    ->", i["date"], i["field_id"], i["kind"], s["water_litres"], "L")
print("终点审计:")
for row in r["audit_sinks"]:
    ident = row["identity"]
    parts = ", ".join(f"{s['identity']['field_id']}={s['water_litres']}L"
                      for s in row["sources"])
    print(" ", ident["date"], ident["field_id"], ident["kind"],
          row["water_litres"], "L <= {", parts, "}")
b = r["system_balance"]
incoming = sum(F(b[k]) for k in ("initial_litres", "rain_litres", "irrigation_litres"))
outgoing = sum(F(b[k]) for k in ("delivery_loss_litres", "runoff_litres",
                                 "drainage_litres", "et_litres", "storage_litres"))
print("系统余额:", b["residual_litres"], " 独立复核:", incoming - outgoing)
PYEOF

echo
echo "报告保留在: $WORK"
