import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple
from urllib import request, error


DEFECT_CATEGORIES = {
    "solder_voiding",
    "pin_misalignment",
    "surface_scratch",
    "no_material_issue",
    "uncertain",
}
SEVERITIES = {"critical", "high", "medium", "low"}
DISPOSITIONS = {"hold", "reinspect", "monitor", "pass"}
OWNER_TEAMS = {"Quality", "Process Engineering", "Manufacturing", "Supplier Quality"}
PATTERN_STRENGTHS = {"strong", "moderate", "weak"}

STAGE_ORDER = [
    "INIT",
    "INPUTS_LOADED",
    "REPORTS_NORMALISED",
    "REPORTS_INTERPRETED",
    "PATTERNS_GROUPED",
    "PRIORITIES_COMPUTED",
    "CASES_SELECTED",
    "ROUTING_COMPLETE",
    "EXPLANATIONS_GENERATED",
    "VALIDATION_COMPLETE",
    "RESULTS_FINALISED",
]

BASE_PRIORITY = {"critical": 40, "high": 25, "medium": 10, "low": 3}
DISPOSITION_BONUS = {"hold": 12, "reinspect": 6, "monitor": 2, "pass": 0}

GENERATED_ARTIFACTS = [
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


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def load_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_stage(
    stage_log: List[Dict[str, Any]],
    stage: str,
    artifacts: Iterable[str],
    input_artifacts: Iterable[str] = (),
) -> None:
    stage_log.append(
        {
            "stage": stage,
            "timestamp": utc_now(),
            "input_artifacts": list(input_artifacts),
            "artifacts": list(artifacts),
        }
    )


def log_llm_call(
    stage: str,
    provider: str,
    model: str,
    prompt_payload: Any,
    input_artifacts: List[str],
    output_artifact: str,
    provider_response_id: str,
    input_report_ids: Iterable[str],
) -> None:
    record = {
        "stage": stage,
        "timestamp": utc_now(),
        "provider": provider,
        "model": model,
        "provider_response_id": provider_response_id,
        "input_report_ids": list(input_report_ids),
        "prompt_hash": stable_hash(prompt_payload),
        "input_artifacts": input_artifacts,
        "output_artifact": output_artifact,
    }
    with open("llm_calls.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def validate_input_schema(reports: List[Dict[str, Any]], catalog: List[Dict[str, Any]]) -> None:
    if not isinstance(reports, list) or not isinstance(catalog, list):
        raise ValueError("Input files must contain JSON arrays.")
    report_required = {
        "report_id",
        "part_id",
        "station",
        "timestamp",
        "operator_note",
        "model_finding",
        "confidence",
        "measurements",
        "image_count",
    }
    catalog_required = {"part_id", "part_name", "product_line"}
    for item in catalog:
        missing = catalog_required - item.keys()
        if missing:
            raise ValueError(f"Catalog item missing fields: {sorted(missing)}")
    known_parts = {item["part_id"] for item in catalog}
    for report in reports:
        missing = report_required - report.keys()
        if missing:
            raise ValueError(f"Report {report.get('report_id', '<unknown>')} missing fields: {sorted(missing)}")
        if report["part_id"] not in known_parts:
            raise ValueError(f"Report {report['report_id']} references unknown part_id {report['part_id']}")
        measurements = report["measurements"]
        for key in ("void_ratio", "misalignment_mm", "scratch_length_mm"):
            if key not in measurements:
                raise ValueError(f"Report {report['report_id']} missing measurement {key}")
        parse_ts(report["timestamp"])


def load_inputs() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    reports = read_json("inspection_reports.json")
    catalog = read_json("parts_catalog.json")
    validate_input_schema(reports, catalog)
    return reports, catalog


def normalise_reports(reports: List[Dict[str, Any]], catalog: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    parts_by_id = {part["part_id"]: part for part in catalog}
    joined = []
    for report in reports:
        part = parts_by_id[report["part_id"]]
        joined.append(
            {
                "report_id": report["report_id"],
                "part_id": report["part_id"],
                "part_name": part["part_name"],
                "product_line": part["product_line"],
                "original_operator_note": report["operator_note"],
            }
        )

    prompt_payload = {
        "stage": "note_normalisation_translation",
        "schema": {
            "translations": [
                {
                    "report_id": "string",
                    "original_language": "ISO 639-1 language code, or a clear language name",
                    "note_for_interpretation": "English translation; preserve the original wording when already English",
                    "translated": "boolean; true exactly when the source note is not English",
                }
            ]
        },
        "instruction": (
            "For every supplied note, identify its language and translate the complete note into natural English "
            "when it is not English. Do not summarize or omit defect details. Preserve English notes exactly. "
            "Return exactly one translation record per report_id."
        ),
        "reports": joined,
    }
    output, response_id, response_model = call_llm_json(prompt_payload)
    translations = output.get("translations")
    validate_translations(translations, joined)
    translations_by_id = {item["report_id"]: item for item in translations}
    normalised = []
    for item in joined:
        translation = translations_by_id[item["report_id"]]
        normalised.append(
            {
                **item,
                "note_for_interpretation": translation["note_for_interpretation"],
                "original_language": translation["original_language"],
                "translated": translation["translated"],
            }
        )
    write_json("normalised_reports.json", normalised)
    log_llm_call(
        "note_normalisation_translation",
        "openai",
        response_model,
        prompt_payload,
        ["inspection_reports.json", "parts_catalog.json"],
        "normalised_reports.json",
        response_id,
        (item["report_id"] for item in joined),
    )
    return normalised


def is_english(language: str) -> bool:
    value = language.strip().casefold().replace("_", "-")
    return value in {"en", "eng", "english"} or value.startswith("en-")


def validate_translations(translations: Any, joined_reports: List[Dict[str, Any]]) -> None:
    if not isinstance(translations, list):
        raise ValueError("Translation response must contain a translations array.")
    expected_ids = [item["report_id"] for item in joined_reports]
    observed_ids = [item.get("report_id") for item in translations if isinstance(item, dict)]
    if len(observed_ids) != len(translations) or len(observed_ids) != len(set(observed_ids)):
        raise ValueError("Translation response contains invalid or duplicate report IDs.")
    if set(observed_ids) != set(expected_ids):
        raise ValueError("Translation response does not match input report IDs.")
    originals = {item["report_id"]: item["original_operator_note"] for item in joined_reports}
    for item in translations:
        if not isinstance(item.get("original_language"), str) or not item["original_language"].strip():
            raise ValueError(f"Translation for {item['report_id']} has no source language.")
        if not isinstance(item.get("note_for_interpretation"), str) or not item["note_for_interpretation"].strip():
            raise ValueError(f"Translation for {item['report_id']} has no interpretation text.")
        if not isinstance(item.get("translated"), bool):
            raise ValueError(f"Translation for {item['report_id']} has an invalid translated flag.")
        english_source = is_english(item["original_language"])
        if item["translated"] == english_source:
            raise ValueError(f"Translation flag does not match the source language for {item['report_id']}.")
        if english_source and item["note_for_interpretation"] != originals[item["report_id"]]:
            raise ValueError(f"English note {item['report_id']} must be preserved unchanged.")
        if not english_source and item["note_for_interpretation"] == originals[item["report_id"]]:
            raise ValueError(f"Non-English note {item['report_id']} was not translated.")


def call_openai_json(prompt_payload: Dict[str, Any]) -> Tuple[Any, str, str]:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    body = {
        "model": selected_llm_model(),
        "input": [
            {
                "role": "system",
                "content": "Return only valid JSON matching the requested schema.",
            },
            {
                "role": "user",
                "content": json.dumps(prompt_payload, ensure_ascii=False),
            },
        ],
        "text": {"format": {"type": "json_object"}},
    }
    req = request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except error.URLError as exc:
        raise RuntimeError(f"OpenAI request failed: {exc}") from exc
    text_parts = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                text_parts.append(content.get("text", ""))
    if not text_parts:
        raise RuntimeError("OpenAI response did not include output text.")
    response_id = payload.get("id")
    response_model = payload.get("model")
    if not isinstance(response_id, str) or not response_id.strip():
        raise RuntimeError("OpenAI response did not include a response ID.")
    if not isinstance(response_model, str) or not response_model.strip():
        raise RuntimeError("OpenAI response did not identify the model used.")
    return json.loads("".join(text_parts)), response_id, response_model


def validate_interpretations(interpreted: List[Dict[str, Any]], report_ids: Iterable[str]) -> None:
    if not isinstance(interpreted, list):
        raise ValueError("Interpretation response must contain an interpreted_reports array.")
    expected_ids = set(report_ids)
    observed_ids = [item.get("report_id") for item in interpreted if isinstance(item, dict)]
    if (
        len(observed_ids) != len(interpreted)
        or len(observed_ids) != len(set(observed_ids))
        or set(observed_ids) != expected_ids
    ):
        raise ValueError("Interpreted reports do not match input report IDs.")
    for item in interpreted:
        required = {
            "report_id",
            "defect_category",
            "severity",
            "disposition",
            "reason_short",
            "translated",
            "original_language",
        }
        missing = required - item.keys()
        if missing:
            raise ValueError(f"Interpretation {item.get('report_id')} is missing fields: {sorted(missing)}")
        if item["defect_category"] not in DEFECT_CATEGORIES:
            raise ValueError(f"Invalid defect category: {item['defect_category']}")
        if item["severity"] not in SEVERITIES:
            raise ValueError(f"Invalid severity: {item['severity']}")
        if item["disposition"] not in DISPOSITIONS:
            raise ValueError(f"Invalid disposition: {item['disposition']}")
        if not isinstance(item["reason_short"], str) or not item["reason_short"].strip():
            raise ValueError(f"Interpretation {item['report_id']} has no reason_short.")
        if not isinstance(item["translated"], bool) or not isinstance(item["original_language"], str):
            raise ValueError(f"Interpretation {item['report_id']} has invalid translation metadata.")


def call_llm_json(prompt_payload: Dict[str, Any]) -> Tuple[Any, str, str]:
    if selected_llm_provider() != "openai":
        raise RuntimeError("Only the OpenAI LLM provider is supported.")
    return call_openai_json(prompt_payload)


def selected_llm_provider() -> str:
    configured = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if configured and configured != "openai":
        raise RuntimeError(f"Unsupported LLM_PROVIDER '{configured}'; configure 'openai'.")
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError("OPENAI_API_KEY is not set; real LLM calls are required to run this pipeline.")
    return "openai"


def selected_llm_model() -> str:
    return os.environ.get("LLM_MODEL", "").strip() or "gpt-4o-mini"


def interpret_reports(reports: List[Dict[str, Any]], normalised: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    reports_without_original_notes = [
        {key: value for key, value in report.items() if key != "operator_note"}
        for report in reports
    ]
    prompt_payload = {
        "stage": "report_interpretation",
        "schema": {
            "report_id": "string",
            "defect_category": sorted(DEFECT_CATEGORIES),
            "severity": sorted(SEVERITIES),
            "disposition": sorted(DISPOSITIONS),
            "reason_short": "string",
            "translated": "boolean",
            "original_language": "string",
        },
        "instruction": (
            "Interpret every report using both its English note and structured measurements. "
            "Return exactly one record per report_id. Use only the provided controlled vocabulary values; "
            "do not invent category, severity, or disposition values."
        ),
        "normalised_reports": normalised,
        "structured_reports": reports_without_original_notes,
    }
    output, response_id, response_model = call_llm_json(prompt_payload)
    interpreted = output.get("interpreted_reports")
    validate_interpretations(interpreted, [report["report_id"] for report in reports])
    normalised_by_id = {item["report_id"]: item for item in normalised}
    for item in interpreted:
        source = normalised_by_id[item["report_id"]]
        if item["translated"] != source["translated"] or item["original_language"] != source["original_language"]:
            raise ValueError(f"Interpretation translation metadata does not match {item['report_id']}.")
    write_json("interpreted_reports.json", interpreted)
    log_llm_call(
        "report_interpretation",
        "openai",
        response_model,
        prompt_payload,
        ["normalised_reports.json", "inspection_reports.json"],
        "interpreted_reports.json",
        response_id,
        (report["report_id"] for report in reports),
    )
    return interpreted


def validate_patterns(patterns: List[Dict[str, Any]], report_ids: Iterable[str]) -> None:
    if not isinstance(patterns, list):
        raise ValueError("Pattern response must contain a patterns array.")
    valid_report_ids = set(report_ids)
    pattern_ids = []
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
            raise ValueError(f"Pattern is missing fields: {sorted(missing)}")
        pattern_ids.append(pattern["pattern_id"])
        if any(not isinstance(pattern[field], str) or not pattern[field].strip() for field in (
            "pattern_id", "title", "likely_driver", "recommended_action"
        )):
            raise ValueError(f"Pattern {pattern.get('pattern_id')} has empty text fields.")
        if pattern["pattern_strength"] not in PATTERN_STRENGTHS:
            raise ValueError(f"Invalid pattern strength: {pattern['pattern_strength']}")
        if not isinstance(pattern["supporting_report_ids"], list):
            raise ValueError(f"Pattern {pattern['pattern_id']} must list supporting report IDs.")
        if len(pattern["supporting_report_ids"]) < 2:
            raise ValueError(f"Pattern {pattern['pattern_id']} has fewer than two reports.")
        if len(pattern["supporting_report_ids"]) != len(set(pattern["supporting_report_ids"])):
            raise ValueError(f"Pattern {pattern['pattern_id']} repeats a supporting report ID.")
        unknown = set(pattern["supporting_report_ids"]) - valid_report_ids
        if unknown:
            raise ValueError(f"Pattern {pattern['pattern_id']} references unknown reports: {sorted(unknown)}")
    if len(pattern_ids) != len(set(pattern_ids)):
        raise ValueError("Pattern IDs must be unique.")


def group_patterns(
    reports: List[Dict[str, Any]],
    catalog: List[Dict[str, Any]],
    normalised: List[Dict[str, Any]],
    interpreted: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    context = []
    norm_by_id = {item["report_id"]: item for item in normalised}
    interp_by_id = {item["report_id"]: item for item in interpreted}
    catalog_by_id = {item["part_id"]: item for item in catalog}
    for report in reports:
        part = catalog_by_id[report["part_id"]]
        context.append(
            {
                "report_id": report["report_id"],
                "part_id": report["part_id"],
                "part_name": part["part_name"],
                "product_line": part["product_line"],
                "station": report["station"],
                "timestamp": report["timestamp"],
                "confidence": report["confidence"],
                "measurements": report["measurements"],
                "note_for_interpretation": norm_by_id[report["report_id"]]["note_for_interpretation"],
                "interpretation": interp_by_id[report["report_id"]],
            }
        )
    prompt_payload = {
        "stage": "pattern_grouping",
        "schema": {
            "pattern_id": "string",
            "title": "string",
            "supporting_report_ids": ["string"],
            "pattern_strength": sorted(PATTERN_STRENGTHS),
            "likely_driver": "string",
            "recommended_action": "string",
        },
        "instruction": (
            "Identify recurring issue patterns from the interpreted category and severity together with report "
            "metadata, timestamps, stations, measurements, confidence, and translated notes. Do not group from "
            "raw notes alone. Each pattern must have at least two distinct supporting report IDs."
        ),
        "reports": context,
    }
    output, response_id, response_model = call_llm_json(prompt_payload)
    patterns = output.get("patterns")
    validate_patterns(patterns, [report["report_id"] for report in reports])
    write_json("patterns.json", patterns)
    log_llm_call(
        "pattern_grouping",
        "openai",
        response_model,
        prompt_payload,
        ["interpreted_reports.json", "normalised_reports.json", "inspection_reports.json", "parts_catalog.json"],
        "patterns.json",
        response_id,
        (report["report_id"] for report in reports),
    )
    return patterns


def measurement_bonus(measurements: Dict[str, Any]) -> int:
    bonus = 0
    if measurements["void_ratio"] >= 0.20:
        bonus += 15
    if measurements["misalignment_mm"] >= 0.20:
        bonus += 10
    if measurements["scratch_length_mm"] >= 6.0:
        bonus += 8
    return bonus


def compute_priority_scores(
    reports: List[Dict[str, Any]],
    catalog: List[Dict[str, Any]],
    interpreted: List[Dict[str, Any]],
    patterns: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    interpreted_by_id = {item["report_id"]: item for item in interpreted}
    catalog_by_id = {item["part_id"]: item for item in catalog}
    pattern_ids_by_report: Dict[str, List[str]] = defaultdict(list)
    for pattern in patterns:
        for report_id in pattern["supporting_report_ids"]:
            pattern_ids_by_report[report_id].append(pattern["pattern_id"])

    scored = []
    for report in reports:
        interpretation = interpreted_by_id[report["report_id"]]
        part = catalog_by_id[report["part_id"]]
        base = BASE_PRIORITY[interpretation["severity"]]
        confidence_bonus = float(report["confidence"]) * 10
        evidence_bonus = int(report["image_count"]) * 2
        measured = measurement_bonus(report["measurements"])
        pattern_bonus = 12 * len(pattern_ids_by_report[report["report_id"]])
        disposition = DISPOSITION_BONUS[interpretation["disposition"]]
        total = base + confidence_bonus + evidence_bonus + measured + pattern_bonus + disposition
        scored.append(
            {
                "report_id": report["report_id"],
                "part_id": report["part_id"],
                "part_name": part["part_name"],
                "product_line": part["product_line"],
                "station": report["station"],
                "timestamp": report["timestamp"],
                "defect_category": interpretation["defect_category"],
                "severity": interpretation["severity"],
                "disposition": interpretation["disposition"],
                "priority_score": round(total, 2),
                "flagged_for_review": False,
                "rank": None,
                "pattern_ids": sorted(pattern_ids_by_report[report["report_id"]]),
                "score_components": {
                    "base_priority": base,
                    "confidence_bonus": round(confidence_bonus, 2),
                    "evidence_bonus": evidence_bonus,
                    "measurement_bonus": measured,
                    "pattern_membership_bonus": pattern_bonus,
                    "disposition_bonus": disposition,
                },
            }
        )
    scored.sort(key=lambda item: (-item["priority_score"], item["timestamp"], item["report_id"]))
    for index, item in enumerate(scored, start=1):
        item["rank"] = index
        item["flagged_for_review"] = index <= min(5, len(scored))
    write_json("priority_scores.json", scored)
    return scored


def validate_routes(routes: List[Dict[str, Any]], flagged_ids: Iterable[str]) -> None:
    if not isinstance(routes, list):
        raise ValueError("Routing response must contain a case_routing array.")
    expected = set(flagged_ids)
    observed = [route.get("report_id") for route in routes if isinstance(route, dict)]
    if len(observed) != len(routes) or len(observed) != len(set(observed)) or set(observed) != expected:
        raise ValueError("Case routing does not cover exactly the flagged cases.")
    for route in routes:
        required = {"report_id", "teams", "briefing_note"}
        missing = required - route.keys()
        if missing:
            raise ValueError(f"Routing for {route.get('report_id')} is missing fields: {sorted(missing)}")
        if not route.get("briefing_note"):
            raise ValueError(f"Routing for {route['report_id']} is missing a briefing note.")
        if not isinstance(route["teams"], list) or not route["teams"]:
            raise ValueError(f"Routing for {route['report_id']} must include at least one owner team.")
        invalid = set(route.get("teams", [])) - OWNER_TEAMS
        if invalid:
            raise ValueError(f"Routing for {route['report_id']} has invalid teams: {sorted(invalid)}")


def route_cases(flagged: List[Dict[str, Any]], patterns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    prompt_payload = {
        "stage": "case_routing",
        "schema": {
            "report_id": "string",
            "teams": sorted(OWNER_TEAMS),
            "briefing_note": "string",
        },
        "instruction": (
            "Route every flagged case to one or more relevant teams from the controlled vocabulary. "
            "Return concise internal handoff briefing notes and exactly one route per report_id."
        ),
        "flagged_cases": flagged,
        "patterns": patterns,
    }
    output, response_id, response_model = call_llm_json(prompt_payload)
    routes = output.get("case_routing")
    validate_routes(routes, [case["report_id"] for case in flagged])
    write_json("case_routing.json", routes)
    log_llm_call(
        "case_routing",
        "openai",
        response_model,
        prompt_payload,
        ["priority_scores.json", "patterns.json"],
        "case_routing.json",
        response_id,
        (case["report_id"] for case in flagged),
    )
    return routes


def generate_review_summaries(
    flagged: List[Dict[str, Any]],
    routes: List[Dict[str, Any]],
    patterns: List[Dict[str, Any]],
) -> None:
    route_by_id = {route["report_id"]: route for route in routes}
    pattern_by_id = {pattern["pattern_id"]: pattern for pattern in patterns}
    lines = ["# Reviewer Summaries", ""]
    for case in flagged:
        pattern_titles = [pattern_by_id[pid]["title"] for pid in case["pattern_ids"] if pid in pattern_by_id]
        pattern_line = ", ".join(pattern_titles) if pattern_titles else "No recurring pattern membership detected."
        components = case["score_components"]
        reason = (
            f"Prioritised because severity contributed {components['base_priority']} points, confidence/evidence added "
            f"{components['confidence_bonus'] + components['evidence_bonus']:.2f} points, measurements added "
            f"{components['measurement_bonus']} points, pattern membership added {components['pattern_membership_bonus']} "
            f"points, and disposition added {components['disposition_bonus']} points."
        )
        lines.extend(
            [
                f"## {case['report_id']} - {case['part_name']}",
                "",
                f"- Defect category: `{case['defect_category']}`",
                f"- Severity: `{case['severity']}`",
                f"- Disposition: `{case['disposition']}`",
                f"- Deterministic priority score: `{case['priority_score']}`",
                f"- Recurring pattern: {pattern_line}",
                f"- Owner teams: {', '.join(route_by_id[case['report_id']]['teams'])}",
                f"- Why prioritised: {reason}",
                (
                    "- Human review gate: Verify the supporting images, measurements, part context, and recurring "
                    "pattern evidence before any production hold, release, rework, or process-change decision is taken."
                ),
                "",
            ]
        )
    Path("review_summaries.md").write_text("\n".join(lines), encoding="utf-8")


def generate_shift_trends(
    reports: List[Dict[str, Any]],
    catalog: List[Dict[str, Any]],
    interpreted: List[Dict[str, Any]],
    patterns: List[Dict[str, Any]],
) -> None:
    interp_by_id = {item["report_id"]: item for item in interpreted}
    catalog_by_id = {item["part_id"]: item for item in catalog}
    by_category = Counter(item["defect_category"] for item in interpreted)
    by_hour: Dict[str, Counter] = defaultdict(Counter)
    by_station = Counter()
    by_product_line = Counter()
    for report in reports:
        category = interp_by_id[report["report_id"]]["defect_category"]
        hour = parse_ts(report["timestamp"]).strftime("%H:00")
        by_hour[hour][category] += 1
        by_station[report["station"]] += 1
        by_product_line[catalog_by_id[report["part_id"]]["product_line"]] += 1

    worsening_patterns = []
    report_by_id = {report["report_id"]: report for report in reports}
    for pattern in patterns:
        ordered = sorted((report_by_id[rid] for rid in pattern["supporting_report_ids"]), key=lambda r: r["timestamp"])
        if len(ordered) < 2:
            continue
        first = ordered[0]["measurements"]
        last = ordered[-1]["measurements"]
        if (
            last["void_ratio"] > first["void_ratio"]
            or last["misalignment_mm"] > first["misalignment_mm"]
            or last["scratch_length_mm"] > first["scratch_length_mm"]
        ):
            worsening_patterns.append(pattern["pattern_id"])

    write_json(
        "shift_trends.json",
        {
            "defect_distribution": dict(sorted(by_category.items())),
            "hourly_distribution": {hour: dict(counter) for hour, counter in sorted(by_hour.items())},
            "worsening_pattern_ids": worsening_patterns,
            "most_affected_station": by_station.most_common(1)[0][0] if by_station else None,
            "most_affected_product_line": by_product_line.most_common(1)[0][0] if by_product_line else None,
        },
    )


def find_disagreements(reports: List[Dict[str, Any]], interpreted: List[Dict[str, Any]]) -> None:
    interp_by_id = {item["report_id"]: item for item in interpreted}
    disagreements = []
    for report in reports:
        interpretation = interp_by_id[report["report_id"]]
        m = report["measurements"]
        reasons = []
        if report["confidence"] < 0.75:
            reasons.append("low source model confidence")
        if interpretation["severity"] == "low" and (
            m["void_ratio"] >= 0.08 or m["misalignment_mm"] >= 0.15 or m["scratch_length_mm"] >= 4.0
        ):
            reasons.append("measurement is near a review threshold but interpreted severity is low")
        if "possible" in report["model_finding"].lower() and interpretation["disposition"] in {"pass", "monitor"}:
            reasons.append("source model used tentative language while disposition is not urgent")
        if reasons:
            disagreements.append(
                {
                    "report_id": report["report_id"],
                    "disagreement_reason": "; ".join(reasons),
                    "recommended_follow_up": "Review source images and confirm measurements before closing the case.",
                }
            )
    write_json("disagreement_cases.json", disagreements)


def generate_alert_rating(priority_scores: List[Dict[str, Any]], patterns: List[Dict[str, Any]]) -> None:
    flagged = [item for item in priority_scores if item["flagged_for_review"]]
    affected_parts = {item["part_id"] for item in flagged}
    affected_stations = {item["station"] for item in flagged}
    max_score = max((item["priority_score"] for item in priority_scores), default=0)
    strong_patterns = [pattern for pattern in patterns if pattern["pattern_strength"] == "strong"]
    if max_score >= 90 or len(strong_patterns) >= 3:
        rating = "red"
    elif max_score >= 70 and len(patterns) >= 2:
        rating = "orange"
    elif max_score >= 45 or patterns:
        rating = "yellow"
    else:
        rating = "green"
    write_json(
        "alert_rating.json",
        {
            "rating": rating,
            "justification": {
                "top_report_ids": [item["report_id"] for item in flagged],
                "top_priority_scores": {item["report_id"]: item["priority_score"] for item in flagged},
                "pattern_ids": [pattern["pattern_id"] for pattern in patterns],
                "multiple_stations_affected": len(affected_stations) > 1,
                "multiple_parts_affected": len(affected_parts) > 1,
            },
            "recommended_operational_posture": (
                "Maintain controlled production while holding top-priority cases for review and sampling adjacent units."
                if rating in {"orange", "red"}
                else "Continue production with routine monitoring and targeted reinspections."
            ),
        },
    )


def generate_reinspection_plan(patterns: List[Dict[str, Any]], priority_scores: List[Dict[str, Any]]) -> None:
    pattern_report_ids = {rid for pattern in patterns for rid in pattern["supporting_report_ids"]}
    affected = [score for score in priority_scores if score["report_id"] in pattern_report_ids]
    parts = sorted({item["part_name"] for item in affected}) or sorted({item["part_name"] for item in priority_scores[:3]})
    stations = sorted({item["station"] for item in affected}) or sorted({item["station"] for item in priority_scores[:3]})
    signatures = sorted({item["defect_category"] for item in affected}) or sorted({item["defect_category"] for item in priority_scores[:3]})
    lines = [
        "# Reinspection Plan",
        "",
        "## First-Sample Parts",
        "",
        *[f"- {part}" for part in parts],
        "",
        "## Priority Stations",
        "",
        *[f"- {station}" for station in stations],
        "",
        "## Defect Signatures",
        "",
        *[f"- {signature}" for signature in signatures],
        "",
        "## Stop Criteria",
        "",
        "- Stop release and escalate if the next shift reproduces a high-severity pattern or any critical interpretation.",
        "",
        "## Continue Criteria",
        "",
        "- Continue controlled production if reinspection finds no repeated signature and measurements remain below thresholds.",
        "",
        "## Owner Team",
        "",
        "- Quality, with Process Engineering support for recurring process signatures.",
        "",
    ]
    Path("reinspection_plan.md").write_text("\n".join(lines), encoding="utf-8")


def run_pipeline() -> None:
    load_env()
    selected_llm_provider()
    for artifact in GENERATED_ARTIFACTS:
        path = Path(artifact)
        if path.exists():
            path.unlink()

    stage_log: List[Dict[str, Any]] = []
    record_stage(stage_log, "INIT", [])
    reports, catalog = load_inputs()
    record_stage(stage_log, "INPUTS_LOADED", ["inspection_reports.json", "parts_catalog.json"])

    normalised = normalise_reports(reports, catalog)
    record_stage(
        stage_log,
        "REPORTS_NORMALISED",
        ["normalised_reports.json"],
        ["inspection_reports.json", "parts_catalog.json"],
    )

    interpreted = interpret_reports(reports, normalised)
    record_stage(
        stage_log,
        "REPORTS_INTERPRETED",
        ["interpreted_reports.json"],
        ["normalised_reports.json", "inspection_reports.json"],
    )

    patterns = group_patterns(reports, catalog, normalised, interpreted)
    record_stage(
        stage_log,
        "PATTERNS_GROUPED",
        ["patterns.json"],
        ["interpreted_reports.json", "normalised_reports.json", "inspection_reports.json", "parts_catalog.json"],
    )

    priority_scores = compute_priority_scores(reports, catalog, interpreted, patterns)
    record_stage(
        stage_log,
        "PRIORITIES_COMPUTED",
        ["priority_scores.json"],
        ["interpreted_reports.json", "patterns.json", "inspection_reports.json", "parts_catalog.json"],
    )

    flagged = [item for item in priority_scores if item["flagged_for_review"]]
    record_stage(stage_log, "CASES_SELECTED", ["priority_scores.json"], ["priority_scores.json"])

    routes = route_cases(flagged, patterns)
    record_stage(
        stage_log,
        "ROUTING_COMPLETE",
        ["case_routing.json"],
        ["priority_scores.json", "patterns.json"],
    )

    generate_review_summaries(flagged, routes, patterns)
    record_stage(
        stage_log,
        "EXPLANATIONS_GENERATED",
        ["review_summaries.md"],
        ["priority_scores.json", "patterns.json", "case_routing.json"],
    )

    generate_shift_trends(reports, catalog, interpreted, patterns)
    find_disagreements(reports, interpreted)
    generate_alert_rating(priority_scores, patterns)
    generate_reinspection_plan(patterns, priority_scores)

    record_stage(
        stage_log,
        "VALIDATION_COMPLETE",
        ["shift_trends.json", "disagreement_cases.json", "alert_rating.json", "reinspection_plan.md"],
        ["inspection_reports.json", "parts_catalog.json", "interpreted_reports.json", "patterns.json", "priority_scores.json"],
    )
    record_stage(stage_log, "RESULTS_FINALISED", GENERATED_ARTIFACTS)
    write_json("pipeline_state.json", {"stages": stage_log})


if __name__ == "__main__":
    try:
        run_pipeline()
    except Exception as exc:
        print(f"Pipeline failed: {exc}", file=sys.stderr)
        sys.exit(1)
