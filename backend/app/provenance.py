"""Immutable archive origin, independent of the current runtime configuration."""

MODES = {"mock", "live", "unknown"}
LABELS = {
    "mock": "模拟结果（不代表真实访谈或模型质量）",
    "live": "真实服务处理记录（不代表结论已核实）",
    "mixed": "混合来源档案（包含模拟或来源未确认内容，不可作为真实质量验收依据）",
    "unknown": "来源未确认（历史档案缺少来源记录，不可作为真实质量验收依据）",
}


def classify(modes):
    values = set(modes) or {"unknown"}
    return next(iter(values)) if len(values) == 1 else "mixed"


def origin_modes(provenance):
    provenance = provenance or {}
    calls = provenance.get("calls", [])
    modes = {c.get("mode", "unknown") for c in calls} or {provenance.get("mode", "unknown")}
    modes.update(provenance.get("source_modes", []))
    return {m if m in MODES else "unknown" for m in modes}


def archive_mode(provenances):
    return classify(m for p in provenances for m in origin_modes(p))


def job_provenance(job, prefix, sources=()):
    calls = []
    for key, checkpoint in (job.result or {}).get("_checkpoints", {}).items():
        if key == prefix or key.startswith(prefix + "-"):
            call = checkpoint.get("provenance", {"mode": "unknown"})
            if call not in calls:
                calls.append(call)
    return {
        "mode": classify(c.get("mode", "unknown") for c in calls),
        "calls": calls,
        "source_modes": sorted({m for p in sources for m in origin_modes(p)}),
    }
