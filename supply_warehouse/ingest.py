"""增量接收层。

职责：
1. 按 manifest 校验资料字节哈希与行数（沿用来源契约，不推断业务结论）；
2. 原始对象以哈希去重后追加进 DuckDB 原始层，记录接收批次；
3. 维护每个自然键的最新状态，支持同一来源目录反复增量接收（幂等）。

原始层永不 UPDATE/DELETE 业务字段；删除只在键状态表标记，所有原始记录
（含被后到记录替代的旧版本）都可按批次回查。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .exact import row_hash

# 每类资料的自然键字段；同一自然键的后到记录视为新版本
NATURAL_KEYS: dict[str, str] = {
    "rights": "right_id",
    "scopes": "scope_id",
    "phase_claims": "claim_id",
    "buildings": "building",
    "assignments": "record_id",
    "permits": "permit_id",
    "land_consideration": "payment_id",
    "right_transfers": "transfer_id",
}

# 原始层 DDL：payload 为完整原始 JSON，另附来源元数据
RAW_DDL = """
create table if not exists raw_{name} (
    ingest_batch  bigint not null,
    received_at   varchar not null,
    source_file   varchar not null,
    source_line   integer not null,
    natural_key   varchar not null,
    payload_hash  varchar not null,
    payload       json not null,
    primary key (ingest_batch, source_line)
);
"""

DDL_STATEMENTS = [
    """
    create table if not exists ingest_batches (
        batch_id      bigint primary key,
        received_at   varchar not null,
        source_dir    varchar not null,
        manifest_hash varchar not null,
        file_count    integer not null,
        row_count     integer not null,
        new_rows      integer not null
    );
    """,
    """
    create table if not exists key_state (
        dataset       varchar not null,
        natural_key   varchar not null,
        latest_hash   varchar not null,
        latest_batch  bigint not null,
        is_deleted    boolean not null default false,
        primary key (dataset, natural_key)
    );
    """,
]


def verify_manifest(data_dir: Path) -> tuple[list[dict], str]:
    """校验目录下所有资料的哈希与行数，返回 (清单条目, manifest 自身哈希)。"""
    manifest_path = data_dir / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    entries = []
    for entry in manifest["files"]:
        path = data_dir / entry["path"]
        blob = path.read_bytes()
        digest = hashlib.sha256(blob).hexdigest()
        if digest != entry["sha256"]:
            raise ValueError(f"资料哈希不符：{entry['path']}")
        lines = [ln for ln in blob.decode("utf-8").splitlines() if ln.strip()]
        if len(lines) != entry["rows"]:
            raise ValueError(f"资料行数不符：{entry['path']}")
        entries.append(entry)
    return entries, hashlib.sha256(manifest_bytes).hexdigest()


def connect(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    """打开（不存在则创建）本地 DuckDB 存储。"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    for stmt in DDL_STATEMENTS:
        con.execute(stmt)
    for name in NATURAL_KEYS:
        con.execute(RAW_DDL.format(name=name))
    return con


def _next_batch_id(con: duckdb.DuckDBPyConnection) -> int:
    value = con.execute("select coalesce(max(batch_id), 0) + 1 from ingest_batches").fetchone()[0]
    return int(value)


def current_rows(con: duckdb.DuckDBPyConnection, dataset: str) -> list[dict]:
    """取某资料当前最新版本的原始行（被替代的旧版本不返回）。"""
    table = f"raw_{dataset}"
    rows = con.execute(
        f"""
        select r.payload
        from {table} r
        join key_state k
          on k.dataset = ? and k.natural_key = r.natural_key
         and k.latest_hash = r.payload_hash
        where not k.is_deleted
        order by r.source_file, r.source_line
        """,
        [dataset],
    ).fetchall()
    return [json.loads(item[0]) for item in rows]


def ingest(con: duckdb.DuckDBPyConnection, data_dir: str | Path) -> dict:
    """增量接收一个来源目录，返回批次摘要。重复目录重复运行只产生零新增批次。"""
    data_dir = Path(data_dir)
    entries, manifest_hash = verify_manifest(data_dir)
    batch_id = _next_batch_id(con)
    received_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    total_rows = 0
    new_rows = 0
    for entry in entries:
        dataset = entry["dataset"]
        key_field = NATURAL_KEYS[dataset]
        path = data_dir / entry["path"]
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        for line_no, line in enumerate(lines, 1):
            payload = json.loads(line)
            digest = row_hash(payload)
            natural_key = str(payload[key_field])
            total_rows += 1

            # 哈希去重：完全相同的原始对象不再入库
            seen = con.execute(
                f"select 1 from raw_{dataset} where payload_hash = ? limit 1",
                [digest],
            ).fetchone()
            if seen is None:
                con.execute(
                    f"""
                    insert into raw_{dataset}
                    (ingest_batch, received_at, source_file, source_line,
                     natural_key, payload_hash, payload)
                    values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [batch_id, received_at, entry["path"], line_no,
                     natural_key, digest, json.dumps(payload, ensure_ascii=False)],
                )
                new_rows += 1

            # 键状态指向最新版本（后到即最新，按批次号判定）
            state = con.execute(
                "select latest_batch from key_state where dataset = ? and natural_key = ?",
                [dataset, natural_key],
            ).fetchone()
            if state is None:
                con.execute(
                    """
                    insert into key_state (dataset, natural_key, latest_hash, latest_batch, is_deleted)
                    values (?, ?, ?, ?, false)
                    """,
                    [dataset, natural_key, digest, batch_id],
                )
            elif batch_id >= int(state[0]):
                con.execute(
                    """
                    update key_state
                       set latest_hash = ?, latest_batch = ?, is_deleted = false
                     where dataset = ? and natural_key = ?
                    """,
                    [digest, batch_id, dataset, natural_key],
                )

    con.execute(
        """
        insert into ingest_batches
        (batch_id, received_at, source_dir, manifest_hash, file_count, row_count, new_rows)
        values (?, ?, ?, ?, ?, ?, ?)
        """,
        [batch_id, received_at, str(data_dir), manifest_hash,
         len(entries), total_rows, new_rows],
    )
    return {
        "batch_id": batch_id,
        "received_at": received_at,
        "source_dir": str(data_dir),
        "files": len(entries),
        "rows": total_rows,
        "new_rows": new_rows,
    }
