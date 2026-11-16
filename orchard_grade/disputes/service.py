"""争议复核服务：连接争议分析、复检工单、放行约束与主管处置。

所有写操作都在主仓储的 ``transaction()`` 内完成，保证工单状态、复检凭据、
分析记录和事件链要么一起提交，要么一起回滚。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Sequence

from orchard_grade.contracts.batches import ContractError, DomainRecord, stable_hash, utc_now
from orchard_grade.disputes import analysis as dispute_analysis
from orchard_grade.disputes import context as dispute_context
from orchard_grade.disputes import store as dispute_store
from orchard_grade.inspection import receipts as receipt_store
from orchard_grade.inspection import work_orders
from orchard_grade.operations import packhouse_queue


DISPOSITIONS = {
    "accept_machine": "接受机器判级并结束争议（人工差异记为已知偏差）",
    "accept_human": "接受最新有效人工复检结论",
    "hold": "暂缓放行，保持争议待复核",
    "reinspect": "要求重新人工复检",
    "release_unverified": "主管在无有效人工证据时批准临时放行（事实保留）",
    "release_followup": "已放行果批的后续处置登记",
}
REVISION_KINDS = {"supplement": "补报", "correct": "更正", "withdraw": "撤回"}
SUPERVISOR_DECISIONS = frozenset(DISPOSITIONS)


class DisputeService:
    def __init__(self, repository):
        self.repository = repository
        with repository.connect() as connection:
            dispute_store.prepare_disputes(connection)
            work_orders.prepare_work(connection)
            packhouse_queue.prepare_queue(connection)

    # ------------------------------------------------------------------ queries
    def _records(self, connection: sqlite3.Connection) -> list[DomainRecord]:
        return dispute_context.load_records(connection)

    def _samples(self, connection: sqlite3.Connection,
                 lot_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        return dispute_context.load_samples(connection, lot_ids)

    def _queue_assignments(self, connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        return dispute_context.load_queue(connection)

    def _build(self, connection: sqlite3.Connection, record: DomainRecord,
               records: Sequence[DomainRecord] | None = None,
               ignore_open_work: bool = False) -> Any:
        return dispute_context.analyse(connection, record, records,
                                       include_open_work=not ignore_open_work)

    def analyse(self, lot_id: str) -> dict[str, Any]:
        """Compute and persist the dispute analysis for one lot (read-mostly)."""

        with self.repository.transaction() as connection:
            analysis = self._analyse_and_record(connection, lot_id)
            return analysis.as_record()

    def _analyse_and_record(self, connection: sqlite3.Connection, lot_id: str) -> Any:
        record = self._record(connection, lot_id)
        analysis = self._build(connection, record)
        dispute_store.save_analysis(connection, analysis.as_record())
        if analysis.requires_review and analysis.review_reason:
            self._ensure_work(connection, record, analysis)
        self._append(connection, "dispute.analysed", lot_id,
                     {"analysis_id": analysis.analysis_id, "status": analysis.status,
                      "disposition": analysis.disposition,
                      "requires_review": analysis.requires_review,
                      "release_allowed": analysis.release_allowed,
                      "input_hash": analysis.input_hash})
        return analysis

    def _record(self, connection: sqlite3.Connection, lot_id: str) -> DomainRecord:
        return dispute_context.load_record(connection, lot_id)

    def _append(self, connection: sqlite3.Connection, event_type: str, lot_id: str,
                payload: Mapping[str, Any]) -> None:
        version = self.repository.event_count(lot_id, connection) + 1
        self.repository.append_event(connection, event_type, lot_id, version, payload)

    def board(self) -> dict[str, Any]:
        """Supervisor-facing view: analyses, open work and unresolved releases."""

        with self.repository.connect() as connection:
            analyses = dispute_store.analysis_rows(connection)
            latest: dict[str, dict[str, Any]] = {}
            for row in analyses:
                latest[row["lot_id"]] = row
            work = [dict(row) for row in
                    connection.execute("SELECT * FROM inspection_work ORDER BY work_id")]
            followups = packhouse_queue.followup_rows(connection)
        pending = [row for row in work if row["status"] in work_orders.OPEN_STATES]
        unresolved = [row for row in latest.values()
                      if row.get("post_release", {}).get("armed")
                      and not any(item["lot_id"] == row["lot_id"] for item in followups)]
        return {"lots": sorted(latest.values(), key=lambda item: item["lot_id"]),
                "open_work": pending, "completed_work": [row for row in work if row["status"] == "completed"],
                "post_release_followups": followups, "unresolved_releases": unresolved,
                "counts": {"analysed": len(latest), "open_work": len(pending),
                           "unresolved_releases": len(unresolved)}}

    def trace(self, lot_id: str) -> dict[str, Any]:
        """Full supervisor trail: evidence, revisions, work, analyses, dispositions, release."""

        with self.repository.connect() as connection:
            record = self._record(connection, lot_id)
            receipts = receipt_store.receipts_for_lot(connection, lot_id)
            effective = receipt_store.effective_receipts(connection, lot_id)
            revisions = receipt_store.revisions_for_lot(connection, lot_id)
            evidence = receipt_store.evidence_for_lot(connection, lot_id)
            work = work_orders.work_for_lot(connection, lot_id)
            analyses = dispute_store.analysis_rows(connection, lot_id)
            dispositions = dispute_store.dispositions_for_lot(connection, lot_id)
            queue = connection.execute("SELECT * FROM packhouse_queue WHERE lot_id=?", (lot_id,)).fetchone()
            followups = packhouse_queue.followup_rows(connection, lot_id)
            events = [dict(row) | {"payload": json.loads(row["payload_json"])}
                      for row in connection.execute(
                          "SELECT * FROM events WHERE aggregate_id=? ORDER BY event_id", (lot_id,))]
            current = self._build(connection, record)
            return {"lot_id": lot_id, "state": record.state, "record_version": record.version,
                    "receipts": receipts, "effective_receipts": effective, "revisions": revisions,
                    "evidence": evidence, "work": work,
                    "completed_work_ids": [row["work_id"] for row in work if row["status"] == "completed"],
                    "analyses": analyses, "dispositions": dispositions,
                    "release": dict(queue) if queue else None, "followups": followups,
                    "events": events, "current": current.as_dict()}

    # ---------------------------------------------------------------- revisions
    def revise_inspection(self, revision: receipt_store.InspectionRevision) -> dict[str, Any]:
        """补报、更正或撤回人工结论，并重评受影响部分。"""

        if revision.kind not in REVISION_KINDS:
            raise ContractError("unknown inspection revision kind")
        with self.repository.transaction() as connection:
            inserted = receipt_store.record_revision(connection, revision)
            record = self._record(connection, revision.lot_id)
            self._append(connection, "inspection.revised", revision.lot_id,
                         {"revision_id": revision.revision_id, "kind": revision.kind,
                          "receipt_id": revision.receipt_id,
                          "inspected_grade": revision.inspected_grade})
            # Supersede work that was opened against the now-stale evidence.
            for work in work_orders.work_for_lot(connection, revision.lot_id):
                if work["status"] in work_orders.OPEN_STATES and work["kind"] != "reinspect":
                    work_orders.supersede_work(connection, work["work_id"],
                                               f"人工结论已{REVISION_KINDS.get(revision.kind, revision.kind)}")
            reassessed = self._reassess_affected(connection, record, reason=f"revision {revision.kind}")
            return {"inserted": inserted, "lot_id": revision.lot_id, "affected": reassessed,
                    "analysis": self._build(connection, record).as_record()}

    def _reassess_affected(self, connection: sqlite3.Connection, record: DomainRecord,
                           reason: str) -> list[dict[str, Any]]:
        records = self._records(connection)
        samples = self._samples(connection, [item.record_id for item in records])
        peers = dispute_analysis.affected_lots(record, records, samples)
        existing = {row["analysis_id"] for row in dispute_store.analysis_rows(connection)}
        results: list[dict[str, Any]] = []
        for lot_id in peers:
            peer = next(item for item in records if item.record_id == lot_id)
            would_review = self._build(connection, peer, records, ignore_open_work=True)
            for row in work_orders.work_for_lot(connection, lot_id):
                if row["status"] not in work_orders.OPEN_STATES:
                    continue
                # Open work is bound to the evidence generation it was raised
                # against. Once that generation is superseded (grade corrected,
                # evidence withdrawn, or the stratum no longer disputed), the old
                # order must not linger and block release.
                if row.get("review_key") != would_review.review_key or not would_review.requires_review:
                    why = (f"重评后不再需要复核：{reason}" if not would_review.requires_review
                           else f"证据已更新，旧工单失效：{reason}")
                    work_orders.supersede_work(connection, row["work_id"], why)
            analysis = self._analyse_and_record(connection, lot_id)
            results.append({"lot_id": lot_id, "analysis_id": analysis.analysis_id,
                            "status": analysis.status, "disposition": analysis.disposition,
                            "requires_review": analysis.requires_review,
                            "recomputed": analysis.analysis_id not in existing})
        return results

    def analyse_in(self, connection: sqlite3.Connection, record: DomainRecord) -> Any:
        """Analyse a record inside an existing transaction (used by release/dispatch)."""

        return self._build(connection, record)

    def receive_inspection(self, receipt: receipt_store.InspectionReceipt,
                           revision: receipt_store.InspectionRevision | None = None) -> dict[str, Any]:
        """Record human evidence, an optional revision, and re-assess, atomically."""

        with self.repository.transaction() as connection:
            inserted = self.repository.record_inspection(connection, receipt, revision)
            if inserted:
                self._append(connection, "inspection.recorded", receipt.lot_id,
                             {"receipt_id": receipt.receipt_id,
                              "inspected_grade": receipt.inspected_grade})
                affected = self._reassess_affected(connection, self._record(connection, receipt.lot_id),
                                                   reason="inspection received")
            else:
                affected = []
            return {"inserted": inserted, "lot_id": receipt.lot_id, "affected": affected}

    # ------------------------------------------------------------- work orders
    def open_review(self, lot_id: str, reason: str | None = None, work_id: str | None = None,
                    force: bool = False) -> dict[str, Any]:
        """Turn a dispute finding into claimable work, avoiding duplicate dispatch."""

        with self.repository.transaction() as connection:
            record = self._record(connection, lot_id)
            analysis = self._build(connection, record)
            if not analysis.requires_review and not force:
                raise ContractError("当前没有需要复核的争议，不能重复派发")
            work = self._ensure_work(connection, record, analysis, reason=reason, work_id=work_id)
            return {"work": work, "analysis": self._build(connection, record).as_record()}

    def _ensure_work(self, connection: sqlite3.Connection, record: DomainRecord,
                     analysis: Any, reason: str | None = None,
                     work_id: str | None = None) -> dict[str, Any]:
        rows = work_orders.work_for_lot(connection, record.record_id)
        existing = [row for row in rows if row["status"] in work_orders.OPEN_STATES]
        if existing:
            return existing[0]
        # A completed review supersedes every earlier evidence generation; a new
        # review may only be dispatched when fresh evidence raises a new question.
        completed = [row for row in rows if row["status"] == "completed"]
        if completed:
            latest_completed = max(completed, key=lambda row: str(row.get("updated_at", "")))
            if latest_completed.get("review_key") == analysis.review_key:
                if work_id:
                    raise ContractError("该证据版本已完成复检，无需重复派发")
                return latest_completed
            if not analysis.requires_review:
                if work_id:
                    raise ContractError("该果批已完成复检，且没有新的争议")
                return latest_completed
        kind = "dispute_sampling"
        if analysis.status == "withdrawn":
            kind = "evidence_withdrawn"
        elif analysis.supervisor_decision == "reinspect":
            kind = "reinspect"
        elif analysis.post_release.get("armed"):
            kind = "follow_up"
        work_id = work_id or f"dispute:{record.record_id}:{analysis.analysis_id[:12]}"
        reason = reason or analysis.review_reason or "争议复核"
        work = work_orders.open_work(connection, work_id, record.record_id, reason, kind=kind,
                                     analysis_id=analysis.analysis_id,
                                     input_hash=analysis.input_hash,
                                     review_key=analysis.review_key)
        self._append(connection, "dispute.work_opened", record.record_id,
                     {"work_id": work["work_id"], "kind": kind, "analysis_id": analysis.analysis_id,
                      "reason": reason})
        return work

    def claim(self, work_id: str, worker: str) -> dict[str, Any]:
        with self.repository.transaction() as connection:
            work = work_orders.claim_work(connection, work_id, worker)
            self._append(connection, "dispute.work_claimed", work["lot_id"],
                         {"work_id": work_id, "worker": worker})
            return work

    def complete_review(self, work_id: str, worker: str,
                        receipt: receipt_store.InspectionReceipt,
                        revision: receipt_store.InspectionRevision | None = None) -> dict[str, Any]:
        """Save the human credential and finish work atomically, then re-analyse."""

        with self.repository.transaction() as connection:
            work = work_orders.complete_work(connection, work_id, worker, receipt)
            if revision is not None:
                if revision.lot_id != receipt.lot_id:
                    raise ContractError("revision belongs to another lot")
                receipt_store.record_revision(connection, revision)
            record = self._record(connection, receipt.lot_id)
            self._append(connection, "inspection.recorded", receipt.lot_id,
                         {"work_id": work_id, "receipt_id": receipt.receipt_id,
                          "inspected_grade": receipt.inspected_grade})
            reassessed = self._reassess_affected(connection, record, reason="review completed")
            return {"work": work, "affected": reassessed,
                    "analysis": self._build(connection, record).as_record()}

    def cancel_review(self, work_id: str, why: str) -> bool:
        with self.repository.transaction() as connection:
            work = work_orders.work_row(connection, work_id)
            if work is None:
                raise ContractError("工单不存在")
            cancelled = work_orders.cancel_work(connection, work_id, why)
            if cancelled:
                self._append(connection, "dispute.work_cancelled", work["lot_id"],
                             {"work_id": work_id, "reason": why})
            return cancelled

    # -------------------------------------------------------------- dispositions
    def record_disposition(self, lot_id: str, decision: str, actor: str,
                           note: str = "", disposition_id: str | None = None) -> dict[str, Any]:
        """Supervisor decision bound to the exact analysis inputs it reviewed."""

        if decision not in SUPERVISOR_DECISIONS:
            raise ContractError("unknown supervisor disposition")
        if not actor.strip():
            raise ContractError("处置人不能为空")
        if decision in {"hold", "reinspect"} and not note.strip():
            raise ContractError("暂缓或重新复检必须说明缘由")
        with self.repository.transaction() as connection:
            record = self._record(connection, lot_id)
            analysis = self._build(connection, record)
            existing = [row for row in dispute_store.dispositions_for_lot(connection, lot_id)
                        if row.get("input_hash") == analysis.input_hash and row["decision"] == decision]
            if existing:
                return {"disposition": existing[0], "analysis": self._build(connection, record).as_record(),
                        "work": None}
            disposition = {"disposition_id": disposition_id or _stable_id("disp", lot_id, analysis.input_hash, decision),
                           "lot_id": lot_id, "decision": decision, "actor": actor, "note": note,
                           "analysis_id": analysis.analysis_id, "input_hash": analysis.input_hash,
                           "decided_at": utc_now()}
            dispute_store.save_disposition(connection, disposition)
            self._append(connection, "dispute.disposition", lot_id,
                         {"disposition_id": disposition["disposition_id"], "decision": decision,
                          "actor": actor, "input_hash": analysis.input_hash})
            work = None
            if decision == "reinspect":
                work = self._ensure_work(connection, record, analysis, reason=note or "主管要求重新复检")
            elif decision == "accept_human":
                for row in work_orders.work_for_lot(connection, lot_id):
                    if row["status"] in work_orders.OPEN_STATES:
                        work_orders.cancel_work(connection, row["work_id"], "主管接受人工结论")
            elif decision in {"accept_machine", "hold", "release_unverified", "release_followup"}:
                for row in work_orders.work_for_lot(connection, lot_id):
                    if row["status"] in work_orders.OPEN_STATES and row["kind"] != "reinspect":
                        work_orders.cancel_work(connection, row["work_id"], f"主管处置：{decision}")
            return {"disposition": disposition, "work": work,
                    "analysis": self._build(connection, record).as_record()}

    # ------------------------------------------------------------------ release
    def release_clearance(self, lot_id: str) -> dict[str, Any]:
        """Read-only clearance check used by dispatch and release."""

        with self.repository.connect() as connection:
            record = self._record(connection, lot_id)
            return self._build(connection, record).as_dict()

    def record_followup(self, lot_id: str, action: str, note: str, actor: str,
                        followup_id: str | None = None) -> dict[str, Any]:
        """Register post-release handling without rewriting the release fact."""

        if not actor.strip() or not action.strip():
            raise ContractError("后续处置需说明动作与经办人")
        with self.repository.transaction() as connection:
            record = self._record(connection, lot_id)
            analysis = self._build(connection, record)
            queue = self._queue_assignments(connection).get(lot_id)
            if not queue:
                raise ContractError("果批尚未放行，无需登记后续处置")
            followup_id = followup_id or _stable_id("followup", lot_id, analysis.input_hash, action)
            created = packhouse_queue.record_followup(
                connection, followup_id, lot_id, queue["destination"],
                analysis.human_grade or analysis.machine_grade, action, note, actor, analysis.analysis_id)
            if created:
                self._append(connection, "dispute.followup", lot_id,
                             {"followup_id": followup_id, "destination": queue["destination"],
                              "action": action, "analysis_id": analysis.analysis_id})
            return {"followup_id": followup_id, "created": created, "lot_id": lot_id,
                    "destination": queue["destination"], "action": action}


def check_clearance(clearance, action: str) -> None:
    """Shared gate for release and dispatch.

    Open review work is reported first because it is the actionable blocker;
    an unverified lot is actionable only through an explicit supervisor decision.
    """

    if clearance.release_blockers and clearance.release_blockers != ("stratum_review_required",):
        raise ContractError(f"{action} blocked by dispute review: {clearance.review_reason}")
    if clearance.status == "unverified" and clearance.supervisor_decision not in {
            "release_unverified", "release_followup"}:
        raise ContractError(f"unverified lot requires supervisor disposition before {action}")
    if not clearance.release_allowed:
        raise ContractError(f"{action} blocked by dispute review: {clearance.review_reason}")


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:{stable_hash(list(parts))[:20]}"

