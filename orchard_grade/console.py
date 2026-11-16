from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from orchard_grade.application import Application, run, verify
from orchard_grade.contracts.batches import ContractError
from orchard_grade.inspection.receipts import InspectionReceipt, InspectionRevision


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog='orchard_grade', description='果品智能分选质量闭环平台')
    commands = root.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--fixture", required=True)
    execute.add_argument("--database", required=True)
    execute.add_argument("--output", required=True)
    check = commands.add_parser("verify")
    check.add_argument("--database", required=True)
    check.add_argument("--output", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--database", required=True)

    dispute = commands.add_parser("dispute", help="争议分析、复核工单与放行处置")
    dispute_commands = dispute.add_subparsers(dest="dispute_command", required=True)
    analyse = dispute_commands.add_parser("analyse", help="重算并保存果批争议分析")
    analyse.add_argument("--database", required=True)
    analyse.add_argument("--lot", required=True)
    board = dispute_commands.add_parser("board", help="主管看板：待复核、未闭环放行")
    board.add_argument("--database", required=True)
    trace = dispute_commands.add_parser("trace", help="单个果批的完整处置链")
    trace.add_argument("--database", required=True)
    trace.add_argument("--lot", required=True)
    open_review = dispute_commands.add_parser("open-review", help="把争议转为可领取的复核工作")
    open_review.add_argument("--database", required=True)
    open_review.add_argument("--lot", required=True)
    open_review.add_argument("--reason", default="")
    open_review.add_argument("--force", action="store_true")
    claim = dispute_commands.add_parser("claim", help="领取复核工作")
    claim.add_argument("--database", required=True)
    claim.add_argument("--work", required=True)
    claim.add_argument("--worker", required=True)
    complete = dispute_commands.add_parser("complete", help="提交复检凭据并结束工单")
    complete.add_argument("--database", required=True)
    complete.add_argument("--work", required=True)
    complete.add_argument("--worker", required=True)
    complete.add_argument("--receipt", required=True, help="复检凭据 JSON 文件")
    complete.add_argument("--revision", default="", help="可选的人工结论变更 JSON 文件")
    decide = dispute_commands.add_parser("disposition", help="主管处置")
    decide.add_argument("--database", required=True)
    decide.add_argument("--lot", required=True)
    decide.add_argument("--decision", required=True,
                        choices=["accept_machine", "accept_human", "hold", "reinspect",
                                 "release_unverified", "release_followup"])
    decide.add_argument("--actor", required=True)
    decide.add_argument("--note", default="")
    revise = dispute_commands.add_parser("revise", help="补报/更正/撤回人工结论并重评")
    revise.add_argument("--database", required=True)
    revise.add_argument("--revision", required=True, help="结论变更 JSON 文件")
    followup = dispute_commands.add_parser("followup", help="已放行果批的后续处置")
    followup.add_argument("--database", required=True)
    followup.add_argument("--lot", required=True)
    followup.add_argument("--action", required=True)
    followup.add_argument("--actor", required=True)
    followup.add_argument("--note", default="")
    dispatch = commands.add_parser("dispatch", help="在放行约束下分配出库目的地")
    dispatch.add_argument("--database", required=True)
    dispatch.add_argument("--lot", required=True)
    release = commands.add_parser("release", help="在争议复核约束下放行果批")
    release.add_argument("--database", required=True)
    release.add_argument("--lot", required=True)
    return root


def _load_object(path: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError("JSON file must contain an object")
    return value


def _receipt(path: str) -> InspectionReceipt:
    value = _load_object(path)
    return InspectionReceipt(**{key: str(value[key]) for key in (
        "receipt_id", "lot_id", "origin_code", "line_id", "camera_batch",
        "inspected_grade", "machine_grade", "sampling_reason", "inspected_at")})


def _revision(path: str) -> InspectionRevision:
    value = _load_object(path)
    return InspectionRevision(**{key: str(value.get(key, "")) for key in (
        "revision_id", "receipt_id", "lot_id", "kind", "inspected_grade",
        "note", "actor", "revised_at")})


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "run":
        result = run(args.fixture, args.database, args.output)
    elif args.command == "verify":
        result = verify(args.database, args.output)
    elif args.command == "inspect":
        result = Application(args.database).inspect()
    elif args.command == "dispatch":
        result = Application(args.database).dispatch(args.lot)
    elif args.command == "release":
        result = Application(args.database).release(args.lot)
    else:
        application = Application(args.database)
        disputes = application.disputes
        if args.dispute_command == "analyse":
            result = disputes.analyse(args.lot)
        elif args.dispute_command == "board":
            result = disputes.board()
        elif args.dispute_command == "trace":
            result = disputes.trace(args.lot)
        elif args.dispute_command == "open-review":
            result = disputes.open_review(args.lot, reason=args.reason or None, force=args.force)
        elif args.dispute_command == "claim":
            result = disputes.claim(args.work, args.worker)
        elif args.dispute_command == "complete":
            result = disputes.complete_review(
                args.work, args.worker, _receipt(args.receipt),
                _revision(args.revision) if args.revision else None)
        elif args.dispute_command == "disposition":
            result = disputes.record_disposition(args.lot, args.decision, args.actor, args.note)
        elif args.dispute_command == "revise":
            result = disputes.revise_inspection(_revision(args.revision))
        else:
            result = disputes.record_followup(args.lot, args.action, args.note, args.actor)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
