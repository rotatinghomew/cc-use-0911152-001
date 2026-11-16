"""从同一数据库读取争议分析所需的记录、凭据、证据快照与工作状态。

放在独立模块里，避免仓储层（放行/出库）反向依赖服务层。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Sequence

from orchard_grade.contracts.batches import DomainRecord
from orchard_grade.disputes import analysis as dispute_analysis
from orchard_grade.disputes import store as dispute_store
from orchard_grade.inspection import receipts as receipt_store
from orchard_grade.inspection import work_orders
from orchard_grade.operations import packhouse_queue


def load_records(connection: sqlite3.Connection) -> list[DomainRecord]:
    rows = connection.execute("SELECT * FROM records ORDER BY record_id").fetchall()
    return [DomainRecord(row["record_id"], json.loads(row["payload_json"]), row["state"],
                         row["version"], row["occurred_at"], row["source"]) for row in rows]


def load_record(connection: sqlite3.Connection, lot_id: str) -> DomainRecord:
    from orchard_grade.contracts.batches import ContractError
    row = connection.execute("SELECT * FROM records WHERE record_id=?", (lot_id,)).fetchone()
    if row is None:
        raise ContractError("果批不存在")
    return DomainRecord(row["record_id"], json.loads(row["payload_json"]), row["state"],
                        row["version"], row["occurred_at"], row["source"])


def load_samples(connection: sqlite3.Connection,
                 lot_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    return {lot_id: {"receipts": receipt_store.effective_receipts(connection, lot_id),
                     "evidence": receipt_store.evidence_for_lot(connection, lot_id)}
            for lot_id in lot_ids}


def load_queue(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    packhouse_queue.prepare_queue(connection)
    return {row["lot_id"]: dict(row) for row in connection.execute("SELECT * FROM packhouse_queue")}


def load_work(connection: sqlite3.Connection, lot_id: str,
              include_open: bool = True) -> list[dict[str, Any]]:
    work_orders.prepare_work(connection)
    states = "('pending','claimed','completed')" if include_open else "('completed')"
    return [dict(row) for row in connection.execute(
        f"SELECT * FROM inspection_work WHERE lot_id=? AND status IN {states}", (lot_id,))]


def analyse(connection: sqlite3.Connection, record: DomainRecord,
            records: Sequence[DomainRecord] | None = None,
            include_open_work: bool = True) -> dispute_analysis.DisputeAnalysis:
    records = records if records is not None else load_records(connection)
    return dispute_analysis.analyse_lot(
        record, records,
        load_samples(connection, [item.record_id for item in records]),
        open_reviews=load_work(connection, record.record_id, include_open_work),
        dispositions=dispute_store.dispositions_for_lot(connection, record.record_id),
        queue_assignment=load_queue(connection).get(record.record_id))
