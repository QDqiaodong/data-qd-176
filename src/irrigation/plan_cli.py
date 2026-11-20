import argparse
from .io import load, publish
from .planner import plan


def plan_main():
    parser = argparse.ArgumentParser(description="共用蓄水池的多日供水计划")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = plan(load(args.source))
        # 完成全部计算后再原子新建报告；目标已存在时 publish 失败，退出 2 并保留原目标。
        publish(args.output, result)
        print(result["status"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"soil-water-plan: {exc}\n")


if __name__ == "__main__":
    plan_main()
