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

## 契约与边界

详见[数据契约](docs/contract.md)。水深使用Fraction精确计算；建议仅供人工安排，不自动控制泵或预测作物。

样例均为本仓原创合成数据，来源见[说明](fixtures/SOURCES.md)，代码/样例MIT许可。业务演示和测试不访问网络，不需要容器或外部服务。
