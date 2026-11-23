"""形成仓集成测试。

用临时目录构造合成来源批次（每个批次独立目录，脚本不进入资料目录执行，
Python 进程以 -I 隔离），验证：
- 联合开发部分转让的份额守恒、不新增额度；
- 跨宗地/跨分期重复主张的争议单列与不先到先得；
- 楼栋改属后建设、许可、归属共同改判；
- 增量接收幂等、版本化发布与历史回查、原始记录可回溯；
- 精确运算与禁止分摊口径。
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from supply_warehouse.ingest import connect, ingest, current_rows
from supply_warehouse.publish import publish, list_releases
from supply_warehouse.checks import run_checks
from supply_warehouse import query as q
from supply_warehouse.exact import parse_fraction, scale_area
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def _write_source(directory: Path, files: dict[str, list[dict]]) -> None:
    """把若干 NDJSON 资料写进独立目录，并按真实文件重建 manifest。"""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    entries = []
    for entry in manifest["files"]:
        name = entry["path"]
        rows = files.get(entry["dataset"],
                         [json.loads(ln) for ln in
                          (DATA / name).read_text(encoding="utf-8").splitlines() if ln.strip()])
        blob = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        (directory / name).write_text(blob, encoding="utf-8")
        entries.append({
            "path": name, "dataset": entry["dataset"],
            "rows": len(rows),
            "sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
        })
    (directory / "schema.json").write_text(
        (DATA / "schema.json").read_text(encoding="utf-8"), encoding="utf-8")
    (directory / "manifest.json").write_text(
        json.dumps({"description": "测试合成资料", "files": entries},
                   ensure_ascii=False, indent=2), encoding="utf-8")


def _load_all() -> dict[str, list[dict]]:
    out = {}
    for entry in json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))["files"]:
        out[entry["dataset"]] = [
            json.loads(ln) for ln in
            (DATA / entry["path"]).read_text(encoding="utf-8").splitlines() if ln.strip()]
    return out


class WarehouseIntegration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.src = self.tmp / "source"
        self.db = self.tmp / "warehouse.duckdb"

    def tearDown(self):
        self._tmp.cleanup()

    def _publish_source(self, files):
        _write_source(self.src, files)
        con = connect(self.db)
        try:
            batch = ingest(con, self.src)
            release = publish(con)
            return con, batch, release
        except Exception:
            con.close()
            raise

    def test_01_all_integration_checks_pass_on_real_data(self):
        con, _, release = self._publish_source(_load_all())
        self.assertTrue(all(c["passed"] for c in release["checks"]))
        self.assertEqual(len(release["checks"]), 17)
        con.close()

    def test_02_joint_development_share_conservation(self):
        """RT1 转让 1/4 后，RG2/B 上份额恒等且额度不增加。"""
        con, _, _ = self._publish_source(_load_all())
        rows = con.execute("""
            select holder, share from current_fact_holding_timeline
            where chain_id = 'RG2' and atom = 'B' and is_current
            order by holder
        """).fetchall()
        shares = {h: parse_fraction(s) for h, s in rows}
        self.assertEqual(shares, {"DEV1": parse_fraction("3/4"),
                                  "DEV2": parse_fraction("1/4")})
        self.assertEqual(sum(shares.values()), parse_fraction("1/1"))
        cap = con.execute("""
            select total_capacity from current_fact_right_capacity_timeline
            where chain_id = 'RG2' and is_current
        """).fetchone()[0]
        self.assertEqual(Decimal(str(cap)), Decimal("6000"))
        con.close()

    def test_03_duplicate_claims_are_disputed_not_first_come(self):
        """CL1/CL3 重复主张 A：争议整体单列，先到的 CL1 也不获支持。"""
        con, _, _ = self._publish_source(_load_all())
        statuses = {r[0]: (Decimal(str(r[1])), Decimal(str(r[2])), r[3])
                    for r in con.execute("""
            select claim_id, supported_capacity, disputed_capacity, status
            from current_fact_claim_resolution where atom = 'A'
        """).fetchall()}
        for claim in ("CL1", "CL3"):
            supported, disputed, status = statuses[claim]
            self.assertEqual(status, "disputed")
            self.assertEqual(supported, Decimal("0"))
            self.assertGreater(disputed, Decimal("0"))
        # 争议区间随修订变化：1000 -> 2000，且区间不重叠
        intervals = con.execute("""
            select valid_from, disputed_capacity, is_disputed
            from current_fact_dispute_interval
            where chain_id = 'RG1' and atom = 'A' order by valid_from
        """).fetchall()
        self.assertEqual([Decimal(str(r[1])) for r in intervals],
                         [Decimal("0"), Decimal("1000"), Decimal("2000")])
        con.close()

    def test_04_building_reassignment_rejudges_all_facts(self):
        """B2 改属 P2 后，建设事实与当前归属一致，旧区间闭合无重叠。"""
        con, _, _ = self._publish_source(_load_all())
        current_phase = con.execute("""
            select current_phase from current_dim_building where building = 'B2'
        """).fetchone()[0]
        self.assertEqual(current_phase, "P2")
        construction_phase = con.execute("""
            select phase from current_fact_construction where building = 'B2'
        """).fetchone()[0]
        self.assertEqual(construction_phase, "P2")
        history = con.execute("""
            select phase, valid_from, valid_to, is_current
            from current_fact_assignment where building = 'B2' order by valid_from
        """).fetchall()
        self.assertEqual([r[0] for r in history], ["P1", "P2"])
        self.assertEqual(history[0][2], history[1][1])  # 首尾相接
        self.assertTrue(history[1][3])
        con.close()

    def test_05_three_area_indicators_kept_distinct(self):
        """capacity/saleable/delivered 三指标不互换，差异进未解释规模。"""
        con, _, _ = self._publish_source(_load_all())
        b1 = con.execute("""
            select capacity_area, saleable_area, delivered_home_area, is_completed
            from current_dim_building where building = 'B1'
        """).fetchone()
        self.assertEqual(tuple(Decimal(str(x)) for x in b1[:3]),
                         (Decimal("6000"), Decimal("5700"), Decimal("0")))
        self.assertFalse(b1[3])  # 未竣工
        # 未竣工已预售：许可存在但竣工为否
        permit = con.execute("""
            select count(*) from current_fact_permit
            where building = 'B1'
        """).fetchone()[0]
        self.assertEqual(permit, 1)
        gaps = {r[0] for r in con.execute("""
            select kind from current_fact_unexplained
        """).fetchall()}
        self.assertIn("indicator_gap", gaps)
        self.assertIn("use_mismatch", gaps)  # B2 facility vs residential
        con.close()

    def test_06_land_consideration_never_allocated(self):
        """地价只到宗地，且总额不被关联过程放大。"""
        con, _, _ = self._publish_source(_load_all())
        total = con.execute(
            "select sum(amount_cents) from current_fact_land_consideration").fetchone()[0]
        self.assertEqual(total, 4_800_000_000)
        cols = {r[0] for r in con.execute(
            "describe current_fact_land_consideration").fetchall()}
        self.assertFalse(cols & {"building", "phase", "atom", "scope_id"})
        con.close()

    def test_07_incremental_ingest_is_idempotent_and_versioned(self):
        """重复接收零新增；资料变化后产生新版本，旧版本仍可回查。"""
        files = _load_all()
        con, batch1, rel1 = self._publish_source(files)
        self.assertEqual(batch1["new_rows"], 18)

        batch2 = ingest(con, self.src)  # 同一目录再收一次
        self.assertEqual(batch2["new_rows"], 0)
        rel2 = publish(con)
        self.assertEqual(rel2["version"], "r_v2")
        self.assertEqual([r["version"] for r in list_releases(con)], ["r_v1", "r_v2"])

        # 两版校验和一致（输入未变）
        h1 = dict(con.execute("""
            select table_name, sha256 from release_r_v1.release_checksums
        """).fetchall())
        h2 = dict(con.execute("""
            select table_name, sha256 from release_r_v2.release_checksums
        """).fetchall())
        self.assertEqual(h1, h2)

        # 历史版本回查与 current 结果一致
        self.assertEqual(
            q.monthly_supply(con, "r_v1"), q.monthly_supply(con, None))

        # 原始层保留全部接收批次，旧版本原始记录可回溯
        raw_count = con.execute(
            "select count(*) from raw_rights").fetchone()[0]
        self.assertEqual(raw_count, 3)
        con.close()

    def test_08_changed_source_propagates_to_new_release(self):
        """新增一条修订把 RG1 额度降到 8000，争议应扩大且新版本体现。"""
        files = _load_all()
        con, _, rel1 = self._publish_source(files)
        before = con.execute("""
            select disputed_capacity from current_fact_dispute_interval
            where chain_id = 'RG1' and is_current
        """).fetchone()[0]
        self.assertEqual(Decimal(str(before)), Decimal("2000"))

        files["rights"].append({
            "effective": "2026-11-20", "holder": "DEV1", "kind": "amend",
            "parcel": "L1", "reference": "RG3", "right_id": "RG4",
            "total_capacity": "8000", "use": "residential"})
        src2 = self.tmp / "source2"
        _write_source(src2, files)
        ingest(con, src2)
        rel2 = publish(con)
        self.assertEqual(rel2["version"], "r_v2")
        after = con.execute("""
            select disputed_capacity from current_fact_dispute_interval
            where chain_id = 'RG1' and is_current
        """).fetchone()[0]
        self.assertEqual(Decimal(str(after)), Decimal("3000"))
        # 旧版本仍保持 2000
        old = con.execute("""
            select disputed_capacity from release_r_v1.fact_dispute_interval
            where chain_id = 'RG1' and is_current
        """).fetchone()[0]
        self.assertEqual(Decimal(str(old)), Decimal("2000"))
        con.close()

    def test_09_permit_drilldown_links_rights_and_construction(self):
        """许可证穿透可串联权益链、建设楼栋、归属与争议。"""
        con, _, _ = self._publish_source(_load_all())
        detail = q.permit_drilldown(con, "PER1")
        self.assertEqual(detail["permit"]["chain_id"], "RG1")
        self.assertEqual(detail["building"]["building"], "B1")
        self.assertTrue(detail["right_capacity_timeline"])
        self.assertTrue(detail["phase_claims_on_chain"])
        with self.assertRaises(KeyError):
            q.permit_drilldown(con, "NO_SUCH_PERMIT")
        con.close()

    def test_10_exact_arithmetic_no_float_allocation(self):
        """份额缩放走 Fraction/Decimal，不产生二进制浮点尾巴。"""
        self.assertEqual(scale_area(Decimal("1000"), parse_fraction("1/4")),
                         Decimal("250.0000"))
        self.assertEqual(scale_area(Decimal("1000"), parse_fraction("1/3")),
                         Decimal("333.3333"))
        # 原始层读出的面积字段仍是字符串，未被 float 污染
        con, _, _ = self._publish_source(_load_all())
        raw = current_rows(con, "buildings")
        self.assertIsInstance(raw[0]["capacity_area"], str)
        con.close()

    def test_11_over_issued_transfers_block_and_rollback(self):
        """联合开发超发（份额和超过 1/1）必须拦截，且版本整体回滚无残留。"""
        files = _load_all()
        base = dict(files["right_transfers"][0])
        extra1, extra2 = dict(base), dict(base)
        extra1.update(transfer_id="RT2", to_holder="DEV3", share="1/2")
        extra2.update(transfer_id="RT3", to_holder="DEV4", share="1/2")
        files["right_transfers"].extend([extra1, extra2])
        _write_source(self.src, files)
        con = connect(self.db)
        ingest(con, self.src)
        with self.assertRaises(AssertionError) as ctx:
            publish(con)
        self.assertIn("holding", str(ctx.exception))
        self.assertEqual(con.execute(
            "select count(*) from release_registry").fetchone()[0], 0)
        self.assertEqual(con.execute(
            "select count(*) from information_schema.schemata "
            "where schema_name like 'release_r_%'").fetchone()[0], 0)
        con.close()

    def test_12_permit_exceeding_saleable_blocks_release(self):
        """许可面积超过楼栋可售面积（凭空释放供应）必须拦截。"""
        files = _load_all()
        files["permits"][0]["saleable_area"] = "99999"
        _write_source(self.src, files)
        con = connect(self.db)
        ingest(con, self.src)
        with self.assertRaises(AssertionError) as ctx:
            publish(con)
        self.assertIn("permit_area_not_exceed_saleable", str(ctx.exception))
        con.close()

    def test_13_overlapping_assignment_blocks_release(self):
        """楼栋归属区间重叠必须拦截，防止改属后重复计数。"""
        files = _load_all()
        files["assignments"] = [a for a in files["assignments"]
                                if a["record_id"] != "AS2"]
        files["assignments"].append({
            "building": "B2", "phase": "P3", "received": "2026-06-01",
            "record_id": "AS9", "valid_from": "2026-06-01", "valid_to": None})
        _write_source(self.src, files)
        con = connect(self.db)
        ingest(con, self.src)
        with self.assertRaises(AssertionError) as ctx:
            publish(con)
        self.assertIn("building_reassignment", str(ctx.exception))
        con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
