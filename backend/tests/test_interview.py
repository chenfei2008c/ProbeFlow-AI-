import json

import pytest

from app.interview import (
    DecisionError,
    ReportError,
    build_context,
    decide_messages,
    fallback_decision,
    mock_report,
    mock_response,
    render_report,
    report_messages,
    report_source_chunks,
    validate_decision,
    validate_report,
)


STUDY = {
    "objective": "了解材料补交流程",
    "target_minutes": 60,
    "exclusions": ["真实客户姓名", "账号"],
    "topics": [
        {"id": "T1", "title": "背景", "priority": 1, "research_question": "角色是什么"},
        {"id": "T2", "title": "补件", "priority": 2, "research_question": "经历如何"},
    ],
}


def participant(turn_id="U1", text="主要是反复补件，比较烦。", **extra):
    return {
        "id": turn_id,
        "role": "participant",
        "text": text,
        "revision_id": f"{turn_id}-r1",
        "topic_id": "T1",
        "confirmed": True,
        **extra,
    }


def valid_decision(**updates):
    value = {
        "action": "ask_example",
        "topic_id": "T1",
        "question": "能讲一次最近发生的具体经历吗？",
        "basis_turn_ids": ["U1"],
        "coverage_update": {"status": "partial", "evidence_turn_ids": ["U1"]},
        "new_evidence": [],
        "unresolved_items": ["尚未获得具体事件"],
        "boundary": "none",
    }
    value.update(updates)
    return value


def test_build_context_keeps_latest_answer_exclusions_refusals_and_reports_overflow():
    huge = "旧" * 30_000
    latest = "当前完整回答" * 5_000
    turns = [participant("U0", huge), participant("U1", latest)]
    memory = {
        "facts": [{"text": "有来源", "turn_ids": ["U0"]}, {"text": "虚构", "turn_ids": ["missing"]}],
        "refusals": [{"text": "不谈具体同事", "turn_ids": ["U1"]}],
    }
    context = build_context(STUDY, turns, memory, 900)
    assert context["turns"][-1]["text"] == latest
    assert context["study"]["exclusions"] == STUDY["exclusions"]
    assert context["memory"]["refusals"] == memory["refusals"]
    assert context["memory"]["facts"] == [{"text": "有来源", "turn_ids": ["U0"]}]
    assert context["diagnostics"]["budget_overflow"] is True
    assert context["remaining_seconds"] == 2700


def test_decision_messages_and_strict_validation():
    context = build_context(STUDY, [participant()], {}, 0)
    messages = decide_messages(context)
    assert messages[0]["role"] == "system"
    prompt = json.loads(messages[-1]["content"])
    assert prompt["task"] == "decide"
    assert set(prompt["output_schema"]) == set(valid_decision())
    assert validate_decision(valid_decision(), context)["action"] == "ask_example"

    for bad in (
        valid_decision(topic_id="foreign"),
        valid_decision(question="甲？乙？"),
        valid_decision(basis_turn_ids=["foreign"]),
        valid_decision(question="字" * 101),
    ):
        with pytest.raises(DecisionError):
            validate_decision(bad, context)


def test_all_previously_refused_topics_close_without_reopening_them():
    context = build_context(
        STUDY, [participant(text="已经讲完了。")], {"refusals": [{"topic_id": "T1"}, {"topic_id": "T2"}]}, 100
    )
    decision = fallback_decision(context)
    assert decision["action"] == "close"
    assert decision["topic_id"] is None


def test_decision_respects_end_refusal_duplicate_and_three_probe_limit():
    ending = build_context(STUDY, [participant(text="提前结束吧")], {}, 0)
    with pytest.raises(DecisionError):
        validate_decision(valid_decision(), ending)
    assert fallback_decision(ending)["action"] == "close"

    refusal = build_context(STUDY, [participant(text="这个不能说")], {}, 0)
    with pytest.raises(DecisionError):
        validate_decision(valid_decision(action="probe_detail"), refusal)
    assert fallback_decision(refusal)["action"] == "transition"

    turns = [participant()]
    turns += [
        {
            "id": f"A{i}",
            "role": "assistant",
            "text": f"追问{i}？",
            "revision_id": None,
            "topic_id": "T1",
            "action": "probe_detail",
            "confirmed": True,
        }
        for i in range(3)
    ]
    context = build_context(STUDY, turns, {}, 0)
    with pytest.raises(DecisionError):
        validate_decision(valid_decision(action="probe_detail"), context)

    duplicate_context = build_context(
        STUDY,
        [
            participant(),
            {
                "id": "A9",
                "role": "assistant",
                "text": valid_decision()["question"],
                "revision_id": None,
                "topic_id": "T1",
                "action": "ask_example",
                "confirmed": True,
            },
        ],
        {},
        0,
    )
    with pytest.raises(DecisionError):
        validate_decision(valid_decision(), duplicate_context)


def test_remaining_time_forces_close_and_blocks_low_priority_new_topic():
    expired = build_context(STUDY, [participant()], {}, 3600)
    with pytest.raises(DecisionError):
        validate_decision(valid_decision(), expired)
    assert fallback_decision(expired)["action"] == "close"

    wrap_up = build_context(STUDY, [participant()], {}, 3300)
    with pytest.raises(DecisionError):
        validate_decision(valid_decision(action="transition", topic_id="T2"), wrap_up)


def test_report_validates_exact_confirmed_revision_and_character_range():
    turns = [
        participant(
            revisions=[
                {"id": "U1-r0", "text": "机器稿"},
                {"id": "U1-r1", "text": "第一轮要了资料，过两天又补情况。"},
            ]
        )
    ]
    report = {
        "summary": "补件经历",
        "findings": [
            {
                "type": "statement",
                "section": "statement",
                "text": "受访者称补件分两次发生。",
                "citations": [
                    {"turn_id": "U1", "revision_id": "U1-r1", "start": 0, "end": 7, "quote": "第一轮要了资料"}
                ],
            }
        ],
        "limitations": ["陈述未核对业务材料"],
        "unanswered": [],
    }
    assert validate_report(report, turns) == report
    for bad_citation in (
        {"turn_id": "foreign", "revision_id": "U1-r1", "start": 0, "end": 7, "quote": "第一轮要了资料"},
        {"turn_id": "U1", "revision_id": "foreign", "start": 0, "end": 7, "quote": "第一轮要了资料"},
        {"turn_id": "U1", "revision_id": "U1-r1", "start": 0, "end": 7, "quote": "伪造的引文"},
    ):
        invalid = {**report, "findings": [{**report["findings"][0], "citations": [bad_citation]}]}
        with pytest.raises(ReportError):
            validate_report(invalid, turns)


def test_report_uses_only_confirmed_sources_chunks_all_sources_and_marks_mock_render():
    turns = [participant(f"U{i}", f"第{i}条" * 20) for i in range(8)]
    turns.append(participant("U9", "待确认", confirmed=False))
    chunks = report_source_chunks(turns, max_chars=100)
    assert [source["turn_id"] for chunk in chunks for source in chunk] == [f"U{i}" for i in range(8)]
    assert all(sum(len(source["text"]) for source in chunk) <= 100 for chunk in chunks)
    payload = json.loads(report_messages(STUDY, turns)[-1]["content"])
    assert payload["task"] == "report"
    assert set(payload["output_schema"]) == {"summary", "findings", "limitations", "unanswered"}
    assert all(turn["id"] != "U9" for turn in payload["turns"])

    report = mock_report(STUDY, turns)
    assert report["findings"][0]["type"] == "statement"
    assert render_report(report, "mock").startswith("> **模拟结果**")
    assert mock_response({"task": "report", "study": STUDY, "turns": turns}) == report


def test_report_rejects_missing_citations_and_unknown_finding_type_and_escapes_html():
    turns = [participant(text="我认为流程偏慢。")]
    report = mock_report(STUDY, turns)
    no_citation = {**report, "findings": [{**report["findings"][0], "citations": []}]}
    with pytest.raises(ReportError):
        validate_report(no_citation, turns)
    unknown = {**report, "findings": [{**report["findings"][0], "type": "fact"}]}
    with pytest.raises(ReportError):
        validate_report(unknown, turns)
    html_report = {**report, "summary": "<script>alert(1)</script>"}
    rendered = render_report(html_report, "live")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_report_sections_reject_claiming_explanations_as_facts_or_citing_questions():
    turns = [
        participant(text="我认为需要核查。"),
        {"id": "Q1", "role": "assistant", "text": "你负责什么？", "revision_id": "QR1", "confirmed": True},
    ]
    report = mock_report(STUDY, turns)
    for updates in (
        {"section": "explanation", "type": "statement"},
        {"section": "role", "type": "hypothesis"},
        {"section": []},
        {"type": []},
    ):
        with pytest.raises(ReportError):
            validate_report({**report, "findings": [{**report["findings"][0], **updates}]}, turns)
    for citation in (
        {"turn_id": "Q1", "revision_id": "QR1", "start": 0, "end": 6, "quote": "你负责什么？"},
        {"turn_id": [], "revision_id": "QR1", "start": 0, "end": 1, "quote": "你"},
    ):
        with pytest.raises(ReportError):
            validate_report(
                {**report, "findings": [{**report["findings"][0], "citations": [citation]}]}, turns
            )
    payload = json.loads(report_messages(STUDY, turns)[-1]["content"])
    assert (
        payload["turns"][-1]["role"] == "assistant"
    )  # Context is read, but never cited as participant evidence.


def test_mock_dispatch_is_deterministic_and_rejects_unknown_task():
    context = build_context(STUDY, [participant()], {}, 0)
    payload = {"task": "decide", "context": context}
    assert mock_response(payload) == mock_response(payload)
    assert mock_response({"task": "outline", "study": STUDY})["topics"] == STUDY["topics"]
    with pytest.raises(ValueError):
        mock_response({"task": "unknown"})


def test_prompt_to_mock_to_validation_contracts_round_trip():
    turns = [participant(text="我认为流程偏慢。")]
    context = build_context(STUDY, turns, {}, 0)
    decision_payload = json.loads(decide_messages(context)[-1]["content"])
    decision_raw = json.dumps(mock_response(decision_payload), ensure_ascii=False)
    assert validate_decision(decision_raw, context)["action"] == "probe_explanation"

    report_payload = json.loads(report_messages(STUDY, turns)[-1]["content"])
    report_raw = json.dumps(mock_response(report_payload), ensure_ascii=False)
    assert validate_report(report_raw, turns)["findings"][0]["type"] == "opinion"
