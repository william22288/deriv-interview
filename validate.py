import json
import re
import sys
from collections import Counter
from datetime import datetime
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
            for key in (
                "stage",
                "timestamp",
                "provider",
                "model",
                "provider_response_id",
                "prompt_hash",
                "input_artifacts",
                "input_report_ids",
                "output_artifact",
            ):
                if key not in record:
                    fail(f"llm_calls.jsonl line {line_no} missing {key}")
            records.append(record)
    return records


def check_stage_order(state: Dict[str, Any]) -> None:
    stage_records = state.get("stages", [])
    observed = [item["stage"] for item in stage_records]
    if observed != pipeline.STAGE_ORDER:
        fail(f"Pipeline stages are out of order. Expected {pipeline.STAGE_ORDER}, observed {observed}")
    inputs_by_stage = {item["stage"]: set(item.get("input_artifacts", [])) for item in stage_records}
    required_inputs = {
        "REPORTS_NORMALISED": {"inspection_reports.json", "parts_catalog.json"},
        "REPORTS_INTERPRETED": {"normalised_reports.json", "inspection_reports.json"},
        "PATTERNS_GROUPED": {"interpreted_reports.json"},
        "PRIORITIES_COMPUTED": {"interpreted_reports.json", "patterns.json"},
        "CASES_SELECTED": {"priority_scores.json"},
        "ROUTING_COMPLETE": {"priority_scores.json", "patterns.json"},
        "EXPLANATIONS_GENERATED": {"priority_scores.json", "patterns.json", "case_routing.json"},
    }
    for stage, expected in required_inputs.items():
        if not expected.issubset(inputs_by_stage[stage]):
            fail(f"Pipeline stage {stage} is missing required input artifacts: {sorted(expected - inputs_by_stage[stage])}")


def check_all_reports_processed(
    reports: List[Dict[str, Any]],
    normalised: List[Dict[str, Any]],
    interpreted: List[Dict[str, Any]],
    scores: List[Dict[str, Any]],
) -> None:
    report_ids = [report["report_id"] for report in reports]
    if len(report_ids) != len(set(report_ids)):
        fail("inspection_reports.json contains duplicate report IDs")
    for name, rows in (
        ("normalised_reports.json", normalised),
        ("interpreted_reports.json", interpreted),
        ("priority_scores.json", scores),
    ):
        observed = [item.get("report_id") for item in rows if isinstance(item, dict)]
        if len(observed) != len(rows) or len(observed) != len(set(observed)) or set(observed) != set(report_ids):
            fail(f"{name} does not contain exactly one record for every input report")


def check_normalised_reports(
    reports: List[Dict[str, Any]],
    catalog: List[Dict[str, Any]],
    normalised: List[Dict[str, Any]],
) -> None:
    raw_by_id = {report["report_id"]: report for report in reports}
    part_by_id = {part["part_id"]: part for part in catalog}
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
        part = part_by_id[raw["part_id"]]
        if (
            item["part_id"] != raw["part_id"]
            or item["part_name"] != part["part_name"]
            or item["product_line"] != part["product_line"]
        ):
            fail(f"Normalised report {item['report_id']} has incorrect joined part metadata")
        if not isinstance(item["translated"], bool) or not isinstance(item["original_language"], str):
            fail(f"Normalised report {item['report_id']} has invalid language metadata")
        english_source = pipeline.is_english(item["original_language"])
        if item["translated"] == english_source:
            fail(f"Normalised report {item['report_id']} has a translation flag inconsistent with its language")
        if not pipeline.is_english(item["original_language"]):
            if not item["translated"]:
                fail(f"Non-English report {item['report_id']} was not marked translated")
            if item["note_for_interpretation"] == item["original_operator_note"]:
                fail(f"Non-English report {item['report_id']} did not include translated interpretation text")
        elif item["note_for_interpretation"] != item["original_operator_note"]:
            fail(f"English report {item['report_id']} was unexpectedly rewritten")


def check_interpretation_translation(
    normalised: List[Dict[str, Any]],
    interpreted: List[Dict[str, Any]],
) -> None:
    normalised_by_id = {item["report_id"]: item for item in normalised}
    required = {
        "report_id",
        "defect_category",
        "severity",
        "disposition",
        "reason_short",
        "translated",
        "original_language",
    }
    for item in interpreted:
        missing = required - item.keys()
        if missing:
            fail(f"Interpretation {item.get('report_id')} is missing fields: {sorted(missing)}")
        source = normalised_by_id[item["report_id"]]
        if item["translated"] != source["translated"] or item["original_language"] != source["original_language"]:
            fail(f"Interpretation {item['report_id']} has translation metadata inconsistent with normalization")
        if not isinstance(item["reason_short"], str) or not item["reason_short"].strip():
            fail(f"Interpretation {item['report_id']} has no reason_short")


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


def check_patterns(
    patterns: List[Dict[str, Any]],
    reports: List[Dict[str, Any]],
    interpreted: List[Dict[str, Any]],
) -> None:
    report_ids = {report["report_id"] for report in reports}
    recurring_categories = sum(
        count >= 2
        for count in Counter(item["defect_category"] for item in interpreted).values()
    )
    if recurring_categories >= 2 and len(patterns) < 2:
        fail("At least two distinct recurring patterns are expected when multiple defect categories recur")
    pattern_ids = [pattern.get("pattern_id") for pattern in patterns]
    if len(pattern_ids) != len(set(pattern_ids)):
        fail("patterns.json contains duplicate pattern IDs")
    for pattern in patterns:
        required = {
            "pattern_id",
            "title",
            "supporting_report_ids",
            "pattern_strength",
            "likely_driver",
            "recommended_action",
        }
        missing = required - pattern.keys()
        if missing:
            fail(f"Pattern {pattern.get('pattern_id')} is missing fields: {sorted(missing)}")
        supporting = pattern.get("supporting_report_ids", [])
        if not isinstance(supporting, list) or len(supporting) != len(set(supporting)):
            fail(f"Pattern {pattern.get('pattern_id')} has an invalid supporting_report_ids list")
        if len(supporting) < 2:
            fail(f"Pattern {pattern.get('pattern_id')} has fewer than two supporting reports")
        for field in ("title", "likely_driver", "recommended_action"):
            if not isinstance(pattern[field], str) or not pattern[field].strip():
                fail(f"Pattern {pattern.get('pattern_id')} has no {field}")
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
        report_id = score["report_id"]
        expected = recompute_score(report_by_id[score["report_id"]], interp_by_id[score["report_id"]], pattern_count[score["report_id"]])
        if score["priority_score"] != expected["priority_score"]:
            fail(f"Score mismatch for {score['report_id']}: expected {expected['priority_score']}, observed {score['priority_score']}")
        if score["score_components"] != expected["score_components"]:
            fail(f"Score components mismatch for {score['report_id']}")
        expected_pattern_ids = sorted(
            pattern["pattern_id"] for pattern in patterns if report_id in pattern["supporting_report_ids"]
        )
        if score.get("pattern_ids") != expected_pattern_ids:
            fail(f"Pattern membership mismatch for {report_id}")
    expected_order = sorted(scores, key=lambda item: (-item["priority_score"], item["timestamp"], item["report_id"]))
    if [item["report_id"] for item in scores] != [item["report_id"] for item in expected_order]:
        fail("priority_scores.json is not sorted by deterministic priority order")
    flagged = [item for item in scores if item["flagged_for_review"]]
    expected_flagged_ids = [item["report_id"] for item in expected_order[: min(5, len(expected_order))]]
    if [item["report_id"] for item in flagged] != expected_flagged_ids:
        fail("Flagged review cases are not the top 5 computed scores")


def check_routes(routes: List[Dict[str, Any]], scores: List[Dict[str, Any]]) -> None:
    flagged_ids = {item["report_id"] for item in scores if item["flagged_for_review"]}
    routed_ids = [item["report_id"] for item in routes]
    if len(routed_ids) != len(set(routed_ids)) or set(routed_ids) != flagged_ids:
        fail("case_routing.json does not contain exactly the flagged cases")
    for route in routes:
        if not isinstance(route.get("teams"), list) or not route["teams"]:
            fail(f"Route {route['report_id']} must include at least one owner team")
        invalid = set(route.get("teams", [])) - pipeline.OWNER_TEAMS
        if invalid:
            fail(f"Route {route['report_id']} uses invalid owner teams: {sorted(invalid)}")
        if not route.get("briefing_note"):
            fail(f"Route {route['report_id']} has no briefing note")


def check_review_summaries(scores: List[Dict[str, Any]], patterns: List[Dict[str, Any]]) -> None:
    text = Path("review_summaries.md").read_text(encoding="utf-8")
    sections = re.split(r"(?m)^##\s+", text)
    section_by_id = {}
    for section in sections[1:]:
        report_id = section.splitlines()[0].split(" - ", 1)[0].strip()
        section_by_id[report_id] = section
    for score in scores:
        if not score["flagged_for_review"]:
            continue
        report_id = score["report_id"]
        section = section_by_id.get(report_id)
        if section is None:
            fail(f"Flagged case {report_id} is missing from review_summaries.md")
        for field_value, description in (
            (score["part_name"], "part name"),
            (score["defect_category"], "defect category"),
            (score["severity"], "severity"),
            (score["disposition"], "disposition"),
            (str(score["priority_score"]), "priority score"),
        ):
            if field_value not in section:
                fail(f"Review summary for {report_id} is missing its {description}")
        required_labels = (
            "- Recurring pattern:",
            "- Why prioritised:",
            "- Human review gate:",
        )
        for label in required_labels:
            if label not in section:
                fail(f"Review summary for {report_id} is missing {label}")
        pattern_titles = [
            pattern["title"]
            for pattern in patterns
            if report_id in pattern["supporting_report_ids"]
        ]
        pattern_line = next(
            (line for line in section.splitlines() if line.startswith("- Recurring pattern:")),
            "",
        )
        expected_pattern_text = (
            "No recurring pattern membership detected."
            if not pattern_titles
            else ", ".join(pattern_titles)
        )
        if expected_pattern_text not in pattern_line:
            fail(f"Review summary for {report_id} does not correctly describe pattern membership")
        if "before any production" not in section.lower():
            fail(f"Review summary for {report_id} does not state what must be verified before a production decision")


def check_optional_artifacts(
    reports: List[Dict[str, Any]],
    catalog: List[Dict[str, Any]],
    interpreted: List[Dict[str, Any]],
    patterns: List[Dict[str, Any]],
    scores: List[Dict[str, Any]],
    trends: Dict[str, Any],
    disagreements: List[Dict[str, Any]],
    alert: Dict[str, Any],
) -> None:
    required_trend_fields = {
        "defect_distribution",
        "hourly_distribution",
        "worsening_pattern_ids",
        "most_affected_station",
        "most_affected_product_line",
    }
    if not required_trend_fields.issubset(trends):
        fail(f"shift_trends.json is missing fields: {sorted(required_trend_fields - trends.keys())}")
    expected_distribution = dict(sorted(Counter(item["defect_category"] for item in interpreted).items()))
    if trends["defect_distribution"] != expected_distribution:
        fail("shift_trends.json defect distribution does not match interpreted reports")
    pattern_ids = {pattern["pattern_id"] for pattern in patterns}
    if set(trends["worsening_pattern_ids"]) - pattern_ids:
        fail("shift_trends.json references unknown worsening patterns")
    interpreted_by_id = {item["report_id"]: item for item in interpreted}
    expected_hourly: Dict[str, Counter] = {}
    station_counts: Counter = Counter()
    product_line_counts: Counter = Counter()
    part_by_id = {part["part_id"]: part for part in catalog}
    report_by_id = {report["report_id"]: report for report in reports}
    for report in reports:
        hour = pipeline.parse_ts(report["timestamp"]).strftime("%H:00")
        expected_hourly.setdefault(hour, Counter())[interpreted_by_id[report["report_id"]]["defect_category"]] += 1
        station_counts[report["station"]] += 1
        product_line_counts[part_by_id[report["part_id"]]["product_line"]] += 1
    expected_hourly_data = {
        hour: dict(counts) for hour, counts in sorted(expected_hourly.items())
    }
    if trends["hourly_distribution"] != expected_hourly_data:
        fail("shift_trends.json hourly distribution does not match input reports")
    if trends["most_affected_station"] != (station_counts.most_common(1)[0][0] if station_counts else None):
        fail("shift_trends.json identifies the wrong most-affected station")
    if trends["most_affected_product_line"] != (
        product_line_counts.most_common(1)[0][0] if product_line_counts else None
    ):
        fail("shift_trends.json identifies the wrong most-affected product line")
    expected_worsening = []
    for pattern in patterns:
        ordered = sorted(
            (report_by_id[report_id] for report_id in pattern["supporting_report_ids"]),
            key=lambda item: item["timestamp"],
        )
        first = ordered[0]["measurements"]
        last = ordered[-1]["measurements"]
        if any(
            last[measurement] > first[measurement]
            for measurement in ("void_ratio", "misalignment_mm", "scratch_length_mm")
        ):
            expected_worsening.append(pattern["pattern_id"])
    if trends["worsening_pattern_ids"] != expected_worsening:
        fail("shift_trends.json worsening assessment does not match pattern measurements")

    report_ids = {report["report_id"] for report in reports}
    disagreement_ids = []
    for item in disagreements:
        required = {"report_id", "disagreement_reason", "recommended_follow_up"}
        missing = required - item.keys()
        if missing:
            fail(f"Disagreement record is missing fields: {sorted(missing)}")
        if item["report_id"] not in report_ids:
            fail(f"Disagreement record references unknown report {item['report_id']}")
        if not item["disagreement_reason"] or not item["recommended_follow_up"]:
            fail(f"Disagreement record for {item['report_id']} is incomplete")
        disagreement_ids.append(item["report_id"])
    if len(disagreement_ids) != len(set(disagreement_ids)):
        fail("disagreement_cases.json contains duplicate report IDs")

    if alert.get("rating") not in {"green", "yellow", "orange", "red"}:
        fail("alert_rating.json has an invalid rating")
    justification = alert.get("justification", {})
    flagged = [item for item in scores if item["flagged_for_review"]]
    if justification.get("top_report_ids") != [item["report_id"] for item in flagged]:
        fail("alert_rating.json does not identify the top flagged reports")
    expected_scores = {item["report_id"]: item["priority_score"] for item in flagged}
    if justification.get("top_priority_scores") != expected_scores:
        fail("alert_rating.json priority scores do not match priority_scores.json")
    if justification.get("pattern_ids") != [pattern["pattern_id"] for pattern in patterns]:
        fail("alert_rating.json does not identify all detected patterns")
    if not isinstance(justification.get("multiple_stations_affected"), bool):
        fail("alert_rating.json is missing its multiple-stations assessment")
    if not isinstance(justification.get("multiple_parts_affected"), bool):
        fail("alert_rating.json is missing its multiple-parts assessment")
    reports_by_id = {report["report_id"]: report for report in reports}
    expected_stations = {reports_by_id[item["report_id"]]["station"] for item in flagged}
    expected_parts = {reports_by_id[item["report_id"]]["part_id"] for item in flagged}
    if justification["multiple_stations_affected"] != (len(expected_stations) > 1):
        fail("alert_rating.json has an incorrect multiple-stations assessment")
    if justification["multiple_parts_affected"] != (len(expected_parts) > 1):
        fail("alert_rating.json has an incorrect multiple-parts assessment")
    max_score = max((item["priority_score"] for item in scores), default=0)
    strong_patterns = [pattern for pattern in patterns if pattern["pattern_strength"] == "strong"]
    if max_score >= 90 or len(strong_patterns) >= 3:
        expected_rating = "red"
    elif max_score >= 70 and len(patterns) >= 2:
        expected_rating = "orange"
    elif max_score >= 45 or patterns:
        expected_rating = "yellow"
    else:
        expected_rating = "green"
    if alert["rating"] != expected_rating:
        fail("alert_rating.json rating does not match its deterministic inputs")
    if not isinstance(alert.get("recommended_operational_posture"), str) or not alert["recommended_operational_posture"].strip():
        fail("alert_rating.json has no recommended operational posture")

    plan = Path("reinspection_plan.md").read_text(encoding="utf-8")
    headings = (
        "First-Sample Parts",
        "Priority Stations",
        "Defect Signatures",
        "Stop Criteria",
        "Continue Criteria",
        "Owner Team",
    )
    for heading in headings:
        if heading not in plan:
            fail(f"reinspection_plan.md is missing {heading}")
    for heading in headings:
        section = re.search(
            rf"(?ms)^## {re.escape(heading)}\s*$\n(.*?)(?=^## |\Z)",
            plan,
        )
        if section is None or not any(line.strip().startswith("- ") for line in section.group(1).splitlines()):
            fail(f"reinspection_plan.md has no entries under {heading}")
    if "Stop release" not in plan or "Continue controlled production" not in plan:
        fail("reinspection_plan.md is missing explicit stop/continue criteria")
    owner_section = re.search(r"(?ms)^## Owner Team\s*$\n(.*?)(?=^## |\Z)", plan)
    if owner_section is None or not any(team in owner_section.group(1) for team in pipeline.OWNER_TEAMS):
        fail("reinspection_plan.md does not identify a controlled-vocabulary owner team")

    pattern_report_ids = {report_id for pattern in patterns for report_id in pattern["supporting_report_ids"]}
    affected = [score for score in scores if score["report_id"] in pattern_report_ids]
    fallback_scores = scores[:3]
    expected_plan_items = {
        "First-Sample Parts": sorted(
            {item["part_name"] for item in affected}
            or {item["part_name"] for item in fallback_scores}
        ),
        "Priority Stations": sorted(
            {item["station"] for item in affected}
            or {item["station"] for item in fallback_scores}
        ),
        "Defect Signatures": sorted(
            {item["defect_category"] for item in affected}
            or {item["defect_category"] for item in fallback_scores}
        ),
    }
    for heading, expected_items in expected_plan_items.items():
        section = re.search(
            rf"(?ms)^## {re.escape(heading)}\s*$\n(.*?)(?=^## |\Z)",
            plan,
        )
        observed_items = [
            line[2:].strip()
            for line in section.group(1).splitlines()
            if line.strip().startswith("- ")
        ]
        if observed_items != expected_items:
            fail(f"reinspection_plan.md has incorrect entries under {heading}")


def check_llm_logs(
    logs: List[Dict[str, Any]],
    reports: List[Dict[str, Any]],
    scores: List[Dict[str, Any]],
) -> None:
    all_report_ids = {report["report_id"] for report in reports}
    flagged_ids = {
        score["report_id"] for score in scores if score["flagged_for_review"]
    }
    expected = {
        "note_normalisation_translation": ("normalised_reports.json", {"inspection_reports.json", "parts_catalog.json"}),
        "report_interpretation": ("interpreted_reports.json", {"normalised_reports.json", "inspection_reports.json"}),
        "pattern_grouping": ("patterns.json", {"interpreted_reports.json"}),
        "case_routing": ("case_routing.json", {"priority_scores.json", "patterns.json"}),
    }
    for stage, (output, required_inputs) in expected.items():
        stage_logs = [record for record in logs if record["stage"] == stage]
        if len(stage_logs) != 1:
            fail(f"llm_calls.jsonl must contain exactly one successful call for stage {stage}")
        record = stage_logs[0]
        if (
            record["provider"] != "openai"
            or not isinstance(record["model"], str)
            or not record["model"].strip()
        ):
            fail(f"LLM log for {stage} does not identify a real configured provider and model")
        if not isinstance(record["provider_response_id"], str) or not record["provider_response_id"].strip():
            fail(f"LLM log for {stage} does not identify the provider's successful response")
        if record["output_artifact"] != output:
            fail(f"LLM log for {stage} names the wrong output artifact")
        if not required_inputs.issubset(set(record["input_artifacts"])):
            fail(f"LLM log for {stage} is missing required input artifacts")
        expected_ids = flagged_ids if stage == "case_routing" else all_report_ids
        observed_ids = record["input_report_ids"]
        if (
            not isinstance(observed_ids, list)
            or len(observed_ids) != len(set(observed_ids))
            or set(observed_ids) != expected_ids
        ):
            fail(f"LLM log for {stage} does not prove it received the required reports")
        try:
            timestamp = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
        except (TypeError, ValueError):
            fail(f"LLM log for {stage} has an invalid ISO-8601 timestamp")
        if timestamp.tzinfo is None:
            fail(f"LLM log for {stage} timestamp must include a timezone")
        if not re.fullmatch(r"[0-9a-f]{64}", record["prompt_hash"]):
            fail(f"LLM log for {stage} has an invalid prompt_hash")


def main() -> None:
    require_artifacts()
    reports = read_json("inspection_reports.json")
    catalog = read_json("parts_catalog.json")
    normalised = read_json("normalised_reports.json")
    interpreted = read_json("interpreted_reports.json")
    patterns = read_json("patterns.json")
    scores = read_json("priority_scores.json")
    routes = read_json("case_routing.json")
    trends = read_json("shift_trends.json")
    disagreements = read_json("disagreement_cases.json")
    alert = read_json("alert_rating.json")
    state = read_json("pipeline_state.json")
    logs = read_llm_logs()

    pipeline.validate_input_schema(reports, catalog)
    check_stage_order(state)
    check_all_reports_processed(reports, normalised, interpreted, scores)
    check_normalised_reports(reports, catalog, normalised)
    check_interpretation_translation(normalised, interpreted)
    check_vocabularies(interpreted, patterns)
    check_patterns(patterns, reports, interpreted)
    check_scores(reports, interpreted, patterns, scores)
    check_routes(routes, scores)
    check_review_summaries(scores, patterns)
    check_optional_artifacts(reports, catalog, interpreted, patterns, scores, trends, disagreements, alert)
    check_llm_logs(logs, reports, scores)
    print("Validation passed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        sys.exit(1)
