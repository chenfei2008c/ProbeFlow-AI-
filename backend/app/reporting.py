"""Deterministic report context; model prose cannot remove observed limitations."""

COVERAGE_LABELS = {
    "not_started": "未谈及",
    "awaiting_answer": "已提问，尚无确认回答",
    "partial": "已讨论，仍待补充",
    "covered": "已覆盖（模型判断）",
    "skipped": "已跳过／拒答",
}
REPORT_SECTIONS = {
    "role": "受访者自述角色",
    "event": "具体事件",
    "statement": "主要陈述",
    "explanation": "受访者对原因的解释",
    "hypothesis": "待验证假设",
    "suggestion": "受访者建议",
}


def topic_coverage(study, turns, memory):
    turns = [t for t in turns if t.get("status") != "superseded"]
    known = {t["id"]: t for t in turns}
    assessments = {t["id"]: t for t in memory.get("topics", []) if isinstance(t, dict) and "id" in t}
    topics = []
    for topic in study.get("topics", []):
        answers = [t for t in turns if t.get("topic_id") == topic["id"] and t["role"] == "participant"]
        confirmed = [t for t in answers if t["confirmed"]]
        questions = [t for t in turns if t.get("topic_id") == topic["id"] and t["role"] == "assistant"]
        refusals = [
            r for r in memory.get("refusals", []) if isinstance(r, dict) and r.get("topic_id") == topic["id"]
        ]
        assessment = assessments.get(topic["id"], {})
        evidence = assessment.get("evidence_turn_ids", [])
        revisions = assessment.get("evidence_revisions", {})
        current = (
            bool(evidence)
            and all(
                tid in {t["id"] for t in confirmed} and revisions.get(tid) == known[tid].get("revision_id")
                for tid in evidence
            )
            and all(t.get("seq", 0) <= assessment.get("through_seq", -1) for t in confirmed)
        )
        status = "partial" if confirmed else "awaiting_answer" if questions else "not_started"
        if current and assessment.get("status") == "covered":
            status = "covered"
        if refusals:
            status = "skipped"
        topics.append(
            {
                "id": topic["id"],
                "title": topic["title"],
                "status": status,
                "confirmed_turn_ids": [t["id"] for t in confirmed],
                "unconfirmed_count": len(answers) - len(confirmed),
                "reasons": list(dict.fromkeys(r.get("reason", "受访者跳过／拒答") for r in refusals)),
            }
        )
    unresolved = []
    for item in memory.get("unresolved", []):
        if isinstance(item, dict) and item.get("text"):
            unresolved.append(
                {"text": item["text"], "turn_ids": [tid for tid in item.get("turn_ids", []) if tid in known]}
            )
    return {"topics": topics, "unresolved": unresolved}


def observed_report_context(study, turns, memory, session, audio_issues=()):
    coverage = topic_coverage(study, turns, memory)
    participants = [t for t in turns if t["role"] == "participant" and t.get("status") != "superseded"]
    limits = ["受访者陈述尚未通过外部材料核实；引用匹配不代表陈述属实或模型理解正确。"]
    if any(not t["confirmed"] for t in participants):
        limits.append("存在尚未确认的回答，未作为报告结论来源。")
    if any(t.get("text_source") == "machine_unconfirmed" for t in participants):
        limits.append("包含机器转写，未逐轮确认。")
    if session.mode == "text" and not any(t["input_mode"] == "voice" for t in participants):
        limits.append("本场为文字输入，无受访者录音。")
    elif any(t["input_mode"] == "text" for t in participants):
        limits.append("部分回答采用文字输入，没有对应的受访者录音。")
    if session.ended_at is not None and session.active_seconds < session.target_seconds:
        limits.append(
            f"访谈提前结束：有效时长 {int(session.active_seconds)} 秒，目标 {session.target_seconds // 60} 分钟；部分主题可能尚未覆盖。"
        )
    elif session.ended_at is None:
        limits.append("访谈尚未结束，本报告仅反映生成时已确认的记录。")
    limits.extend(audio_issues)
    unanswered = [
        f"{t['title']}：{COVERAGE_LABELS[t['status']]}"
        + (f"（{'；'.join(t['reasons'])}）" if t["reasons"] else "")
        for t in coverage["topics"]
        if t["status"] != "covered"
    ]
    unanswered.extend(item["text"] for item in coverage["unresolved"])
    return {
        "background": {
            "title": study["title"],
            "objective": study["objective"],
            "study_version_id": session.study_version_id,
        },
        "coverage": coverage,
        "limitations": list(dict.fromkeys(limits)),
        "unanswered": list(dict.fromkeys(unanswered)),
    }
