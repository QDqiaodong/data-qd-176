# 每日根层水账 v1

使用者为田间灌溉记录整理人员。fields 提供唯一 id、正整数 area_m2/capacity_mm/infiltration_mm、初始 opening_mm、需水触发 threshold_mm、补至 target_mm、1..1000 的 efficiency_permille。0≤threshold≤target≤capacity，opening 不得超容量。毫米为水深，mm×m²=升。weather 为1..3660条严格逐日连续、按日期升序的雨量和参考作物耗水；日期必须 YYYY-MM-DD。applications 是唯一 (field_id,date) 的非负整数 gross_mm，未给定等于0，未知田块/日期失败。

每日约定：供水先乘效率，输水损失单独计；雨量与有效灌溉混合后受当日 infiltration_mm 上限限制，多余为地表径流；加入昨日期末储水，超过capacity部分为深层渗漏；再按et_mm消耗，无法满足的部分计unmet_et_mm，禁止负储水。不把缺水留成次日负债。期末低于threshold时建议 ceil((target-closing)/efficiency) 的次日毛供水，仅提示、不执行、不预知次日降雨。与每日同源水账共用状态，不是独立算法模式。

Fraction保持精确有理数，水深输出整数/分数字符串；每行完整水量守恒残差为0。CLI验证和计算完成后新建报告，目标已存在（包括符号链接）则退出2且保留原文件。单次离线批处理，不提供多年气象预测、地下水、共泵调度或真实作物模型；ET是调用者给定值。
