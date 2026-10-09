import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pipeline


REQUIRED_ARTIFACTS = [
    "inspection_reports.json",
    "parts_catalog.json",
    "normalised_reports.json",
    "interpreted_reports.json",
    "patterns.json",
    "priority_scores.json",
    "case_routing.json",
    "review_summaries.md",
    "shift_trends.json",
    "disagreement_cases.json",
    "alert_rating.json",
    "reinspection_plan.md",
    "llm_calls.jsonl",
    "pipeline_state.json",
]


def fail(message: str) -> None:
    raise AssertionError(message)


def read_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        fail(f"{path} is not valid JSON: {exc}")


def require_artifacts() -> None:
    missing = [path for path in REQUIRED_ARTIFACTS if not Path(path).exists()]
    if missing:
        fail(f"Missing required artifacts: {missing}")


def read_llm_logs() -> List[Dict[str, Any]]:
    records = []
    with open("llm_calls.jsonl", "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                fail(f"llm_calls.jsonl line {line_no} is invalid JSON: {exc}")
            for key in ("stage", "timestamp", "provider", "model", "prompt_hash", "input_artifacts", "output_artifact"):
                if key not in record:
                    fail(f"llm_calls.jsonl line {line_no} missing {key}")
            records.append(record)
    return records


def check_stage_order(state: Dict[str, Any]) -> None:
    observed = [item["stage"] for item in state.get("stages", [])]
    if observed != pipeline.STAGE_ORDER:
        fail(f"Pipeline stages are out of order. Expected {pipeline.STAGE_ORDER}, observed {observed}")


def check_all_reports_processed(
    reports: List[Dict[str, Any]],
    normalised: List[Dict[str, Any]],
    interpreted: List[Dict[str, Any]],
    scores: List[Dict[str, Any]],
) -> None:
    report_ids = {report["report_id"] for report in reports}
    if {item["report_id"] for item in normalised} != report_ids:
        fail("normalised_reports.json does not contain exactly all input reports")
    if {item["report_id"] for item in interpreted} != report_ids:
        fail("interpreted_reports.json does not contain exactly all input reports")
    if {item["report_id"] for item in scores} != report_ids:
        fail("priority_scores.json does not contain exactly all input reports")


def check_normalised_reports(reports: List[Dict[str, Any]], normalised: List[Dict[str, Any]]) -> None:
    raw_by_id = {report["report_id"]: report for report in reports}
    required = {
        "report_id",
        "part_id",
        "part_name",
        "product_line",
        "original_operator_note",
        "note_for_interpretation",
        "original_language",
        "translated",
    }
    for item in normalised:
        missing = required - item.keys()
        if missing:
            fail(f"Normalised report {item.get('report_id')} missing fields: {sorted(missing)}")
        raw = raw_by_id[item["report_id"]]
        if item["original_operator_note"] != raw["operator_note"]:
            fail(f"Normalised report {item['report_id']} did not preserve original note")
        if item["original_language"] != "en":
            if not item["translated"]:
                fail(f"Non-English report {item['report_id']} was not marked translated")
            if item["note_for_interpretation"] == item["original_operator_note"]:
                fail(f"Non-English report {item['report_id']} did not include translated interpretation text")


def check_vocabularies(interpreted: List[Dict[str, Any]], patterns: List[Dict[str, Any]]) -> None:
    for item in interpreted:
        if item["defect_category"] not in pipeline.DEFECT_CATEGORIES:
            fail(f"Invalid defect category in {item['report_id']}: {item['defect_category']}")
        if item["severity"] not in pipeline.SEVERITIES:
            fail(f"Invalid severity in {item['report_id']}: {item['severity']}")
        if item["disposition"] not in pipeline.DISPOSITIONS:
            fail(f"Invalid disposition in {item['report_id']}: {item['disposition']}")
    for pattern in patterns:
        if pattern["pattern_strength"] not in pipeline.PATTERN_STRENGTHS:
            fail(f"Invalid pattern strength in {pattern['pattern_id']}: {pattern['pattern_strength']}")


def check_patterns(patterns: List[Dict[str, Any]], reports: List[Dict[str, Any]]) -> None:
    report_ids = {report["report_id"] for report in reports}
    for pattern in patterns:
        supporting = pattern.get("supporting_report_ids", [])
        if len(supporting) < 2:
            fail(f"Pattern {pattern.get('pattern_id')} has fewer than two supporting reports")
        unknown = set(supporting) - report_ids
        if unknown:
            fail(f"Pattern {pattern.get('pattern_id')} references unknown reports: {sorted(unknown)}")


def recompute_score(
    report: Dict[str, Any],
    interpretation: Dict[str, Any],
    pattern_count: int,
) -> Dict[str, Any]:
    base = pipeline.BASE_PRIORITY[interpretation["severity"]]
    confidence = round(float(report["confidence"]) * 10, 2)
    evidence = int(report["image_count"]) * 2
    measured = pipeline.measurement_bonus(report["measurements"])
    pattern_bonus = 12 * pattern_count
    disposition = pipeline.DISPOSITION_BONUS[interpretation["disposition"]]
    total = round(base + confidence + evidence + measured + pattern_bonus + disposition, 2)
    return {
        "priority_score": total,
        "score_components": {
            "base_priority": base,
            "confidence_bonus": confidence,
            "evidence_bonus": evidence,
            "measurement_bonus": measured,
            "pattern_membership_bonus": pattern_bonus,
            "disposition_bonus": disposition,
        },
    }


def check_scores(
    reports: List[Dict[str, Any]],
    interpreted: List[Dict[str, Any]],
    patterns: List[Dict[str, Any]],
    scores: List[Dict[str, Any]],
) -> None:
    report_by_id = {report["report_id"]: report for report in reports}
    interp_by_id = {item["report_id"]: item for item in interpreted}
    pattern_count = {report["report_id"]: 0 for report in reports}
    for pattern in patterns:
        for report_id in pattern["supporting_report_ids"]:
            pattern_count[report_id] += 1
    for score in scores:
        expected = recompute_score(report_by_id[score["report_id"]], interp_by_id[score["report_id"]], pattern_count[score["report_id"]])
        if score["priority_score"] != expected["priority_score"]:
            fail(f"Score mismatch for {score['report_id']}: expected {expected['priority_score']}, observed {score['priority_score']}")
        if score["score_components"] != expected["score_components"]:
            fail(f"Score components mismatch for {score['report_id']}")
    expected_order = sorted(scores, key=lambda item: (-item["priority_score"], item["timestamp"], item["report_id"]))
    if [item["report_id"] for item in scores] != [item["report_id"] for item in expected_order]:
        fail("priority_scores.json is not sorted by deterministic priority order")
    flagged = [item for item in scores if item["flagged_for_review"]]
    expected_flagged_ids = [item["report_id"] for item in expected_order[: min(5, len(expected_order))]]
    if [item["report_id"] for item in flagged] != expected_flagged_ids:
        fail("Flagged review cases are not the top 5 computed scores")


def check_routes(routes: List[Dict[str, Any]], scores: List[Dict[str, Any]]) -> None:
    flagged_ids = {item["report_id"] for item in scores if item["flagged_for_review"]}
    routed_ids = {item["report_id"] for item in routes}
    if routed_ids != flagged_ids:
        fail("case_routing.json does not contain exactly the flagged cases")
    for route in routes:
        invalid = set(route.get("teams", [])) - pipeline.OWNER_TEAMS
        if invalid:
            fail(f"Route {route['report_id']} uses invalid owner teams: {sorted(invalid)}")
        if not route.get("briefing_note"):
            fail(f"Route {route['report_id']} has no briefing note")


def check_review_summaries(scores: List[Dict[str, Any]]) -> None:
    text = Path("review_summaries.md").read_text(encoding="utf-8")
    lowered = text.lower()
    if "human review gate" not in lowered:
        fail("review_summaries.md does not include a human review gate note")
    for score in scores:
        if score["flagged_for_review"] and score["report_id"] not in text:
            fail(f"Flagged case {score['report_id']} is missing from review_summaries.md")


def check_llm_logs(logs: List[Dict[str, Any]]) -> None:
    stages = {record["stage"] for record in logs}
    for required in ("report_interpretation", "pattern_grouping", "case_routing"):
        if required not in stages:
            fail(f"llm_calls.jsonl missing stage {required}")
    pattern_logs = [record for record in logs if record["stage"] == "pattern_grouping"]
    if not any("interpreted_reports.json" in record["input_artifacts"] for record in pattern_logs):
        fail("Pattern grouping log does not include interpreted_reports.json as an input artifact")


def main() -> None:
    require_artifacts()
    reports = read_json("inspection_reports.json")
    catalog = read_json("parts_catalog.json")
    normalised = read_json("normalised_reports.json")
    interpreted = read_json("interpreted_reports.json")
    patterns = read_json("patterns.json")
    scores = read_json("priority_scores.json")
    routes = read_json("case_routing.json")
    read_json("shift_trends.json")
    read_json("disagreement_cases.json")
    read_json("alert_rating.json")
    state = read_json("pipeline_state.json")
    logs = read_llm_logs()

    pipeline.validate_input_schema(reports, catalog)
    check_stage_order(state)
    check_all_reports_processed(reports, normalised, interpreted, scores)
    check_normalised_reports(reports, normalised)
    check_vocabularies(interpreted, patterns)
    check_patterns(patterns, reports)
    check_scores(reports, interpreted, patterns, scores)
    check_routes(routes, scores)
    check_review_summaries(scores)
    check_llm_logs(logs)
    print("Validation passed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        sys.exit(1)
