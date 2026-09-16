import { useEffect, useRef, useState } from 'react'
import { apiRequest } from '../lib/api'
import { useApi } from '../hooks/useApi'
import type { Config, ImportResult, ImportSource, Job, Study, StudyConfig } from '../types'
import { ErrorPanel, JobNotice } from './Common'

export function StudyImport({ study, onUploaded, onApply, onBusy, disabled = false }: {
  study?: Study
  onUploaded: (result: { study_id: string; job_id: string }) => void
  onApply?: (draft: StudyConfig) => void
  onBusy?: (busy: boolean) => void
  disabled?: boolean
}) {
  const { data: config, error: configError, reload: reloadConfig } = useApi<Config>('/api/config')
  const [file, setFile] = useState<File>()
  const [accepted, setAccepted] = useState(false)
  const [chargeAccepted, setChargeAccepted] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<Error>()
  const [job, setJob] = useState<Job | null>(study?.import_job ?? null)
  const [source, setSource] = useState(study?.import_source)
  const uploadKey = useRef(crypto.randomUUID())
  const applyWhenReady = useRef(false)
  const onApplyRef = useRef(onApply)
  onApplyRef.current = onApply
  useEffect(() => { setJob(study?.import_job ?? null) }, [study?.import_job])
  useEffect(() => { setSource(study?.import_source) }, [study?.import_source])
  const active = job?.status === 'queued' || job?.status === 'running'
  const unknownCharge = job?.status === 'external_status_unknown'
  const busy = uploading || active
  useEffect(() => { onBusy?.(Boolean(busy)) }, [busy, onBusy])
  useEffect(() => {
    if (!active || !job || error) return
    let cancelled = false
    const timer = window.setTimeout(async () => {
      try {
        const next = await apiRequest<Job>(`/api/admin/jobs/${job.id}`)
        if (cancelled) return
        setJob(next)
        if (next.status === 'succeeded' && (applyWhenReady.current || !study?.current_version_id)) {
          onApplyRef.current?.((next.result as ImportResult).study)
          applyWhenReady.current = false
        }
      } catch (value) { if (!cancelled) setError(value as Error) }
    }, 1200)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [job, active, error, study?.current_version_id])

  const upload = async () => {
    if (!file || !accepted || busy || disabled || !config || (unknownCharge && !chargeAccepted)) return
    setError(undefined)
    if (file.size > 10 * 1024 * 1024) { setError(new Error('文件不能超过 10 MB。')); return }
    setUploading(true)
    try {
      const result = await apiRequest<{ study_id: string; job_id: string; source: ImportSource }>(study ? `/api/admin/studies/${study.id}/import` : '/api/admin/studies/import', {
        method: 'POST', body: file, idempotencyKey: uploadKey.current, headers: { 'X-File-Name': encodeURIComponent(file.name), 'X-Import-Consent': 'accepted', 'X-Import-Version': config.consent_version, 'X-Accept-Possible-Charge': String(chargeAccepted) },
      })
      applyWhenReady.current = true
      setJob({ id: result.job_id, kind: 'study_import', status: 'queued', created_at: new Date().toISOString() })
      setSource(result.source)
      uploadKey.current = crypto.randomUUID()
      setAccepted(false)
      setChargeAccepted(false)
      onUploaded(result)
    } catch (value) { setError(value as Error) } finally { setUploading(false) }
  }
  const retry = async (acceptPossibleCharge: boolean) => {
    if (!study || !job || uploading || disabled) return
    setUploading(true); setError(undefined)
    try {
      await apiRequest(`/api/admin/studies/${study.id}/imports/${job.id}/retry`, { method: 'POST', body: { accept_possible_charge: acceptPossibleCharge } })
      applyWhenReady.current = true
      setJob({ ...job, status: 'queued', error_message: undefined })
    } catch (value) { setError(value as Error) } finally { setUploading(false) }
  }
  const result = job?.status === 'succeeded' ? job.result as ImportResult : undefined
  return <section className="panel import-panel" aria-label="导入调研方案">
    <div className="section-heading"><div><span className="eyebrow">从已有方案开始</span><h2>上传调研方案，自动整理研究设计</h2></div></div>
    <p className="section-help">支持 PDF、Word（.docx）、Excel（.xlsx）、PPT（.pptx），每个文件不超过 10 MB。上传后自动填写目标、受访者和访谈主题，你只需核对并发布。</p>
    <p className="muted">读取可复制的文字、表格和幻灯片备注；扫描件、图片及图表内的文字暂不识别。旧版 .doc／.xls／.ppt 请先另存为新格式。</p>
    <div className="import-controls"><label className="field"><span>选择调研方案文件</span><input type="file" accept=".pdf,.docx,.xlsx,.pptx" disabled={Boolean(busy) || disabled} onChange={event => { setFile(event.target.files?.[0]); uploadKey.current = crypto.randomUUID(); setAccepted(false); setError(undefined) }} /></label>
      {config && <p className="section-help">{config.mode === 'live' ? `方案提取文本将发送至 ${config.providers.interview.provider}（${config.providers.interview.model}）整理，费用计入月度预算。` : '当前为模拟模式：可验证文件读取和导入流程；真实自动整理需要启用模型服务。'}提取文本随研究保存，原文件请另行保留。</p>}
      <label className="switch-row"><input type="checkbox" checked={accepted} disabled={Boolean(busy) || disabled || !config} onChange={event => setAccepted(event.target.checked)} /><span>我同意按上述说明解析此方案并保存提取文本。</span></label>
      {unknownCharge && <label className="charge-confirm"><input type="checkbox" checked={chargeAccepted} onChange={event => setChargeAccepted(event.target.checked)} /><span>我了解上次导入可能已计费，重新上传仍可能增加费用。</span></label>}
      <button className="button primary" disabled={!file || !accepted || Boolean(busy) || disabled || !config || (unknownCharge && !chargeAccepted)} onClick={upload}>{uploading ? '正在上传并读取…' : active ? '正在整理方案…' : '上传并生成研究草稿'}</button>
    </div>
    {configError && <ErrorPanel error={configError} retry={reloadConfig} />}
    {error && <ErrorPanel error={error} retry={active ? () => setError(undefined) : undefined} />}
    {job && <JobNotice job={job} onRetry={study && !uploading && !disabled && job.error_code !== 'IMPORT_CONSENT_CHANGED' ? retry : undefined} />}
    {result && <div className="import-result"><strong>研究草稿已准备好，请核对后发布。</strong>{result.mode === 'mock' && <p className="mock-warning">模拟导入：内容与示例主题仅用于流程验证，不代表 AI 已理解方案。</p>}
      {result.warnings.length > 0 && <ul>{result.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
      {onApply && <button className="button secondary" disabled={disabled} onClick={() => onApply(result.study)}>应用到当前草稿</button>}
    </div>}
    {source && <details className="import-source"><summary>核对提取原文 · {source.filename}</summary><p className="muted">提取 {source.characters.toLocaleString()} 字符。排版可能变化，请重点核对目标、问题、禁问范围和表格对应关系。</p><pre>{source.text}</pre></details>}
  </section>
}
