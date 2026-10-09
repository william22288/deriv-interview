import hashlib
import json
import os
import re
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


def record_stage(stage_log: List[Dict[str, Any]], stage: str, artifacts: Iterable[str]) -> None:
    stage_log.append(
        {
            "stage": stage,
            "timestamp": utc_now(),
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
) -> None:
    record = {
        "stage": stage,
        "timestamp": utc_now(),
        "provider": provider,
        "model": model,
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


MALAY_MARKERS = {
    "malay",
    "penjajaran",
    "sebelah",
    "kiri",
    "nampak",
    "senget",
    "sedikit",
    "mungkin",
    "perlu",
    "semakan",
    "kedua",
}


def detect_language(note: str) -> str:
    lowered = note.lower()
    tokens = set(re.findall(r"[a-zA-Z]+", lowered))
    if tokens & MALAY_MARKERS:
        return "ms"
    return "en"


def translate_note(note: str, language: str) -> str:
    if language == "en":
        return note
    if language == "ms":
        if "penjajaran pin" in note.lower():
            return "Visual check: pin alignment on the left side looks slightly skewed, may need a second review."
        return f"Translated from Malay for interpretation: {note}"
    return f"Translated to English for interpretation: {note}"


def normalise_reports(reports: List[Dict[str, Any]], catalog: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    parts_by_id = {part["part_id"]: part for part in catalog}
    normalised = []
    for report in reports:
        part = parts_by_id[report["part_id"]]
        language = detect_language(report["operator_note"])
        translated = language != "en"
        normalised.append(
            {
                "report_id": report["report_id"],
                "part_id": report["part_id"],
                "part_name": part["part_name"],
                "product_line": part["product_line"],
                "original_operator_note": report["operator_note"],
                "note_for_interpretation": translate_note(report["operator_note"], language),
                "original_language": language,
                "translated": translated,
            }
        )
    write_json("normalised_reports.json", normalised)
    return normalised


def infer_category(report: Dict[str, Any], normalised: Dict[str, Any]) -> str:
    text = f"{report['model_finding']} {normalised['note_for_interpretation']}".lower()
    measurements = report["measurements"]
    if "void" in text or measurements["void_ratio"] >= 0.05:
        return "solder_voiding"
    if "pin" in text or "align" in text or measurements["misalignment_mm"] >= 0.10:
        return "pin_misalignment"
    if "scratch" in text or "mark" in text or "line" in text or measurements["scratch_length_mm"] >= 2.0:
        return "surface_scratch"
    if max(measurements.values()) == 0:
        return "no_material_issue"
    return "uncertain"


def infer_severity_and_disposition(category: str, report: Dict[str, Any]) -> Tuple[str, str]:
    m = report["measurements"]
    confidence = float(report["confidence"])
    note = report["operator_note"].lower()
    if category == "solder_voiding":
        if m["void_ratio"] >= 0.30:
            return "critical", "hold"
        if m["void_ratio"] >= 0.20:
            return "high", "hold"
        if m["void_ratio"] >= 0.12 or "borderline" in note:
            return "medium", "reinspect"
        return "low", "monitor"
    if category == "pin_misalignment":
        if m["misalignment_mm"] >= 0.30:
            return "critical", "hold"
        if m["misalignment_mm"] >= 0.20:
            return "high", "reinspect"
        if m["misalignment_mm"] >= 0.12 or confidence < 0.75:
            return "medium", "reinspect"
        return "low", "monitor"
    if category == "surface_scratch":
        if m["scratch_length_mm"] >= 10.0:
            return "high", "hold"
        if m["scratch_length_mm"] >= 6.0:
            return "medium", "reinspect"
        if m["scratch_length_mm"] > 0:
            return "low", "monitor"
        return "low", "pass"
    if category == "no_material_issue":
        return "low", "pass"
    return "low", "reinspect"


def reason_for(category: str, severity: str, report: Dict[str, Any], normalised: Dict[str, Any]) -> str:
    m = report["measurements"]
    if category == "solder_voiding":
        return f"{severity} solder voiding based on void_ratio {m['void_ratio']:.2f} and note evidence."
    if category == "pin_misalignment":
        return f"{severity} pin alignment concern based on {m['misalignment_mm']:.2f} mm misalignment and note evidence."
    if category == "surface_scratch":
        return f"{severity} surface scratch based on {m['scratch_length_mm']:.1f} mm scratch evidence."
    if category == "no_material_issue":
        return "No material defect signal exceeded the review thresholds."
    return "Insufficiently aligned signals; human review recommended."


def local_interpret_reports(reports: List[Dict[str, Any]], normalised: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalised_by_id = {item["report_id"]: item for item in normalised}
    interpreted = []
    for report in reports:
        norm = normalised_by_id[report["report_id"]]
        category = infer_category(report, norm)
        severity, disposition = infer_severity_and_disposition(category, report)
        interpreted.append(
            {
                "report_id": report["report_id"],
                "defect_category": category,
                "severity": severity,
                "disposition": disposition,
                "reason_short": reason_for(category, severity, report, norm),
                "translated": norm["translated"],
                "original_language": norm["original_language"],
            }
        )
    return interpreted


def call_openai_json(prompt_payload: Dict[str, Any]) -> Any:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    body = {
        "model": model,
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
    return json.loads("".join(text_parts))


def validate_interpretations(interpreted: List[Dict[str, Any]], report_ids: Iterable[str]) -> None:
    expected_ids = set(report_ids)
    observed_ids = {item.get("report_id") for item in interpreted}
    if observed_ids != expected_ids:
        raise ValueError("Interpreted reports do not match input report IDs.")
    for item in interpreted:
        if item["defect_category"] not in DEFECT_CATEGORIES:
            raise ValueError(f"Invalid defect category: {item['defect_category']}")
        if item["severity"] not in SEVERITIES:
            raise ValueError(f"Invalid severity: {item['severity']}")
        if item["disposition"] not in DISPOSITIONS:
            raise ValueError(f"Invalid disposition: {item['disposition']}")


def maybe_call_openai(prompt_payload: Dict[str, Any]) -> Any:
    provider = selected_llm_provider()
    if provider != "openai":
        raise RuntimeError("External provider disabled; using local deterministic model.")
    return call_openai_json(prompt_payload)


def selected_llm_provider() -> str:
    configured = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if configured:
        return configured
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return "openai"
    return "local"


def interpret_reports(reports: List[Dict[str, Any]], normalised: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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
        "instruction": "Interpret each report using note text and structured measurements. Do not invent vocabulary values.",
        "normalised_reports": normalised,
        "raw_reports": reports,
    }
    provider = selected_llm_provider()
    model = os.environ.get("LLM_MODEL", "deterministic-inspection-v1")
    try:
        output = maybe_call_openai(prompt_payload)
        interpreted = output.get("interpreted_reports", output)
        validate_interpretations(interpreted, [report["report_id"] for report in reports])
        provider_name = provider
    except Exception:
        interpreted = local_interpret_reports(reports, normalised)
        validate_interpretations(interpreted, [report["report_id"] for report in reports])
        provider_name = "local-deterministic"
        model = "inspection-rules-v1"
    write_json("interpreted_reports.json", interpreted)
    log_llm_call("report_interpretation", provider_name, model, prompt_payload, ["normalised_reports.json"], "interpreted_reports.json")
    return interpreted


def pattern_group_key(report: Dict[str, Any], interpretation: Dict[str, Any]) -> Tuple[str, str, str]:
    category = interpretation["defect_category"]
    if category == "solder_voiding":
        return category, report["part_id"], report["station"]
    if category == "surface_scratch":
        return category, report["part_id"], report["station"]
    if category == "pin_misalignment":
        return category, report["part_id"], "alignment"
    return category, report["part_id"], report["station"]


def describe_pattern(category: str, reports: List[Dict[str, Any]], catalog_by_id: Dict[str, Dict[str, Any]]) -> Tuple[str, str, str]:
    part = catalog_by_id[reports[0]["part_id"]]
    station_names = ", ".join(sorted({report["station"] for report in reports}))
    if category == "solder_voiding":
        return (
            f"Recurring solder voiding on {part['part_name']}",
            f"Repeated void signatures around the same part family and station path: {station_names}.",
            "Hold affected cases for quality review and check solder process settings before next release.",
        )
    if category == "pin_misalignment":
        return (
            f"Recurring pin alignment concern on {part['part_name']}",
            f"Multiple alignment findings on {part['part_name']} suggest placement or fixture drift.",
            "Reinspect alignment on recent lots and verify fixture calibration.",
        )
    if category == "surface_scratch":
        return (
            f"Recurring surface scratch on {part['part_name']}",
            f"Repeated scratch findings from {station_names} indicate a possible handling or rework issue.",
            "Inspect station handling path and sample adjacent units for the same scratch signature.",
        )
    return (
        f"Recurring uncertain signal on {part['part_name']}",
        "Multiple related low-certainty signals require human triage.",
        "Review supporting images before changing production posture.",
    )


def local_group_patterns(
    reports: List[Dict[str, Any]],
    catalog: List[Dict[str, Any]],
    normalised: List[Dict[str, Any]],
    interpreted: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    interpretations = {item["report_id"]: item for item in interpreted}
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for report in reports:
        interpretation = interpretations[report["report_id"]]
        if interpretation["defect_category"] in {"no_material_issue", "uncertain"}:
            continue
        groups[pattern_group_key(report, interpretation)].append(report)

    catalog_by_id = {part["part_id"]: part for part in catalog}
    patterns = []
    for _, group in sorted(groups.items(), key=lambda item: min(parse_ts(r["timestamp"]) for r in item[1])):
        if len(group) < 2:
            continue
        category = interpretations[group[0]["report_id"]]["defect_category"]
        title, driver, action = describe_pattern(category, group, catalog_by_id)
        high_measurements = 0
        for report in group:
            m = report["measurements"]
            if m["void_ratio"] >= 0.20 or m["misalignment_mm"] >= 0.20 or m["scratch_length_mm"] >= 6.0:
                high_measurements += 1
        if len(group) >= 3 or high_measurements >= 2:
            strength = "strong"
        elif len(group) == 2:
            strength = "moderate"
        else:
            strength = "weak"
        patterns.append(
            {
                "pattern_id": f"P{len(patterns) + 1:03d}",
                "title": title,
                "supporting_report_ids": [report["report_id"] for report in sorted(group, key=lambda r: r["timestamp"])],
                "pattern_strength": strength,
                "likely_driver": driver,
                "recommended_action": action,
            }
        )
    return patterns


def validate_patterns(patterns: List[Dict[str, Any]], report_ids: Iterable[str]) -> None:
    valid_report_ids = set(report_ids)
    for pattern in patterns:
        if pattern["pattern_strength"] not in PATTERN_STRENGTHS:
            raise ValueError(f"Invalid pattern strength: {pattern['pattern_strength']}")
        if len(pattern["supporting_report_ids"]) < 2:
            raise ValueError(f"Pattern {pattern['pattern_id']} has fewer than two reports.")
        unknown = set(pattern["supporting_report_ids"]) - valid_report_ids
        if unknown:
            raise ValueError(f"Pattern {pattern['pattern_id']} references unknown reports: {sorted(unknown)}")


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
        "instruction": "Identify recurring patterns using interpreted reports and supporting metadata.",
        "reports": context,
    }
    provider = selected_llm_provider()
    model = os.environ.get("LLM_MODEL", "deterministic-inspection-v1")
    try:
        output = maybe_call_openai(prompt_payload)
        patterns = output.get("patterns", output)
        validate_patterns(patterns, [report["report_id"] for report in reports])
        provider_name = provider
    except Exception:
        patterns = local_group_patterns(reports, catalog, normalised, interpreted)
        validate_patterns(patterns, [report["report_id"] for report in reports])
        provider_name = "local-deterministic"
        model = "inspection-rules-v1"
    write_json("patterns.json", patterns)
    log_llm_call(
        "pattern_grouping",
        provider_name,
        model,
        prompt_payload,
        ["interpreted_reports.json", "normalised_reports.json", "inspection_reports.json", "parts_catalog.json"],
        "patterns.json",
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


def local_route_cases(flagged: List[Dict[str, Any]], patterns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    routes = []
    for case in flagged:
        category = case["defect_category"]
        teams = ["Quality"]
        if category == "solder_voiding":
            teams.append("Process Engineering")
        elif category == "pin_misalignment":
            teams.append("Manufacturing")
        elif category == "surface_scratch":
            teams.append("Manufacturing")
            if case["pattern_ids"]:
                teams.append("Process Engineering")
        if case["disposition"] == "hold" and "Manufacturing" not in teams:
            teams.append("Manufacturing")
        pattern_text = f" recurring pattern {', '.join(case['pattern_ids'])}" if case["pattern_ids"] else " no recurring pattern"
        routes.append(
            {
                "report_id": case["report_id"],
                "teams": teams,
                "briefing_note": (
                    f"{case['severity']} {category} on {case['part_name']} scored {case['priority_score']} with"
                    f"{pattern_text}; review disposition is {case['disposition']}."
                ),
            }
        )
    return routes


def validate_routes(routes: List[Dict[str, Any]], flagged_ids: Iterable[str]) -> None:
    expected = set(flagged_ids)
    observed = {route["report_id"] for route in routes}
    if observed != expected:
        raise ValueError("Case routing does not cover exactly the flagged cases.")
    for route in routes:
        if not route.get("briefing_note"):
            raise ValueError(f"Routing for {route['report_id']} is missing a briefing note.")
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
        "instruction": "Route each flagged case to one or more owner teams from the controlled vocabulary.",
        "flagged_cases": flagged,
        "patterns": patterns,
    }
    provider = selected_llm_provider()
    model = os.environ.get("LLM_MODEL", "deterministic-inspection-v1")
    try:
        output = maybe_call_openai(prompt_payload)
        routes = output.get("case_routing", output)
        validate_routes(routes, [case["report_id"] for case in flagged])
        provider_name = provider
    except Exception:
        routes = local_route_cases(flagged, patterns)
        validate_routes(routes, [case["report_id"] for case in flagged])
        provider_name = "local-deterministic"
        model = "inspection-rules-v1"
    write_json("case_routing.json", routes)
    log_llm_call(
        "case_routing",
        provider_name,
        model,
        prompt_payload,
        ["priority_scores.json", "patterns.json"],
        "case_routing.json",
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
    for artifact in GENERATED_ARTIFACTS:
        path = Path(artifact)
        if path.exists():
            path.unlink()

    stage_log: List[Dict[str, Any]] = []
    record_stage(stage_log, "INIT", [])
    reports, catalog = load_inputs()
    record_stage(stage_log, "INPUTS_LOADED", ["inspection_reports.json", "parts_catalog.json"])

    normalised = normalise_reports(reports, catalog)
    record_stage(stage_log, "REPORTS_NORMALISED", ["normalised_reports.json"])

    interpreted = interpret_reports(reports, normalised)
    record_stage(stage_log, "REPORTS_INTERPRETED", ["interpreted_reports.json"])

    patterns = group_patterns(reports, catalog, normalised, interpreted)
    record_stage(stage_log, "PATTERNS_GROUPED", ["patterns.json"])

    priority_scores = compute_priority_scores(reports, catalog, interpreted, patterns)
    record_stage(stage_log, "PRIORITIES_COMPUTED", ["priority_scores.json"])

    flagged = [item for item in priority_scores if item["flagged_for_review"]]
    record_stage(stage_log, "CASES_SELECTED", ["priority_scores.json"])

    routes = route_cases(flagged, patterns)
    record_stage(stage_log, "ROUTING_COMPLETE", ["case_routing.json"])

    generate_review_summaries(flagged, routes, patterns)
    record_stage(stage_log, "EXPLANATIONS_GENERATED", ["review_summaries.md"])

    generate_shift_trends(reports, catalog, interpreted, patterns)
    find_disagreements(reports, interpreted)
    generate_alert_rating(priority_scores, patterns)
    generate_reinspection_plan(patterns, priority_scores)

    record_stage(
        stage_log,
        "VALIDATION_COMPLETE",
        ["shift_trends.json", "disagreement_cases.json", "alert_rating.json", "reinspection_plan.md"],
    )
    record_stage(stage_log, "RESULTS_FINALISED", GENERATED_ARTIFACTS)
    write_json("pipeline_state.json", {"stages": stage_log})


if __name__ == "__main__":
    try:
        run_pipeline()
    except Exception as exc:
        print(f"Pipeline failed: {exc}", file=sys.stderr)
        sys.exit(1)
