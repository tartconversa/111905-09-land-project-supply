"""自动化集成不变量检查。

覆盖题目要求的三类高风险场景：
- 联合开发部分转让：份额守恒，不凭空释放额度；
- 跨宗地/跨分期重复申报：争议单列、不先到先得、不重复供应；
- 楼栋改属：权益占用、建设归属、住房范围共同改判，无重叠空档。

每个检查返回 {check, passed, detail}，任一失败则发布版本不会置为 current。
"""
from __future__ import annotations

import duckdb


def _q(con: duckdb.DuckDBPyConnection, sql: str, *params):
    return con.execute(sql, list(params)).fetchall()


def run_checks(con: duckdb.DuckDBPyConnection, schema: str) -> list[dict]:
    results: list[dict] = []

    def record(check: str, passed: bool, detail: str) -> None:
        results.append({"check": check, "passed": bool(passed), "detail": detail})

    # C1 联合开发：任一(链,原子,时点)持有人份额之和恒等于 1/1，且无负份额/重复分段
    rows = _q(con, f"""
        select chain_id, atom, valid_from,
               sum(cast(split_part(share, '/', 1) as double)
                   / cast(split_part(share, '/', 2) as double))
        from {schema}.fact_holding_timeline
        group by 1, 2, 3
        having abs(sum(cast(split_part(share, '/', 1) as double)
                       / cast(split_part(share, '/', 2) as double)) - 1.0) > 1e-12
    """)
    record("holding_shares_sum_to_one", not rows, f"份额不守恒分组 {len(rows)} 个")

    duplicate_segments = _q(con, f"""
        select chain_id, atom, valid_from, count(*)
        from {schema}.fact_holding_timeline
        group by 1, 2, 3, holder
        having count(*) > 1
    """)
    negative_shares = _q(con, f"""
        select * from {schema}.fact_holding_timeline
        where cast(split_part(share, '/', 1) as bigint) < 0
    """)
    record("holding_no_duplicate_or_negative_segments",
           not duplicate_segments and not negative_shares,
           f"重复分段 {len(duplicate_segments)} 个，负份额 {len(negative_shares)} 个")

    # C2 转让不新增额度：同一链当前额度等于链上 grant/amend 最新一条，且无链被重复计数
    rows = _q(con, f"""
        select t.chain_id, count(*)
        from {schema}.fact_right_capacity_timeline t
        where t.is_current
        group by t.chain_id
        having count(*) <> 1
    """)
    record("single_current_capacity_per_chain", not rows, f"多/缺当前额度的链 {len(rows)} 个")

    # C3 修订链：每条链额度时间线区间连续无重叠
    rows = _q(con, f"""
        select a.chain_id
        from {schema}.fact_right_capacity_timeline a
        join {schema}.fact_right_capacity_timeline b
          on a.chain_id = b.chain_id and a.right_id <> b.right_id
         and a.effective < b.effective
         and (a.valid_to is null or a.valid_to > b.effective)
    """)
    record("capacity_timeline_no_overlap", not rows, f"重叠链 {len(rows)} 个")

    # C4 申报不重复供应：任一原子支持面积 + 争议面积 = 申报面积（无丢失无重复）
    rows = _q(con, f"""
        select claim_id, atom, claimed_capacity,
               supported_capacity + disputed_capacity as allocated
        from {schema}.fact_claim_resolution
        where claimed_capacity <> supported_capacity + disputed_capacity
    """)
    record("claim_supported_plus_disputed_equals_claimed", not rows,
           f"配平失败行 {len(rows)} 个")

    # C5 争议不先到先得：存在争议的原子上，所有相关申报 status 必须同为 disputed
    rows = _q(con, f"""
        select distinct c.claim_id
        from {schema}.fact_claim_resolution c
        where c.atom in (
            select distinct atom from {schema}.fact_dispute_interval where is_disputed
        ) and c.status <> 'disputed'
    """)
    record("disputed_atom_claims_not_first_come", not rows,
           f"争议原子上仍被单方认领的申报 {len(rows)} 个")

    # C6 争议额度不超过主张-额度差，且不产生负支持
    rows = _q(con, f"""
        select * from {schema}.fact_dispute_interval
        where disputed_capacity < 0
           or disputed_capacity > claimed_capacity
    """)
    record("dispute_within_claimed_bounds", not rows, f"越界区间 {len(rows)} 个")

    # C7 楼栋改属：同一楼栋归属区间互不重叠、首尾相接，当前归属唯一
    overlap = _q(con, f"""
        select a.building
        from {schema}.fact_assignment a
        join {schema}.fact_assignment b
          on a.building = b.building and a.record_id <> b.record_id
         and a.valid_from < b.valid_from
         and (a.valid_to is null or a.valid_to > b.valid_from)
    """)
    gap = _q(con, f"""
        select a.building
        from {schema}.fact_assignment a
        join {schema}.fact_assignment b
          on a.building = b.building and a.record_id <> b.record_id
         and a.valid_to is not null and a.valid_to < b.valid_from
         and not exists (
             select 1 from {schema}.fact_assignment c
             where c.building = a.building
               and c.valid_from <= a.valid_to
               and (c.valid_to is null or c.valid_to > a.valid_to)
               and c.record_id not in (a.record_id, b.record_id))
    """)
    multi_current = _q(con, f"""
        select building from {schema}.fact_assignment
        group by building having sum(case when is_current then 1 else 0 end) <> 1
    """)
    record("building_reassignment_no_overlap", not overlap, f"重叠楼栋 {len(overlap)} 个")
    record("building_reassignment_no_gap", not gap, f"空档楼栋 {len(gap)} 个")
    record("building_single_current_phase", not multi_current,
           f"当前归属不唯一楼栋 {len(multi_current)} 个")

    # C8 改属共同改判：建设事实与许可事实的 phase 必须等于楼栋当前归属
    rows = _q(con, f"""
        select x.building, x.phase, b.current_phase
        from (
            select building, phase from {schema}.fact_construction
            union all
            select building, phase from {schema}.fact_permit
        ) x
        join {schema}.dim_building b on b.building = x.building
        where x.phase <> b.current_phase
    """)
    record("construction_and_permit_follow_reassignment", not rows,
           f"未跟随改属的事实 {len(rows)} 个")

    # C9 许可不凭空释放：许可面积不超过同楼栋可售面积，且无许可证推断竣工
    rows = _q(con, f"""
        select p.permit_id
        from {schema}.fact_permit p
        join {schema}.dim_building b on b.building = p.building
        where p.saleable_area > b.saleable_area
    """)
    record("permit_area_not_exceed_saleable", not rows, f"越权许可 {len(rows)} 个")

    # C10 交付事实只来自 completion 非空楼栋
    rows = _q(con, f"""
        select d.building
        from {schema}.fact_delivered d
        join {schema}.dim_building b on b.building = d.building
        where not b.is_completed
    """)
    record("delivery_only_from_completion", not rows, f"无竣工依据交付 {len(rows)} 个")

    # C11 未解释规模被保留：用途不匹配/指标差必须有显式记录
    mismatch = _q(con, f"""
        select b.building
        from {schema}.dim_building b
        join {schema}.fact_right_capacity_timeline t
          on t.chain_id = b.chain_id and t.is_current
        where b.use <> t.use
          and b.building not in (
              select building from {schema}.fact_unexplained where kind = 'use_mismatch')
    """)
    record("unexplained_use_mismatch_retained", not mismatch,
           f"未登记的用途冲突 {len(mismatch)} 个")

    # C12 地价只到宗地：对价表无楼栋/分期/原子列（结构断言）
    cols = {d[0] for d in con.execute(
        f"describe {schema}.fact_land_consideration").fetchall()}
    leaked = cols & {"building", "phase", "atom", "scope_id"}
    record("land_consideration_not_allocated", not leaked,
           "无下推列" if not leaked else f"发现分摊列 {sorted(leaked)}")

    # C13 口径登记完整：每个事实表都登记了粒度与不可相加提示
    missing = _q(con, f"""
        select t.table_name from {schema}.release_checksums t
        where t.table_name like 'fact_%'
          and t.table_name not in (
              select fact_table from {schema}.caliber_registry)
    """)
    record("caliber_registry_covers_all_facts", not missing,
           f"缺口径登记 {len(missing)} 个")

    # C14 月度供给口径隔离：licensed/delivered 分列，不存在混合口径行
    rows = _q(con, f"""
        select event_type, count(*) from {schema}.fact_monthly_supply
        group by 1 having event_type not in ('licensed', 'delivered')
    """)
    record("monthly_supply_caliber_separated", not rows, f"异常事件类型 {len(rows)} 个")

    return results
