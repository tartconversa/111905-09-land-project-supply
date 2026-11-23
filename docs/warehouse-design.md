# 供应形成数仓设计说明

把土地宗地权益、项目分期申报、楼栋建设兑现与住房销售许可串成一条可回查、
可增量、带口径约束的形成链。原始资料的范围标识、用途与计量单位以
`docs/source-contract.md` 为准，本文只定义入仓后的事实结构与关联规则。

## 1. 分层架构

```
data/*.ndjson (合成来源，每批次独立目录，只读)
      │  字节哈希 + 行数 + 字段形状校验（ingest.verify_manifest）
      ▼
原始层 raw_*（DuckDB，只追加；payload 原文 JSON + 行哈希 + 接收批次）
      │  key_state 维护自然键最新版本；同对象哈希去重，幂等
      ▼
建模层 transform.build_model（纯函数；Fraction/Decimal 精确运算，不写库）
      ▼
发布层 release_r_vN（不可变 schema，14 张表 + 校验和 + 口径登记）
      │  17 项集成检查全部通过才置 current；失败整体 drop 回滚
      ▼
current_* 视图 + 查询层 query（月度比较、许可证穿透、余额调节、版本回查）
```

- 原始层永不更新业务字段：被替代的旧版本随批次保留，可按 `ingest_batch` 回查。
- 发布版本不可变：新版本写新 schema，`release_registry` 记录 current 指针，
  历史版本用 `--release r_v1` 回查；每表有全量行 SHA256 校验和。
- 脚本目录与资料目录分离，所有运行使用 `python3 -I`，来源路径只作参数传入。

## 2. 关键业务规则的落点

### 2.1 权益修订（amend）是替代不是新增

`rights` 中 `kind=amend` 的记录沿 `reference` 追溯链根（RG3→RG1），
形成 `fact_right_capacity_timeline`：同一权益链的额度区间左闭右开、首尾相接，
任一时刻只有一条 `is_current`。本例 RG1 链：10000（至 2026-11-05）→ 9000（至今），
额度不允许跨区间求和（口径登记已标注）。

### 2.2 重复主张单列争议，不先到先得

`phase_claims` 按 scope 的 coverage 原子展开后，在 `(权益链, 原子)` 上累计主张，
与当日适用额度比较，写入 `fact_dispute_interval`：

| 区间 | 累计主张 | 适用额度 | 争议 |
|---|---|---|---|
| 2026-03-01 → 11-01 | 7000 | 10000 | 0 |
| 2026-11-01 → 11-05 | 11000 | 10000 | 1000 |
| 2026-11-05 → 至今 | 11000 | 9000 | 2000 |

只要原子进入过争议区间，该原子上的**所有**申报（含先到的 CL1）在
`fact_claim_resolution` 中都标 `disputed`，支持面积记 0，争议额度不分配给任何一方。
`supported + disputed = claimed` 由集成检查 C4 强制配平。

### 2.3 联合开发只转移份额，不释放额度

`right_transfers` 在 `fact_holding_timeline` 中按日期净额结算生成
`(链, 原子, 持有人)` 份额时间线。RT1 生效后 RG2/B：DEV1=3/4、DEV2=1/4，
各时点份额和恒等 1/1，权益总额度 6000 不变。同日多份转让先净额再切轴，
超发（负份额或份额和≠1）由 C1 检查拦截并回滚发布。

### 2.4 三个面积指标全程分离

`capacity_area`（容积率口径）、`saleable_area`（可售口径）、
`delivered_home_area`（交付口径）分别建列，绝不互换或相加成一个“进度”。
两者之差与用途冲突进入 `fact_unexplained`：

- B1：6000 vs 5700，300 ㎡为占用额度但不形成可售房屋的差异；
- B2：facility 用途建在 residential 权益链上，1000 ㎡用途不匹配 + 指标差，
  只登记不分摊。

### 2.5 楼栋改属共同改判

`assignments` 是带有效期的 SCD（AS1 P1 至 11-03，AS2 P2 起）。
建设事实、许可事实的 `phase` 统一取楼栋当前归属，集成检查 C7/C8 断言
归属区间无重叠、无空档、当前唯一且下游事实全部跟随。B2 改属后在
建设、许可、余额调节中一律归 P2。

### 2.6 地价禁止下推

`fact_land_consideration` 只保留宗地粒度（LP1/LP2，分与元双列），
表结构上不存在 building/phase/atom/scope 列（C12 结构断言）。
余额调节表里的 residual 是未解释差额，不乘任何平均比例。

## 3. 事实表清单与粒度

| 表 | 粒度 | 主要可加指标 |
|---|---|---|
| dim_right | 权益记录 | total_capacity（快照，不跨期加） |
| fact_right_capacity_timeline | 链×生效区间 | 同时点每链一条 |
| fact_holding_timeline | 链×原子×持有人×区间 | share（时点内和=1） |
| fact_claim_resolution | 申报×原子 | claimed/supported/disputed |
| fact_dispute_interval | 链×原子×时间区间 | disputed_capacity |
| dim_building | 楼栋 | 三面积指标分列 |
| fact_construction | 楼栋 | 承诺/在建兑现 |
| fact_assignment | 楼栋×归属区间 | 关系，无度量 |
| fact_permit | 许可证×楼栋 | saleable_area（许可口径） |
| fact_delivered | 竣工楼栋 | delivered_home_area（交付口径） |
| fact_monthly_supply | 月×事件类型×楼栋 | area（同 event_type 内可加） |
| fact_atom_balance | 原子 | 额度/申报/建设/许可/交付/残留 |
| fact_unexplained | 未解释项 | area（不进供给口径） |
| fact_land_consideration | 宗地×付款 | amount_cents/amount_yuan |

每张事实表的可加范围与禁止相加原因都在发布版本的 `caliber_registry` 中登记，
可通过 `query calibers` 读取。

## 4. 操作手册

```bash
python3 -m pip install -r requirements.txt
python3 -m pip install -e .        # 供 -I 隔离模式导入本仓包

# 一键：增量接收 + 集成检查 + 版本发布
python3 -I -m supply_warehouse.cli run --data-dir ./data --db ./output/supply.duckdb

# 分步
python3 -I -m supply_warehouse.cli ingest  --data-dir ./data --db ./output/supply.duckdb
python3 -I -m supply_warehouse.cli publish --db ./output/supply.duckdb
python3 -I -m supply_warehouse.cli checks  --db ./output/supply.duckdb

# 下钻查询
python3 -I -m supply_warehouse.cli query monthly      --db ./output/supply.duckdb
python3 -I -m supply_warehouse.cli query permit PER1  --db ./output/supply.duckdb
python3 -I -m supply_warehouse.cli query snapshot     --db ./output/supply.duckdb
python3 -I -m supply_warehouse.cli query balance      --db ./output/supply.duckdb
python3 -I -m supply_warehouse.cli query unexplained  --db ./output/supply.duckdb
python3 -I -m supply_warehouse.cli query calibers     --db ./output/supply.duckdb
python3 -I -m supply_warehouse.cli query releases     --db ./output/supply.duckdb
# 历史版本回查（许可证/月度/余额等均支持）
python3 -I -m supply_warehouse.cli query monthly --release r_v1 --db ./output/supply.duckdb

# 测试：13 项仓内集成测试 + 3 项来源资料测试
python3 -I -m unittest discover -s tests -v
```

## 5. 增量接收语义

1. 来源目录先过 manifest 哈希/行数校验，任何字节不符直接拒收；
2. 每行计算规范 JSON 哈希，已存在的对象跳过（重复目录重复接收 `new_rows=0`）；
3. 自然键的新版本更新 `key_state` 指针，旧原文保留在 `raw_*`；
4. 每次接收产生 `ingest_batches` 记录；每次发布产生不可变 `release_r_vN`；
5. 输入未变时重发版本的表校验和与上一版逐表一致，可用于证明幂等。

## 6. 集成检查（17 项，任一失败即回滚发布）

份额守恒与无负份额/重复分段、每链当前额度唯一、额度时间线无重叠、
申报三口径配平、争议不先到先得、争议不越界、楼栋归属无重叠/空档/当前唯一、
建设与许可跟随改属、许可不超过可售面积、交付只来自竣工、用途冲突必须登记、
地价无下推列、口径登记全覆盖、月度供给口径隔离。
