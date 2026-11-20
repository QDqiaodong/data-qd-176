# 回用水盐分核算与水质拦截 v1（irrigation.reuse_water）

合作社把田间径流与渗漏收回蓄水池再次灌溉；回流水可能带回土壤中的盐，池面蒸发也会
让池水变咸。本模块只核对**给定的实测操作**：逐池核对实际输水记录，指出哪些抽水因
水质超限被弃用、田块实际得到多少水、盐最终留在何处。它不选择供水计划、不推算收费、
不操作设备，也不读写计量台账；`irrigation.service.run`、`irrigation.planner.plan`、
`irrigation.meter_ledger` 与三个既有命令的全部契约保持不变。

公开接口为 `irrigation.reuse_water.run(data)`，命令为
`soil-water-reuse --source INPUT --output REPORT`，二者共用同一业务逻辑，仅用
Python >=3.11 标准库。全部计算使用 `fractions.Fraction` 精确有理数，不使用浮点或
容差；输入对象不会被修改。命令沿用既有严格 JSON 读取（重复键、`NaN`/`Infinity`
一律拒绝）与“全部计算完成后原子新建报告”的约定：成功退出 0；输入或文件错误退出 2
并给出可读错误，目标已存在时保留原文件、绝不覆盖。

## 1. 输入结构

输入根对象恰好包含 `fields`、`weather`、`tanks`、`field_reuse`、`operations` 五个键，
**不接收** `applications`、`planning`，任何未声明字段（含拼写错误）一律拒绝。

- `fields`/`weather` 完全沿用旧水账的字段、校验与规模上限（1..200 个田块、
  1..3660 个严格连续升序气象日；日期必须规范 `YYYY-MM-DD`），其额外字段仍按旧接口
  约定处理。
- `tanks`：1..12 条，每条恰好含 `id`、`capacity_litres`、`opening_litres`、
  `opening_salt_mg`。容量为正整数；初始水量与盐量为非负整数，初始水量不得超过容量。
  池水量为 0 时允许保留非零干盐。
- `field_reuse`：每个田块恰好一条，恰好含 `field_id`、`opening_salt_mg`、
  `max_source_mg_per_litre`、`return_permille`、`return_tank_id`。初始土壤盐量与
  水质限值为非负整数；回收比例为 0..1000 的整数（‰）；回流池必须存在，即使回收比例
  为 0。土壤初始水量仍为 `opening_mm × area_m2`（升），盐量为整个田块的毫克数。
- `operations`：每个气象日恰好一条，含 `date` 与以下四个必填数组，数组均可为空、
  可无序；数组缺行表示该操作为零。
  - `inflows`（外来补水）：每条 `tank_id`、`water_litres`、`salt_mg`，每池当日最多
    一条；水量、盐量为非负整数，水量为 0 时盐量必须为 0。
  - `evaporations`（池面蒸发）：每条 `tank_id`、`water_litres`，每池当日最多一条，
    水量为非负整数。
  - `transfers`（池间输水）：每条 `from_tank_id`、`to_tank_id`、`water_litres`；
    不允许自环；同一有向池对当日最多一条；水量为正整数；反向记录允许同时存在。
  - `withdrawals`（到田口抽水）：每条 `field_id`、`tank_id`、`gross_litres`，每个
    田块当日最多一条，毛水量为非负整数。

通用口径：所有引用必须存在；日期不得重复、缺失或超出 `weather`；字符串身份为非空
字符串并**保留原值**，不做 strip、大小写或 Unicode 归并；所有整数按 `type is int`
校验，**bool 一律拒绝**。缺必填字段、类型/范围/引用错误及下文的物理不可执行情况均
抛 `ValueError`，文件错误可抛 `OSError`。

## 2. 每日业务时点

各池严格按下列时点核算，当日状态成为次日期初：

1. **接收回流**：各池先接收前一天已排定的回流及其盐（首日只接收初始存量）。回流在
   产生当天不能被任何池或田块使用。
2. **池面蒸发**：扣除当天指定蒸发。蒸发只移走纯水，盐全部留在池中；蒸发量不能超过
   该时点已有水量，当天外来补水或同日池间输水不能替它补足，否则整批输入失败。
3. **外来补水**：接收当天 `inflows` 的水与盐。
4. **同日完全混合期**：当天所有 `transfers` 属于同一个混合期，所有池同时混匀。
   池 i 的混合水量 = 蒸发后水量 + 外来水 + 全部当日入池输水；混合盐量 = 池内留存盐
   + 外来盐 + 各入池输水携带的盐。每条输水携带的盐 = 其水量 × 来源池在本混合期的
   浓度。存在环路时各池浓度由线性方程组联合求解，**不能按数组顺序抽走、把环路断开
   或假设循环水为淡水**。
5. **底水可达性校验**：当日正水量输水图中，每个参与正水量输水的池都必须能沿正向
   路径从某个有正水量**底水**的池到达；底水 = 该池蒸发后剩水 + 当日外来水（含今晨
   已到账回流），底水池自身也算可达。不满足时整个输入失败，即使各池表面进出平衡
   （用于排除“没有水源却凭循环边记录造出水”的伪循环）；有真实底水支撑的环路必须
   正确核算，不把所有环路都当错误。
6. **出水量约束**：每池当日出池输水加 `withdrawals` 的总水量不得超过其混合水量。
   这是日尺度模型，不要求提供同日逐时调度。
7. **抽水与水质拦截**：抽水已从来源池抽出。`gross_litres > 0` 且来源浓度**严格大于**
   该田块 `max_source_mg_per_litre` 时，整笔抽水被拦截，水和盐全部作为 `rejected`
   排出系统，田块当天按零灌溉计算；浓度等于限值时允许入田。不允许部分稀释、改抽
   其他池或缩减给定抽水。零升记录始终接受；来源无混合水时浓度输出 `null`。
8. **出水后溢流**：完成输水及抽水后，池中剩水超过 `capacity_litres` 的部分溢流，
   按当日相同浓度带走盐，其余水盐成为次日期初。容量约束发生在出水之后：不能先溢流
   再抽水，也不能把被拦截的抽水退回池中。输水无沿途损失，池面不直接接收 `weather`
   中的降雨。

没有混合水且不参与正量输水的池浓度为 `null`，干盐保留；下次有水时全部干盐重新参与
溶解。不考虑盐析、化学反应或盐浓度的物理饱和上限。

## 3. 田间盐迁移与回流

被接受的抽水以 `gross_litres / area_m2` 得到毛水深（允许分数），逐日水账严格复用
现有 `balance.step` 的输水效率、雨水入渗、径流、容量渗漏、ET、跨日储水与建议顺序；
旧接口的整数供水约束不放宽。盐按下列约定随水迁移：

- **输水损失**：带走与损失水量成比例的盐；输水损失的水不回收。
- **雨水稀释先于径流**：到田水与无盐降雨先混匀，径流从这份混合水中按水量比例带走
  盐，剩余水盐入渗。
- **土壤混合先于渗漏**：入渗水盐与田块期初土壤水盐完全混匀，容量渗漏按此时的浓度
  带走盐；随后 ET 只带走纯水。
- **干涸保留**：土壤干涸时剩盐保留，之后重新遇水即全部参与混合；不能因当日没有水
  就丢掉盐。
- **回流排定**：当日径流与渗漏的水量、盐量分别乘 `return_permille/1000`，排定为
  次日一早流入 `return_tank_id`；余下部分是未回收排水，离开系统。水质拦截的水不
  回收。最后一天产生的回流保留为窗口外 `pending_returns`，不能丢弃或提前计入末日
  池水。

水深转升严格乘 `area_m2`；盐量单位始终为 mg，浓度为 mg/L。零水量混合时不做除零，
既有干盐继续保留。接受/拒绝的水质判断使用毛抽水的**来源池混合浓度**，不用 ET 后
土壤浓度替代。未记录抽水的田块也必须生成每日水账。

## 4. 报告（schema_version = 1）

业务排序与输入排列无关。除 `schema_version`、身份日期、`accepted`、
`water_balance.advised_next_gross_mm` 外，所有数量统一使用 Fraction 规范整数/分数
字符串（如 `"100/7"`，绝不出现小数点或指数）；浓度按规则可为 `null`。
`water_balance` 中所有 `*_mm` 字段同为字符串口径。

- `tank_daily`：每个日期、每池一行，按 `date`、`tank_id` 排序。含
  `date`、`tank_id`、`opening_litres`、`opening_salt_mg`、`return_litres`、
  `return_salt_mg`、`evaporation_litres`、`external_litres`、`external_salt_mg`、
  `transfer_in_litres`、`transfer_in_salt_mg`、`mixed_litres`、
  `concentration_mg_per_litre`、`transfer_out_litres`、`transfer_out_salt_mg`、
  `withdrawn_litres`、`withdrawn_salt_mg`、`spill_litres`、`spill_salt_mg`、
  `closing_litres`、`closing_salt_mg`、`water_residual_litres`、
  `salt_residual_mg`。`opening` 是昨日池末值（不含今晨回流），`mixed` 是同日混合期
  的量。水残差 = opening+return+external+transfer_in−evaporation−transfer_out−
  withdrawn−spill−closing；盐残差同式去掉蒸发项。
- `deliveries`：仅覆盖输入 `withdrawals`，按 `date`、`field_id` 排序。含
  `date`、`field_id`、`tank_id`、`requested_litres`、`source_mg_per_litre`、
  `accepted`（布尔）、`applied_litres`、`applied_salt_mg`、`rejected_litres`、
  `rejected_salt_mg`。`applied` 是获准抽水的毛量（尚未扣输水损失）；拒绝时 applied
  为零，接受时 rejected 为零。
- `field_daily`：覆盖全部田块/气象日，按 `field_id`、`date` 排序。含 `field_id`、
  `date`、`water_balance`（现有 daily 的全部字段及身份）、`opening_salt_mg`、
  `irrigation_salt_mg`（获准毛供水携带的盐）、`delivery_loss_salt_mg`、
  `runoff_salt_mg`、`drainage_salt_mg`、`closing_salt_mg`、`recovered_litres`、
  `recovered_salt_mg`、`discharged_litres`、`discharged_salt_mg`、
  `salt_residual_mg`。田间盐残差 = opening+irrigation−delivery_loss−runoff−
  drainage−closing。
- `pending_returns`：仅列最后一日产生的非零水量或非零盐量回流，按 `tank_id` 排序、
  同池汇总；每行 `date` 为末日次日，另含 `tank_id`、`water_litres`、`salt_mg`。
- `system_balance`：含 `water_litres` 与 `salt_mg` 两个对象，各有 `opening`、
  `external`、`closing`、`pending_returns`、`spill`、`rejected`、`delivery_loss`、
  `discharged`、`residual`；`water_litres` 另含 `rain`、`evaporation`、`actual_et`。
  `opening`/`closing` 是全系统池与土壤存量之和；`external` 仅外来补水；
  `discharged` 仅未回收田间排水；全部回流与池间输水属于内部转移。残差 = 期初 +
  外部输入 − 期末及全部外流，必须精确为 `"0"`。未满足 ET、供水建议不能记成实际
  外流。

每行水盐残差及全系统残差都必须精确为 `"0"`，不得四舍五入或用浮点容差判限值。

## 5. 边界

本模块是对给定记录的离线核算，不做调度优化、不预测未来来水或水质、不模拟盐析/化学
反应/饱和上限，也不与计量台账或收费共享任何读写。池面不接收降雨，降雨只进入田块。
