"""soil-water-ledger 命令行：实际用水计量台账与结算更正。

子命令：
  create --db DB --config JSON
  apply  --db DB --source JSON
  close  --db DB --source JSON
  report --db DB --output JSON

成功退出 0；输入、业务冲突、文件或数据库错误退出 2 并给出可读错误，
失败不污染台账。报告沿用严格 JSON 读取约定，计算完成后原子新建，
目标已存在时保留原文件。
"""
import argparse
import json
import sqlite3
import sys

from . import meter_ledger
from .io import load, publish


def _print_json(payload):
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )


def _fail(parser, exc):
    parser.exit(2, f"soil-water-ledger: {exc}\n")


def _add_db(p):
    p.add_argument("--db", required=True)


def build_parser():
    parser = argparse.ArgumentParser(description="实际用水计量台账与结算更正")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="初始化新台账")
    _add_db(p_create)
    p_create.add_argument("--config", required=True)

    p_apply = sub.add_parser("apply", help="录入/重放读数批次")
    _add_db(p_apply)
    p_apply.add_argument("--source", required=True)

    p_close = sub.add_parser("close", help="区间结算")
    _add_db(p_close)
    p_close.add_argument("--source", required=True)

    p_report = sub.add_parser("report", help="输出只读快照报告")
    _add_db(p_report)
    p_report.add_argument("--output", required=True)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            result = meter_ledger.create(args.db, load(args.config))
            _print_json(result)
        elif args.command == "apply":
            result = meter_ledger.apply(args.db, load(args.source))
            _print_json(result)
        elif args.command == "close":
            result = meter_ledger.close(args.db, load(args.source))
            _print_json(result)
        else:  # report
            # 全部计算完成后才原子新建；目标已存在时 publish 失败并保留原文件。
            result = meter_ledger.report(args.db)
            publish(args.output, result)
    except (OSError, ValueError, KeyError, TypeError, OverflowError,
            sqlite3.Error) as exc:
        _fail(parser, exc)


if __name__ == "__main__":
    main()
