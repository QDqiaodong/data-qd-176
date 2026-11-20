# 每日根层水账 v1

使用者为田间灌溉记录整理人员。fields 提供唯一 id、正整数 area_m2/capacity_mm/infiltration_mm、初始 opening_mm、需水触发 threshold_mm、补至 target_mm、1..1000 的 efficiency_permille。0≤threshold≤target≤capacity，opening 不得超容量。毫米为水深，mm×m²=升。weather 为1..3660条严格逐日连续、按日期升序的雨量和参考作物耗水；日期必须 YYYY-MM-DD。applications 是唯一 (field_id,date) 的非负整数 gross_mm，未给定等于0，未知田块/日期失败。

每日约定：供水先乘效率，输水损失单独计；雨量与有效灌溉混合后受当日 infiltration_mm 上限限制，多余为地表径流；加入昨日期末储水，超过capacity部分为深层渗漏；再按et_mm消耗，无法满足的部分计unmet_et_mm，禁止负储水。不把缺水留成次日负债。期末低于threshold时建议 ceil((target-closing)/efficiency) 的次日毛供水，仅提示、不执行、不预知次日降雨。与每日同源水账共用状态，不是独立算法模式。

Fraction保持精确有理数，水深输出整数/分数字符串；每行完整水量守恒残差为0。CLI验证和计算完成后新建报告，目标已存在（包括符号链接）则退出2且保留原文件。单次离线批处理，不提供多年气象预测、地下水、共泵调度或真实作物模型；ET是调用者给定值。

# 共用蓄水池的多日供水计划 v1（planning）

在不改变上述 fields/weather/applications 字段与口径的前提下，输入可增加 planning 对象进入规划模式。`irrigation.planner.plan(data)` 与命令 `soil-water-plan --source INPUT --output REPORT` 共用同一业务逻辑：规划仅产出供人工审核的计划，不操作任何设备。规划模式支持 1..3 个田块、1..7 个严格连续气象日；不含 planning 的旧模式规模上限（1..200 田块、1..3660 日）与行为完全不变。所有整数仍按 `type is int` 校验，bool 一律拒绝；无隐式单位换算，mm×m²=升；日期和田块身份沿用原契约，weather 仍须升序连续输入。

planning 包含：

- reservoir：capacity_litres 为正整数；opening_litres 为非负整数且不超过容量。
- supply_days：每个气象日恰好一条，可无序输入但日期不得重复、缺失或超出 weather。每行含 date、非负整数 inflow_litres、pump_limit_litres、minimum_closing_litres；minimum_closing_litres 不得超过容量。
- field_rules：每个田块恰好一条，不得遗漏、重复或引用未知田块。每行含 field_id、正整数 stress_weight、gross_mm_choices。gross_mm_choices 是长度 1..3 的严格递增非负整数数组且首项必须为 0，表示该田块每个未锁定日期可选的离散毛供水深度（mm）。

applications 在规划中表示已承诺供水：出现的 (field_id,date) 必须原样执行，包括显式 gross_mm=0（locked=true）；未出现的组合才由计划在 gross_mm_choices 中选择（locked=false）。必须区分“缺记录”与“明确锁定为零”。锁定值不必属于 gross_mm_choices，仍按原有非负整数约束校验。输入对象不会被 plan 修改；输入数组（supply_days、field_rules、applications 等）的排列不影响业务结果。

## 蓄水池每日账（整数升）

每日严格按以下顺序：(1) 蓄水池先接收当日 inflow；(2) 超过 capacity 的部分记为 spill_litres，溢流水已经流走，当日抽水不能倒过来减少溢流；(3) 各田块毛供水按 gross_mm×area_m2 折升后全部从池中扣除，田块输水损失已包含在抽出的毛供水内，不得再次从池中扣款；(4) 当日总抽水量必须同时不超过 pump_limit_litres 和溢流后的 available_litres，扣除后的 closing_litres 不得低于 minimum_closing_litres。池中不能借入未来来水，最低蓄水是每日硬约束；当日 closing 成为次日 opening。降雨只进入田块，绝不进入蓄水池。reservoir_daily 按日输出 opening/inflow/spill/available/gross_used/closing/minimum_closing/pump_limit 各升数及 balance_residual_litres=opening+inflow-spill-gross_used-closing，残差恒为 0。

## 田间平衡与四级目标

选定每个组合的 gross_mm 后，各田块逐日严格复用第 1 节的水量平衡顺序、输水效率、入渗上限、容量、ET 短缺与跨日储水规则；建议字段 advised_next_gross_mm 只是提示，不作为必须执行量，未满足 ET 不得转为次日负储水。规划在整个窗口内精确求全局最优（按日动态规划，状态为池内存水与各田块期末储水；不使用浮点、容差、贪心或近似/超时成功），按下列目标依次比较，前一目标相等才比较下一目标：

1. 全窗口 Σ stress_weight×unmet_et_mm 最小。
2. 末日 Σ stress_weight×max(0, target_mm-closing_mm) 最小。
3. 全窗口抽取毛供水升数最小（锁定供水也计入）。
4. 仍相等时，把全部组合按日期升序、同日 field_id 按 Python 字符串升序排列，取 gross_mm 序列字典序最小者。

所有目标用 Fraction/整数精确比较，weighted_unmet_et_mm 与 weighted_terminal_deficit_mm 以 Fraction 规范整数/分数字符串输出，gross_supply_litres 为整数。

## 报告、不可行与失败行为

成功时报告 schema_version=1、status="optimal"，含：decisions（覆盖每个田块/日期组合，按日期、field_id 顺序，每行 field_id、date、整数 gross_mm、布尔 locked）；objective（上述三项目标）；reservoir_daily；water_balance（把 decisions 作为完整 applications 调用原核算得到的完整 daily 与 summary，期末储水、输水损失、径流、渗漏与 ET 与目标吻合，原有田间守恒残差仍为 0）。

结构与引用合法但不存在满足全部硬约束的组合时（例如来水不足以维持最低蓄水、锁定供水超当日泵限、锁定后低于最低蓄水），返回 schema_version=1、status="infeasible" 和非空中文 reason，CLI 仍退出 0，且不附带任何部分 decisions、objective、reservoir_daily 或 water_balance。负数、重复身份、气象断日、缺失 supply_days/field_rules、未知田块、规模超限等属于输入错误：API 抛异常，CLI 退出 2；目标已存在时同样退出 2 并保留原目标，报告只在全部计算完成后原子新建。
