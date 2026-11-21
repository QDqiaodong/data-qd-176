"""soil-water-rain-audit 命令行：雨量证据补核与供水建议签发。

用法：
    soil-water-rain-audit --source INPUT --output REPORT

沿用既有严格 JSON 读取（重复键、NaN/Infinity 一律拒绝）与“全部计算完成后
原子新建报告”的约定：目标已存在时失败并保留原文件。成功退出 0（包括证据
互相矛盾、status="inconsistent" 的情形）；输入或文件错误退出 2 并给出可读
错误。本命令不重派已发生的灌溉，也不下发任何设备指令。
"""
import argparse

from .io import load, publish
from .rain_audit import run


def main(argv=None):
    parser = argparse.ArgumentParser(description="雨量证据补核与供水建议签发")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = run(load(args.source))
        # 完成全部计算后再原子新建；目标已存在时 publish 失败，退出 2 并保留原目标。
        publish(args.output, result)
        if result["status"] == "consistent":
            released = sum(
                1 for row in result["next_advice"]
                if row["released_gross_mm"] is not None)
            print(
                f"status=consistent histories={result['history_count']} "
                f"released={released}/{len(result['next_advice'])} "
                f"follow-up={result['follow_up']['date'] if result['follow_up'] else 'none'}"
            )
        else:
            print("status=inconsistent histories=0")
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"soil-water-rain-audit: {exc}\n")


if __name__ == "__main__":
    main()
