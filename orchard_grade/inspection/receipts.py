from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from orchard_grade.contracts.batches import ContractError, DomainRecord, canonical_json, stable_hash, utc_now
from orchard_grade.sensing.fusion import grade_probability

GRADES = ("premium", "standard", "processing")
REVISION_KINDS = ("supplement", "withdraw", "correct")
SENSOR_FIELDS = ("sweetness_brix", "diameter_mm", "mass_g", "skin_color", "bruise_area",
                 "internal_void", "ripeness", "harvest_age_h", "camera_coverage", "ct_confidence")


def evidence_snapshot(record: DomainRecord, machine_grade: str | None = None) -> dict[str, Any]:
    """Freeze the sensor readings and machine judgement that existed at inspection time.

    Dispute analysis must compare against the machine decision *as it was*, not a
    later re-score of an edited record. ``machine_grade`` is normally the grade the
    inspector saw on the machine terminal; the derived probabilities still come
    from the archived sensor payload.
    """

    probabilities = grade_probability(record)
    sensors = {field: record.payload.get(field) for field in SENSOR_FIELDS
               if field in record.payload}
    return {"record_version": record.version, "record_fingerprint": record.fingerprint,
            "machine_grade": machine_grade or max(GRADES, key=lambda grade: probabilities[grade]),
            "probabilities": probabilities, "sensors": sensors}


@dataclass(frozen=True)
class InspectionReceipt:
    receipt_id: str
    lot_id: str
    origin_code: str
    line_id: str
    camera_batch: str
    inspected_grade: str
    machine_grade: str
    sampling_reason: str
    inspected_at: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) for value in self.__dict__.values()):
            raise ContractError("inspection receipt fields must be text")
        if not all((self.receipt_id, self.lot_id, self.origin_code, self.line_id,
                    self.camera_batch, self.inspected_grade, self.machine_grade,
                    self.sampling_reason, self.inspected_at)):
            raise ContractError("inspection receipt requires evidence identity and sampling provenance")
        if self.inspected_grade not in GRADES:
            raise ContractError("unknown inspected grade")
        if self.machine_grade not in GRADES:
            raise ContractError("unknown machine grade")

    @property
    def fingerprint(self) -> str:
        return stable_hash(self.__dict__)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class InspectionRevision:
    """Append-only correction of a human inspection conclusion.

    补报/更正使用 ``supplement``/``correct`` 并携带新的 ``inspected_grade``；
    撤回使用 ``withdraw``。原始凭据永不覆盖，因此复检历史可追溯。
    """

    revision_id: str
    receipt_id: str
    lot_id: str
    kind: str
    inspected_grade: str
    note: str
    actor: str
    revised_at: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) for value in self.__dict__.values()):
            raise ContractError("inspection revision fields must be text")
        if not all((self.revision_id, self.receipt_id, self.lot_id, self.kind,
                    self.actor, self.revised_at)):
            raise ContractError("inspection revision requires identity, kind, actor and timestamp")
        if self.kind not in REVISION_KINDS:
            raise ContractError("unknown inspection revision kind")
        if self.kind == "withdraw":
            if self.inspected_grade:
                raise ContractError("withdrawal cannot carry a grade")
        elif self.inspected_grade not in GRADES:
            raise ContractError("unknown revised inspected grade")

    @property
    def fingerprint(self) -> str:
        return stable_hash(self.__dict__)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def prepare_receipts(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS inspection_receipts(
        receipt_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL, origin_code TEXT NOT NULL,
        line_id TEXT NOT NULL, camera_batch TEXT NOT NULL, inspected_grade TEXT NOT NULL,
        machine_grade TEXT NOT NULL, sampling_reason TEXT NOT NULL,
        inspected_at TEXT NOT NULL, receipt_json TEXT NOT NULL, fingerprint TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_receipts_lot ON inspection_receipts(lot_id, inspected_at)")
    connection.execute("""CREATE TABLE IF NOT EXISTS inspection_evidence(
        receipt_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL, record_version INTEGER NOT NULL,
        record_fingerprint TEXT NOT NULL, machine_grade TEXT NOT NULL,
        probabilities_json TEXT NOT NULL, sensor_json TEXT NOT NULL,
        captured_at TEXT NOT NULL
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_evidence_lot ON inspection_evidence(lot_id)")
    prepare_revisions(connection)


def prepare_revisions(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS inspection_receipt_revisions(
        revision_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL, lot_id TEXT NOT NULL,
        kind TEXT NOT NULL, inspected_grade TEXT NOT NULL, note TEXT NOT NULL,
        actor TEXT NOT NULL, revised_at TEXT NOT NULL, revision_json TEXT NOT NULL,
        fingerprint TEXT NOT NULL, recorded_at TEXT NOT NULL,
        FOREIGN KEY(receipt_id) REFERENCES inspection_receipts(receipt_id)
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_revisions_lot ON inspection_receipt_revisions(lot_id, revised_at)")


def record_receipt(connection: sqlite3.Connection, receipt: InspectionReceipt,
                   evidence: Mapping[str, Any] | None = None) -> bool:
    prepare_receipts(connection)
    row = connection.execute("SELECT fingerprint FROM inspection_receipts WHERE receipt_id=?",
                             (receipt.receipt_id,)).fetchone()
    if row:
        if row[0] != receipt.fingerprint:
            raise ContractError("conflicting inspection receipt identity")
        # Evidence is frozen at first recording; replays must not re-derive it.
        return False
    connection.execute("""INSERT INTO inspection_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (receipt.receipt_id, receipt.lot_id, receipt.origin_code, receipt.line_id,
                        receipt.camera_batch, receipt.inspected_grade, receipt.machine_grade,
                        receipt.sampling_reason, receipt.inspected_at, canonical_json(receipt.__dict__),
                        receipt.fingerprint, utc_now()))
    if evidence is not None:
        _record_evidence(connection, receipt, evidence)
    return True


def _record_evidence(connection: sqlite3.Connection, receipt: InspectionReceipt,
                     evidence: Mapping[str, Any]) -> None:
    row = connection.execute("SELECT record_fingerprint FROM inspection_evidence WHERE receipt_id=?",
                             (receipt.receipt_id,)).fetchone()
    fingerprint = str(evidence.get("record_fingerprint", ""))
    if row:
        if row[0] != fingerprint:
            raise ContractError("inspection evidence conflicts with stored snapshot")
        return
    connection.execute("""INSERT INTO inspection_evidence VALUES(?,?,?,?,?,?,?,?)""",
                       (receipt.receipt_id, receipt.lot_id, int(evidence.get("record_version", 0)),
                        fingerprint, str(evidence.get("machine_grade", receipt.machine_grade)),
                        canonical_json(dict(evidence.get("probabilities") or {})),
                        canonical_json(dict(evidence.get("sensors") or {})), utc_now()))


def record_revision(connection: sqlite3.Connection, revision: InspectionRevision) -> bool:
    prepare_revisions(connection)
    receipt = connection.execute("SELECT lot_id FROM inspection_receipts WHERE receipt_id=?",
                                 (revision.receipt_id,)).fetchone()
    if receipt is None:
        raise ContractError("inspection receipt does not exist")
    if receipt[0] != revision.lot_id:
        raise ContractError("inspection revision belongs to another lot")
    row = connection.execute("SELECT fingerprint FROM inspection_receipt_revisions WHERE revision_id=?",
                             (revision.revision_id,)).fetchone()
    if row:
        if row[0] != revision.fingerprint:
            raise ContractError("conflicting inspection revision identity")
        return False
    connection.execute("""INSERT INTO inspection_receipt_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (revision.revision_id, revision.receipt_id, revision.lot_id, revision.kind,
                        revision.inspected_grade, revision.note, revision.actor, revision.revised_at,
                        canonical_json(revision.__dict__), revision.fingerprint, utc_now()))
    return True


def receipts_for_lot(connection: sqlite3.Connection, lot_id: str) -> list[dict[str, Any]]:
    prepare_receipts(connection)
    rows = connection.execute("SELECT receipt_json FROM inspection_receipts WHERE lot_id=? ORDER BY inspected_at,receipt_id",
                              (lot_id,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def evidence_for_lot(connection: sqlite3.Connection, lot_id: str) -> dict[str, dict[str, Any]]:
    prepare_receipts(connection)
    rows = connection.execute("SELECT * FROM inspection_evidence WHERE lot_id=?", (lot_id,)).fetchall()
    return {row["receipt_id"]: {"record_version": row["record_version"],
                                "record_fingerprint": row["record_fingerprint"],
                                "machine_grade": row["machine_grade"],
                                "probabilities": json.loads(row["probabilities_json"]),
                                "sensors": json.loads(row["sensor_json"]),
                                "captured_at": row["captured_at"]} for row in rows}


def revisions_for_lot(connection: sqlite3.Connection, lot_id: str) -> list[dict[str, Any]]:
    prepare_revisions(connection)
    rows = connection.execute("SELECT revision_json FROM inspection_receipt_revisions WHERE lot_id=? ORDER BY revised_at,revision_id",
                              (lot_id,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def effective_receipts(connection: sqlite3.Connection, lot_id: str) -> list[dict[str, Any]]:
    """Return base receipts with append-only revisions applied.

    Withdrawn receipts stay in the result with ``withdrawn=True`` so the
    withdrawal itself remains auditable; they never count as human evidence.
    """

    revisions = revisions_for_lot(connection, lot_id)
    by_receipt: dict[str, list[dict[str, Any]]] = {}
    for revision in revisions:
        by_receipt.setdefault(revision["receipt_id"], []).append(revision)
    result: list[dict[str, Any]] = []
    for base in receipts_for_lot(connection, lot_id):
        effective = dict(base)
        applied = by_receipt.get(base["receipt_id"], [])
        withdrawn = False
        for revision in applied:
            if revision["kind"] == "withdraw":
                withdrawn = True
            else:
                withdrawn = False
                if revision["inspected_grade"]:
                    effective["inspected_grade"] = revision["inspected_grade"]
            effective.setdefault("revision_ids", []).append(revision["revision_id"])
            effective.setdefault("revision_notes", []).append(revision["note"])
        effective["withdrawn"] = withdrawn
        effective["effective"] = not withdrawn
        effective["effective_fingerprint"] = stable_hash({
            "base": base.get("receipt_id"), "grade": effective["inspected_grade"],
            "withdrawn": withdrawn, "revisions": [item["revision_id"] for item in applied]})
        result.append(effective)
    return result
