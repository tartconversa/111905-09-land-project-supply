"""供应数据集发布。

每次发布生成一个不可变 schema（r_v1、r_v2 …），事实表只追加不改写；
current 视图始终指向最新通过集成检查的版本。原始记录留在 raw_* 层，
发布表附行级校验和，满足“原始记录和已发版本均可回查”。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

import duckdb

from .checks import run_checks
from .exact import row_hash
from .transform import build_model

# 表名 -> 建列 DDL。DECIMAL(18,4) 承载平方米面积，金额分别用 BIGINT 分与 DECIMAL 元。
TABLE_DDL: dict[str, str] = {
    "dim_right": """(
        right_id varchar, chain_id varchar, parcel varchar, holder varchar,
        use varchar, kind varchar, reference varchar, effective varchar,
        total_capacity decimal(18,4))""",
    "fact_right_capacity_timeline": """(
        chain_id varchar, right_id varchar, parcel varchar, use varchar, kind varchar,
        effective varchar, valid_to varchar, is_current boolean,
        total_capacity decimal(18,4))""",
    "fact_holding_timeline": """(
        chain_id varchar, atom varchar, holder varchar, valid_from varchar,
        valid_to varchar, is_current boolean, share varchar,
        source_transfer_id varchar)""",
    "fact_claim_resolution": """(
        claim_id varchar, phase varchar, right_id varchar, chain_id varchar,
        atom varchar, scope_id varchar, effective varchar,
        claimed_capacity decimal(18,4), supported_capacity decimal(18,4),
        disputed_capacity decimal(18,4), status varchar)""",
    "fact_dispute_interval": """(
        chain_id varchar, atom varchar, valid_from varchar, valid_to varchar,
        is_current boolean, claimed_capacity decimal(18,4),
        available_capacity decimal(18,4), disputed_capacity decimal(18,4),
        is_disputed boolean)""",
    "dim_building": """(
        building varchar, scope_id varchar, use varchar, atoms varchar,
        chain_id varchar, parcel varchar, current_phase varchar,
        completion varchar, is_completed boolean,
        capacity_area decimal(18,4), saleable_area decimal(18,4),
        delivered_home_area decimal(18,4))""",
    "fact_construction": """(
        building varchar, phase varchar, chain_id varchar, parcel varchar,
        scope_id varchar, use varchar,
        capacity_area decimal(18,4), saleable_area decimal(18,4),
        delivered_home_area decimal(18,4), is_completed boolean, completion varchar)""",
    "fact_assignment": """(
        record_id varchar, building varchar, phase varchar, valid_from varchar,
        valid_to varchar, is_current boolean, received varchar)""",
    "fact_permit": """(
        permit_id varchar, building varchar, phase varchar, chain_id varchar,
        parcel varchar, scope_id varchar, use varchar,
        saleable_area decimal(18,4), valid_from varchar, supply_month varchar)""",
    "fact_delivered": """(
        building varchar, phase varchar, chain_id varchar, parcel varchar,
        use varchar, delivered_month varchar,
        delivered_home_area decimal(18,4), completion varchar)""",
    "fact_monthly_supply": """(
        supply_month varchar, event_type varchar, chain_id varchar, parcel varchar,
        grant_month varchar, building varchar, phase varchar, use varchar,
        area decimal(18,4))""",
    "fact_atom_balance": """(
        atom varchar, chain_id varchar, parcel varchar,
        current_capacity decimal(18,4), claimed_capacity decimal(18,4),
        supported_claim_capacity decimal(18,4), disputed_capacity decimal(18,4),
        building_capacity_area decimal(18,4), building_saleable_area decimal(18,4),
        delivered_home_area decimal(18,4), licensed_saleable_area decimal(18,4),
        claim_minus_building_residual decimal(18,4),
        capacity_minus_building_residual decimal(18,4),
        saleable_minus_licensed_residual decimal(18,4),
        unexplained_count integer)""",
    "fact_unexplained": """(
        kind varchar, building varchar, chain_id varchar, atom varchar,
        scope_id varchar, area decimal(18,4), detail varchar)""",
    "fact_land_consideration": """(
        payment_id varchar, parcel varchar, amount_cents bigint,
        amount_yuan decimal(20,2), date varchar)""",
}

# 粒度与口径登记：可加指标、禁止相加的口径提示
CALIBER_REGISTRY: list[tuple[str, str, str, str]] = [
    ("fact_right_capacity_timeline", "权益链×生效区间",
     "total_capacity（仅同一时点按链取一条，跨区间不可相加）",
     "时间线快照口径：同一链不同区间是替代关系，求和会重复计算额度"),
    ("fact_holding_timeline", "权益链×原子×持有人×有效期",
     "share（同一链原子时点内可加，且恒等于 1/1；跨区间不可相加）",
     "份额是结构占比不是面积，不能与面积指标相加"),
    ("fact_claim_resolution", "申报×原子",
     "claimed_capacity/supported_capacity/disputed_capacity（同状态列内可加）",
     "三类列互斥但同源，跨列相加会重复；争议列不得先到先得"),
    ("fact_dispute_interval", "权益链×原子×争议区间",
     "disputed_capacity（同区间可加，跨时间区间不可相加）",
     "区间是时间切片，同一争议跨期会重复"),
    ("fact_construction", "楼栋",
     "capacity_area/saleable_area/delivered_home_area（各自列内可加）",
     "三个指标口径不同，禁止互换或跨列相加"),
    ("fact_assignment", "楼栋×归属区间",
     "无面积/金额度量，仅归属关系；按楼栋查询时取 is_current 一条",
     "同一楼栋多条区间是历史版本，行数不可当作建设规模相加"),
    ("fact_permit", "许可证×楼栋",
     "saleable_area（许可口径内可加）",
     "许可面积不等于建设面积，也不代表竣工"),
    ("fact_delivered", "楼栋（已竣工）",
     "delivered_home_area（交付口径内可加）",
     "交付以 completion 为准，无许可证推断竣工"),
    ("fact_monthly_supply", "月份×事件类型×楼栋",
     "area（同 event_type 同口径内可加）",
     "licensed 与 delivered 是不同口径，禁止跨 event_type 求和"),
    ("fact_atom_balance", "原子",
     "各面积列在列内可加；residual 列是差额不是供给",
     "残留为未解释规模，禁止按平均比例分摊到楼栋或地价"),
    ("fact_land_consideration", "宗地×付款",
     "amount_cents/amount_yuan（宗地粒度可加）",
     "地价只到宗地，不存在楼栋/分期分摊列，禁止下推"),
    ("fact_unexplained", "未解释项",
     "area（同类项可加）",
     "这是无法对齐的保留差异，不进入任何供给口径"),
]

SNAPSHOT_VIEW = """
create or replace view {schema}.v_supply_snapshot as
select b.building, b.current_phase as phase, b.use, b.is_completed,
       b.capacity_area            as committed_capacity_area,
       b.capacity_area            as construction_capacity_area,
       coalesce(p.licensed_saleable_area, 0) as licensed_saleable_area,
       b.delivered_home_area
from {schema}.dim_building b
left join (
    select building, sum(saleable_area) as licensed_saleable_area
    from {schema}.fact_permit
    group by building
) p on p.building = b.building
"""


def _ensure_registry(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
        create table if not exists release_registry (
            version varchar primary key, published_at varchar,
            source_manifest_hash varchar, is_current boolean)
    """)


def list_releases(con: duckdb.DuckDBPyConnection) -> list[dict]:
    _ensure_registry(con)
    return [
        {"version": r[0], "published_at": r[1],
         "manifest_hash": r[2], "is_current": bool(r[3])}
        for r in con.execute(
            "select version, published_at, source_manifest_hash, is_current "
            "from release_registry order by version").fetchall()
    ]


def current_version(con: duckdb.DuckDBPyConnection) -> str | None:
    rows = list_releases(con)
    current = [r for r in rows if r["is_current"]]
    return current[0]["version"] if current else None


def publish(con: duckdb.DuckDBPyConnection) -> dict:
    """执行一次完整发布：建模 -> 建版本表 -> 集成检查 -> 切换 current 视图。"""
    _ensure_registry(con)
    next_no = con.execute(
        "select coalesce(max(cast(substr(version, 4) as integer)), 0) + 1 "
        "from release_registry").fetchone()[0]
    version = f"r_v{next_no}"
    schema = f"release_{version}"
    published_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest_hash = con.execute(
        "select manifest_hash from ingest_batches "
        "order by batch_id desc limit 1").fetchone()[0]

    model = build_model(con)
    con.execute(f"create schema {schema}")

    checksums: list[dict] = []
    for table, ddl in TABLE_DDL.items():
        con.execute(f"create table {schema}.{table} {ddl}")
        rows = model[table]
        if rows:
            columns = list(rows[0].keys())
            placeholders = ", ".join("?" for _ in columns)
            con.executemany(
                f"insert into {schema}.{table} ({', '.join(columns)}) "
                f"values ({placeholders})",
                [[_normalise(row[col]) for col in columns] for row in rows],
            )
        digest = _table_checksum(con, schema, table)
        checksums.append({"table_name": table, "row_count": len(rows), "sha256": digest})

    # 发布元数据、口径登记、表校验和
    con.execute(f"""
        create table {schema}.release_meta (key varchar, value varchar)
    """)
    con.executemany(
        f"insert into {schema}.release_meta values (?, ?)",
        [("release_version", version), ("published_at", published_at),
         ("source_manifest_hash", manifest_hash)],
    )
    con.execute(f"""
        create table {schema}.caliber_registry (
            fact_table varchar, grain varchar, additive_measures varchar,
            non_additive_note varchar)
    """)
    con.executemany(
        f"insert into {schema}.caliber_registry values (?, ?, ?, ?)",
        CALIBER_REGISTRY,
    )
    con.execute(f"""
        create table {schema}.release_checksums (
            table_name varchar, row_count integer, sha256 varchar)
    """)
    con.executemany(
        f"insert into {schema}.release_checksums values (?, ?, ?)",
        [(c["table_name"], c["row_count"], c["sha256"]) for c in checksums],
    )
    con.execute(SNAPSHOT_VIEW.format(schema=schema))

    # 集成检查不通过则回滚整个版本（drop schema、不登记），通过后才置 current
    check_results = run_checks(con, schema)
    failures = [c for c in check_results if not c["passed"]]
    if failures:
        con.execute(f"drop schema {schema} cascade")
        raise AssertionError(
            "集成检查未通过，版本已回滚："
            + "; ".join(f"{c['check']}={c['detail']}" for c in failures))

    con.execute(
        "insert into release_registry values (?, ?, ?, true)",
        [version, published_at, manifest_hash],
    )
    con.execute("update release_registry set is_current = false where version <> ?", [version])
    _swap_current_views(con, schema)
    return {
        "version": version,
        "schema": schema,
        "published_at": published_at,
        "tables": {c["table_name"]: c["row_count"] for c in checksums},
        "checks": check_results,
    }


def _normalise(value):
    if isinstance(value, Decimal):
        return value
    return value


def _table_checksum(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> str:
    """对发布表全量行做规范哈希，版本回查时可逐表比对。"""
    rows = con.execute(
        f"select * from {schema}.{table} order by all").fetchall()
    cols = [d[0] for d in con.description]
    payload = [
        {col: (float(v) if isinstance(v, Decimal) else v) for col, v in zip(cols, row)}
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _swap_current_views(con: duckdb.DuckDBPyConnection, schema: str) -> None:
    for table in list(TABLE_DDL) + ["v_supply_snapshot", "caliber_registry",
                                    "release_checksums", "release_meta"]:
        con.execute(f"create or replace view current_{table} as select * from {schema}.{table}")
