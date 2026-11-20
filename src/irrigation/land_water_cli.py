"""soil-water-land 命令行：田块拆并接续与用水来源审计。

用法：
    soil-water-land --source INPUT --output REPORT

沿用既有严格 JSON 读取（重复键、NaN/Infinity 一律拒绝）与“全部计算完成后
原子新建报告”的约定：目标已存在时失败并保留原文件。成功退出 0；输入或文件
错误退出 2 并给出可读错误。本命令不修改既有四个命令的任何行为。
"""
import argparse

from .io import load, publish
from .land_water import run


def main(argv=None):
    parser = argparse.ArgumentParser(description="田块拆并接续与用水来源审计")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = run(load(args.source))
        # 完成全部计算后再原子新建；目标已存在时 publish 失败，退出 2 并保留原目标。
        publish(args.output, result)
        print(
            f"field-days={len(result['daily'])} "
            f"land-transfers={len(result['land_transfers'])} "
            f"audit-sources={len(result['audit_sources'])} "
            f"audit-sinks={len(result['audit_sinks'])}"
        )
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"soil-water-land: {exc}\n")


if __name__ == "__main__":
    main()
