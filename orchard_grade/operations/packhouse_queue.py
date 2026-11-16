from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Mapping

from orchard_grade.contracts.batches import ContractError, DomainRecord, stable_hash, utc_now
from orchard_grade.inspection.receipts import effective_receipts
from orchard_grade.sensing.fusion import grade_probability


@dataclass(frozen=True)
class QueueAssignment:
    lot_id: str
    destination: str
    reason: str
    evidence_ids: tuple[str, ...]
    record_version: int


def prepare_queue(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS packhouse_queue(
        lot_id TEXT PRIMARY KEY, destination TEXT NOT NULL, reason TEXT NOT NULL,
        evidence_hash TEXT NOT NULL, record_version INTEGER NOT NULL, assigned_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS packhouse_followups(
        followup_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL, destination TEXT NOT NULL,
        recorded_grade TEXT NOT NULL, action TEXT NOT NULL, note TEXT NOT NULL,
        actor TEXT NOT NULL, analysis_id TEXT NOT NULL, recorded_at TEXT NOT NULL
    )""")


def plan_assignment(record: DomainRecord, receipts: list[dict],
                    clearance: Mapping[str, object] | None = None) -> QueueAssignment:
    if record.state == "recalled":
        return QueueAssignment(record.record_id, "hold", "recalled lot", (), record.version)
    if record.state != "released":
        raise ContractError("packhouse queue accepts released lots only")
    if any(receipt["lot_id"] != record.record_id for receipt in receipts):
        raise ContractError("inspection evidence belongs to another lot")
    if clearance is not None:
        blockers = tuple(clearance.get("release_blockers") or ())
        if blockers or clearance.get("release_allowed") is False:
            detail = ", ".join(str(item) for item in blockers) or str(clearance.get("review_reason", ""))
            raise ContractError(f"lot is under dispute review: {detail}")
        status = str(clearance.get("status", ""))
        decision = str(clearance.get("supervisor_decision", ""))
        if status == "unverified" and decision not in {"release_unverified", "release_followup"}:
            raise ContractError("unverified lot requires supervisor disposition before dispatch")
    effective = [receipt for receipt in receipts if not receipt.get("withdrawn")]
    if effective:
        latest = sorted(effective, key=lambda item: (item["inspected_at"], item["receipt_id"]))[-1]
        reason = "latest human inspection"
        if clearance is not None and clearance.get("supervisor_decision") in {"release_followup", "release_unverified"}:
            reason = f"supervisor {clearance['supervisor_decision']}; latest human inspection"
        return QueueAssignment(record.record_id, latest["inspected_grade"], reason,
                               (latest["receipt_id"],), record.version)
    if receipts:
        raise ContractError("all human inspection evidence has been withdrawn; release blocked")
    probabilities = grade_probability(record)
    destination = max(probabilities, key=probabilities.__getitem__)
    return QueueAssignment(record.record_id, destination,
                           "machine grade; no human inspection; provisional pending supervisor release",
                           (), record.version)


def assign_released_lot(connection: sqlite3.Connection, record: DomainRecord,
                        clearance: Mapping[str, object] | None = None) -> QueueAssignment:
    prepare_queue(connection)
    assignment = plan_assignment(record, effective_receipts(connection, record.record_id), clearance)
    evidence_hash = stable_hash({"record": record.fingerprint, "receipts": assignment.evidence_ids})
    existing = connection.execute("SELECT destination,reason,evidence_hash,record_version FROM packhouse_queue WHERE lot_id=?",
                                  (record.record_id,)).fetchone()
    if existing:
        # Replaying the same facts is idempotent even if audit timestamps moved.
        if (existing["destination"], existing["evidence_hash"]) != (assignment.destination, evidence_hash) \
                or existing["record_version"] != record.version:
            raise ContractError("existing dispatch cannot be silently replaced")
        return assignment
    connection.execute("INSERT INTO packhouse_queue VALUES(?,?,?,?,?,?)",
                       (record.record_id, assignment.destination, assignment.reason,
                        evidence_hash, record.version, utc_now()))
    return assignment


def record_followup(connection: sqlite3.Connection, followup_id: str, lot_id: str, destination: str,
                    recorded_grade: str, action: str, note: str, actor: str, analysis_id: str) -> bool:
    prepare_queue(connection)
    row = connection.execute("SELECT 1 FROM packhouse_followups WHERE followup_id=?", (followup_id,)).fetchone()
    if row:
        return False
    connection.execute("""INSERT INTO packhouse_followups VALUES(?,?,?,?,?,?,?,?,?)""",
                       (followup_id, lot_id, destination, recorded_grade, action, note,
                        actor, analysis_id, utc_now()))
    return True


def followup_rows(connection: sqlite3.Connection, lot_id: str | None = None) -> list[dict]:
    prepare_queue(connection)
    if lot_id is None:
        rows = connection.execute("SELECT * FROM packhouse_followups ORDER BY recorded_at, followup_id").fetchall()
    else:
        rows = connection.execute("SELECT * FROM packhouse_followups WHERE lot_id=? ORDER BY recorded_at, followup_id",
                                  (lot_id,)).fetchall()
    return [dict(row) for row in rows]
