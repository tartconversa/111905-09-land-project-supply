"""下钻查询层。

所有查询默认走 current_* 视图（最新已发布版本），也可指定历史版本
（r_v1 或完整 schema release_r_v1）回查。查询函数只做读取，不产生新事实。
"""
from __future__ import annotations

import duckdb


def _table(release: str | None, table: str) -> str:
    """返回查询对象全名：缺省走 current 视图，指定版本走 schema 内表。"""
    if not release:
        return f"current_{table}"
    schema = release if release.startswith("release_") else f"release_{release}"
    return f"{schema}.{table}"


def _dicts(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def monthly_supply(con: duckdb.DuckDBPyConnection, release: str | None = None) -> list[dict]:
    """同一供地批次（权益链/宗地）在各月份形成的住宅供给，许可与交付分列。

    注意：licensed 与 delivered 是不同口径，结果中以 event_type 区分，
    展示层不得把两列直接求和。
    """
    return _dicts(con, f"""
        select grant_month, parcel, chain_id, supply_month, event_type,
               sum(area) as area
        from {_table(release, 'fact_monthly_supply')}
        group by 1, 2, 3, 4, 5
        order by grant_month, parcel, supply_month, event_type
    """)


def permit_drilldown(con: duckdb.DuckDBPyConnection, permit_id: str,
                     release: str | None = None) -> dict:
    """从一张预售许可证穿透到销售范围、建设事实、分期归属与土地权益链。"""
    permits = _dicts(con, f"""
        select * from {_table(release, 'fact_permit')} where permit_id = ?
    """, [permit_id])
    if not permits:
        raise KeyError(f"许可证不存在：{permit_id}")
    head = permits[0]
    buildings = _dicts(con, f"""
        select * from {_table(release, 'dim_building')} where building = ?
    """, [head["building"]])
    return {
        "permit": head,
        "building": buildings[0] if buildings else None,
        "assignment_history": _dicts(con, f"""
            select * from {_table(release, 'fact_assignment')}
            where building = ? order by valid_from
        """, [head["building"]]),
        "right_capacity_timeline": _dicts(con, f"""
            select * from {_table(release, 'fact_right_capacity_timeline')}
            where chain_id = ? order by effective
        """, [head["chain_id"]]),
        "holding_timeline": _dicts(con, f"""
            select * from {_table(release, 'fact_holding_timeline')}
            where chain_id = ? order by atom, valid_from, holder
        """, [head["chain_id"]]),
        "phase_claims_on_chain": _dicts(con, f"""
            select * from {_table(release, 'fact_claim_resolution')}
            where chain_id = ? order by claim_id, atom
        """, [head["chain_id"]]),
        "dispute_intervals": _dicts(con, f"""
            select * from {_table(release, 'fact_dispute_interval')}
            where chain_id = ? order by atom, valid_from
        """, [head["chain_id"]]),
        "unexplained_items": _dicts(con, f"""
            select * from {_table(release, 'fact_unexplained')}
            where building = ? order by kind
        """, [head["building"]]),
        "caliber_note": "许可面积是销售口径；建设 capacity_area 是容积率口径；"
                        "delivered_home_area 是交付口径，三者不可相加",
    }


def atom_balance(con: duckdb.DuckDBPyConnection, release: str | None = None) -> list[dict]:
    """原子余额调节表：额度、申报、建设、许可、交付与残留并列。"""
    return _dicts(con, f"""
        select * from {_table(release, 'fact_atom_balance')} order by atom
    """)


def caliber_hints(con: duckdb.DuckDBPyConnection, release: str | None = None) -> list[dict]:
    """返回每张事实表的粒度、可加指标与不可相加口径提示。"""
    return _dicts(con, f"""
        select * from {_table(release, 'caliber_registry')} order by fact_table
    """)


def snapshot(con: duckdb.DuckDBPyConnection, release: str | None = None) -> list[dict]:
    """四口径楼栋快照：承诺/在建兑现/已许可/已交付并列，不混合为阶段进度。"""
    return _dicts(con, f"""
        select * from {_table(release, 'v_supply_snapshot')} order by building
    """)


def unexplained(con: duckdb.DuckDBPyConnection, release: str | None = None) -> list[dict]:
    return _dicts(con, f"""
        select * from {_table(release, 'fact_unexplained')} order by kind, building
    """)


def land_consideration(con: duckdb.DuckDBPyConnection,
                       release: str | None = None) -> list[dict]:
    return _dicts(con, f"""
        select * from {_table(release, 'fact_land_consideration')} order by payment_id
    """)
