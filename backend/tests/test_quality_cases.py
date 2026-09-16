import json
from pathlib import Path

import pytest

from app.interview import build_context, decide_messages, mock_response, validate_decision
from test_interview import STUDY, participant

CASES = json.loads((Path(__file__).resolve().parents[2] / "tests/interview_quality/cases.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_fixed_cases_obey_controller_contract_in_mock_mode(case):
    context = build_context(STUDY, [participant(text=case["answer"])], {}, case["active_seconds"])
    payload = json.loads(decide_messages(context)[-1]["content"])
    result = validate_decision(mock_response(payload), context)
    assert result["action"] in case["allowed_actions"]
    assert result["boundary"] == case["expected_boundary"]
    assert all(source == "U1" for source in result["basis_turn_ids"])
    assert result["new_evidence"] == []
    assert "密钥" not in result["question"] and "500" not in result["question"]
