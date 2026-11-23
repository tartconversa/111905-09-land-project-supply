"""命令行入口。

用法（脚本目录与来源资料目录分离，来源路径以参数传入，不进入资料目录执行）：

    python3 -m supply_warehouse.cli run --data-dir ./data --db ./output/supply.duckdb
    python3 -m supply_warehouse.cli ingest --data-dir ./data
    python3 -m supply_warehouse.cli publish
    python3 -m supply_warehouse.cli checks
    python3 -m supply_warehouse.cli query monthly
    python3 -m supply_warehouse.cli query permit PER1
    python3 -m supply_warehouse.cli query snapshot|balance|unexplained|calibers|consideration|releases
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from .ingest import connect, ingest
from .publish import list_releases, publish
from .checks import run_checks
from . import query as q

DEFAULT_DB = Path("output/supply.duckdb")


class _Encoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, cls=_Encoder, default=str))


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=DEFAULT_DB, help="DuckDB 文件路径")
    parser = argparse.ArgumentParser(description="土地开发权益到住房供应形成仓", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="增量接收来源资料", parents=[common])
    p_ingest.add_argument("--data-dir", type=Path, required=True)

    sub.add_parser("publish", parents=[common], help="建模、集成检查并发布新版本")
    sub.add_parser("checks", parents=[common], help="对当前发布版本运行集成检查")
    sub.add_parser("releases", parents=[common], help="列出全部已发版本")

    p_run = sub.add_parser("run", help="一键：接收 + 发布", parents=[common])
    p_run.add_argument("--data-dir", type=Path, required=True)

    p_query = sub.add_parser("query", help="下钻查询", parents=[common])
    p_query.add_argument("name", choices=[
        "monthly", "permit", "snapshot", "balance", "unexplained",
        "calibers", "consideration", "releases"])
    p_query.add_argument("key", nargs="?", help="permit 查询需要的许可证 id")
    p_query.add_argument("--release", help="指定历史版本（如 r_v1），默认 current")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    con = connect(args.db)
    try:
        if args.command == "ingest":
            _print(ingest(con, args.data_dir))
        elif args.command == "publish":
            _print(publish(con))
        elif args.command == "run":
            from .pipeline import run_all
            result = run_all(args.db, args.data_dir)
            con = None
            _print(result)
            return
        elif args.command == "checks":
            from .publish import current_version
            version = current_version(con)
            if version is None:
                raise SystemExit("尚无已发布版本")
            _print(run_checks(con, f"release_{version}"))
        elif args.command == "releases":
            _print(list_releases(con))
        elif args.command == "query":
            if args.name == "monthly":
                _print(q.monthly_supply(con, args.release))
            elif args.name == "permit":
                if not args.key:
                    raise SystemExit("permit 查询需要许可证 id")
                _print(q.permit_drilldown(con, args.key, args.release))
            elif args.name == "snapshot":
                _print(q.snapshot(con, args.release))
            elif args.name == "balance":
                _print(q.atom_balance(con, args.release))
            elif args.name == "unexplained":
                _print(q.unexplained(con, args.release))
            elif args.name == "calibers":
                _print(q.caliber_hints(con, args.release))
            elif args.name == "consideration":
                _print(q.land_consideration(con, args.release))
            elif args.name == "releases":
                _print(list_releases(con))
    finally:
        if con is not None:
            con.close()


if __name__ == "__main__":
    main()
