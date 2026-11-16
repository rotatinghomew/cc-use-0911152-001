"""争议分析、复核工单与放行约束的行为测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchard_grade.application import Application
from orchard_grade.contracts.batches import ContractError, DomainRecord
from orchard_grade.inspection.receipts import InspectionReceipt, InspectionRevision, effective_receipts


def payload(**overrides):
    base = {"origin_code": "north", "line_id": "line-a", "camera_batch": "camera-1",
            "category": "apple", "sweetness_brix": 19, "diameter_mm": 72, "mass_g": 105,
            "bruise_area": 1, "camera_coverage": 0.9, "internal_void": 1, "ct_confidence": 0.9,
            "ripeness": 0.8, "harvest_age_h": 12}
    base.update(overrides)
    return base


class DisputeCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "disputes.sqlite3"
        self.application = Application(self.database)

    def add_lot(self, lot_id, **overrides):
        record = DomainRecord(lot_id, payload(**overrides))
        self.application.repository.save_record(record)
        return record

    def release(self, lot_id, version=2):
        record = self.application.repository.get_record(lot_id)
        released = DomainRecord(record.record_id, record.payload, "released", version,
                                record.occurred_at, record.source)
        self.application.repository.save_record(released, expected_version=record.version)
        return released

    def receipt(self, receipt_id, lot_id="lot-1", grade="processing", machine="premium",
                reason="random", at="2026-11-15T10:00:00Z", **overrides):
        values = {"lot_id": lot_id, "origin_code": "north", "line_id": "line-a",
                  "camera_batch": "camera-1", "inspected_grade": grade, "machine_grade": machine,
                  "sampling_reason": reason, "inspected_at": at}
        values.update(overrides)
        return InspectionReceipt(receipt_id, **values)


class DisputeAnalysisTests(DisputeCase):
    def test_analysis_cites_sensor_snapshot_and_machine_judgement(self):
        self.add_lot("lot-1")
        self.add_lot("lot-2")
        self.application.receive_inspection(self.receipt("insp-1"))
        snapshot = self.application.disputes.trace("lot-1")["evidence"]["insp-1"]
        self.assertEqual(snapshot["record_version"], 1)
        self.assertIn("sweetness_brix", snapshot["sensors"])
        self.assertTrue(snapshot["probabilities"])
        analysis = self.application.analyse_disputes("lot-1")
        self.assertEqual(analysis["status"], "disagreement")
        self.assertEqual(analysis["machine_grade"], "premium")
        self.assertEqual(analysis["human_grade"], "processing")
        self.assertEqual(analysis["evidence_ids"], ["insp-1"])
        self.assertEqual(analysis["severity"], "severe")
        self.assertTrue(any("传感器" in text for text in analysis["explanations"]))

    def test_analysis_uses_snapshot_even_after_lot_payload_changes(self):
        record = self.add_lot("lot-1")
        self.application.receive_inspection(self.receipt("insp-1"))
        self.application.repository.save_record(
            DomainRecord(record.record_id, payload(sweetness_brix=2), record.state, 2,
                         record.occurred_at, record.source), expected_version=1)
        analysis = self.application.analyse_disputes("lot-1")
        self.assertEqual(analysis["machine_source"], "evidence_snapshot")
        self.assertEqual(analysis["machine_grade"], "premium")

    def test_unsampled_lot_is_never_labelled_pass_or_fail(self):
        self.add_lot("lot-1")
        self.add_lot("lot-2")
        self.application.receive_inspection(self.receipt("insp-1"))
        analysis = self.application.analyse_disputes("lot-2")
        self.assertEqual(analysis["status"], "unverified")
        self.assertNotIn(analysis["disposition"], {"pass", "fail", "approved", "rejected"})
        self.assertEqual(analysis["human_grade"], "")
        self.assertTrue(any("未人工抽检" in text for text in analysis["explanations"]))
        self.assertTrue(any("不作合格或不合格判定" in text for text in analysis["explanations"]))

    def test_sampling_bias_and_confidence_are_reported(self):
        for index in range(6):
            self.add_lot(f"lot-{index + 1}")
        self.application.receive_inspection(self.receipt("insp-1", reason="suspected defect"))
        analysis = self.application.analyse_disputes("lot-1")
        self.assertEqual(analysis["sampling"]["method"], "targeted")
        self.assertTrue(analysis["sampling"]["selection_bias"])
        self.assertFalse(analysis["sampling"]["generalizable"])
        self.assertIn(analysis["confidence"], {"high", "medium", "low"})
        self.assertGreater(analysis["uncertainty"], 0.0)

    def test_small_random_sample_is_not_generalizable(self):
        for index in range(8):
            self.add_lot(f"lot-{index + 1}")
        for index in range(1, 4):
            self.application.receive_inspection(self.receipt(f"insp-{index}", lot_id=f"lot-{index}"))
        analysis = self.application.analyse_disputes("lot-1")
        self.assertEqual(analysis["sampling"]["method"], "random")
        self.assertFalse(analysis["sampling"]["sample_adequate"])
        self.assertFalse(analysis["sampling"]["generalizable"])

    def test_strata_cover_origin_category_and_device(self):
        self.add_lot("lot-1")
        self.add_lot("lot-2")
        self.add_lot("lot-3", origin_code="south", category="pear", line_id="line-b",
                     camera_batch="camera-2")
        for index in (1, 2):
            self.application.receive_inspection(self.receipt(f"insp-{index}", lot_id=f"lot-{index}"))
        analysis = self.application.analyse_disputes("lot-1")
        dimensions = {item["dimension"] for item in analysis["strata"]}
        self.assertEqual(dimensions, {"origin", "category", "device"})
        north = next(item for item in analysis["strata"]
                     if item["dimension"] == "origin" and item["value"] == "north")
        self.assertEqual(north["sampled_count"], 2)
        self.assertEqual(north["disagreement_count"], 2)
        self.assertTrue(north["alert"])
        self.assertIn(north["direction"], {"machine_over_grades", "machine_under_grades", "mixed"})
        self.assertLessEqual(north["lower_bound"], north["upper_bound"])

    def test_missing_category_is_stated_as_extra_uncertainty(self):
        record = DomainRecord("lot-1", {key: value for key, value in payload().items() if key != "category"})
        self.application.repository.save_record(record)
        self.application.receive_inspection(self.receipt("insp-1"))
        analysis = self.application.analyse_disputes("lot-1")
        self.assertTrue(any("品类" in text and "未分类" in text for text in analysis["explanations"]))


class ReviewWorkTests(DisputeCase):
    def setUp(self):
        super().setUp()
        for index in range(1, 4):
            self.add_lot(f"lot-{index}")
        for index in (1, 2):
            self.application.receive_inspection(self.receipt(f"insp-{index}", lot_id=f"lot-{index}"))

    def test_stratum_alert_creates_claimable_work_for_unsampled_lot(self):
        # New evidence opens the peer's review work automatically, before anyone
        # asks for a report.
        auto = [row for row in self.application.inspection_work.inspect()
                if row["lot_id"] == "lot-3"]
        self.assertEqual(len(auto), 1)
        self.assertEqual(auto[0]["status"], "pending")
        self.assertEqual(auto[0]["kind"], "dispute_sampling")
        analysis = self.application.analyse_disputes("lot-3")
        self.assertTrue(analysis["requires_review"])
        self.assertEqual(analysis["disposition"], "dispute_held")
        self.assertIn("open_inspection_work", analysis["release_blockers"])
        opened = self.application.disputes.open_review("lot-3")
        work = opened["work"]
        self.assertEqual(work["status"], "pending")
        self.assertEqual(work["kind"], "dispute_sampling")
        # The work is stamped with the analysis that justified it and that
        # analysis is persisted, not just echoed back.
        self.assertEqual(work["review_key"], opened["analysis"]["review_key"])
        stored = {row["analysis_id"] for row in self.application.trace_lot("lot-3")["analyses"]}
        self.assertIn(work["analysis_id"], stored)
        self.assertEqual(self.application.disputes.claim(work["work_id"], "甲")["status"], "claimed")

    def test_work_blocks_dispatch_until_completed(self):
        self.application.disputes.open_review("lot-3")
        self.release("lot-3")
        with self.assertRaisesRegex(ContractError, "dispute review"):
            self.application.dispatch("lot-3")
        self.assertEqual(self.application.repository.queue_rows(), [])

    def test_completed_review_is_not_dispatched_again(self):
        opened = self.application.disputes.open_review("lot-3")
        work_id = opened["work"]["work_id"]
        self.application.disputes.claim(work_id, "甲")
        receipt = self.receipt("insp-3", lot_id="lot-3", reason="争议复核", at="2026-11-16T09:00:00Z")
        result = self.application.disputes.complete_review(work_id, "甲", receipt)
        self.assertEqual(result["work"]["status"], "completed")
        self.assertEqual(result["analysis"]["status"], "disagreement")
        self.assertFalse(result["analysis"]["requires_review"])
        with self.assertRaisesRegex(ContractError, "重复派发"):
            self.application.disputes.open_review("lot-3", work_id="second")

    def test_completed_work_keeps_receipt_and_evidence(self):
        opened = self.application.disputes.open_review("lot-3")
        self.application.disputes.claim(opened["work"]["work_id"], "甲")
        self.application.disputes.complete_review(
            opened["work"]["work_id"], "甲",
            self.receipt("insp-3", lot_id="lot-3", at="2026-11-16T09:00:00Z"))
        trace = self.application.disputes.trace("lot-3")
        self.assertEqual(trace["completed_work_ids"], [opened["work"]["work_id"]])
        self.assertIn("insp-3", trace["evidence"])
        self.assertEqual(len(trace["receipts"]), 1)

    def test_review_completion_reassesses_peer_lots(self):
        opened = self.application.disputes.open_review("lot-3")
        self.application.disputes.claim(opened["work"]["work_id"], "甲")
        result = self.application.disputes.complete_review(
            opened["work"]["work_id"], "甲",
            self.receipt("insp-3", lot_id="lot-3", at="2026-11-16T09:00:00Z"))
        affected = {row["lot_id"] for row in result["affected"]}
        self.assertEqual(affected, {"lot-1", "lot-2", "lot-3"})

    def test_work_cannot_be_claimed_by_two_workers(self):
        opened = self.application.disputes.open_review("lot-3")
        work_id = opened["work"]["work_id"]
        self.application.disputes.claim(work_id, "甲")
        with self.assertRaises(ContractError):
            self.application.disputes.claim(work_id, "乙")
        with self.assertRaises(ContractError):
            self.application.disputes.complete_review(
                work_id, "乙", self.receipt("insp-3", lot_id="lot-3", at="2026-11-16T09:00:00Z"))


class DispositionTests(DisputeCase):
    def setUp(self):
        super().setUp()
        self.add_lot("lot-1")
        self.application.receive_inspection(self.receipt("insp-1"))

    def test_supervisor_must_disposition_unsampled_lot_before_dispatch(self):
        self.add_lot("lot-2")
        self.release("lot-2")
        with self.assertRaisesRegex(ContractError, "supervisor disposition"):
            self.application.dispatch("lot-2")
        self.application.disputes.record_disposition("lot-2", "release_unverified", "主管A",
                                                     note="客户催发，先按机器判级")
        dispatched = self.application.dispatch("lot-2")
        self.assertEqual(dispatched["destination"], "premium")
        self.assertIn("provisional", dispatched["reason"])

    def test_hold_and_reinspect_require_reason_and_keep_blocking(self):
        with self.assertRaisesRegex(ContractError, "说明缘由"):
            self.application.disputes.record_disposition("lot-1", "hold", "主管A")
        self.application.disputes.record_disposition("lot-1", "hold", "主管A", note="等待复核")
        self.release("lot-1")
        with self.assertRaisesRegex(ContractError, "dispute review"):
            self.application.dispatch("lot-1")

    def test_accept_human_releases_dispute_and_opens_no_new_work(self):
        self.release("lot-1")
        result = self.application.disputes.record_disposition("lot-1", "accept_human", "主管A")
        self.assertEqual(result["analysis"]["supervisor_decision"], "accept_human")
        self.assertTrue(result["analysis"]["release_allowed"])
        self.assertEqual(self.application.dispatch("lot-1")["destination"], "processing")

    def test_disposition_is_bound_to_the_evidence_it_reviewed(self):
        self.release("lot-1")
        self.application.disputes.record_disposition("lot-1", "accept_machine", "主管A")
        self.application.disputes.revise_inspection(
            InspectionRevision("rev-1", "insp-1", "lot-1", "correct", "standard", "复议",
                               "主管A", "2026-11-16T10:00:00Z"))
        current = self.application.analyse_disputes("lot-1")
        self.assertNotEqual(current["supervisor_decision"], "accept_machine")
        self.assertFalse(current["disposition_id"])
        self.assertEqual(current["status"], "disagreement")
        self.assertEqual(current["human_grade"], "standard")

    def test_dispatch_records_the_release_fact_permanently(self):
        self.release("lot-1")
        self.application.disputes.record_disposition("lot-1", "accept_human", "主管A")
        first = self.application.dispatch("lot-1")
        self.application.disputes.revise_inspection(
            InspectionRevision("rev-1", "insp-1", "lot-1", "correct", "premium", "复议",
                               "主管A", "2026-11-16T10:00:00Z"))
        current = self.application.analyse_disputes("lot-1")
        self.assertTrue(current["post_release"]["dispatched"])
        self.assertEqual(current["post_release"]["destination"], first["destination"])
        self.assertTrue(current["post_release"]["armed"])
        self.assertEqual(self.application.repository.queue_rows()[0]["destination"], "processing")
        with self.assertRaisesRegex(ContractError, "silently"):
            self.application.dispatch("lot-1")


class RevisionTests(DisputeCase):
    def setUp(self):
        super().setUp()
        for index in range(1, 5):
            self.add_lot(f"lot-{index}")
        for index in (1, 2, 4):
            self.application.receive_inspection(self.receipt(f"insp-{index}", lot_id=f"lot-{index}"))

    def test_revision_reassesses_peers_but_keeps_valid_open_work(self):
        opened = self.application.disputes.open_review("lot-3")
        result = self.application.disputes.revise_inspection(
            InspectionRevision("rev-1", "insp-1", "lot-1", "correct", "premium", "补报误判",
                               "主管A", "2026-11-16T10:00:00Z"))
        affected = {row["lot_id"] for row in result["affected"]}
        self.assertEqual(affected, {"lot-1", "lot-2", "lot-3", "lot-4"})
        self.assertEqual(result["analysis"]["status"], "agreement")
        self.assertEqual(result["analysis"]["revision_ids"], ["rev-1"])
        # lot-2 and lot-4 still disagree, so lot-3's open review stays actionable.
        open_ids = {row["work_id"] for row in self.application.disputes.board()["open_work"]}
        self.assertIn(opened["work"]["work_id"], open_ids)

    def test_revision_supersedes_work_that_is_no_longer_needed(self):
        # lot-3 needs review while lot-1/lot-2/lot-4 disagree. Correct every
        # disputed conclusion and the stratum alert disappears.
        opened = self.application.disputes.open_review("lot-3")
        self.assertEqual(self.application.disputes.claim(opened["work"]["work_id"], "甲")["status"],
                         "claimed")
        for receipt_id, lot_id in (("insp-1", "lot-1"), ("insp-2", "lot-2"), ("insp-4", "lot-4")):
            self.application.disputes.revise_inspection(
                InspectionRevision(f"rev-{lot_id}", receipt_id, lot_id, "correct", "premium",
                                   "复核后一致", "主管A", "2026-11-16T10:00:00Z"))
        analysis = self.application.analyse_disputes("lot-3")
        self.assertFalse(analysis["requires_review"])
        work = {row["work_id"]: row for row in self.application.inspection_work.inspect()}
        self.assertEqual(work[opened["work"]["work_id"]]["status"], "superseded")

    def test_withdraw_removes_evidence_and_blocks_release(self):
        result = self.application.disputes.revise_inspection(
            InspectionRevision("rev-1", "insp-1", "lot-1", "withdraw", "", "样本污染",
                               "主管A", "2026-11-16T10:00:00Z"))
        analysis = result["analysis"]
        self.assertEqual(analysis["status"], "withdrawn")
        self.assertTrue(analysis["requires_review"])
        self.assertIn("human_evidence_withdrawn", analysis["release_blockers"])
        self.release("lot-1")
        with self.assertRaises(ContractError):
            self.application.dispatch("lot-1")

    def test_raw_receipt_history_is_never_overwritten(self):
        self.application.disputes.revise_inspection(
            InspectionRevision("rev-1", "insp-1", "lot-1", "correct", "standard", "更正",
                               "主管A", "2026-11-16T10:00:00Z"))
        self.assertEqual(self.application.repository.inspection_rows("lot-1")[0]["inspected_grade"],
                         "processing")
        trace = self.application.disputes.trace("lot-1")
        self.assertEqual(trace["effective_receipts"][0]["inspected_grade"], "standard")
        self.assertEqual(trace["revisions"][0]["kind"], "correct")
        self.assertEqual(len(trace["events"]) > 0, True)

    def test_supplement_and_withdraw_are_append_only(self):
        self.application.disputes.revise_inspection(
            InspectionRevision("rev-1", "insp-1", "lot-1", "supplement", "standard", "补报",
                               "主管A", "2026-11-16T10:00:00Z"))
        self.application.disputes.revise_inspection(
            InspectionRevision("rev-2", "insp-1", "lot-1", "withdraw", "", "撤回",
                               "主管A", "2026-11-16T11:00:00Z"))
        with self.application.repository.connect() as connection:
            effective = effective_receipts(connection, "lot-1")
        self.assertTrue(effective[0]["withdrawn"])
        self.assertFalse(effective[0]["effective"])
        self.assertEqual(effective[0]["revision_ids"], ["rev-1", "rev-2"])

    def test_unknown_receipt_cannot_be_revised(self):
        with self.assertRaisesRegex(ContractError, "does not exist"):
            self.application.disputes.revise_inspection(
                InspectionRevision("rev-1", "missing", "lot-1", "correct", "premium", "",
                                   "主管A", "2026-11-16T10:00:00Z"))


class FollowupTests(DisputeCase):
    def test_released_lot_followup_is_recorded_without_rewriting_release(self):
        self.add_lot("lot-1")
        self.application.receive_inspection(self.receipt("insp-1"))
        self.release("lot-1")
        self.application.disputes.record_disposition("lot-1", "accept_human", "主管A")
        dispatched = self.application.dispatch("lot-1")
        self.application.disputes.revise_inspection(
            InspectionRevision("rev-1", "insp-1", "lot-1", "correct", "premium", "复议",
                               "主管A", "2026-11-16T10:00:00Z"))
        result = self.application.disputes.record_followup(
            "lot-1", "notify_customer", "已通知客户并按新判级复检", "主管B")
        self.assertTrue(result["created"])
        self.assertEqual(result["destination"], dispatched["destination"])
        board = self.application.disputes.board()
        self.assertTrue(any(row["lot_id"] == "lot-1" for row in board["post_release_followups"]))
        self.assertFalse(any(row["lot_id"] == "lot-1" for row in board["unresolved_releases"]))
        self.assertEqual(self.application.repository.queue_rows()[0]["destination"], "processing")

    def test_followup_requires_prior_release_and_actor(self):
        self.add_lot("lot-1")
        with self.assertRaisesRegex(ContractError, "尚未放行"):
            self.application.disputes.record_followup("lot-1", "recall", "", "主管A")
        self.application.receive_inspection(self.receipt("insp-1"))
        self.release("lot-1")
        self.application.disputes.record_disposition("lot-1", "accept_human", "主管A")
        self.application.dispatch("lot-1")
        with self.assertRaisesRegex(ContractError, "经办人"):
            self.application.disputes.record_followup("lot-1", "recall", "", "")


if __name__ == "__main__":
    unittest.main()
