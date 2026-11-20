# 实际用水计量台账与结算更正 v1（irrigation.meter_ledger）

本模块只处理**已经发生的用水**：合作社拿到的是各田块逻辑水表的累计读数，读数会迟到、
改版，换表当天重新起算。模块据此核算实际供水与真实田间水账，并对已发出的结算单做
可追溯的正负差额更正。它不选择未来供水、不操作任何设备，也不修改
`irrigation.service.run`、`irrigation.planner.plan`、`soil-water`、`soil-water-plan`
的任何既有接口与行为。

持久化完全落在本地 SQLite 文件中（标准库即可运行）：进程结束后重开同一文件继续工作，
两个录入进程通过 `BEGIN IMMEDIATE` 写事务串行化。每个 apply/close 都是一个原子业务
提交，`generation` 全生命周期单调递增、绝不重用。命令行为 `soil-water-ledger`。

## 1. 配置与初始化 `create(db_path, config)`

config 含 `fields`、`weather`、`meters` 三个键，**不含** `applications` 或 `planning`。

- fields/weather 完全沿用原核算的字段、范围、连续日期与校验口径（1..200 田块、
  1..3660 个严格连续升序气象日）。
- meters 每个田块恰好一条：`field_id`、非负整数 `opening_litres`（首个气象日前一日的
  累计读数）、非负整数 `price_microunits_per_litre`（每升收费微单位）。不得缺失、重复
  或引用未知田块。每个田块只有一只当前逻辑水表，换实体表不增加田块身份，用读数中的
  重置读数表示。
- 配置创建后固定，没有任何配置修订接口。
- `create` 只创建不存在的数据库：路径已存在报 `OSError`，绝不覆盖；无效配置在触碰
  文件前失败；初始化中途失败会删除本调用刚创建的文件，不留可正常打开的半初始化台账。
  初始 `generation=0`，没有读数与结算。

通用输入口径：日期必须规范 `YYYY-MM-DD`；所有整数按 `type is int` 校验，**bool 一律
拒绝**；字符串身份为非空字符串并保留原值，不做 strip、大小写或 Unicode 归并；输入
对象不会被修改。结构、类型、范围、引用、业务冲突抛 `ValueError`，文件访问错误抛
`OSError`；其他应用的数据库文件或不支持的 schema 版本明确失败，绝不自动重建。

## 2. 读数录入 `apply(db_path, request)`

request 含 `request_id`、非负整数 `expected_generation`、非空 `readings` 数组。每行：
`field_id`、`date`、正整数 `revision`、非负整数 `closing_litres`，以及**必须出现**的
`reset_opening_litres`（值为 `null` 或非负整数）。一个请求内同一
(field_id, date) 只允许一行。

- 每个 (field_id, date, revision) 是一个**完整读数版本**：包含累计期末数与当日是否
  换表。新版本里的 `null` 就是“当日未换表”，不能当作沿用旧版本的换表标志。
- 全部已接受版本永久保留；当前版本始终取已接受的最大 revision，到达顺序没有业务
  优先级。同身份同 revision 且业务内容完全相同是证据重放；内容不同则**整个请求**
  冲突回滚。迟到的较低 revision 若此前未存则归档，但不替换当前版本，也不产生供水或
  收费变化。
- 请求中的读数先整体并入候选状态再判断结果，绝不因数组顺序出现临时负用水而拒绝本来
  整体合法的批次；候选状态任何可算差分为负，整批失败（含本次修订间接改变的次日）。

### 毛供水日差分

- 换表日（`reset_opening_litres` 非 null）：当日升数 =
  `closing_litres - reset_opening_litres`，独立成立，不补计旧表表前水量（调用者已把
  该部分结清在前一日）。
- 普通日：当日升数 = 当日 `closing_litres` − 前一气象日当前 `closing_litres`；首日
  前值取配置 `opening_litres`。换表次日仍使用换表当天的 closing。
- 没有读数、或普通日缺前一日读数时毛供水**未知**：不能记零、不能按间隔均摊、不能用
  规划建议补齐。后面的换表日即使前面有缺口，仍能独立得出当日计量升数。
- 计量升数 ÷ `area_m2` 得到实际 `gross_mm`，允许精确分数（Fraction）；旧接口的整数
  供水校验不放宽。

### 真实田间水账

各田块从配置 `opening_mm` 开始，严格按现有 `balance.step` 的雨水、输水效率、入渗、
径流、渗漏、ET 与建议顺序计算；`gross_mm` 不向上取整，未满足 ET 不变成次日负储水。
一个日期之前若存在未知毛供水，该田块**从缺口当天起**的水账全部待补，包括后面已有
独立计量的换表日；换表不是土壤储水重置。补齐缺口或更正旧读数后，所有受影响后续日
按当前完整事实重新核算，结果与一次性重算完全一致。

### generation 与回执

新的合法 apply 成功时 `generation` 恰好加一——即使它只归档旧 revision 或只重放已存
版本；失败不加一。回执含 `schema_version=1`、`operation="apply"`、`request_id`、
生成后的 generation、`accepted_versions`（仅本次首次归档的版本身份）、
`current_changes`（仅本次确实切换当前版本的田块日；量值没变也算切换但不产生用水
差额）、本次 `adjustments`。前两个数组按 field_id、date、revision 排序，差额按
date、field_id 排序。

## 3. 结算 `close(db_path, request)`

request 含 `request_id`、`expected_generation`、`through_date`。首次结算覆盖首个气象日
至 through_date；之后只结算上次结算末日的次日至 through_date，严格前进且不超出气象
窗口，不允许重叠、跳过日期或单独重开旧区间。区间内每个田块每天的计量升数必须已知，
否则整次结算失败。结算针对毛供水收费，不因 ET 是否满足或输水损失折价。

成功 close 生成**不可变**结算单且 generation 加一。单据含稳定 `statement_id`、
生成后的 generation、`from_date`、`through_date`、按 date、field_id 排序的 `lines`
（field_id、date、gross_litres、price_microunits_per_litre、amount_microunits；
0 升和 0 单价仍保留明细）与 `total_amount_microunits`。结算不修改计量事实或水账，
也不把结算日当作物理计算截断点。

## 4. 更正差额 adjustment

已结算日期的当前毛供水日后可以因合法读数更正而变化。每次 apply 为最终状态中**每一个
用水升数改变的已结算田块日**追加恰好一条 adjustment：

- `previous_gross_litres` 是**财务已确认值**＝原结算明细升数＋该日此前全部 adjustment
  的累计效果，不是永远拿新值减最初账单；
- `delta_litres = 当前实际升数 − 财务已确认值`，
  `delta_amount_microunits = delta_litres × 单价`，差额可为负；
- 零差额、未结算日期不生成记录；0 单价但升数非零的更正仍生成金额为 0 的记录；
- 一条旧读数修订可能同时改变当天与次日，两天分别判断是否已结算、分别出记录。

每条记录含稳定 `adjustment_id`、`request_id`、生成后的 generation、原
`statement_id`、field_id、date、previous/current 升数与两个差额。原结算单的明细与
总额逐值不变，旧差额不能删除或合并。任意成功写入后，每个已结算田块日恒满足：

> 原结算升数 ＋ 历次升数差额 ＝ 当前实际升数（金额同理）。

## 5. 请求幂等、重试与并发

- `request_id` 在台账所有写操作中唯一，其判断**优先于** generation 冲突判断。
- 已成功的相同 request_id、相同操作、相同业务内容，即使当前 generation 已前进或请求
  里的 expected_generation 过时，也返回第一次保存的完整回执，不重新计算、不再加
  generation。真正重试应带原来的 expected_generation。
- 同 request_id 的不同操作或不同内容一律失败，即使表面上是 no-op。比较内容时忽略
  JSON 键顺序与 readings 数组顺序；其余字段（含 expected_generation）都属于内容。
- 未使用过的 request_id 只有 expected_generation 等于当前 generation 才能提交；失败
  请求不占用 request_id。并发相同请求只记账一次；不同请求带相同旧 generation 至多一个
  提交，其余明确报 generation 冲突；这两类冲突与证据版本冲突分别报错，不混淆。
- 每个写请求在单个事务内提交回执、读数版本与当前版本、当前用水与水账可见结果、结算单
  与差额记录；进程异常终止或竞争时不会出现读数已改而差额丢失、结算只覆盖部分田块、
  重复差额或重用 generation。测试通过在真实提交中断点杀死写进程验证：可观察结果只能是
  完整提交前或完整提交后，同一请求重试后仅保留一份效果。

## 6. 查询 `report(db_path)`

返回 `schema_version=1`、当前 `generation`、`daily`、`statements`、`adjustments`、
`financial_summary` 的只读一致快照，不改变 generation 或任何业务记录。

- daily 覆盖配置的**全部**田块/日期，按 field_id、date 排序。每行含 field_id、date、
  `current_revision`（无读数为 null）、`meter_status`（`ready`/`pending`）、
  `gross_litres`、`gross_mm`（未知为 null；已知为 Fraction 规范整数/分数字符串）、
  `balance_status`（`ready`/`pending`）、`water_balance`（待补为 null）。
- water_balance 包含现有 daily 的全部字段（含 field_id/date）；除
  `advised_next_gross_mm` 仍为整数外，其余所有 `*_mm` 字段统一输出 Fraction 规范
  字符串（含 rain_mm、gross_irrigation_mm），每个已完成行 `balance_residual_mm`
  必为 `"0"`。pending 行完整保留，不被当作已完成而忽略。
- statements 按结算 generation 排序；adjustments 按 generation、date、field_id 排序。
- financial_summary 含 `original_amount_microunits`（全部原结算金额）、
  `adjustment_amount_microunits`（全部差额金额，含正负）、
  `recognized_amount_microunits`（两者之和），不混入未结算日期费用。

## 7. 命令行 `soil-water-ledger`

```
soil-water-ledger create --db DB --config JSON
soil-water-ledger apply  --db DB --source JSON
soil-water-ledger close  --db DB --source JSON
soil-water-ledger report  --db DB --output JSON
```

create 成功输出 `{"schema_version": 1, "generation": 0}`；apply/close 成功输出完整回执；
report 沿用严格 JSON 读取约定（拒绝重复键、NaN/Infinity、非对象根），完成全部计算后
原子新建报告，目标已存在时保留原文件。成功退出 0；输入、业务冲突、文件或数据库错误
退出 2 并给可读错误，失败不污染台账。提交后若回执输出失败，已提交结果不受影响，
调用者用同一 request_id 重试即可取回原回执。
