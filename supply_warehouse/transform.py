"""范围关联与事实建模（纯函数层）。

输入：ingest 原始层当前版本的原始对象。
输出：一组粒度明确的事实行字典，由 publish 装载进不可变发布版本。

核心建模原则（对应来源契约与业务要求）：
1. 修订（amend）替代原权益总额，沿引用链形成带生效期的额度时间线，不新增额度；
2. 份额只在 scope 的 coverage 原子内计算，不跨原子平均；
3. 同一原子被多份分期申报重复主张时，超额整体单列为争议，不按上报顺序先到先得；
4. 转让只改变持有人份额结构，全部分份额恒等，不释放、不新增额度；
5. capacity_area / saleable_area / delivered_home_area 是三个独立指标，全程保留；
6. 无法对齐的用途与指标差留作“未解释规模”，不用整体平均比例分摊地价或补齐面积。
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from fractions import Fraction

from .exact import fraction_str, parse_area, parse_fraction
from .ingest import current_rows

ZERO = Decimal("0")
ONE = Fraction(1, 1)


def _coverage_atoms(scopes_by_id: dict, scope_id: str) -> list[tuple[str, Fraction]]:
    """把一个 scope 展开为 (原子, 份额) 列表；scope 缺失时显式报错而非猜测。"""
    if scope_id not in scopes_by_id:
        raise ValueError(f"引用了不存在的 scope：{scope_id}")
    return [
        (item["atom"], parse_fraction(item["fraction"]))
        for item in scopes_by_id[scope_id]["coverage"]
    ]


def _root_chain(rights: list[dict]) -> dict[str, str]:
    """沿 amend 的 reference 追溯每条权益的链根（初始出让权益 id）。"""
    by_id = {r["right_id"]: r for r in rights}
    cache: dict[str, str] = {}

    def root_of(right_id: str) -> str:
        if right_id in cache:
            return cache[right_id]
        chain: list[str] = []
        cur = right_id
        while by_id.get(cur, {}).get("kind") == "amend":
            if cur in chain:  # 防御：引用环
                raise ValueError(f"权益修订链存在循环引用：{cur}")
            chain.append(cur)
            ref = by_id[cur].get("reference")
            if ref is None or ref not in by_id:
                raise ValueError(f"修订记录缺少有效 reference：{cur}")
            cur = ref
        for node in chain:
            cache[node] = cur
        cache[cur] = cur
        return cur

    return {r["right_id"]: root_of(r["right_id"]) for r in rights}


def _capacity_timeline(rights: list[dict], chain_of: dict[str, str]) -> list[dict]:
    """每条权益链按生效期形成额度时间线；区间为左闭右开，valid_to 为 null 表示至今。"""
    chain_members: dict[str, list[dict]] = defaultdict(list)
    for row in rights:
        chain_members[chain_of[row["right_id"]]].append(row)

    timeline: list[dict] = []
    for chain_id, members in chain_members.items():
        ordered = sorted(members, key=lambda r: r["effective"])
        for index, row in enumerate(ordered):
            valid_to = ordered[index + 1]["effective"] if index + 1 < len(ordered) else None
            timeline.append({
                "chain_id": chain_id,
                "right_id": row["right_id"],
                "parcel": row["parcel"],
                "use": row["use"],
                "kind": row["kind"],
                "effective": row["effective"],
                "valid_to": valid_to,
                "is_current": valid_to is None,
                "total_capacity": parse_area(row["total_capacity"]),
            })
    return sorted(timeline, key=lambda x: (x["chain_id"], x["effective"]))


def _capacity_on(timeline: list[dict], chain_id: str, on_date: str) -> Decimal:
    """取某权益链在指定日期适用的总额度。"""
    applicable = [
        row for row in timeline
        if row["chain_id"] == chain_id
        and row["effective"] <= on_date
        and (row["valid_to"] is None or on_date < row["valid_to"])
    ]
    if not applicable:
        # 申报早于链根生效：链根生效前额度视为 0，整体落入争议/未解释
        return ZERO
    return applicable[0]["total_capacity"]


def _holdings(rights: list[dict], transfers: list[dict], scopes_by_id: dict,
              claims: list[dict], chain_of: dict[str, str]) -> list[dict]:
    """构造 (链, 原子, 持有人) 的持有份额时间线。

    基础状态为链根持有人对全部相关原子持有 1/1；转让在其有效期内把对应
    原子的 share 从转出方移到转入方。任何时点各持有人份额之和恒等于 1。
    """
    by_id = {r["right_id"]: r for r in rights}
    root_holder = {
        chain: next(r["holder"] for r in rights
                    if chain_of[r["right_id"]] == chain and r["kind"] == "grant")
        for chain in {chain_of[r["right_id"]] for r in rights}
    }

    # 每个链涉及的原子：来自转让范围与申报范围
    chain_atoms: dict[str, set[str]] = defaultdict(set)
    for tr in transfers:
        chain = chain_of[tr["right_id"]]
        for atom, _ in _coverage_atoms(scopes_by_id, tr["scope_id"]):
            chain_atoms[chain].add(atom)
    for cl in claims:
        chain = chain_of[cl["right_id"]]
        for atom, _ in _coverage_atoms(scopes_by_id, cl["scope_id"]):
            chain_atoms[chain].add(atom)

    out: list[dict] = []
    for chain, atoms in sorted(chain_atoms.items()):
        grant_date = min(
            r["effective"] for r in rights
            if chain_of[r["right_id"]] == chain and r["kind"] == "grant"
        )
        for atom in sorted(atoms):
            # 按日期汇总净变动：同一日期多份转让先净额结算，再切时间轴，
            # 避免同日事件被分段重复施加。
            net_changes: dict[str, dict[str, Fraction]] = defaultdict(
                lambda: defaultdict(lambda: Fraction(0)))
            transfer_by_date: dict[str, list[str]] = defaultdict(list)
            for tr in transfers:
                if chain_of[tr["right_id"]] != chain:
                    continue
                if atom not in {a for a, _ in _coverage_atoms(scopes_by_id, tr["scope_id"])}:
                    continue
                share = parse_fraction(tr["share"])
                net_changes[tr["valid_from"]][tr["from_holder"]] -= share
                net_changes[tr["valid_from"]][tr["to_holder"]] += share
                transfer_by_date[tr["valid_from"]].append(tr["transfer_id"])
                if tr["valid_to"]:
                    net_changes[tr["valid_to"]][tr["to_holder"]] -= share
                    net_changes[tr["valid_to"]][tr["from_holder"]] += share
                    transfer_by_date[tr["valid_to"]].append(tr["transfer_id"])

            cuts = sorted(net_changes)
            bounds = [grant_date] + cuts
            active: dict[str, Fraction] = defaultdict(lambda: Fraction(0))
            active[root_holder[chain]] = ONE
            applied_transfers: list[str] = []
            for seg_index, start in enumerate(bounds):
                if start in net_changes:
                    for holder, delta in net_changes[start].items():
                        active[holder] += delta
                    applied_transfers = sorted(
                        set(applied_transfers) | set(transfer_by_date[start]))
                end = bounds[seg_index + 1] if seg_index + 1 < len(bounds) else None
                for holder, share in sorted(active.items()):
                    if share == 0:
                        continue
                    out.append({
                        "chain_id": chain,
                        "atom": atom,
                        "holder": holder,
                        "valid_from": start,
                        "valid_to": end,
                        "is_current": end is None,
                        "share": fraction_str(share),
                        "source_transfer_id": applied_transfers[-1] if applied_transfers else None,
                    })
    return out


def _dispute_intervals(claims: list[dict], scopes_by_id: dict,
                       chain_of: dict[str, str], timeline: list[dict]) -> list[dict]:
    """按 (链, 原子) 把申报主张与适用额度对齐，产出争议区间。

    同一原子上所有申报的累计主张超过当期总额度时，该区间整体标记争议：
    争议额度不分配给任何一份申报（包括先到的），只做单列。
    """
    # (chain, atom) -> 日期 -> 新增主张面积
    claim_events: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: ZERO))
    for cl in claims:
        chain = chain_of[cl["right_id"]]
        area = parse_area(cl["capacity_area"])
        for atom, fraction in _coverage_atoms(scopes_by_id, cl["scope_id"]):
            if fraction != ONE:
                # 来源若出现部分原子份额，按份额精确缩放，不做平均
                from .exact import scale_area
                claim_events[(chain, atom)][cl["effective"]] += scale_area(area, fraction)
            else:
                claim_events[(chain, atom)][cl["effective"]] += area

    out: list[dict] = []
    for (chain, atom), by_date in sorted(claim_events.items()):
        cap_dates = sorted({
            row["effective"] for row in timeline if row["chain_id"] == chain
        })
        cuts = sorted(set(list(by_date.keys()) + cap_dates))
        running_claim = ZERO
        for index, start in enumerate(cuts):
            running_claim += by_date.get(start, ZERO)
            end = cuts[index + 1] if index + 1 < len(cuts) else None
            capacity = _capacity_on(timeline, chain, start)
            disputed = running_claim - capacity if running_claim > capacity else ZERO
            if running_claim == ZERO:
                continue
            out.append({
                "chain_id": chain,
                "atom": atom,
                "valid_from": start,
                "valid_to": end,
                "is_current": end is None,
                "claimed_capacity": running_claim,
                "available_capacity": capacity,
                "disputed_capacity": disputed,
                "is_disputed": disputed > ZERO,
            })
    return out


def _claim_resolution(claims: list[dict], scopes_by_id: dict,
                      chain_of: dict[str, str],
                      disputes: list[dict]) -> list[dict]:
    """每份申报展开到原子，并按争议区间拆分支持/争议面积（不先到先得）。"""
    out: list[dict] = []
    for cl in claims:
        chain = chain_of[cl["right_id"]]
        area = parse_area(cl["capacity_area"])
        for atom, fraction in _coverage_atoms(scopes_by_id, cl["scope_id"]):
            from .exact import scale_area
            atom_claim = scale_area(area, fraction) if fraction != ONE else area
            related = [
                d for d in disputes
                if d["chain_id"] == chain and d["atom"] == atom
                and d["valid_from"] >= cl["effective"]
            ]
            ever_disputed = any(d["is_disputed"] for d in related)
            # 争议区间内该原子的全部主张都不获支持；无争议区间全额支持
            supported = ZERO if ever_disputed else atom_claim
            disputed = atom_claim - supported
            out.append({
                "claim_id": cl["claim_id"],
                "phase": cl["phase"],
                "right_id": cl["right_id"],
                "chain_id": chain,
                "atom": atom,
                "scope_id": cl["scope_id"],
                "effective": cl["effective"],
                "claimed_capacity": atom_claim,
                "supported_capacity": supported,
                "disputed_capacity": disputed,
                "status": "disputed" if ever_disputed else "supported",
            })
    return sorted(out, key=lambda x: (x["claim_id"], x["atom"]))


def _atom_chain_map(claims: list[dict], transfers: list[dict],
                    scopes_by_id: dict, chain_of: dict[str, str]) -> dict[str, str]:
    """原子到权益链的归属映射（来自申报与转让的范围声明）。"""
    mapping: dict[str, str] = {}

    def register(scope_id: str, right_id: str) -> None:
        chain = chain_of[right_id]
        for atom, _ in _coverage_atoms(scopes_by_id, scope_id):
            if atom in mapping and mapping[atom] != chain:
                raise ValueError(f"原子 {atom} 被映射到多个权益链，无法对齐")
            mapping[atom] = chain

    for cl in claims:
        register(cl["scope_id"], cl["right_id"])
    for tr in transfers:
        register(tr["scope_id"], tr["right_id"])
    return mapping


def build_model(con) -> dict[str, list[dict]]:
    """从原始层当前版本构造全部发布事实表。"""
    rights = current_rows(con, "rights")
    scopes = current_rows(con, "scopes")
    claims = current_rows(con, "phase_claims")
    buildings = current_rows(con, "buildings")
    assignments = current_rows(con, "assignments")
    permits = current_rows(con, "permits")
    consideration = current_rows(con, "land_consideration")
    transfers = current_rows(con, "right_transfers")
    scopes_by_id = {s["scope_id"]: s for s in scopes}
    chain_of = _root_chain(rights)
    by_id = {r["right_id"]: r for r in rights}

    # ---- 1. 权益维度与额度时间线 ----
    dim_right = [{
        "right_id": r["right_id"],
        "chain_id": chain_of[r["right_id"]],
        "parcel": r["parcel"],
        "holder": r["holder"],
        "use": r["use"],
        "kind": r["kind"],
        "reference": r["reference"],
        "effective": r["effective"],
        "total_capacity": parse_area(r["total_capacity"]),
    } for r in sorted(rights, key=lambda r: r["right_id"])]
    timeline = _capacity_timeline(rights, chain_of)

    # ---- 2. 联合开发持有份额 ----
    holdings = _holdings(rights, transfers, scopes_by_id, claims, chain_of)

    # ---- 3. 申报与争议 ----
    disputes = _dispute_intervals(claims, scopes_by_id, chain_of, timeline)
    claim_resolution = _claim_resolution(claims, scopes_by_id, chain_of, disputes)

    # ---- 4. 楼栋、分期归属、许可、交付 ----
    atom_chain = _atom_chain_map(claims, transfers, scopes_by_id, chain_of)
    latest_assignment: dict[str, dict] = {}
    for asm in assignments:
        cur = latest_assignment.get(asm["building"])
        if cur is None or asm["valid_from"] > cur["valid_from"]:
            latest_assignment[asm["building"]] = asm

    dim_building = []
    fact_construction = []
    fact_permit = []
    fact_delivered = []
    unexplained: list[dict] = []

    for b in sorted(buildings, key=lambda x: x["building"]):
        atoms = _coverage_atoms(scopes_by_id, b["scope_id"])
        chains = sorted({atom_chain.get(a) for a, _ in atoms if atom_chain.get(a)})
        root_use = None
        parcel = None
        if chains:
            root = next(r for r in rights
                        if chain_of[r["right_id"]] == chains[0] and r["kind"] == "grant")
            root_use, parcel = root["use"], root["parcel"]
        asm = latest_assignment.get(b["building"])
        capacity = parse_area(b["capacity_area"])
        saleable = parse_area(b["saleable_area"])
        delivered = parse_area(b["delivered_home_area"])

        dim_building.append({
            "building": b["building"],
            "scope_id": b["scope_id"],
            "use": b["use"],
            "atoms": ",".join(a for a, _ in atoms),
            "chain_id": chains[0] if chains else None,
            "parcel": parcel,
            "current_phase": asm["phase"] if asm else None,
            "completion": b["completion"],
            "is_completed": b["completion"] is not None,
            "capacity_area": capacity,
            "saleable_area": saleable,
            "delivered_home_area": delivered,
        })

        # 承诺/在建兑现事实（楼栋粒度，三指标并列保留，不互换）
        fact_construction.append({
            "building": b["building"],
            "phase": asm["phase"] if asm else None,
            "chain_id": chains[0] if chains else None,
            "parcel": parcel,
            "scope_id": b["scope_id"],
            "use": b["use"],
            "capacity_area": capacity,
            "saleable_area": saleable,
            "delivered_home_area": delivered,
            "is_completed": b["completion"] is not None,
            "completion": b["completion"],
        })

        # 未解释规模一：建设用途与权益用途不一致（如配套设施占用住宅额度）
        if root_use is not None and b["use"] != root_use:
            unexplained.append({
                "kind": "use_mismatch",
                "building": b["building"],
                "chain_id": chains[0] if chains else None,
                "atom": atoms[0][0] if atoms else None,
                "scope_id": b["scope_id"],
                "area": capacity,
                "detail": f"楼栋用途 {b['use']} 与权益用途 {root_use} 不一致，保留差异不做分摊",
            })
        # 未解释规模二：计入容积率面积与可售/交付面积的指标差
        gap = capacity - saleable
        if gap != ZERO:
            unexplained.append({
                "kind": "indicator_gap",
                "building": b["building"],
                "chain_id": chains[0] if chains else None,
                "atom": atoms[0][0] if atoms else None,
                "scope_id": b["scope_id"],
                "area": gap,
                "detail": "capacity_area 与 saleable_area 之差：配套/公摊占用额度但不形成可售房屋",
            })

        if b["completion"] is not None:
            fact_delivered.append({
                "building": b["building"],
                "phase": asm["phase"] if asm else None,
                "chain_id": chains[0] if chains else None,
                "parcel": parcel,
                "use": b["use"],
                "delivered_month": b["completion"][:7],
                "delivered_home_area": delivered,
                "completion": b["completion"],
            })

    for p in sorted(permits, key=lambda x: x["permit_id"]):
        atoms = _coverage_atoms(scopes_by_id, p["scope_id"])
        chains = sorted({atom_chain.get(a) for a, _ in atoms if atom_chain.get(a)})
        parcel = None
        if chains:
            root = next(r for r in rights
                        if chain_of[r["right_id"]] == chains[0] and r["kind"] == "grant")
            parcel = root["parcel"]
        asm = latest_assignment.get(p["building"])
        fact_permit.append({
            "permit_id": p["permit_id"],
            "building": p["building"],
            "phase": asm["phase"] if asm else None,
            "chain_id": chains[0] if chains else None,
            "parcel": parcel,
            "scope_id": p["scope_id"],
            "use": p["use"],
            "saleable_area": parse_area(p["saleable_area"]),
            "valid_from": p["valid_from"],
            "supply_month": p["valid_from"][:7],
        })

    # 未解释规模三：范围无法对齐到任何权益链
    for b in buildings:
        for atom, _ in _coverage_atoms(scopes_by_id, b["scope_id"]):
            if atom not in atom_chain:
                unexplained.append({
                    "kind": "orphan_scope",
                    "building": b["building"],
                    "chain_id": None,
                    "atom": atom,
                    "scope_id": b["scope_id"],
                    "area": parse_area(b["capacity_area"]),
                    "detail": "建设范围无法对齐到土地权益链，留作未解释规模",
                })

    # ---- 5. 楼栋分期归属 SCD ----
    fact_assignment = [{
        "record_id": a["record_id"],
        "building": a["building"],
        "phase": a["phase"],
        "valid_from": a["valid_from"],
        "valid_to": a["valid_to"],
        "is_current": a["valid_to"] is None,
        "received": a["received"],
    } for a in sorted(assignments, key=lambda x: (x["building"], x["valid_from"]))]

    # ---- 6. 原子余额调节表（可加指标与残留并列，残留不分摊） ----
    balance: list[dict] = []
    all_atoms = sorted({a for s in scopes for a, _ in
                        _coverage_atoms(scopes_by_id, s["scope_id"])})
    for atom in all_atoms:
        chain = atom_chain.get(atom)
        parcel = None
        if chain:
            parcel = next(r["parcel"] for r in rights
                          if chain_of[r["right_id"]] == chain and r["kind"] == "grant")
        cur_cap = next((row["total_capacity"] for row in timeline
                        if row["chain_id"] == chain and row["is_current"]), ZERO)
        cl_rows = [x for x in claim_resolution if x["atom"] == atom]
        b_rows = [b for b in buildings
                  if atom in {a for a, _ in _coverage_atoms(scopes_by_id, b["scope_id"])}]
        p_rows = [p for p in permits
                  if atom in {a for a, _ in _coverage_atoms(scopes_by_id, p["scope_id"])}]
        claimed = sum((x["claimed_capacity"] for x in cl_rows), ZERO)
        supported = sum((x["supported_capacity"] for x in cl_rows), ZERO)
        disputed = sum((x["disputed_capacity"] for x in cl_rows), ZERO)
        built = sum((parse_area(b["capacity_area"]) for b in b_rows), ZERO)
        saleable = sum((parse_area(b["saleable_area"]) for b in b_rows), ZERO)
        delivered = sum((parse_area(b["delivered_home_area"]) for b in b_rows), ZERO)
        licensed = sum((parse_area(p["saleable_area"]) for p in p_rows), ZERO)
        balance.append({
            "atom": atom,
            "chain_id": chain,
            "parcel": parcel,
            "current_capacity": cur_cap,
            "claimed_capacity": claimed,
            "supported_claim_capacity": supported,
            "disputed_capacity": disputed,
            "building_capacity_area": built,
            "building_saleable_area": saleable,
            "delivered_home_area": delivered,
            "licensed_saleable_area": licensed,
            "claim_minus_building_residual": claimed - built,
            "capacity_minus_building_residual": cur_cap - built,
            "saleable_minus_licensed_residual": saleable - licensed,
            "unexplained_count": sum(1 for u in unexplained if u.get("atom") == atom),
        })

    # ---- 7. 月度住房供给（许可与交付事件；同口径可加，跨口径不可加） ----
    monthly: list[dict] = []
    grant_month_by_chain = {}
    parcel_by_chain = {}
    for r in rights:
        if r["kind"] == "grant":
            chain = chain_of[r["right_id"]]
            grant_month_by_chain[chain] = r["effective"][:7]
            parcel_by_chain[chain] = r["parcel"]
    for row in fact_permit:
        monthly.append({
            "supply_month": row["supply_month"],
            "event_type": "licensed",
            "chain_id": row["chain_id"],
            "parcel": row["parcel"],
            "grant_month": grant_month_by_chain.get(row["chain_id"]),
            "building": row["building"],
            "phase": row["phase"],
            "use": row["use"],
            "area": row["saleable_area"],
        })
    for row in fact_delivered:
        monthly.append({
            "supply_month": row["delivered_month"],
            "event_type": "delivered",
            "chain_id": row["chain_id"],
            "parcel": row["parcel"],
            "grant_month": grant_month_by_chain.get(row["chain_id"]),
            "building": row["building"],
            "phase": row["phase"],
            "use": row["use"],
            "area": row["delivered_home_area"],
        })
    monthly.sort(key=lambda x: (x["supply_month"], x["event_type"], x["building"]))

    # ---- 8. 宗地对价（只到宗地，禁止下推分摊） ----
    fact_consideration = [{
        "payment_id": p["payment_id"],
        "parcel": p["parcel"],
        "amount_cents": int(p["amount_cents"]),
        "amount_yuan": Decimal(int(p["amount_cents"])) / Decimal(100),
        "date": p["date"],
    } for p in sorted(consideration, key=lambda x: x["payment_id"])]

    return {
        "dim_right": dim_right,
        "fact_right_capacity_timeline": timeline,
        "fact_holding_timeline": holdings,
        "fact_claim_resolution": claim_resolution,
        "fact_dispute_interval": disputes,
        "dim_building": dim_building,
        "fact_construction": fact_construction,
        "fact_assignment": fact_assignment,
        "fact_permit": fact_permit,
        "fact_delivered": fact_delivered,
        "fact_monthly_supply": monthly,
        "fact_atom_balance": balance,
        "fact_unexplained": unexplained,
        "fact_land_consideration": fact_consideration,
    }
