"""Exports remain traceable without access to the application or current config."""

import csv
import io
import json

from sqlalchemy import select

from app.interview import render_text
from app.models import Citation, InterviewSession, Revision, StudyVersion
from test_audio_flow import ready, upload, wav_bytes
from test_core import headers
from test_interview_flow import drain


def test_exports_preserve_historical_versions_models_and_citation_identity(admin, app):
    participant, sid = ready(admin, app)
    tid = participant.post("/api/participant/turns", json={"input_mode": "voice"}, headers=headers()).json()[
        "turn_id"
    ]
    _, manifest = upload(participant, tid, 0, wav_bytes())
    assert (
        participant.post(
            f"/api/participant/turns/{tid}/finalize", json={"chunks": [manifest]}, headers=headers()
        ).status_code
        == 200
    )
    drain(app)
    assert (
        participant.post(
            f"/api/participant/turns/{tid}/confirm",
            json={"text": "虚构案例：提交一次材料。"},
            headers=headers(),
        ).status_code
        == 200
    )
    assert (
        participant.post("/api/participant/control", json={"action": "end"}, headers=headers()).status_code
        == 200
    )
    drain(app)
    assert (
        admin.post(
            f"/api/admin/turns/{tid}/revision", json={"text": "虚构案例：提交两次材料。"}, headers=headers()
        ).status_code
        == 200
    )
    assert admin.post(f"/api/admin/sessions/{sid}/reports", headers=headers()).status_code == 200
    drain(app)
    before = admin.get(f"/api/admin/sessions/{sid}").json()
    historical_asr, historical_report = app.state.settings.asr_model, app.state.settings.report_model
    app.state.settings.asr_model = app.state.settings.report_model = "future-model-not-in-archive"
    with app.state.db.read() as db:
        expected_ids = {c.id for c in db.scalars(select(Citation))}
    exported = admin.get(f"/api/admin/sessions/{sid}/export").json()
    assert {c["id"] for r in exported["reports"] for c in r["citations"]} == expected_ids
    turn = next(t for t in before["turns"] if t["id"] == tid)
    revisions = turn["revisions"]
    assert len(revisions) == 3 and len(before["reports"]) == 2
    csv_text = admin.get(f"/api/admin/sessions/{sid}/export?format=csv").text
    rows = list(csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"))))
    answer_rows = [r for r in rows if r["发言ID"] == tid]
    assert len(answer_rows) == 3
    assert [r["确认标记"] for r in answer_rows] == ["机器转写，未逐轮确认", "受访者确认", "研究者勘误"]
    assert [r["当前文本版本"] for r in answer_rows] == ["否", "否", "是"]
    for row, revision in zip(answer_rows, revisions, strict=True):
        assert row["研究版本ID"] == before["study_version"]["id"]
        assert row["提示词版本"] == before["study_version"]["prompt_version"]
        assert row["创建时间"] == revision["created_at"]
        assert row["前一文本版本"] == (revision["previous_id"] or "")
        assert row["保存策略"] == "permanent" and row["导出时间"]
    indexed = [c for row in answer_rows for c in json.loads(row["引用索引"])]
    assert {c["id"] for c in indexed} == expected_ids
    for entry in indexed:
        revision = next(r for r in revisions if r["id"] == entry["revision_id"])
        assert revision["text"][entry["start"] : entry["end"]] == entry["quote"]
        assert entry["report_id"] in {r["id"] for r in before["reports"]}
        assert entry["report_created_at"] and entry["report_provenance"]["calls"]
    markdown = admin.get(f"/api/admin/sessions/{sid}/export?format=markdown").text
    for value in (
        before["study_version"]["id"],
        before["study_version"]["prompt_version"],
        historical_asr,
        historical_report,
        *expected_ids,
        *(r["id"] for r in revisions),
        *(r["created_at"] for r in revisions),
    ):
        assert render_text(value) in markdown
    assert historical_asr in csv_text and historical_report in csv_text
    assert "future-model-not-in-archive" not in csv_text + markdown
    assert "机器转写，未逐轮确认" in markdown and "研究者勘误" in markdown
    assert "来源已更新" in markdown  # Historical report V1 still identifies stale sources.


def test_export_metadata_escapes_external_values_and_does_not_invent_legacy_models(admin, app):
    _, sid = ready(admin, app, "text")
    hostile = "=1+1 [链接](javascript:alert(1)) <script>alert(1)</script>"
    with app.state.db.transaction() as db:
        session = db.get(InterviewSession, sid)
        version = db.get(StudyVersion, session.study_version_id)
        version.config = {**version.config, "title": hostile}
        for revision in db.scalars(select(Revision)):
            revision.provenance = {}
    app.state.settings.interview_model = "future-model-not-in-archive"
    csv_text = admin.get(f"/api/admin/sessions/{sid}/export?format=csv").text
    row = next(csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"))))
    assert row["研究标题"] == "'" + hostile
    assert row["历史模型调用"] == "未记录具体模型"
    markdown = admin.get(f"/api/admin/sessions/{sid}/export?format=markdown").text
    assert "<script>" not in markdown and "[链接](javascript:" not in markdown
    assert "\\[链接\\]" in markdown and "未记录具体模型" in markdown
    assert "future-model-not-in-archive" not in csv_text + markdown
    assert "来源未确认" in csv_text + markdown
