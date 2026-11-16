"""人工复检工单：领取与结果凭据属于同一数据库事务。

争议分析生成的复核工作同样落在这里，因此：
* 待复核果批会形成可领取（claim）的工作；
* 同一证据下的已完成复检不会被重复派发；
* 果批资料更正后旧工单转为 superseded，需要重新确认的会生成新工单；
* 工单完成时把复检当时的传感器快照与人工凭据一起写入。
"""

from __future__ import annotations

import json
import sqlite3

from orchard_grade.contracts.batches import ContractError, utc_now
from orchard_grade.inspection.receipts import evidence_snapshot, record_receipt

WORK_STATES = ("pending", "claimed", "completed", "cancelled", "superseded")
WORK_KINDS = ("manual", "dispute_sampling", "evidence_withdrawn", "reinspect", "follow_up")
OPEN_STATES = ("pending", "claimed")


def prepare_work(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS inspection_work(
        work_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL, evidence_hash TEXT NOT NULL,
        reason TEXT NOT NULL, status TEXT NOT NULL, worker TEXT NOT NULL DEFAULT '',
        receipt_id TEXT)""")
    existing = {row[1] for row in connection.execute("PRAGMA table_info(inspection_work)")}
    for column, definition in (("kind", "TEXT NOT NULL DEFAULT 'manual'"),
                               ("analysis_id", "TEXT NOT NULL DEFAULT ''"),
                               ("input_hash", "TEXT NOT NULL DEFAULT ''"),
                               ("review_key", "TEXT NOT NULL DEFAULT ''"),
                               ("updated_at", "TEXT NOT NULL DEFAULT ''"),
                               ("outcome", "TEXT NOT NULL DEFAULT ''")):
        if column not in existing:
            connection.execute(f"ALTER TABLE inspection_work ADD COLUMN {column} {definition}")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_work_lot ON inspection_work(lot_id, status)")


def work_row(connection: sqlite3.Connection, work_id: str) -> dict | None:
    prepare_work(connection)
    row = connection.execute("SELECT * FROM inspection_work WHERE work_id=?", (work_id,)).fetchone()
    return dict(row) if row else None


def work_for_lot(connection: sqlite3.Connection, lot_id: str) -> list[dict]:
    prepare_work(connection)
    return [dict(row) for row in connection.execute(
        "SELECT * FROM inspection_work WHERE lot_id=? ORDER BY work_id", (lot_id,))]


def open_work(connection: sqlite3.Connection, work_id: str, lot_id: str, reason: str,
              kind: str = "manual", analysis_id: str = "", input_hash: str = "",
              review_key: str = "", evidence_hash: str | None = None) -> dict:
    prepare_work(connection)
    if not work_id or not reason.strip():
        raise ContractError("工单及缘由不能为空")
    if kind not in WORK_KINDS:
        raise ContractError("unknown work kind")
    row = connection.execute("SELECT fingerprint FROM records WHERE record_id=?", (lot_id,)).fetchone()
    if row is None:
        raise ContractError("果批不存在")
    evidence_hash = evidence_hash or row[0]
    old = work_row(connection, work_id)
    if old:
        if (old["lot_id"], old["evidence_hash"], old["reason"]) != (lot_id, evidence_hash, reason):
            raise ContractError("工单不能替换果批或依据")
        if old["status"] == "cancelled" and kind != "manual":
            connection.execute("UPDATE inspection_work SET status='pending',worker='',updated_at=? WHERE work_id=?",
                               (utc_now(), work_id))
            return work_row(connection, work_id) or old
        return old
    connection.execute("""INSERT INTO inspection_work(work_id,lot_id,evidence_hash,reason,status,kind,
                          analysis_id,input_hash,review_key,updated_at)
                          VALUES(?,?,?,?, 'pending',?,?,?,?,?)""",
                       (work_id, lot_id, evidence_hash, reason, kind, analysis_id, input_hash,
                        review_key, utc_now()))
    return work_row(connection, work_id) or {}


def claim_work(connection: sqlite3.Connection, work_id: str, worker: str) -> dict:
    prepare_work(connection)
    if not worker.strip():
        raise ContractError("领取人不能为空")
    row = work_row(connection, work_id)
    if row is None or (row["status"] != "pending" and (row["status"], row["worker"]) != ("claimed", worker)):
        raise ContractError("工单不能领取")
    connection.execute("UPDATE inspection_work SET status='claimed',worker=?,updated_at=? WHERE work_id=?",
                       (worker, utc_now(), work_id))
    return work_row(connection, work_id) or row


def complete_work(connection: sqlite3.Connection, work_id: str, worker: str, receipt) -> dict:
    prepare_work(connection)
    row = work_row(connection, work_id)
    if row is None or row["worker"] != worker or receipt.lot_id != row["lot_id"]:
        raise ContractError("工单领取人与果批不匹配")
    if row["status"] == "completed":
        if row["receipt_id"] != receipt.receipt_id:
            raise ContractError("已完成结果不能替换")
        record_receipt(connection, receipt)
        return row
    if row["status"] in {"cancelled", "superseded"}:
        raise ContractError("工单已失效")
    current = connection.execute("SELECT fingerprint FROM records WHERE record_id=?", (row["lot_id"],)).fetchone()
    if row["status"] != "claimed" or current[0] != row["evidence_hash"]:
        raise ContractError("果批在复检期间已更正")
    payload = json.loads(connection.execute("SELECT payload_json FROM records WHERE record_id=?",
                                            (row["lot_id"],)).fetchone()[0])
    for key in ("origin_code", "line_id", "camera_batch"):
        if str(payload.get(key, "")) != getattr(receipt, key):
            raise ContractError("复检凭据来源不匹配")
    snapshot = evidence_snapshot_from_payload(row["lot_id"], payload, row["evidence_hash"],
                                              machine_grade=receipt.machine_grade)
    record_receipt(connection, receipt, evidence=snapshot)
    connection.execute("""UPDATE inspection_work SET status='completed',receipt_id=?,outcome=?,
                          updated_at=? WHERE work_id=?""",
                       (receipt.receipt_id, receipt.inspected_grade, utc_now(), work_id))
    return work_row(connection, work_id) or row


def evidence_snapshot_from_payload(lot_id: str, payload: dict, fingerprint: str,
                                   machine_grade: str | None = None) -> dict:
    from orchard_grade.contracts.batches import DomainRecord
    record = DomainRecord(lot_id, payload, "graded", 1, utc_now(), "work-order")
    snapshot = evidence_snapshot(record)
    snapshot["record_fingerprint"] = fingerprint
    if machine_grade:
        snapshot["claimed_machine_grade"] = machine_grade
    return snapshot


def cancel_work(connection: sqlite3.Connection, work_id: str, why: str) -> bool:
    prepare_work(connection)
    row = work_row(connection, work_id)
    if row is None or row["status"] not in OPEN_STATES:
        return False
    connection.execute("UPDATE inspection_work SET status='cancelled',outcome=?,updated_at=? WHERE work_id=?",
                       (why[:200], utc_now(), work_id))
    return True


def supersede_work(connection: sqlite3.Connection, work_id: str, why: str) -> bool:
    prepare_work(connection)
    row = work_row(connection, work_id)
    if row is None or row["status"] not in OPEN_STATES:
        return False
    connection.execute("UPDATE inspection_work SET status='superseded',outcome=?,updated_at=? WHERE work_id=?",
                       (why[:200], utc_now(), work_id))
    return True


class InspectionWork:
    """手工登记工单的既有接口；争议工作由服务层通过上面的连接级函数创建。"""

    def __init__(self, repository):
        self.repository = repository
        with repository.connect() as connection:
            prepare_work(connection)

    def open(self, work_id, lot_id, reason):
        with self.repository.transaction() as connection:
            return open_work(connection, work_id, lot_id, reason)

    def claim(self, work_id, worker):
        with self.repository.transaction() as connection:
            claim_work(connection, work_id, worker)

    def complete(self, work_id, worker, receipt):
        with self.repository.transaction() as connection:
            complete_work(connection, work_id, worker, receipt)

    def inspect(self):
        with self.repository.connect() as connection:
            prepare_work(connection)
            return [dict(row) for row in connection.execute("SELECT * FROM inspection_work ORDER BY work_id")]
