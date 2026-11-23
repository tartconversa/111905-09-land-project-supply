"""端到端流水线：增量接收 -> 版本发布（内含集成检查）。"""
from __future__ import annotations

from pathlib import Path

from .ingest import connect, ingest
from .publish import publish


def run_all(db_path: str | Path, data_dir: str | Path) -> dict:
    con = connect(db_path)
    try:
        batch = ingest(con, data_dir)
        release = publish(con)
        return {"ingest": batch, "release": release}
    finally:
        con.close()
