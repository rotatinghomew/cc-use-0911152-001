"""Release gate: work orders and disputes must constrain the release transition itself."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchard_grade.application import Application
from orchard_grade.contracts.batches import ContractError, DomainRecord
from orchard_grade.inspection.receipts import InspectionReceipt


def record(lot_id, **overrides):
    payload = {"origin_code": "north", "line_id": "line-a", "camera_batch": "camera-1",
               "category": "apple", "sweetness_brix": 19, "diameter_mm": 72, "mass_g": 105,
               "bruise_area": 1, "camera_coverage": 0.9, "internal_void": 1,
               "ct_confidence": 0.9, "ripeness": 0.8, "harvest_age_h": 12}
    payload.update(overrides)
    return DomainRecord(lot_id, payload, "graded")


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.application = Application(Path(temporary.name) / "release.sqlite3")
        for index in range(1, 4):
            self.application.repository.save_record(record(f"lot-{index}"))
        for index in (1, 2):
            self.application.receive_inspection(InspectionReceipt(
                f"insp-{index}", f"lot-{index}", "north", "line-a", "camera-1",
                "processing", "premium", "random", f"2026-11-15T0{index}:00:00Z"))

    def test_open_review_blocks_release_transition(self):
        self.application.disputes.open_review("lot-3")
        with self.assertRaisesRegex(ContractError, "dispute review"):
            self.application.release("lot-3")
        self.assertEqual(self.application.repository.get_record("lot-3").state, "graded")

    def test_unverified_lot_needs_disposition_before_release(self):
        # Every new evidence arrival now opens review work for the unsampled peer,
        # so close that work first to exercise the disposition gate itself.
        for work in self.application.inspection_work.inspect():
            self.application.disputes.cancel_review(work["work_id"], "人工直接处置")
        with self.assertRaisesRegex(ContractError, "supervisor disposition"):
            self.application.release("lot-3")
        self.application.disputes.record_disposition("lot-3", "release_unverified", "主管A",
                                                     note="客户催发")
        released = self.application.release("lot-3")
        self.assertEqual(released["state"], "released")
        self.assertEqual(self.application.dispatch("lot-3")["destination"], "premium")

    def test_release_is_idempotent_but_not_silent_repeatable(self):
        for work in self.application.inspection_work.inspect():
            self.application.disputes.cancel_review(work["work_id"], "人工直接处置")
        self.application.disputes.record_disposition("lot-3", "release_unverified", "主管A",
                                                     note="催发")
        self.application.release("lot-3")
        with self.assertRaisesRegex(ContractError, "already released"):
            self.application.release("lot-3")

    def test_withdrawn_evidence_blocks_release(self):
        from orchard_grade.inspection.receipts import InspectionRevision
        self.application.disputes.revise_inspection(InspectionRevision(
            "rev-1", "insp-1", "lot-1", "withdraw", "", "样本污染", "主管A",
            "2026-11-16T10:00:00Z"))
        with self.assertRaises(ContractError):
            self.application.release("lot-1")

    def test_dispute_analyses_are_auditable_events(self):
        self.application.disputes.analyse("lot-1")
        events = self.application.disputes.trace("lot-1")["events"]
        types = [event["event_type"] for event in events]
        self.assertIn("dispute.analysed", types)
        errors = self.application.repository.verify_chain()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
