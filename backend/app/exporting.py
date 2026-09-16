"""Portable archive exports use saved provenance, never current model settings."""

import csv
import io
import json

from app.interview import render_text
from app.provenance import LABELS, archive_mode


SOURCE_LABELS = {
    "machine_unconfirmed": "机器转写，未逐轮确认",
    "participant_confirmed": "受访者确认",
    "researcher_correction": "研究者勘误",
    "typed": "受访者原始输入",
    "model": "AI 问题",
}


def confirmation_label(revision):
    return SOURCE_LABELS.get(revision["source"], "确认来源未记录")


def model_calls(provenance):
    calls = (provenance or {}).get("calls", [])
    return json.dumps(calls, ensure_ascii=False) if calls else "未记录具体模型"


def citation_index(data):
    return [
        {
            **citation,
            "report_id": report["id"],
            "report_version": report["version"],
            "report_created_at": report["created_at"],
            "report_provenance": report["provenance"],
            "source_updated": report["source_updated"],
        }
        for report in data["reports"]
        for citation in report["citations"]
    ]


def csv_cell(value):
    text = "" if value is None else str(value)
    # Protect every external field, including new metadata columns.
    return (
        "'" + text
        if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n"))
        else text
    )


def csv_export(data, metadata):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "模式",
            "轮次",
            "发言ID",
            "文本版本",
            "角色",
            "来源",
            "文本",
            "创建时间",
            "保存策略",
            "研究ID",
            "研究标题",
            "研究版本ID",
            "研究版本",
            "提示词版本",
            "历史模型调用",
            "确认标记",
            "当前文本版本",
            "前一文本版本",
            "引用索引",
            "导出时间",
        ]
    )
    version = data["study_version"]
    study = [
        data["session"]["study_id"],
        data["study"]["title"],
        version["id"],
        version["number"],
        version["prompt_version"],
    ]
    citations = {}
    for citation in citation_index(data):
        citations.setdefault(citation["revision_id"], []).append(citation)
    for turn in data["turns"]:
        for revision in turn["revisions"]:
            row = [
                LABELS[archive_mode([revision["provenance"]])],
                turn["seq"],
                turn["id"],
                revision["id"],
                turn["role"],
                revision["source"],
                revision["text"],
                revision["created_at"],
                "permanent",
                *study,
                model_calls(revision["provenance"]),
                confirmation_label(revision),
                "是" if turn["revision_id"] == revision["id"] else "否",
                revision["previous_id"],
                json.dumps(citations.get(revision["id"], []), ensure_ascii=False),
                metadata["exported_at"],
            ]
            writer.writerow([csv_cell(value) for value in row])
    return "\ufeff" + buffer.getvalue()


def markdown_export(data, metadata):
    version = data["study_version"]
    lines = ["# 访谈记录", "", metadata["label"], "", "保存策略：永久保存", ""]

    def field(label, value):
        lines.extend([f"{label}：{render_text(value)}", ""])

    field("研究", data["study"]["title"])
    field("研究 ID", data["session"]["study_id"])
    field("研究版本", f"{version['number']} · {version['id']}")
    field("提示词版本", version["prompt_version"])
    field("访谈 ID", data["session"]["id"])
    field("访谈创建时间", data["session"]["created_at"])
    field("导出时间", metadata["exported_at"])
    if data["reports"]:
        latest = data["reports"][-1]
        if latest["source_updated"]:
            lines.extend(["**来源已更新，请重新生成报告。**", ""])
        lines.extend([latest["markdown"], ""])
    else:
        lines.extend(["报告尚未生成。", ""])

    lines.extend(["## 报告版本与引用索引", "", "正文展示最新报告；以下索引保留全部报告的引用关系。", ""])
    for report in data["reports"]:
        lines.extend([f"### 报告版本 {report['version']}", ""])
        field("报告 ID", report["id"])
        field("生成时间", report["created_at"])
        field("资料来源", LABELS[report["archive_mode"]])
        field("历史模型调用", model_calls(report["provenance"]))
        field("来源状态", "来源已更新" if report["source_updated"] else "来源未更新")
        for citation in report["citations"]:
            field("引用 ID", citation["id"])
            field("发言 ID／文本版本 ID", f"{citation['turn_id']}／{citation['revision_id']}")
            field("字符范围（起点含，终点不含）", f"{citation['start']}—{citation['end']}")
            field("引文", citation["quote"])
        if not report["citations"]:
            lines.extend(["没有引用。", ""])

    lines.extend(["## 逐字稿与修订历史", ""])
    for turn in data["turns"]:
        lines.extend([f"### {turn['seq']} · {render_text(turn['role'])}", ""])
        field("发言 ID", turn["id"])
        for number, revision in enumerate(turn["revisions"], 1):
            lines.extend([f"#### 文本版本 {number}", ""])
            field("文本版本 ID", revision["id"])
            field("前一文本版本 ID", revision["previous_id"] or "无")
            field("当前文本版本", "是" if turn["revision_id"] == revision["id"] else "否")
            field("创建时间", revision["created_at"])
            field("确认标记", confirmation_label(revision))
            field("来源类型", revision["source"])
            field("资料来源", LABELS[archive_mode([revision["provenance"]])])
            field("历史模型调用", model_calls(revision["provenance"]))
            lines.extend([render_text(revision["text"]), ""])
    return "\n".join(lines)
