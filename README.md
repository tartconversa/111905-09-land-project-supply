# 土地开发权益到住房供应的形成仓

宗地用途权益、开发分期、建设兑现和住房许可范围的本地数仓。原始资料为
`data/` 下的合成 NDJSON，范围标识、用途与计量单位见 `docs/source-contract.md`；
仓内建模、口径与操作见 `docs/warehouse-design.md`。

## 能力

- **增量接收**：字节哈希校验 + 行级哈希去重，原始对象只追加、按批次可回查，重复接收幂等；
- **范围关联**：scope 覆盖原子展开、修订链额度时间线、联合开发份额时间线、
  分期申报争议单列（不先到先得）、楼栋归属 SCD 共同改判；
- **版本发布**：不可变 `release_r_vN` schema，17 项集成检查全过才置 current，
  失败整体回滚；每表附 SHA256 校验和，历史版本可回查；
- **下钻查询**：月度住宅供给（许可/交付口径分列）、许可证穿透权益与建设事实、
  原子余额调节、未解释规模、地价宗地事实与口径提示；
- **口径约束**：capacity_area / saleable_area / delivered_home_area 三指标分离，
  争议与残留不分摊、地价不下推，所有不可相加规则登记在 `caliber_registry`。

## 环境

Python 3.11，依赖见 `requirements.txt`（DuckDB 1.4.1）。

    python3 -m pip install -r requirements.txt
    python3 -m pip install -e .

## 运行

    # 一键：增量接收 + 集成检查 + 版本发布
    python3 -I -m supply_warehouse.cli run --data-dir ./data --db ./output/supply.duckdb

    # 下钻查询
    python3 -I -m supply_warehouse.cli query monthly     --db ./output/supply.duckdb
    python3 -I -m supply_warehouse.cli query permit PER1 --db ./output/supply.duckdb
    python3 -I -m supply_warehouse.cli query snapshot    --db ./output/supply.duckdb
    python3 -I -m supply_warehouse.cli query releases    --db ./output/supply.duckdb

    # 全量测试（来源资料检查 + 13 项仓内集成测试，含联合开发超发、
    # 跨宗地重复主张、楼栋改属、增量幂等与版本回查）
    python3 -I -m unittest discover -s tests -v

## 来源资料检查（不含业务结论）

    python3 tools/inspect_source.py

`data/` 内均为本地合成记录；`output/` 与 `*.duckdb` 已在 `.gitignore` 忽略。
