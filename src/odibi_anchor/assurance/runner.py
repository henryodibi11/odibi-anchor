"""Contained command-line harness for public assurance qualification artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

from odibi_anchor.assurance.qualification import (
    OutcomeComparison,
    QualificationRun,
    ScenarioJudgement,
    build_scorecard,
    canonical_json_bytes,
    compare_cohorts,
    load_scenario_manifest,
    render_scorecard_markdown,
    validate_run,
)

QUALIFIED = 0
OUTCOME_FAILURE = 1
INVALID_EVIDENCE = 2
HARNESS_UNAVAILABLE = 3


def packaged_resource(name: str) -> Path:
    """Return one public package resource as a concrete path."""
    return Path(str(files("odibi_anchor.assurance").joinpath("resources", name)))


def _load(path: Path) -> Any:
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON: {path}") from exc
    if raw != canonical_json_bytes(value):
        raise ValueError(f"non-canonical JSON: {path}")
    return value


def _write(output: Path, name: str, value: object) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    destination = output / name
    if destination.exists():
        raise ValueError(f"refusing to overwrite retained artifact: {destination}")
    destination.write_bytes(canonical_json_bytes(value))
    return destination


def validate_evaluators(manifest: Path, evaluator_root: Path) -> tuple[str, ...]:
    """Verify external evaluator files by digest without loading or copying their content."""
    scenarios = load_scenario_manifest(manifest)
    unavailable: list[str] = []
    for scenario in scenarios:
        path = evaluator_root / scenario.evaluator_reference
        if not path.is_file():
            unavailable.append(scenario.scenario_id)
            continue
        actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != scenario.evaluator_digest:
            raise ValueError(f"evaluator digest mismatch: {scenario.scenario_id}")
    return tuple(unavailable)


def _validate_command(args: argparse.Namespace) -> int:
    unavailable = validate_evaluators(args.manifest, args.evaluator_root)
    if unavailable:
        print(json.dumps({"status": "unavailable", "scenarios": unavailable}), file=sys.stderr)
        return HARNESS_UNAVAILABLE
    print(json.dumps({"status": "valid", "scenario_count": len(load_scenario_manifest(args.manifest))}))
    return QUALIFIED


def _execute_command(args: argparse.Namespace) -> int:
    unavailable = validate_evaluators(args.manifest, args.evaluator_root)
    if unavailable:
        _write(
            args.output,
            "execution-status.json",
            {
                "promotion_claim": False,
                "status": "unavailable",
                "unavailable_scenarios": list(unavailable),
            },
        )
        return HARNESS_UNAVAILABLE
    matrix = _load(args.matrix)
    if not isinstance(matrix, dict) or set(matrix) != {"judgements", "runs", "synthetic"}:
        raise ValueError("matrix must contain exactly judgements, runs, and synthetic")
    runs = tuple(QualificationRun.from_dict(value) for value in matrix["runs"])
    judgements = tuple(ScenarioJudgement.from_dict(value) for value in matrix["judgements"])
    manifest = load_scenario_manifest(args.manifest)
    for run in runs:
        validate_run(run, manifest)
    _write(args.output, "runs.json", [item.to_dict() for item in runs])
    _write(args.output, "judgements.json", [item.to_dict() for item in judgements])
    _write(
        args.output,
        "execution-status.json",
        {
            "promotion_claim": False,
            "status": "synthetic_complete" if matrix["synthetic"] else "complete",
            "synthetic": bool(matrix["synthetic"]),
        },
    )
    return QUALIFIED


def _records(directory: Path) -> tuple[tuple[QualificationRun, ...], tuple[ScenarioJudgement, ...]]:
    runs = tuple(QualificationRun.from_dict(value) for value in _load(directory / "runs.json"))
    judgements = tuple(ScenarioJudgement.from_dict(value) for value in _load(directory / "judgements.json"))
    return runs, judgements


def _score_command(args: argparse.Namespace) -> int:
    candidate = build_scorecard(*_records(args.runs))
    baseline = build_scorecard(*_records(args.baseline))
    comparison = compare_cohorts(candidate, baseline)
    _write(args.output, "candidate-scorecard.json", candidate.to_dict())
    _write(args.output, "baseline-scorecard.json", baseline.to_dict())
    _write(args.output, "comparison.json", comparison.to_dict())
    report = render_scorecard_markdown(candidate)
    report_path = args.output / "scorecard.md"
    if report_path.exists():
        raise ValueError(f"refusing to overwrite retained artifact: {report_path}")
    report_path.write_text(report, encoding="utf-8")
    return QUALIFIED if comparison.comparable and not comparison.regressions else OUTCOME_FAILURE


def _packet_command(args: argparse.Namespace) -> int:
    comparison = OutcomeComparison.from_dict(_load(args.comparison))
    packet = {
        "comparison": comparison.to_dict(),
        "control_version": args.control,
        "promotion_claim": False,
        "status": "pending_policy_assessment",
    }
    _write(args.output, "promotion-packet.json", packet)
    return OUTCOME_FAILURE


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m odibi_anchor.assurance.runner")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--manifest", type=Path, default=packaged_resource("scenario_manifest.json"))
    validate.add_argument("--evaluator-root", type=Path, required=True)
    validate.set_defaults(handler=_validate_command)
    execute = commands.add_parser("execute")
    execute.add_argument("--manifest", type=Path, default=packaged_resource("scenario_manifest.json"))
    execute.add_argument("--evaluator-root", type=Path, required=True)
    execute.add_argument("--matrix", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.set_defaults(handler=_execute_command)
    score = commands.add_parser("score")
    score.add_argument("--runs", type=Path, required=True)
    score.add_argument("--baseline", type=Path, required=True)
    score.add_argument("--output", type=Path, required=True)
    score.set_defaults(handler=_score_command)
    packet = commands.add_parser("promotion-packet")
    packet.add_argument("--comparison", type=Path, required=True)
    packet.add_argument("--control", required=True)
    packet.add_argument("--output", type=Path, required=True)
    packet.set_defaults(handler=_packet_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the contained harness with stable evidence-status exit codes."""
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (OSError, TypeError, ValueError) as exc:
        print(f"invalid or incomplete qualification evidence: {exc}", file=sys.stderr)
        return INVALID_EVIDENCE


if __name__ == "__main__":
    raise SystemExit(main())
