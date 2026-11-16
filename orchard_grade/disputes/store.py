"""争议分析、主管处置与复核工作的持久化（与主仓储同一数据库）。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from orchard_grade.contracts.batches import ContractError, canonical_json, utc_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS dispute_analyses(
    analysis_id TEXT PRIMARY KEY,
    review_key TEXT NOT NULL,
    lot_id TEXT NOT NULL,
    status TEXT NOT NULL,
    disposition TEXT NOT NULL,
    requires_review INTEGER NOT NULL,
    release_allowed INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    analysis_json TEXT NOT NULL,
    computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disputes_lot ON dispute_analyses(lot_id, computed_at);
CREATE TABLE IF NOT EXISTS dispute_dispositions(
    disposition_id TEXT PRIMARY KEY,
    lot_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    disposition_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispositions_lot ON dispute_dispositions(lot_id, decided_at);
CREATE TABLE IF NOT EXISTS dispute_supersessions(
    analysis_id TEXT NOT NULL,
    superseded_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(analysis_id)
);
"""


def prepare_disputes(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


def save_analysis(connection: sqlite3.Connection, analysis: Mapping[str, Any]) -> str:
    prepare_disputes(connection)
    analysis_id = str(analysis["analysis_id"])
    body = canonical_json(analysis)
    row = connection.execute("SELECT input_hash FROM dispute_analyses WHERE analysis_id=?",
                             (analysis_id,)).fetchone()
    if row:
        if row[0] != analysis.get("input_hash"):
            raise ContractError("conflicting dispute analysis identity")
        return analysis_id
    connection.execute("""INSERT INTO dispute_analyses(analysis_id,review_key,lot_id,status,disposition,
                          requires_review,release_allowed,input_hash,analysis_json,computed_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?)""",
                       (analysis_id, analysis.get("review_key", ""), analysis["lot_id"],
                        analysis["status"], analysis["disposition"],
                        int(bool(analysis["requires_review"])), int(bool(analysis["release_allowed"])),
                        analysis["input_hash"], body, utc_now()))
    return analysis_id


def latest_analysis(connection: sqlite3.Connection, lot_id: str) -> dict[str, Any] | None:
    prepare_disputes(connection)
    row = connection.execute("SELECT analysis_json FROM dispute_analyses WHERE lot_id=? ORDER BY computed_at DESC,analysis_id DESC LIMIT 1",
                             (lot_id,)).fetchone()
    return json.loads(row[0]) if row else None


def analysis_rows(connection: sqlite3.Connection, lot_id: str | None = None) -> list[dict[str, Any]]:
    prepare_disputes(connection)
    if lot_id is None:
        rows = connection.execute("SELECT analysis_json FROM dispute_analyses ORDER BY lot_id,computed_at").fetchall()
    else:
        rows = connection.execute("SELECT analysis_json FROM dispute_analyses WHERE lot_id=? ORDER BY computed_at",
                                  (lot_id,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def save_disposition(connection: sqlite3.Connection, disposition: Mapping[str, Any]) -> bool:
    prepare_disputes(connection)
    row = connection.execute("SELECT disposition_json FROM dispute_dispositions WHERE disposition_id=?",
                             (disposition["disposition_id"],)).fetchone()
    if row:
        if canonical_json(json.loads(row[0])) != canonical_json(disposition):
            raise ContractError("conflicting disposition identity")
        return False
    connection.execute("""INSERT INTO dispute_dispositions(disposition_id,lot_id,decision,input_hash,actor,note,
                          decided_at,disposition_json) VALUES(?,?,?,?,?,?,?,?)""",
                       (disposition["disposition_id"], disposition["lot_id"], disposition["decision"],
                        disposition["input_hash"], disposition["actor"], disposition.get("note", ""),
                        disposition["decided_at"], canonical_json(disposition)))
    return True


def dispositions_for_lot(connection: sqlite3.Connection, lot_id: str) -> list[dict[str, Any]]:
    prepare_disputes(connection)
    rows = connection.execute("SELECT disposition_json FROM dispute_dispositions WHERE lot_id=? ORDER BY decided_at,disposition_id",
                              (lot_id,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def disposition_rows(connection: sqlite3.Connection, lot_id: str | None = None) -> list[dict[str, Any]]:
    prepare_disputes(connection)
    if lot_id is None:
        rows = connection.execute("SELECT disposition_json FROM dispute_dispositions ORDER BY lot_id,decided_at").fetchall()
    else:
        rows = connection.execute("SELECT disposition_json FROM dispute_dispositions WHERE lot_id=? ORDER BY decided_at",
                                  (lot_id,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def supersede(connection: sqlite3.Connection, analysis_id: str, superseded_by: str, reason: str) -> None:
    prepare_disputes(connection)
    connection.execute("""INSERT OR IGNORE INTO dispute_supersessions(analysis_id,superseded_by,reason,recorded_at)
                          VALUES(?,?,?,?)""", (analysis_id, superseded_by, reason, utc_now()))
