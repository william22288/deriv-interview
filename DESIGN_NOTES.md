# Design Notes: Replayable AI Inspection Pipeline

## Objective

Build a replayable pipeline that ingests machine-inspection reports, joins them with part metadata, normalises operator notes, interprets defects, groups recurring issue patterns, computes deterministic review priorities, routes selected cases to internal owners, and generates concise reviewer summaries.

The solution is designed as a staged batch pipeline rather than a notebook-style analysis. Each stage reads defined input artifacts, writes defined output artifacts, and can be validated independently. Generated artifacts may be deleted and regenerated from a clean checkout.

## Key Requirements

- Read `inspection_reports.json` and `parts_catalog.json` from disk.
- Join report records with part catalog metadata before interpretation.
- Detect and translate non-English notes before interpretation while preserving original text.
- Use controlled vocabularies for all model-generated categorical fields.
- Separate LLM-assisted stages:
  - report interpretation
  - recurring pattern grouping
  - case routing
- Compute priority scores deterministically in code, never by LLM.
- Preserve intermediate artifacts for replayability and audit.
- Log every LLM call to `llm_calls.jsonl`.
- Validate required artifacts, schema consistency, stage order, vocabulary usage, score calculation, top-case selection, routing, and reviewer summaries.

## Pipeline Stages

The pipeline enforces this state progression:

```text
INIT
 -> INPUTS_LOADED
 -> REPORTS_NORMALISED
 -> REPORTS_INTERPRETED
 -> PATTERNS_GROUPED
 -> PRIORITIES_COMPUTED
 -> CASES_SELECTED
 -> ROUTING_COMPLETE
 -> EXPLANATIONS_GENERATED
 -> VALIDATION_COMPLETE
 -> RESULTS_FINALISED
```

Each stage receives the previous stage's artifact or in-memory equivalent. Pattern grouping consumes interpreted reports plus supporting report metadata, not raw notes alone. Priority scoring occurs only after pattern grouping because pattern membership contributes to the final score.

## Controlled Vocabularies

The following vocabularies are defined in code and reused in prompts, output validation, and the validation command.

```text
Defect categories:
- solder_voiding
- pin_misalignment
- surface_scratch
- no_material_issue
- uncertain

Severity:
- critical
- high
- medium
- low

Disposition:
- hold
- reinspect
- monitor
- pass

Owner teams:
- Quality
- Process Engineering
- Manufacturing
- Supplier Quality

Pattern strengths:
- strong
- moderate
- weak
```

Any model output outside these vocabularies is rejected. The pipeline does not substitute local heuristic outputs for failed or invalid model responses.

## Artifact Flow

| Stage | Input | Output |
| --- | --- | --- |
| Load inputs | `inspection_reports.json`, `parts_catalog.json` | in-memory raw reports and catalog |
| Normalise reports | raw reports, catalog | `normalised_reports.json` |
| Interpret reports | `normalised_reports.json` | `interpreted_reports.json` |
| Group patterns | interpreted reports plus report metadata | `patterns.json` |
| Score priorities | interpreted reports, patterns, raw measurements | `priority_scores.json` |
| Select cases | `priority_scores.json` | top 5 flagged cases in score artifact |
| Route cases | top 5 cases, patterns | `case_routing.json` |
| Generate summaries | top 5 cases, routing, patterns | `review_summaries.md` |
| Optional trends | interpreted reports, timestamps, patterns | `shift_trends.json` |
| Optional disagreements | raw findings, measurements, interpretations | `disagreement_cases.json` |
| Optional alert | scores, patterns, trends | `alert_rating.json` |
| Optional reinspection plan | patterns, routed owners | `reinspection_plan.md` |
| LLM audit log | every LLM stage | `llm_calls.jsonl` |

## Stage Design

### 1. Input Loading

The pipeline reads the two required JSON files from the working directory:

- `inspection_reports.json`
- `parts_catalog.json`

The implementation treats input IDs, ordering, wording, and expected ranking as variable. It relies only on the documented schema.

Validation at this point checks:

- JSON parses successfully.
- Required top-level structures are lists.
- Each report has a `report_id`, `part_id`, `station`, `timestamp`, `operator_note`, `model_finding`, `confidence`, `measurements`, and `image_count`.
- Each catalog item has `part_id`, `part_name`, and `product_line`.
- Every report `part_id` exists in the catalog.

### 2. Report Normalisation

Each inspection report is joined with part metadata before interpretation.

For every report, the normalised artifact preserves:

```json
{
  "report_id": "string",
  "part_id": "string",
  "part_name": "string",
  "product_line": "string",
  "original_operator_note": "string",
  "note_for_interpretation": "string",
  "original_language": "string",
  "translated": true
}
```

The `original_operator_note` remains unchanged. `note_for_interpretation` contains English text used by downstream LLM stages. If a note is already English, it is copied directly.

For the public fixture, report `R09` contains Malay and must be translated before interpretation.

Implementation:

- A batch LLM request identifies each source language and translates non-English notes before interpretation.
- English notes are preserved byte-for-byte as text.
- The translation request and provider response ID are recorded in `llm_calls.jsonl`.
- Preserve the source language code, for example `en`, `ms`, or `es`.
- Fail clearly if translation cannot be completed; do not pass untranslated notes downstream.

Output:

```text
normalised_reports.json
```

### 3. Report Interpretation

This stage makes one Stage 1 LLM call using all normalised reports. Original operator notes are not resent in this call; the English interpretation text and structured measurements are used.

The prompt includes:

- input report fields
- controlled defect categories
- controlled severity values
- controlled disposition values
- required JSON schema
- instruction to use both structured measurements and note text
- instruction not to invent new categorical values

Expected output item:

```json
{
  "report_id": "string",
  "defect_category": "solder_voiding",
  "severity": "high",
  "disposition": "hold",
  "reason_short": "string",
  "translated": false,
  "original_language": "en"
}
```

The implementation validates that every input report has exactly one interpreted output and that all categorical fields use allowed values.

Output:

```text
interpreted_reports.json
```

### 4. Recurring Pattern Grouping

This stage makes a separate Stage 2 LLM call after report interpretation.

The call receives:

- interpreted report outputs
- report IDs
- part metadata
- station
- timestamp
- confidence
- measurements
- translated note text used for interpretation

The model identifies recurring issue patterns that may indicate process problems rather than isolated defects.

Expected output item:

```json
{
  "pattern_id": "string",
  "title": "string",
  "supporting_report_ids": ["R01", "R06"],
  "pattern_strength": "strong",
  "likely_driver": "string",
  "recommended_action": "string"
}
```

Validation checks:

- each `supporting_report_ids` item exists in the input reports
- `pattern_strength` is controlled
- each pattern has at least two supporting reports
- the public fixture should produce at least two distinct patterns
- the Stage 2 log entry lists `interpreted_reports.json` as an input artifact

Output:

```text
patterns.json
```

### 5. Deterministic Priority Scoring

Priority scoring is implemented in code and does not call an LLM.

Formula:

```text
base priority:
critical = 40
high = 25
medium = 10
low = 3

confidence bonus = confidence * 10

evidence bonus = image_count * 2

measurement bonus:
- +15 if void_ratio >= 0.20
- +10 if misalignment_mm >= 0.20
- +8 if scratch_length_mm >= 6.0
- otherwise +0 for that measurement type

pattern membership bonus = 12 per pattern containing the report

disposition bonus:
hold = 12
reinspect = 6
monitor = 2
pass = 0

priority_score = base_priority
  + confidence_bonus
  + evidence_bonus
  + measurement_bonus
  + pattern_membership_bonus
  + disposition_bonus
```

The score artifact should include enough components to make each score auditable.

Suggested output item:

```json
{
  "report_id": "R01",
  "priority_score": 63.2,
  "rank": 1,
  "flagged_for_review": true,
  "score_components": {
    "base_priority": 25,
    "confidence_bonus": 8.2,
    "evidence_bonus": 6,
    "measurement_bonus": 0,
    "pattern_membership_bonus": 12,
    "disposition_bonus": 12
  },
  "pattern_ids": ["P001"]
}
```

Reports are sorted by descending `priority_score`. The top 5 are flagged for review. Tie-breaking should be deterministic, for example by timestamp then `report_id`.

Output:

```text
priority_scores.json
```

### 6. Case Routing

This stage makes a separate Stage 3 LLM call.

The call receives:

- top 5 flagged reports
- detected patterns
- controlled owner team vocabulary
- relevant interpretation and score details

Expected output item:

```json
{
  "report_id": "string",
  "teams": ["Quality", "Process Engineering"],
  "briefing_note": "string"
}
```

Validation checks:

- every routed `report_id` is one of the flagged top 5 reports
- every team is in the owner vocabulary
- each top 5 report receives a routing item
- briefing notes are concise and non-empty

Output:

```text
case_routing.json
```

### 7. Reviewer Summary Generation

The reviewer summary is generated as Markdown for the top 5 flagged cases. This can be produced deterministically from artifacts, with optional LLM assistance only for concise wording if logged.

Each case summary includes:

- report ID and part name
- interpreted defect category
- severity and disposition
- deterministic priority score
- recurring pattern membership
- plain-English explanation of why the case was prioritised
- human review gate note

The human review gate note must state what must be verified before any production decision is made.

Output:

```text
review_summaries.md
```

## Optional Analysis

### Shift-Level Trend Summary

`shift_trends.json` characterises defect activity across the shift using timestamps, interpreted categories, part metadata, stations, and patterns.

It should identify:

- defect distribution over time
- whether recurring issue evidence appears to worsen
- most affected station
- most affected part family or product line

### Low-Confidence Disagreement Finder

`disagreement_cases.json` identifies reports where the model finding, measurements, confidence, and interpreted output are weakly aligned.

Expected output item:

```json
{
  "report_id": "string",
  "disagreement_reason": "string",
  "recommended_follow_up": "string"
}
```

### Lot-Level Alert

`alert_rating.json` assigns one of:

```text
green
yellow
orange
red
```

The rating is justified using report IDs, patterns, top scores, affected stations, and affected parts.

### Reinspection Plan

`reinspection_plan.md` recommends next-shift inspection priorities:

- parts to sample first
- stations to prioritise
- defect signatures to watch
- stop or continue criteria
- owner team

## LLM Call Logging

Every LLM call appends one JSON object to `llm_calls.jsonl`.

Required fields:

```json
{
  "stage": "string",
  "timestamp": "ISO-8601 timestamp",
  "provider": "string",
  "model": "string",
  "prompt_hash": "string",
  "input_artifacts": ["path"],
  "output_artifact": "path"
}
```

Required log entries:

- note language detection and translation
- report interpretation
- recurring pattern grouping
- case routing
- optional analysis stages if they use an LLM

Each required call is made to the configured OpenAI provider. Its log records the actual model and provider response ID; local heuristic fallback is not permitted. The `prompt_hash` is computed from the final prompt payload sent to the model. This gives reviewers a lightweight audit trail without storing secrets.

## Configuration

Configuration should be environment-driven so the evaluator can run the pipeline from a clean checkout.

Suggested variables:

```text
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=<provided externally>
```

The repository should not commit real API keys. `.env` can be supported locally, but secrets must remain outside generated artifacts and logs. Set `OPENAI_API_KEY` in the execution environment (or a private, ignored local `.env`) before running `make run`. The pipeline fails before deleting previous artifacts if no key or supported provider is configured.

## Validation Command

The repository should include a validation entry point such as:

```bash
python validate.py
```

`python validate.py` checks the existing generated artifacts. The clean-run target regenerates them with the configured LLM, runs the mocked integration/failure-path tests, and then validates the fresh outputs:

```bash
make validate
```

Validation checks:

- required artifacts exist
- JSON artifacts are valid
- all reports were processed
- joined part metadata is present in normalised reports
- non-English notes preserve original text and include translated interpretation text
- interpreted outputs use only controlled vocabularies
- pattern grouping consumes interpreted reports as an input artifact
- priority scores are recomputable from source artifacts
- priority scoring happens after pattern grouping
- top 5 review cases match computed score ordering
- routing teams use only the controlled owner vocabulary
- every flagged case appears in `review_summaries.md`
- every flagged case has a human review gate note
- `llm_calls.jsonl` contains exactly one successful provider response record for translation, interpretation, pattern grouping, and routing, with response IDs, model, timestamp, prompt hash, and artifact inputs/outputs
- every flagged case has its own complete reviewer section and human review gate
- pipeline stage inputs prove that pattern grouping receives interpreted data and scoring follows grouping

## Error Handling

The pipeline should fail fast for structural input problems, unknown vocabulary values, missing reports, invalid JSON, missing part metadata, or inconsistent artifact counts.

LLM output is parsed as strict JSON and checked against required fields and controlled vocabularies. Invalid output or provider failures stop the pipeline with a clear error; the pipeline never records heuristic results as LLM calls.

## Replayability Notes

Replayability is achieved by:

- reading input fixtures from disk every run
- avoiding static precomputed outputs
- preserving intermediate artifacts
- making LLM stages explicit
- logging LLM calls
- computing scores deterministically
- validating that downstream artifacts align with upstream artifacts

The evaluator can delete generated files, replace the input fixtures with equivalent schema-compatible records, run the pipeline, and inspect all generated artifacts.

## Proposed Implementation Layout

```text
.
├── inspection_reports.json
├── parts_catalog.json
├── pipeline.py
├── validate.py
├── DESIGN_NOTES.md
├── normalised_reports.json
├── interpreted_reports.json
├── patterns.json
├── priority_scores.json
├── case_routing.json
├── review_summaries.md
├── shift_trends.json
├── disagreement_cases.json
├── alert_rating.json
├── reinspection_plan.md
└── llm_calls.jsonl
```

Core code can be split later if needed, but a compact implementation with clearly named stage functions is acceptable for the challenge:

- `load_inputs()`
- `normalise_reports()`
- `interpret_reports()`
- `group_patterns()`
- `compute_priority_scores()`
- `select_cases()`
- `route_cases()`
- `generate_review_summaries()`
- `generate_shift_trends()`
- `find_disagreements()`
- `generate_alert_rating()`
- `generate_reinspection_plan()`
- `validate_outputs()`

## Acceptance Criteria

The submission is complete when:

- a clean checkout can regenerate all required artifacts
- the pipeline stages execute in the required order
- LLM calls are separated and logged
- priority scores are deterministic and auditable
- top 5 cases are selected from computed scores
- human reviewer summaries are generated in Markdown
- validation passes on the public fixture and remains schema-driven for replacement fixtures
