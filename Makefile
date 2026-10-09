.PHONY: run validate clean

run:
	python pipeline.py

validate:
	python validate.py

clean:
	python -c "from pathlib import Path; [p.unlink() for p in map(Path, ['normalised_reports.json','interpreted_reports.json','patterns.json','priority_scores.json','case_routing.json','review_summaries.md','shift_trends.json','disagreement_cases.json','alert_rating.json','reinspection_plan.md','llm_calls.jsonl','pipeline_state.json']) if p.exists()]"
