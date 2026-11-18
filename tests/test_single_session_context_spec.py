"""Offline generation contract; no model or provider execution."""
import json
import unittest
from packages.travel_agent.single_session_agent import SYSTEM, DESTINATION_CONTEXT_SPEC
from packages.travel_agent.single_session_context import validate

class ContextSpecTests(unittest.TestCase):
    def test_schema_example_matches_renderer(self):
        line=next(s for s in DESTINATION_CONTEXT_SPEC.splitlines() if s.startswith('{"season_note"'))
        validate(json.loads(line))
        self.assertIn(DESTINATION_CONTEXT_SPEC,SYSTEM)

    def test_four_dimensions_and_evidence_boundaries(self):
        for text in ['2–3 档','intake','selected=true','budget_totals','单项价格当整趟总额','verified_items','estimated_items','one_day_less','one_day_more','太阳时差','夏令时','calculated','lookup','method=unknown','days.timeline.time','不硬塞时差块','不能只加总览提醒','单源标未交叉确认']:
            with self.subTest(text=text): self.assertIn(text,DESTINATION_CONTEXT_SPEC)
