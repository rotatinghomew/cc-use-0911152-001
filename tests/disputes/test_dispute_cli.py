"""CLI-level smoke tests: the supervisor can drive the whole loop from the shell."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from orchard_grade.application import Application
from orchard_grade.console import main
from orchard_grade.contracts.batches import DomainRecord
from orchard_grade.inspection.receipts import InspectionReceipt


class DisputeCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "cli.sqlite3"
        application = Application(self.database)
        for index in range(1, 4):
            application.repository.save_record(DomainRecord(f"lot-{index}", {
                "origin_code": "north", "line_id": "line-a", "camera_batch": "camera-1",
                "category": "apple", "sweetness_brix": 19, "diameter_mm": 72, "mass_g": 105,
                "bruise_area": 1, "camera_coverage": 0.9, "internal_void": 1,
                "ct_confidence": 0.9, "ripeness": 0.8, "harvest_age_h": 12}))

    def run_cli(self, *args):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(list(args))
        return code, json.loads(buffer.getvalue())

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def receipt_file(self, name, receipt_id, lot_id, grade="processing"):
        return self.write_json(name, {
            "receipt_id": receipt_id, "lot_id": lot_id, "origin_code": "north",
            "line_id": "line-a", "camera_batch": "camera-1", "inspected_grade": grade,
            "machine_grade": "premium", "sampling_reason": "random",
            "inspected_at": "2026-11-15T10:00:00Z"})

    def test_board_analyse_and_trace(self):
        code, board = self.run_cli("dispute", "board", "--database", str(self.database))
        self.assertEqual(code, 0)
        self.assertEqual(board["counts"]["analysed"], 0)
        code, analysis = self.run_cli("dispute", "analyse", "--database", str(self.database),
                                      "--lot", "lot-1")
        self.assertEqual(code, 0)
        self.assertEqual(analysis["status"], "unverified")
        code, trace = self.run_cli("dispute", "trace", "--database", str(self.database),
                                   "--lot", "lot-1")
        self.assertEqual(code, 0)
        self.assertEqual(trace["lot_id"], "lot-1")
        self.assertIn("current", trace)

    def test_full_review_loop_from_cli(self):
        application = Application(self.database)
        for index in (1, 2):
            application.receive_inspection(InspectionReceipt(
                f"insp-{index}", f"lot-{index}", "north", "line-a", "camera-1",
                "processing", "premium", "random", f"2026-11-15T0{index}:00:00Z"))
        code, opened = self.run_cli("dispute", "open-review", "--database", str(self.database),
                                    "--lot", "lot-3")
        self.assertEqual(code, 0)
        work_id = opened["work"]["work_id"]
        code, claimed = self.run_cli("dispute", "claim", "--database", str(self.database),
                                     "--work", work_id, "--worker", "甲")
        self.assertEqual(claimed["status"], "claimed")
        path = self.receipt_file("receipt.json", "insp-3", "lot-3")
        code, completed = self.run_cli("dispute", "complete", "--database", str(self.database),
                                       "--work", work_id, "--worker", "甲",
                                       "--receipt", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(completed["work"]["status"], "completed")

    def test_disposition_and_followup_from_cli(self):
        application = Application(self.database)
        application.receive_inspection(InspectionReceipt(
            "insp-1", "lot-1", "north", "line-a", "camera-1", "processing", "premium",
            "random", "2026-11-15T10:00:00Z"))
        record = application.repository.get_record("lot-1")
        application.repository.save_record(
            DomainRecord("lot-1", record.payload, "released", 2, record.occurred_at, record.source),
            expected_version=record.version)
        code, decided = self.run_cli("dispute", "disposition", "--database", str(self.database),
                                     "--lot", "lot-1", "--decision", "accept_human",
                                     "--actor", "主管A")
        self.assertEqual(code, 0)
        self.assertTrue(decided["analysis"]["release_allowed"])
        code, dispatched = self.run_cli("dispatch", "--database", str(self.database), "--lot", "lot-1")
        self.assertEqual(dispatched["destination"], "processing")
        revision = self.write_json("revision.json", {
            "revision_id": "rev-1", "receipt_id": "insp-1", "lot_id": "lot-1",
            "kind": "correct", "inspected_grade": "premium", "note": "复议",
            "actor": "主管A", "revised_at": "2026-11-16T10:00:00Z"})
        code, revised = self.run_cli("dispute", "revise", "--database", str(self.database),
                                     "--revision", str(revision))
        self.assertEqual(code, 0)
        self.assertTrue(revised["analysis"]["post_release"]["armed"])
        code, followup = self.run_cli("dispute", "followup", "--database", str(self.database),
                                      "--lot", "lot-1", "--action", "notify_customer",
                                      "--actor", "主管B", "--note", "通知客户")
        self.assertTrue(followup["created"])


if __name__ == "__main__":
    unittest.main()
