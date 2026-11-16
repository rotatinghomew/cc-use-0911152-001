"""Pure analysis-layer tests: no database, deterministic inputs."""

from __future__ import annotations

import unittest

from orchard_grade.contracts.batches import DomainRecord
from orchard_grade.disputes.analysis import (
    affected_lots,
    analyse_lot,
    build_fact,
    summarize_analysis,
)


def record(lot_id, origin="north", category="apple", line="line-a", camera="camera-1", **overrides):
    payload = {"origin_code": origin, "category": category, "line_id": line,
               "camera_batch": camera, "sweetness_brix": 19, "diameter_mm": 72,
               "mass_g": 105, "bruise_area": 1, "camera_coverage": 0.9,
               "internal_void": 1, "ct_confidence": 0.9, "ripeness": 0.8,
               "harvest_age_h": 12}
    payload.update(overrides)
    return DomainRecord(lot_id, payload)


def receipt(receipt_id, lot_id, grade="processing", machine="premium",
            reason="random", at="2026-11-15T10:00:00Z"):
    return {"receipt_id": receipt_id, "lot_id": lot_id, "origin_code": "north",
            "line_id": "line-a", "camera_batch": "camera-1", "inspected_grade": grade,
            "machine_grade": machine, "sampling_reason": reason, "inspected_at": at,
            "withdrawn": False, "effective": True}


def samples(*entries):
    result = {}
    for item in entries:
        lot_id = item["lot_id"]
        result.setdefault(lot_id, {"receipts": [], "evidence": {}})
        result[lot_id]["receipts"].append(item)
    return result


class BuildFactTests(unittest.TestCase):
    def test_without_evidence_uses_current_model_and_marks_unverified(self):
        fact = build_fact(record("lot-1"), [], {})
        self.assertFalse(fact.sampled)
        self.assertEqual(fact.machine_source, "current_model")
        self.assertTrue(fact.machine_grade)
        self.assertEqual(fact.device, "line-a/camera-1")

    def test_frozen_snapshot_wins_over_mutated_payload(self):
        current = record("lot-1", sweetness_brix=1)
        evidence = {"r1": {"record_version": 1, "machine_grade": "processing",
                           "probabilities": {"premium": 0.1, "standard": 0.2, "processing": 0.7},
                           "sensors": {"camera_coverage": 0.4, "ct_confidence": 0.6}}}
        fact = build_fact(current, [receipt("r1", "lot-1", grade="standard")], evidence)
        self.assertEqual(fact.machine_source, "evidence_snapshot")
        self.assertEqual(fact.machine_grade, "processing")
        self.assertEqual(fact.sensor_confidence, 0.5)

    def test_machine_over_grade_direction(self):
        fact = build_fact(record("lot-1"), [receipt("r1", "lot-1", grade="processing")], {})
        self.assertTrue(fact.disagreement)
        self.assertTrue(fact.machine_over)
        self.assertEqual(fact.distance, 2)


class SamplingAnalysisTests(unittest.TestCase):
    def test_no_samples_is_reported_clearly(self):
        records = [record("lot-1"), record("lot-2")]
        analysis = analyse_lot(records[0], records, {})
        self.assertEqual(analysis.sampling["method"], "none")
        self.assertEqual(analysis.sampling["sampled_count"], 0)
        self.assertFalse(analysis.sampling["generalizable"])
        self.assertEqual(analysis.status, "unverified")

    def test_systematic_targeted_sampling_cannot_be_extrapolated(self):
        records = [record(f"lot-{index}") for index in range(1, 6)]
        sample = samples(*[receipt(f"r{index}", f"lot-{index}", reason="complaint")
                           for index in range(1, 4)])
        analysis = analyse_lot(records[0], records, sample)
        self.assertEqual(analysis.sampling["method"], "targeted")
        self.assertTrue(analysis.sampling["selection_bias"])
        self.assertFalse(analysis.sampling["generalizable"])
        self.assertTrue(any("不可外推" in text for text in analysis.explanations))

    def test_random_sample_of_five_is_generalizable(self):
        records = [record(f"lot-{index}") for index in range(1, 6)]
        sample = samples(*[receipt(f"r{index}", f"lot-{index}") for index in range(1, 6)])
        analysis = analyse_lot(records[0], records, sample)
        self.assertTrue(analysis.sampling["sample_adequate"])
        self.assertTrue(analysis.sampling["generalizable"])
        self.assertTrue(any("可按比例外推" in text for text in analysis.explanations))

    def test_representativeness_gap_exposed(self):
        records = [record(f"lot-{index}", origin="north" if index < 4 else "south")
                   for index in range(1, 6)]
        sample = samples(receipt("r1", "lot-1"), receipt("r2", "lot-2"),
                         receipt("r3", "lot-3"))
        analysis = analyse_lot(records[0], records, sample)
        self.assertGreater(analysis.sampling["representativeness_gap"]["origin"], 0.0)

    def test_optional_category_and_device_dimensions_are_present(self):
        records = [record("lot-1"), record("lot-2", category="pear", line="line-b")]
        analysis = analyse_lot(records[0], records, {})
        categories = {item.value for item in analysis.strata if item.dimension == "category"}
        devices = {item.value for item in analysis.strata if item.dimension == "device"}
        self.assertEqual(categories, {"apple", "pear"})
        self.assertEqual(devices, {"line-a/camera-1", "line-b/camera-1"})

    def test_uncertainty_grows_without_samples(self):
        records = [record("lot-1"), record("lot-2")]
        sampled = analyse_lot(records[0], records, samples(receipt("r1", "lot-1")))
        unsampled = analyse_lot(records[1], records, samples(receipt("r1", "lot-1")))
        self.assertGreater(unsampled.uncertainty, sampled.uncertainty)
        self.assertIn(unsampled.confidence, {"high", "medium", "low"})


class StratumAlertTests(unittest.TestCase):
    def test_two_disagreements_raise_stratum_alert(self):
        records = [record(f"lot-{index}") for index in range(1, 5)]
        sample = samples(receipt("r1", "lot-1"), receipt("r2", "lot-2"))
        analysis = analyse_lot(records[2], records, sample)
        self.assertEqual(analysis.status, "unverified")
        self.assertTrue(analysis.requires_review)
        self.assertIn("stratum_review_required", analysis.release_blockers)
        self.assertIn("未抽检", analysis.review_reason)

    def test_already_reviewed_evidence_is_not_flagged_again(self):
        records = [record(f"lot-{index}") for index in range(1, 5)]
        sample = samples(receipt("r1", "lot-1"), receipt("r2", "lot-2"))
        pending = analyse_lot(records[2], records, sample)
        completed = [{"work_id": "w", "status": "completed", "review_key": pending.review_key}]
        resolved = analyse_lot(records[2], records, sample, open_reviews=completed)
        self.assertFalse(resolved.requires_review)
        self.assertEqual(resolved.status, "reviewed")
        self.assertNotIn("stratum_review_required", resolved.release_blockers)

    def test_other_strata_are_not_tainted(self):
        records = [record("lot-1", line="line-a"), record("lot-2", line="line-a"),
                   record("lot-3", line="line-b", origin="south", category="pear"),
                   record("lot-4", line="line-b", origin="south", category="pear")]
        sample = samples(receipt("r1", "lot-1"), receipt("r2", "lot-2"))
        unrelated = analyse_lot(records[2], records, sample)
        self.assertFalse(unrelated.requires_review)
        self.assertEqual(unrelated.status, "unverified")


class SummaryTests(unittest.TestCase):
    def test_summary_has_no_pass_fail_vocabulary(self):
        records = [record("lot-1")]
        analysis = analyse_lot(records[0], records, {})
        summary = summarize_analysis(analysis)
        self.assertEqual(summary["status"], "unverified")
        self.assertNotIn(summary["disposition"], {"pass", "fail"})
        self.assertIn("analysis_id", summary)

    def test_affected_lots_share_strata(self):
        records = [record("lot-1"), record("lot-2", origin="south"),
                   record("lot-3", category="pear")]
        peers = affected_lots(records[0], records, {})
        self.assertEqual(peers, ["lot-1"])


if __name__ == "__main__":
    unittest.main()
