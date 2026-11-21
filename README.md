# data-qd-176 · 灌溉土壤水量平衡

从田块、连续气象日和实际供水，生成水量守恒日账、缺水量和次日毛供水建议。

## 独立安装与测试

Python >=3.11，本批实测3.12.14。运行与unittest仅用标准库，构建工具精确固定。每仓独立venv，可复用公共下载缓存。

```sh
uv venv --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
```

以上离线命令需要缓存已有setuptools==80.9.0和wheel==0.45.1；无缓存时先准备这两个公共构建工具，不声称空缓存可离线安装。修改源码后重装本仓。

## 端到端演示

在仓库根目录执行，输出路径必须不存在。重复演示请换新路径。

```sh
.venv/bin/soil-water --source fixtures/input.json --output run/report.json
```

预期：3个田块日、毛供水25000升、缺水日0；期末储水依次30、10、50mm。

## 多日供水计划（共用蓄水池）

`soil-water-plan` 在有限蓄水、逐日来水和泵量下，为未来 1..7 天、1..3 个田块精确选择离散毛供水，输出完整田间水账与蓄水池水账；计划仅供人工审核，不操作设备。

```sh
.venv/bin/soil-water-plan --source fixtures/plan-input.json --output run/plan-report.json
```

样例 `fixtures/plan-input.json` 为两天无雨、池内仅 5 升且第二天泵限为 0 的跨日算例：最优计划只在第一天给田块 b 供水 5mm（而不是权重更大的 a），加权未满足 ET 为 "8"、期末目标缺水为 "0"、毛供水 5 升。API 形式为 `irrigation.planner.plan(data)`，与 CLI 共用同一业务逻辑。合法但无可行组合时报告 status="infeasible" 并退出 0；输入无效或目标已存在时退出 2 并保留原目标。

输入或业务规则无效、输出已存在时退出2，保留原目标；成功退出0。报告/数据库先完整生成再原子新建，无覆盖。run/为忽略目录。

## 实际用水计量台账与结算更正（soil-water-ledger）

`irrigation.meter_ledger` 只处理**已经发生的用水**：把各田块逻辑水表迟到、改版、换表
重起算的累计读数版本，持久化到本地 SQLite（仅标准库），核算实际毛供水、真实田间水账，
并在历史读数更正时以可追溯的正负差额追加 adjustment，绝不改写已发出的结算单，也不会
因重试重复收费。不选择未来供水、不操作设备；`run`、`plan` 与两个既有命令的接口行为
完全不变。

```sh
PYTHONPATH=src python3 examples/ledger_worked_example.py     # API 算例（含断言）
PYTHONPATH=src sh examples/ledger-cli/00-run.sh              # 等价 CLI 演示
```

四个子命令：`create --db DB --config JSON`、`apply --db DB --source JSON`、
`close --db DB --source JSON`、`report --db DB --output JSON`。每个 apply/close 是
一个原子业务提交，`generation` 单调递增；`request_id` 幂等且其判断优先于 generation
冲突，精确重试返回第一次的完整回执。完整口径见[计量台账契约](docs/ledger-contract.md)。

## 回用水盐分核算与水质拦截（soil-water-reuse）

`irrigation.reuse_water` 核对合作社把田间径流、渗漏收回蓄水池再灌溉的**给定实测
操作**：按日尺度完全混合模型逐池核算蒸发、外来补水、同日池间循环输水（含环路浓度
联合求解）、抽水与溢流，逐田块核算水质拦截、输水损失携盐、雨水稀释、径流/渗漏携盐
与跨日回流，输出精确有理数的水盐残差。它不选择供水计划、不推算收费、不操作设备，
也不读写计量台账；`run`、`plan`、`meter_ledger` 与三个既有命令的接口行为完全不变。

```sh
PYTHONPATH=src python3 examples/reuse_worked_example.py     # API 两日算例（含断言）
PYTHONPATH=src sh examples/reuse-cli/00-run.sh              # 等价 CLI 演示（含循环混合）
```

或直接运行命令（输出路径必须不存在，重复演示请换新路径）：

```sh
PYTHONPATH=src python3 -m irrigation.reuse_cli \
  --source fixtures/reuse-input.json --output run/reuse-report.json
```

样例 `fixtures/reuse-input.json` 是契约规定的两日可复核算例：首日 5L 淡水获准到田，
田间渗漏携盐 `100/7` mg 并于次日早全量回流；池水次日浓度升至 `10/7` mg/L，超过
1 mg/L 限值，1L 抽水被整笔拦截（携盐 `10/7` mg），田块零灌溉。全系统期初水 12L、
盐 20mg，期末水 10L、盐 `130/7` mg，加拒水 1L/盐 `10/7` mg 与 ET 1L，水盐残差均
精确为 `"0"`，末日无窗口外回流。API 形式为 `irrigation.reuse_water.run(data)`。
完整口径见[回用水契约](docs/reuse-contract.md)。

## 田块拆并接续与用水来源审计（soil-water-land）

`irrigation.land_water` 处理合作社季中拆分、合并田块的接续核算：旧田块退出后
土壤水留在原地，新田块按接管土地面积比例接续其期末储水与全部来源份额。日水账
逐字段复用现有 `balance.step` 口径；在均匀混合模型下逐笔追踪每一股历史来水
（首日土壤存量、每日降雨、每日毛供水）最终流向哪些终点（输水损耗、径流、渗漏、
实际蒸散、末日结存），支持双向审计：某笔历史进水去了哪里，某项损耗由哪些历史
进水构成。它不选择供水计划、不操作设备，也不读写计量台账或回用水台账；`run`、
`plan`、`meter_ledger`、`reuse_water` 与四个既有命令的接口行为完全不变。

```sh
PYTHONPATH=src python3 examples/land_worked_example.py     # API 三日算例（含独立断言）
PYTHONPATH=src sh examples/land-cli/00-run.sh              # 等价 CLI 演示
```

或直接运行命令（输出路径必须不存在，重复演示请换新路径）：

```sh
PYTHONPATH=src python3 -m irrigation.land_water_cli \
  --source fixtures/land-input.json --output run/land-report.json
```

样例 `fixtures/land-input.json` 是契约规定的三个连续气象日算例：首日 a、b 各
2m²（a 期初 4mm、b 灌溉 4mm，各结存 8L）；次日 a、b 退出，c、d 各接管两半土地，
c 因容量 3mm 渗漏 2L（两笔来源各 1L）、ET 各 2L；第三日 c、d 并入 e，末存 10L
中 a 的初始水与 b 的灌溉水各 5L，系统余额精确为 `"0"`。换成 c 独占 a、d 独占 b
后末存来源变为 a=4L、b=6L；去掉灌溉且次日 ET=3mm 时，半接管 c 缺水 1mm、独占
c 渗漏 1mm 且缺水 0。完整口径见[拆并接续契约](docs/land-contract.md)。

## 雨量证据补核与供水建议签发（soil-water-rain-audit）

`irrigation.rain_audit` 核对月底田间水账时**只留下几个候选值的自动雨量记录**：
在完全保留现有日水账口径（输水效率、入渗上限、径流、容量渗漏、实际 ET、缺水与
建议顺序）的前提下，枚举全部完整可能历史（逐日雨量序列），使每条分段雨量合计
成立、逐田块延续真实上日结存并满足全部日终探头界限，再判断下一日的田间供水建议
能否签发。证据互相矛盾时报告 status="inconsistent"。需要补查时给出一份**单日**
追查单：先查哪个日期、查到不同数值后哪些田块可以签发，只复核不重派已发生的
灌溉，也不下发设备指令。

```sh
PYTHONPATH=src python3 examples/rain_audit_worked_example.py  # API 算例（含独立断言）
PYTHONPATH=src sh examples/rain-audit-cli/00-run.sh            # 等价 CLI 演示
```

或直接运行命令（输出路径必须不存在，重复演示请换新路径）：

```sh
PYTHONPATH=src python3 -m irrigation.rain_audit_cli \
  --source fixtures/rain-audit-input.json --output run/rain-audit-report.json
```

样例 `fixtures/rain-audit-input.json` 是契约强制的三日算例：1m² 田块期初 0mm、
容量 4mm、入渗上限 10mm、效率 1000‰、阈值 3mm、目标 4mm，不灌溉；三天 ET 依次
2、0、0mm，候选雨量依次 [0,4]、[0,4]、[0,2]，仅前两日合计 4mm。共 4 条可能
历史；第一天未满足 ET 范围 0..2mm，最后一天结存 2..4mm、渗漏 0..2mm，建议集合
[0,2] 暂不签发；追查单指向第一天，查到 0mm 时签发 0，查到 4mm 时建议仍为
[0,2]。补入“第二天结存恰为 4mm”的探头后只剩 2 条历史，签发 0，不再开追查单。
API 形式为 `irrigation.rain_audit.run(data)`。完整口径见[雨量补核契约]
(docs/rain-audit-contract.md)。

## 契约与边界

详见[数据契约](docs/contract.md)。水深使用Fraction精确计算；建议仅供人工安排，不自动控制泵或预测作物。

样例均为本仓原创合成数据，来源见[说明](fixtures/SOURCES.md)，代码/样例MIT许可。业务演示和测试不访问网络，不需要容器或外部服务。
