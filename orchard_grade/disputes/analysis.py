"""争议分析：把机器判级与人工复检的差异放回证据、抽样与分层环境中解释。

设计约束（来自业务要求）：

* 分析引用复检当时的传感器快照与机器判断，而不是事后重算的分数；
* 未人工抽检的果批只能得到 ``unverified`` 状态，绝不输出合格/不合格；
* 产地、品类、设备条件上的差异显式分层，并报告抽样偏差与不确定性；
* 分析是“是否需要复核”的建议，不能替代主管最终处置，也不能改写已放行历史。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from orchard_grade.contracts.batches import DomainRecord, bounded, stable_hash
from orchard_grade.sensing.fusion import grade_probability

GRADES = ("premium", "standard", "processing")
DIMENSIONS = ("origin", "category", "device")
DIMENSION_LABELS = {"origin": "产地", "category": "品类", "device": "设备条件"}
UNCLASSIFIED = "未分类"
OPEN_WORK_STATES = ("pending", "claimed")
RESOLVING_DECISIONS = ("accept_machine", "accept_human", "release_unverified", "release_followup")
BLOCKING_DECISIONS = ("hold", "reinspect")


def _wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 1.0
    rate = successes / total
    denominator = 1.0 + z * z / total
    center = (rate + z * z / (2.0 * total)) / denominator
    margin = z * math.sqrt(rate * (1.0 - rate) / total + z * z / (4.0 * total * total)) / denominator
    return bounded(center - margin), bounded(center + margin)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = " ".join(str(value).split())
    return text or default


def _category(record: DomainRecord) -> str:
    for key in ("category", "variety", "category_code", "fruit_category"):
        value = _text(record.payload.get(key))
        if value:
            return value
    return UNCLASSIFIED


def _device(record: DomainRecord, receipt: Mapping[str, Any] | None) -> str:
    source = receipt or record.payload
    line = _text(source.get("line_id"))
    camera = _text(source.get("camera_batch"))
    if line or camera:
        return f"{line}/{camera}"
    return "unknown"


def _grade_distance(left: str, right: str) -> int:
    if left not in GRADES or right not in GRADES:
        return 0
    return abs(GRADES.index(left) - GRADES.index(right))


def _severity(distance: int) -> str:
    return ("none", "moderate", "severe")[max(0, min(2, distance))]


def _round(value: float) -> float:
    return round(float(value), 6)


@dataclass(frozen=True)
class LotFact:
    lot_id: str
    origin: str
    category: str
    device: str
    machine_grade: str
    machine_probabilities: dict[str, float]
    machine_source: str
    human_grade: str
    human_receipt_id: str
    sampled: bool
    withdrawn_only: bool
    receipts: tuple[dict[str, Any], ...]
    evidence: dict[str, dict[str, Any]]
    sensor_confidence: float
    revision_ids: tuple[str, ...]
    revision_notes: tuple[str, ...]

    @property
    def disagreement(self) -> bool:
        return bool(self.sampled and self.human_grade and self.machine_grade != self.human_grade)

    @property
    def machine_over(self) -> bool:
        return self.disagreement and GRADES.index(self.machine_grade) < GRADES.index(self.human_grade)

    @property
    def distance(self) -> int:
        return _grade_distance(self.machine_grade, self.human_grade) if self.sampled else 0

    def stratum(self, dimension: str) -> str:
        return {"origin": self.origin, "category": self.category, "device": self.device}[dimension]

    def in_stratum(self, dimension: str, value: str) -> bool:
        return self.stratum(dimension) == value

    def fact_payload(self) -> dict[str, Any]:
        return {"lot_id": self.lot_id, "origin": self.origin, "category": self.category,
                "device": self.device, "machine_grade": self.machine_grade,
                "machine_probabilities": self.machine_probabilities,
                "machine_source": self.machine_source, "human_grade": self.human_grade,
                "human_receipt_id": self.human_receipt_id, "sampled": self.sampled,
                "withdrawn_only": self.withdrawn_only,
                "receipt_ids": [item.get("receipt_id") for item in self.receipts],
                "revision_ids": list(self.revision_ids),
                "sensor_confidence": self.sensor_confidence}


@dataclass(frozen=True)
class StratumFinding:
    dimension: str
    value: str
    lot_count: int
    sampled_count: int
    disagreement_count: int
    machine_over: int
    machine_under: int
    agreement_rate: float
    lower_bound: float
    upper_bound: float
    sample_adequate: bool
    alert: bool
    direction: str
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"dimension": self.dimension, "value": self.value, "lot_count": self.lot_count,
                "sampled_count": self.sampled_count,
                "disagreement_count": self.disagreement_count,
                "machine_over": self.machine_over, "machine_under": self.machine_under,
                "agreement_rate": self.agreement_rate, "lower_bound": self.lower_bound,
                "upper_bound": self.upper_bound, "sample_adequate": self.sample_adequate,
                "alert": self.alert, "direction": self.direction, "notes": list(self.notes)}


@dataclass(frozen=True)
class DisputeAnalysis:
    lot_id: str
    status: str
    disposition: str
    machine_grade: str
    machine_source: str
    human_grade: str
    effective_receipt_id: str
    effective_receipt_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    revision_ids: tuple[str, ...]
    disagreement: bool
    severity: str
    requires_review: bool
    release_allowed: bool
    release_blockers: tuple[str, ...]
    review_reason: str
    open_review_id: str
    uncertainty: float
    confidence: str
    input_hash: str
    review_key: str
    sampling: dict[str, Any]
    strata: tuple[StratumFinding, ...]
    explanations: tuple[str, ...]
    post_release: dict[str, Any]
    supervisor_decision: str
    disposition_id: str

    def as_dict(self) -> dict[str, Any]:
        return {"lot_id": self.lot_id, "status": self.status, "disposition": self.disposition,
                "machine_grade": self.machine_grade, "machine_source": self.machine_source,
                "human_grade": self.human_grade,
                "effective_receipt_id": self.effective_receipt_id,
                "effective_receipt_ids": list(self.effective_receipt_ids),
                "evidence_ids": list(self.evidence_ids), "revision_ids": list(self.revision_ids),
                "disagreement": self.disagreement, "severity": self.severity,
                "requires_review": self.requires_review, "release_allowed": self.release_allowed,
                "release_blockers": list(self.release_blockers),
                "review_reason": self.review_reason, "open_review_id": self.open_review_id,
                "uncertainty": self.uncertainty, "confidence": self.confidence,
                "input_hash": self.input_hash, "review_key": self.review_key,
                "sampling": self.sampling, "strata": [item.as_dict() for item in self.strata],
                "explanations": list(self.explanations), "post_release": self.post_release,
                "supervisor_decision": self.supervisor_decision,
                "disposition_id": self.disposition_id}

    @property
    def fingerprint(self) -> str:
        return stable_hash(self.as_dict())

    @property
    def analysis_id(self) -> str:
        return stable_hash({"lot_id": self.lot_id, "fingerprint": self.fingerprint})[:24]

    def as_record(self) -> dict[str, Any]:
        return self.as_dict() | {"analysis_id": self.analysis_id}


def build_fact(record: DomainRecord, receipts: Sequence[Mapping[str, Any]],
               evidence: Mapping[str, Mapping[str, Any]]) -> LotFact:
    ordered = sorted((dict(item) for item in receipts),
                     key=lambda item: (str(item.get("inspected_at", "")), str(item.get("receipt_id", ""))))
    latest = ordered[-1] if ordered else None
    effective = [item for item in ordered if not item.get("withdrawn")]
    latest_effective = effective[-1] if effective else None
    snapshot = evidence.get(str(latest.get("receipt_id", ""))) if latest else None
    if snapshot:
        probabilities = {grade: float(snapshot.get("probabilities", {}).get(grade, 0.0)) for grade in GRADES}
        machine_grade = str(snapshot.get("machine_grade") or "")
        machine_source = "evidence_snapshot"
        sensors = snapshot.get("sensors") or {}
    else:
        probabilities = grade_probability(record)
        machine_grade = max(GRADES, key=lambda grade: probabilities[grade])
        machine_source = "current_model"
        sensors = record.payload
    confidence_values = [bounded(sensors[key]) for key in ("camera_coverage", "ct_confidence")
                         if isinstance(sensors.get(key), (int, float)) and not isinstance(sensors.get(key), bool)]
    sensor_confidence = statistics.fmean(confidence_values) if confidence_values else 0.5
    revision_ids = tuple(str(revision_id) for item in ordered
                         for revision_id in item.get("revision_ids", []) if revision_id)
    revision_notes = tuple(str(note) for item in ordered for note in item.get("revision_notes", []) if note)
    return LotFact(
        lot_id=record.record_id,
        origin=_text(record.payload.get("origin_code"), "unknown"),
        category=_category(record),
        device=_device(record, latest_effective or latest),
        machine_grade=machine_grade or max(GRADES, key=lambda grade: probabilities[grade]),
        machine_probabilities={grade: _round(value) for grade, value in probabilities.items()},
        machine_source=machine_source,
        human_grade=str(latest_effective.get("inspected_grade", "")) if latest_effective else "",
        human_receipt_id=str(latest_effective.get("receipt_id", "")) if latest_effective else "",
        sampled=latest_effective is not None,
        withdrawn_only=bool(ordered) and latest_effective is None,
        receipts=tuple(ordered),
        evidence={str(key): dict(value) for key, value in evidence.items()},
        sensor_confidence=_round(sensor_confidence),
        revision_ids=tuple(dict.fromkeys(revision_ids)),
        revision_notes=tuple(dict.fromkeys(revision_notes)))


def _stratum_findings(facts: Sequence[LotFact]) -> tuple[list[StratumFinding], dict[str, float]]:
    findings: list[StratumFinding] = []
    representativeness: dict[str, float] = {}
    sampled = [fact for fact in facts if fact.sampled]
    for dimension in DIMENSIONS:
        groups: dict[str, list[LotFact]] = {}
        for fact in facts:
            groups.setdefault(fact.stratum(dimension), []).append(fact)
        population_shares = {value: len(rows) / max(1, len(facts)) for value, rows in groups.items()}
        sample_shares = {value: sum(1 for row in rows if row.sampled) / max(1, len(sampled))
                         for value, rows in groups.items()}
        gaps = [abs(sample_shares.get(value, 0.0) - population_shares.get(value, 0.0)) for value in groups]
        representativeness[dimension] = _round(max(gaps) if gaps else 0.0)
        for value in sorted(groups):
            rows = groups[value]
            tested = [row for row in rows if row.sampled]
            disagreements = [row for row in tested if row.disagreement]
            count, hits = len(tested), len(disagreements)
            lower, upper = _wilson(hits, count)
            rate = hits / count if count else 0.0
            over = sum(1 for row in disagreements if row.machine_over)
            under = hits - over
            adequate = count >= 5
            alert = bool(count >= 2 and hits >= 2 and lower >= 0.2)
            if not hits:
                direction = "consistent"
            elif over == hits:
                direction = "machine_over_grades"
            elif under == hits:
                direction = "machine_under_grades"
            else:
                direction = "mixed"
            notes: list[str] = []
            if count and not adequate:
                notes.append(f"{DIMENSION_LABELS[dimension]}『{value}』仅抽检 {count} 批，区间过宽")
            if direction == "machine_over_grades":
                notes.append(f"{DIMENSION_LABELS[dimension]}『{value}』机器偏向高判 {over}/{hits}")
            if direction == "machine_under_grades":
                notes.append(f"{DIMENSION_LABELS[dimension]}『{value}』机器偏向低判 {under}/{hits}")
            if alert:
                notes.append(f"{DIMENSION_LABELS[dimension]}『{value}』争议率 {rate:.0%}，可能需要设备校准或加严抽检")
            findings.append(StratumFinding(dimension, value, len(rows), count, hits, over, under,
                                            _round(rate), _round(lower), _round(upper),
                                            adequate, alert, direction, tuple(notes)))
    return findings, representativeness


def _uncertainty(fact: LotFact, findings: Sequence[StratumFinding]) -> float:
    own = [item for item in findings
           if (item.dimension == "origin" and item.value == fact.origin)
           or (item.dimension == "category" and item.value == fact.category)
           or (item.dimension == "device" and item.value == fact.device)]
    minimum_sample = min((item.sampled_count for item in own), default=0)
    margin = 0.0
    if fact.machine_probabilities:
        ordered = sorted(fact.machine_probabilities.values(), reverse=True)
        margin = ordered[0] - ordered[1] if len(ordered) > 1 else ordered[0]
    value = (1.0 - fact.sensor_confidence) * 0.55 + (1.0 - bounded(margin)) * 0.25
    if minimum_sample < 5:
        value += 0.15
    if not fact.sampled:
        value += 0.2
    if fact.disagreement:
        value += 0.05 * fact.distance
    return _round(bounded(value))


def _confidence(value: float) -> str:
    if value < 0.35:
        return "high"
    if value < 0.6:
        return "medium"
    return "low"


def _review_key(fact: LotFact, record: DomainRecord) -> str:
    """Identity of the evidence generation a review would be dispatched against."""

    return stable_hash({"record": record.fingerprint,
                        "receipts": [{"receipt_id": item.get("receipt_id"),
                                      "grade": item.get("inspected_grade"),
                                      "withdrawn": bool(item.get("withdrawn")),
                                      "revisions": list(item.get("revision_ids", [])),
                                      "effective": item.get("effective_fingerprint")}
                                     for item in fact.receipts]})


def _latest_disposition(dispositions: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if not dispositions:
        return None
    return sorted(dispositions, key=lambda item: (str(item.get("decided_at", "")),
                                                   str(item.get("disposition_id", ""))))[-1]


def analyse_lot(record: DomainRecord, records: Sequence[DomainRecord],
                samples: Mapping[str, Mapping[str, Any]],
                open_reviews: Sequence[Mapping[str, Any]] = (),
                dispositions: Sequence[Mapping[str, Any]] = (),
                queue_assignment: Mapping[str, Any] | None = None) -> DisputeAnalysis:
    """Build a deterministic dispute analysis for one lot.

    ``samples`` maps lot_id to ``{"receipts": effective receipts, "evidence": {...}}``.
    No pass/fail label is produced for lots without effective human evidence.
    """

    facts = [build_fact(item, *_sample_parts(samples.get(item.record_id))) for item in records]
    facts_by_id = {fact.lot_id: fact for fact in facts}
    fact = facts_by_id[record.record_id]
    findings, representativeness = _stratum_findings(facts)
    own_alerts = [item for item in findings
                  if (item.dimension == "origin" and item.value == fact.origin)
                  or (item.dimension == "category" and item.value == fact.category)
                  or (item.dimension == "device" and item.value == fact.device)]
    own_alerts = [item for item in own_alerts if item.alert]
    sampled_count = sum(1 for item in facts if item.sampled)
    reasons = sorted({str(receipt.get("sampling_reason", ""))
                      for item in facts for receipt in item.receipts
                      if receipt.get("effective", not receipt.get("withdrawn")) and receipt.get("sampling_reason")})
    if sampled_count == 0:
        method = "none"
    elif all(reason in {"random", "stratified", "随机", "分层"} for reason in reasons):
        method = "random"
    elif any(reason in {"random", "stratified", "随机", "分层"} for reason in reasons):
        method = "mixed"
    else:
        method = "targeted"
    coverage = sampled_count / max(1, len(facts))
    sample_adequate = sampled_count >= 5
    generalizable = method == "random" and sample_adequate
    sampling = {"population_count": len(facts), "sampled_count": sampled_count,
                "coverage": _round(coverage), "method": method, "reasons": reasons,
                "sample_adequate": sample_adequate,
                "selection_bias": sampled_count > 0 and method != "random",
                "generalizable": generalizable,
                "representativeness_gap": representativeness}

    open_work = [item for item in open_reviews if str(item.get("status", "")) in OPEN_WORK_STATES]
    disposition = _latest_disposition(dispositions)
    decision = str((disposition or {}).get("decision", ""))
    disposition_id = str((disposition or {}).get("disposition_id", ""))
    # A disposition only governs the analysis inputs it was made against. The
    # digest deliberately excludes the disposition itself and open work status so
    # that resolving a review does not silently invalidate the supervisor's call.
    evidence_hash = _evidence_hash(record, facts)
    disposition_current = bool(disposition and str(disposition.get("input_hash", "")) == evidence_hash)
    if disposition and not disposition_current:
        decision = ""

    # Replay of an already-recorded release is not a new decision point.
    released_before = bool((queue_assignment or {}).get("destination"))
    completed_keys = {str(item.get("review_key", "")) for item in open_reviews
                      if str(item.get("status", "")) == "completed"}
    review_required = False
    review_reason = ""
    if open_work:
        review_required = True
        review_reason = f"复检工单 {open_work[0].get('work_id', '')} 尚未完成"
    elif decision in BLOCKING_DECISIONS:
        review_required = True
        review_reason = f"主管处置要求：{decision}"
    elif fact.withdrawn_only:
        review_required = True
        review_reason = "人工复检结论已撤回，需要重新复核"
    elif not fact.sampled and own_alerts and not released_before and not disposition_current:
        key = _review_key(fact, record)
        if key not in completed_keys:
            labels = "、".join(f"{DIMENSION_LABELS[item.dimension]}『{item.value}』" for item in own_alerts)
            review_required = True
            review_reason = f"未抽检果批处于争议集中分层：{labels}"

    if fact.sampled:
        status = "disagreement" if fact.disagreement else "agreement"
    elif fact.withdrawn_only:
        status = "withdrawn"
    elif _review_key(fact, record) in completed_keys:
        status = "reviewed"
    else:
        status = "unverified"

    blockers: list[str] = []
    if open_work:
        blockers.append("open_inspection_work")
    if decision in BLOCKING_DECISIONS:
        blockers.append(f"supervisor_{decision}")
    if fact.withdrawn_only:
        blockers.append("human_evidence_withdrawn")
    if (not fact.sampled and own_alerts and not released_before and not disposition_current
            and _review_key(fact, record) not in completed_keys):
        blockers.append("stratum_review_required")

    if fact.withdrawn_only:
        code = "evidence_withdrawn"
    elif open_work:
        code = "dispute_held"
    elif decision in BLOCKING_DECISIONS:
        code = f"dispute_{decision}"
    elif fact.sampled and fact.disagreement:
        code = "dispute_pending" if not disposition_current else "dispute_resolved"
    elif fact.sampled:
        code = "human_confirmed" if not fact.revision_ids else "human_corrected"
    elif (own_alerts and not released_before and not disposition_current
          and _review_key(fact, record) not in completed_keys):
        code = "stratum_review_required"
    elif not fact.sampled and _review_key(fact, record) in completed_keys:
        code = "stratum_reviewed"
    else:
        code = "machine_only_unverified"
    if decision == "release_unverified" and disposition_current:
        code = "released_unverified"
    if decision == "release_followup" and disposition_current:
        code = "released_follow_up"

    uncertainty = _uncertainty(fact, findings)
    explanations: list[str] = []
    if fact.sampled:
        explanations.append(f"机器判级 {fact.machine_grade}（{fact.machine_source}），"
                            f"人工复检 {fact.human_grade}（凭据 {fact.human_receipt_id}），"
                            f"差异 {fact.distance} 级（{_severity(fact.distance)}）")
    elif fact.withdrawn_only:
        explanations.append("人工复检结论已撤回，原结论不再作为放行依据，需要重新复核")
    else:
        explanations.append("本果批未人工抽检；分析只说明机器判级的可信度，不作合格或不合格判定")
    if sampled_count == 0:
        explanations.append("当前范围内没有有效人工复检样本，无法估计机器与人工的一致率")
    else:
        explanations.append(f"抽检覆盖 {sampled_count}/{len(facts)}（{coverage:.0%}），抽样方式为 {method}，"
                            + ("可按比例外推" if generalizable else "证据不足或非随机抽样，结论不可外推"))
    for item in own_alerts:
        explanations.append(f"{DIMENSION_LABELS[item.dimension]}『{item.value}』争议 "
                            f"{item.disagreement_count}/{item.sampled_count}，"
                            f"95% 区间 [{item.lower_bound:.2f}, {item.upper_bound:.2f}]")
    if fact.category == UNCLASSIFIED:
        explanations.append("果批缺少品类字段，品类维度按『未分类』处理，结论存在额外不确定性")
    explanations.append(f"传感器条件均值 {fact.sensor_confidence:.2f}，"
                        f"综合不确定性 {uncertainty:.2f}（{_confidence(uncertainty)}）")
    if fact.revision_notes:
        explanations.append("已应用人工结论更正：" + "；".join(fact.revision_notes))

    post_release: dict[str, Any] = {}
    if queue_assignment:
        destination = str(queue_assignment.get("destination", ""))
        recorded = fact.human_grade or fact.machine_grade
        if destination != recorded:
            post_release = {"dispatched": True, "destination": destination, "recorded_grade": recorded,
                            "armed": True,
                            "action_required": "released_fact_preserved; follow-up disposition required"}
            explanations.append(f"该果批已分配出库目的地 {destination}，放行事实保留；"
                                f"目前有效判级为 {recorded}，需说明后续处置")
        else:
            post_release = {"dispatched": True, "destination": destination, "recorded_grade": recorded,
                            "armed": False, "action_required": "none"}

    input_hash = _evidence_hash(record, facts)
    review_key = _review_key(fact, record)
    effective_receipts = [item for item in fact.receipts if not item.get("withdrawn")]
    return DisputeAnalysis(
        lot_id=fact.lot_id, status=status, disposition=code,
        machine_grade=fact.machine_grade, machine_source=fact.machine_source,
        human_grade=fact.human_grade if fact.sampled else "",
        effective_receipt_id=fact.human_receipt_id,
        effective_receipt_ids=tuple(str(item.get("receipt_id", "")) for item in effective_receipts),
        evidence_ids=tuple(str(item.get("receipt_id", "")) for item in fact.receipts),
        revision_ids=fact.revision_ids,
        disagreement=fact.disagreement, severity=_severity(fact.distance),
        requires_review=review_required, release_allowed=not blockers,
        release_blockers=tuple(blockers), review_reason=review_reason,
        open_review_id=str(open_work[0].get("work_id", "")) if open_work else "",
        uncertainty=uncertainty, confidence=_confidence(uncertainty), input_hash=input_hash,
        review_key=review_key,
        sampling=sampling, strata=tuple(findings), explanations=tuple(explanations),
        post_release=post_release, supervisor_decision=decision,
        disposition_id=disposition_id if disposition_current else "")


def _sample_parts(sample: Mapping[str, Any] | None) -> tuple[Sequence[Mapping[str, Any]], Mapping[str, Mapping[str, Any]]]:
    sample = sample or {}
    return sample.get("receipts", ()), sample.get("evidence", {})


def _evidence_hash(record: DomainRecord, facts: Sequence[LotFact]) -> str:
    """Digest of the evidence a supervisor's disposition is bound to.

    Dispatch outcomes are deliberately excluded: registering a release after a
    disposition must not invalidate that same disposition.
    """

    return stable_hash({"record_id": record.record_id, "payload": record.payload,
                        "facts": [item.fact_payload() for item in facts]})


def affected_lots(record: DomainRecord, records: Sequence[DomainRecord],
                  samples: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Lots sharing origin/category/device with ``record`` whose statistics may shift."""

    target = build_fact(record, *_sample_parts(samples.get(record.record_id)))
    peers = {(target.origin, target.category, target.device)}
    result = []
    for item in records:
        candidate = build_fact(item, *_sample_parts(samples.get(item.record_id)))
        if (candidate.origin, candidate.category, candidate.device) in peers:
            result.append(item.record_id)
    return sorted(set(result))


def summarize_analysis(analysis: DisputeAnalysis) -> dict[str, Any]:
    return {"analysis_id": analysis.analysis_id, "lot_id": analysis.lot_id, "status": analysis.status,
            "disposition": analysis.disposition, "requires_review": analysis.requires_review,
            "release_allowed": analysis.release_allowed, "disagreement": analysis.disagreement,
            "severity": analysis.severity, "uncertainty": analysis.uncertainty,
            "confidence": analysis.confidence, "review_reason": analysis.review_reason,
            "human_grade": analysis.human_grade, "machine_grade": analysis.machine_grade,
            "post_release": analysis.post_release}
