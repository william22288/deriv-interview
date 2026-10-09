import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline
import validate


class PipelineIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.temp_dir.name)
        catalog = [
            {"part_id": "PT-A", "part_name": "Assembly A", "product_line": "Line A"},
            {"part_id": "PT-B", "part_name": "Assembly B", "product_line": "Line B"},
            {"part_id": "PT-C", "part_name": "Assembly C", "product_line": "Line C"},
        ]
        source_rows = [
            ("CASE-001", "PT-A", "XRay-1", "possible solder voiding", "Void cluster near joint.", {"void_ratio": 0.22, "misalignment_mm": 0.0, "scratch_length_mm": 0.0}),
            ("CASE-002", "PT-A", "XRay-1", "possible solder voiding", "Another void cluster.", {"void_ratio": 0.21, "misalignment_mm": 0.0, "scratch_length_mm": 0.0}),
            ("CASE-003", "PT-B", "AOI-2", "pin misalignment", "Pins appear shifted.", {"void_ratio": 0.0, "misalignment_mm": 0.24, "scratch_length_mm": 0.0}),
            ("CASE-004", "PT-B", "AOI-2", "pin misalignment", "Another alignment concern.", {"void_ratio": 0.0, "misalignment_mm": 0.18, "scratch_length_mm": 0.0}),
            ("CASE-005", "PT-C", "Manual-3", "surface scratch", "La superficie muestra una marca larga.", {"void_ratio": 0.0, "misalignment_mm": 0.0, "scratch_length_mm": 6.3}),
            ("CASE-006", "PT-C", "Manual-3", "surface scratch", "A second visible line mark.", {"void_ratio": 0.0, "misalignment_mm": 0.0, "scratch_length_mm": 4.8}),
        ]
        reports = [
            {
                "report_id": report_id,
                "part_id": part_id,
                "station": station,
                "timestamp": f"2026-03-15T{8 + index:02d}:00:00Z",
                "operator_note": note,
                "model_finding": finding,
                "confidence": 0.7 + index * 0.03,
                "measurements": measurements,
                "image_count": 1 + index % 3,
            }
            for index, (report_id, part_id, station, finding, note, measurements) in enumerate(source_rows)
        ]
        (self.work_dir / "inspection_reports.json").write_text(json.dumps(reports), encoding="utf-8")
        (self.work_dir / "parts_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
        self.translated_report_id = "CASE-005"
        self.report_count = len(reports)
        self.original_cwd = Path.cwd()
        os.chdir(self.work_dir)
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(os.chdir, self.original_cwd)
        self.prompts = []
        self.response_counter = 0
        self.environment = mock.patch.dict(
            os.environ,
            {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "unit-test-key", "LLM_MODEL": "unit-test-model"},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def mocked_openai(self, http_request, timeout):
        self.assertEqual(http_request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(http_request.get_header("Authorization"), "Bearer unit-test-key")
        request_body = json.loads(http_request.data.decode("utf-8"))
        prompt = json.loads(request_body["input"][1]["content"])
        self.prompts.append(prompt)
        stage = prompt["stage"]

        if stage == "note_normalisation_translation":
            output = {"translations": []}
            for report in prompt["reports"]:
                note = report["original_operator_note"]
                if report["report_id"] == self.translated_report_id:
                    output["translations"].append(
                        {
                            "report_id": report["report_id"],
                            "original_language": "es",
                            "note_for_interpretation": "The pin alignment appears slightly skewed.",
                            "translated": True,
                        }
                    )
                else:
                    output["translations"].append(
                        {
                            "report_id": report["report_id"],
                            "original_language": "en",
                            "note_for_interpretation": note,
                            "translated": False,
                        }
                    )
        elif stage == "report_interpretation":
            categories = {
                "possible solder voiding": "solder_voiding",
                "pin misalignment": "pin_misalignment",
                "surface scratch": "surface_scratch",
            }
            normalized_by_id = {item["report_id"]: item for item in prompt["normalised_reports"]}
            output = {
                "interpreted_reports": [
                    {
                        "report_id": report["report_id"],
                        "defect_category": categories[report["model_finding"]],
                        "severity": "medium",
                        "disposition": "reinspect",
                        "reason_short": "Measurements and translated note indicate a reviewable defect.",
                        "translated": normalized_by_id[report["report_id"]]["translated"],
                        "original_language": normalized_by_id[report["report_id"]]["original_language"],
                    }
                    for report in prompt["structured_reports"]
                ]
            }
        elif stage == "pattern_grouping":
            report_groups = {}
            for report in prompt["reports"]:
                category = report["interpretation"]["defect_category"]
                report_groups.setdefault(category, []).append(report["report_id"])
            output = {
                "patterns": [
                    {
                        "pattern_id": f"TEST-{index}",
                        "title": f"Repeated {category} findings",
                        "supporting_report_ids": report_ids[:],
                        "pattern_strength": "moderate",
                        "likely_driver": "A repeated defect signature requires review.",
                        "recommended_action": "Inspect affected parts and process conditions.",
                    }
                    for index, (category, report_ids) in enumerate(report_groups.items(), start=1)
                    if len(report_ids) >= 2
                ]
            }
        elif stage == "case_routing":
            output = {
                "case_routing": [
                    {
                        "report_id": case["report_id"],
                        "teams": ["Quality"],
                        "briefing_note": "Review the defect evidence and confirm disposition.",
                    }
                    for case in prompt["flagged_cases"]
                ]
            }
        else:
            self.fail(f"Unexpected model stage: {stage}")

        self.response_counter += 1
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {
                "id": f"resp-unit-{self.response_counter}",
                "model": "unit-test-model",
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(output),
                            }
                        ]
                    }
                ],
            }
        ).encode("utf-8")
        return response

    def test_full_pipeline_uses_llm_stages_and_validates_outputs(self) -> None:
        with mock.patch("pipeline.request.urlopen", side_effect=self.mocked_openai):
            pipeline.run_pipeline()
            validate.main()

        self.assertEqual(
            [prompt["stage"] for prompt in self.prompts],
            [
                "note_normalisation_translation",
                "report_interpretation",
                "pattern_grouping",
                "case_routing",
            ],
        )
        interpretation_prompt = self.prompts[1]
        self.assertEqual(len(interpretation_prompt["normalised_reports"]), self.report_count)
        self.assertNotIn("operator_note", interpretation_prompt["structured_reports"][4])
        self.assertEqual(
            interpretation_prompt["normalised_reports"][4]["note_for_interpretation"],
            "The pin alignment appears slightly skewed.",
        )
        pattern_reports = self.prompts[2]["reports"]
        required_pattern_fields = {
            "report_id",
            "part_id",
            "part_name",
            "product_line",
            "station",
            "timestamp",
            "confidence",
            "measurements",
            "note_for_interpretation",
            "interpretation",
        }
        self.assertTrue(all(required_pattern_fields.issubset(report) for report in pattern_reports))
        self.assertEqual(len(self.prompts[3]["flagged_cases"]), 5)
        self.assertEqual(self.response_counter, 4)

        call_log = [json.loads(line) for line in Path("llm_calls.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(call_log), 4)
        self.assertTrue(all(record["provider_response_id"].startswith("resp-unit-") for record in call_log))

        scores = json.loads(Path("priority_scores.json").read_text(encoding="utf-8"))
        summary_path = Path("review_summaries.md")
        summary = summary_path.read_text(encoding="utf-8")
        summary_path.write_text(summary.replace("- Human review gate:", "- Review gate:", 1), encoding="utf-8")
        patterns = json.loads(Path("patterns.json").read_text(encoding="utf-8"))
        with self.assertRaises(AssertionError):
            validate.check_review_summaries(scores, patterns)

    def test_missing_provider_configuration_fails_before_artifact_cleanup(self) -> None:
        Path("interpreted_reports.json").write_text("keep", encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY is not set"):
                pipeline.run_pipeline()
        self.assertEqual(Path("interpreted_reports.json").read_text(encoding="utf-8"), "keep")

    def test_provider_error_is_not_replaced_with_local_interpretation(self) -> None:
        reports = pipeline.read_json("inspection_reports.json")
        normalised = [
            {
                "report_id": report["report_id"],
                "note_for_interpretation": report["operator_note"],
                "translated": False,
                "original_language": "en",
            }
            for report in reports
        ]
        with mock.patch("pipeline.request.urlopen", side_effect=pipeline.error.URLError("offline")):
            with self.assertRaisesRegex(RuntimeError, "OpenAI request failed"):
                pipeline.interpret_reports(reports, normalised)
        self.assertFalse(Path("interpreted_reports.json").exists())
        self.assertFalse(Path("llm_calls.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
