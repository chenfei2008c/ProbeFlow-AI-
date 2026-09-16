from app.reporting import topic_coverage


def test_coverage_never_calls_unanswered_or_stale_evidence_covered():
    study = {"topics": [{"id": "T1", "title": "经历"}, {"id": "T2", "title": "建议"}]}
    question = {
        "id": "q",
        "role": "assistant",
        "seq": 1,
        "topic_id": "T1",
        "confirmed": True,
        "revision_id": "qr",
    }
    answer = {
        "id": "a",
        "role": "participant",
        "seq": 2,
        "topic_id": "T1",
        "confirmed": True,
        "revision_id": "ar",
    }
    memory = {
        "topics": [
            {
                "id": "T1",
                "status": "covered",
                "evidence_turn_ids": ["q"],
                "evidence_revisions": {"q": "qr"},
                "through_seq": 2,
            }
        ]
    }
    assert topic_coverage(study, [question], memory)["topics"][0]["status"] == "awaiting_answer"
    memory["topics"][0].update(evidence_turn_ids=["a"], evidence_revisions={"a": "ar"})
    assert topic_coverage(study, [question, answer], memory)["topics"][0]["status"] == "covered"
    answer["revision_id"] = "ar2"
    result = topic_coverage(study, [question, answer], memory)
    assert result["topics"][0]["status"] == "partial"
    assert result["topics"][1]["status"] == "not_started"
