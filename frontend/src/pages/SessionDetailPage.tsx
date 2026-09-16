import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError, apiRequest } from '../lib/api'
import { dateTime, duration, money, actionLabel } from '../lib/format'
import { Citation, Detail, Job, Report, ReportSection, Turn } from '../types'
import { ArchiveNotice, ErrorPanel, JobNotice, Loading, StatusBadge } from '../components/Common'
import { useApi } from '../hooks/useApi'
import { TopicCoverage } from '../components/TopicCoverage'

export function SessionDetailPage() {
  const { id = '' } = useParams()
  const { data: detail, error, loading, reload } = useApi<Detail>(`/api/admin/sessions/${id}`)
  const [report, setReport] = useState<Report>()
  const [editing, setEditing] = useState<Turn>()
  const [editText, setEditText] = useState('')
  const [selectedCitation, setSelectedCitation] = useState<Citation>()
  const [mediaErrors, setMediaErrors] = useState<Set<string>>(new Set())
  const [budget, setBudget] = useState('')
  const [job, setJob] = useState<Job>()
  const [recoveryInvite, setRecoveryInvite] = useState<{ url: string; expires_at: string }>()
  const [actionError, setActionError] = useState<ApiError>()
  useEffect(() => { if (detail?.reports?.length) setReport(detail.reports[detail.reports.length - 1]) }, [detail?.reports])
  useEffect(() => {
    if (!job || !['queued', 'running'].includes(job.status)) return
    const timer = setTimeout(async () => { const found = await apiRequest<Job>(`/api/admin/jobs/${job.id}`); setJob(found); if (!['queued', 'running'].includes(found.status)) await reload() }, 1500)
    return () => clearTimeout(timer)
  }, [job, reload, detail?.jobs])
  if (loading || !detail) return <div className="page"><Loading label="正在读取访谈档案…" />{error && <ErrorPanel error={error} retry={reload} />}</div>
  const generate = async () => { if (job?.status === 'external_status_unknown' && !window.confirm('上一份报告的外部状态未知，服务商可能已经计费。仍要创建新报告任务吗？')) return; try { const result = await apiRequest<{ job_id: string }>(`/api/admin/sessions/${id}/reports`, { method: 'POST', body: {} }); setJob({ id: result.job_id, kind: '生成报告', status: 'queued', created_at: new Date().toISOString() }) } catch (value) { setActionError(value as ApiError) } }
  const saveRevision = async () => { if (!editing || !editText.trim()) return; try { await apiRequest(`/api/admin/turns/${editing.id}/revision`, { method: 'POST', body: { text: editText.trim() } }); setEditing(undefined); await reload() } catch (value) { setActionError(value as ApiError) } }
  const saveBudget = async () => { try { await apiRequest(`/api/admin/sessions/${id}/budget`, { method: 'POST', body: { budget_cny: budget } }); setBudget(''); await reload() } catch (value) { setActionError(value as ApiError) } }
  const jump = (citation: Citation) => { setSelectedCitation(citation); const turnId = citation.turn_id; const el = document.getElementById(`turn-${turnId}`); el?.scrollIntoView({ behavior: 'smooth', block: 'center' }); el?.classList.add('highlight'); setTimeout(() => el?.classList.remove('highlight'), 1800) }
  const createRecoveryInvite = async () => { try { setRecoveryInvite(await apiRequest(`/api/admin/sessions/${id}/recovery-invite`, { method: 'POST', body: {} })) } catch (value) { setActionError(value as ApiError) } }
  const remove = async () => { if (!window.confirm('删除本场会覆盖录音、全部文字修订、报告、引用与相关任务。确定继续吗？')) return; await apiRequest(`/api/admin/sessions/${id}`, { method: 'DELETE' }); location.assign(`/studies/${detail.session.study_id}`) }
  return <div className="page session-page">
    <div className="page-heading compact"><div><Link className="back-link" to={`/studies/${detail.session.study_id}`}>← 返回研究</Link><h1>{detail.session.participant_code}</h1><p>创建于 {dateTime(detail.session.created_at)} · 资料永久保存</p></div><div className="button-row"><button className="button ghost" onClick={createRecoveryInvite}>恢复邀请</button><ExportMenu id={id} /><button className="button primary" onClick={generate}>生成新报告</button></div></div>
    <ArchiveNotice mode={detail.archive_mode} />
    {detail.session.consent_version !== detail.consent_version && <p className="warning-banner">处理说明已更新。请受访者重新确认后再发起处理；已结束场次可通过恢复邀请进入补充授权。既有档案仍可读取和导出。</p>}
    {recoveryInvite && <div className="recovery-banner"><div><strong>恢复链接已创建，旧访问凭证已撤销</strong><p>{recoveryInvite.url} · 有效至 {dateTime(recoveryInvite.expires_at)}</p></div><button className="button secondary" onClick={() => navigator.clipboard.writeText(recoveryInvite.url)}>复制链接</button></div>}
    {actionError && <ErrorPanel error={actionError} />}{job && <JobNotice job={job} />}
    <section className="session-summary"><div><span>场次状态</span><StatusBadge status={detail.session.status} /></div><div><span>有效时长</span><strong>{duration(detail.session.active_seconds)}</strong></div><div><span>本场支出</span><strong>{money(detail.session.spent_cny)}</strong><small>预占 {money(detail.session.reserved_cny)}</small></div><div><span>访谈方式</span><strong>{detail.session.mode === 'voice' ? '语音' : '文字'}</strong></div><div><span>保存策略</span><strong className="teal">永久</strong></div></section>
    <section className="panel"><div className="section-heading"><h2>本场预算</h2><p>当前限额 {money(detail.session.budget_cny)}，追加后由受访者继续访谈。</p></div>{(detail.session.spent_cny + detail.session.reserved_cny) >= detail.session.budget_cny * 0.8 && <p className="warning-banner">本场已使用或预占至少 80% 预算。</p>}<label className="field"><span>新的本场总预算（元）</span><input type="number" min="0.01" step="0.01" value={budget} onChange={event => setBudget(event.target.value)} /></label><button className="button secondary" disabled={!budget || Number(budget) <= 0} onClick={saveBudget}>保存预算</button></section>
    {detail.coverage && <section className="panel"><div className="section-heading"><div><h2>主题覆盖与边界</h2><p>覆盖程度是整理提示，不能代替研究者判断；跳过和拒答会保留。</p></div></div><TopicCoverage coverage={detail.coverage} onTurn={turnId => document.getElementById(`turn-${turnId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })} /></section>}
    <div className="detail-tabs"><button className="active">逐轮记录</button><button onClick={() => document.getElementById('report')?.scrollIntoView({ behavior: 'smooth' })}>分析报告</button><button onClick={() => document.getElementById('jobs')?.scrollIntoView({ behavior: 'smooth' })}>处理记录</button></div>
    <div className="session-layout"><section className="transcript-panel"><div className="section-heading"><div><h2>逐轮记录</h2><p>{detail.turns.length} 条发言 · 修订历史全部保留</p></div></div><div className="turn-list">{detail.turns.map(turn => <article id={`turn-${turn.id}`} className={`turn ${turn.role}`} key={turn.id}><div className="turn-rail"><span>{String(turn.seq).padStart(2, '0')}</span><i /></div><div className="turn-content"><div className="turn-meta"><strong>{turn.role === 'assistant' ? 'AI 主持' : detail.session.participant_code}</strong><span>{dateTime(turn.created_at)}</span>{turn.confirmed && <span className="confirmed">✓ 已确认</span>}{turn.action && <span>{actionLabel[turn.action] ?? turn.action}</span>}</div><p>{turn.text || '（尚无可用文字）'}</p>{turn.audio_asset_id && turn.audio_status === 'available' && !mediaErrors.has(turn.id) ? <audio controls preload="none" src={`/api/media/${turn.audio_asset_id}`} onError={() => setMediaErrors(current => new Set([...current, turn.id]))} /> : <div className="audio-unavailable">{audioMessage(turn, detail.session.mode, mediaErrors.has(turn.id))}</div>}{turn.revisions?.length > 0 && <div className="revision-history">{turn.revisions.map((revision, index) => <details key={revision.id} open={selectedCitation?.revision_id === revision.id ? true : undefined}><summary>文本版本 {index + 1} · {dateTime(revision.created_at)}{revision.id === turn.revision_id ? ' · 当前版本' : ''}</summary><p>{selectedCitation?.revision_id === revision.id ? <>{revision.text.slice(0, selectedCitation.start)}<mark>{revision.text.slice(selectedCitation.start, selectedCitation.end)}</mark>{revision.text.slice(selectedCitation.end)}</> : revision.text}</p></details>)}</div>}{turn.role === 'participant' && <div className="turn-tools"><button className="text-button" onClick={() => { setEditing(turn); setEditText(turn.text) }}>添加勘误</button><span>{turn.revisions?.length ?? 0} 个版本</span></div>}{editing?.id === turn.id && <div className="revision-editor"><label>新修订不会覆盖历史版本</label><textarea rows={4} value={editText} onChange={e => setEditText(e.target.value)} /><div className="button-row end"><button className="button ghost" onClick={() => setEditing(undefined)}>取消</button><button className="button secondary" onClick={saveRevision}>保存修订</button></div></div>}</div></article>)}</div></section>
      <aside className="report-panel" id="report"><div className="section-heading"><div><h2>分析报告</h2><p>每项发现都应回到受访者原话。</p></div>{detail.reports && detail.reports.length > 1 && <select value={report?.id} onChange={e => setReport(detail.reports?.find(item => item.id === e.target.value))}>{detail.reports.map(item => <option value={item.id} key={item.id}>版本 {item.version}</option>)}</select>}</div>{!report ? <div className="report-empty"><span>⌘</span><h3>尚未生成报告</h3><p>报告只使用已确认的记录；生成任务独立运行，失败不会影响逐字稿。</p><button className="button secondary" onClick={generate}>生成报告</button></div> : <ReportView report={report} onCitation={jump} />}</aside>
    </div>
    <section id="jobs" className="panel jobs-panel"><div className="section-heading"><h2>处理记录</h2><small>失败与未知费用不会被隐藏</small></div>{detail.jobs.length ? detail.jobs.map(item => <JobNotice key={item.id} job={item} />) : <p className="muted">暂无后台任务记录。</p>}</section><div className="danger-zone"><div><strong>删除本场档案</strong><p>覆盖录音、全部修订、报告、引用与关联任务，并写入不含访谈内容的删除清单。</p></div><button className="button ghost danger-text" onClick={remove}>删除本场</button></div>
  </div>
}

const reportSections: Array<[ReportSection, string]> = [
  ['role', '受访者自述角色'], ['event', '具体事件'], ['statement', '主要陈述'],
  ['explanation', '受访者对原因的解释'], ['hypothesis', '待验证假设'], ['suggestion', '受访者建议'],
]

export function ReportView({ report, onCitation }: { report: Report; onCitation: (citation: Citation) => void }) {
  return <div className="report-body">
    <ArchiveNotice mode={report.archive_mode} />
    {report.source_updated && <div className="warning-banner">来源修订已更新。此历史报告仍引用生成时的文本版本。</div>}
    <div className="report-version"><StatusBadge status={report.status === 'ready' ? 'succeeded' : report.status} /><span>版本 {report.version} · {dateTime(report.created_at)}</span></div>
    {!!report.body.limitations?.length && <section className="report-limitations"><h3>研究局限</h3><ul>{report.body.limitations.map(item => <li key={item}>{item}</li>)}</ul></section>}
    <section><h3>研究背景</h3>{report.body.background ? <><strong>{report.body.background.title}</strong><p>{report.body.background.objective}</p></> : <p className="muted">该历史版本未保存研究背景快照。</p>}</section>
    {report.body.summary && <section><h3>概览</h3><p>{report.body.summary}</p></section>}
    <section><h3>已讨论范围</h3>{report.body.coverage ? <TopicCoverage coverage={report.body.coverage} /> : <p className="muted">该历史版本未保存主题覆盖快照。</p>}</section>
    {reportSections.map(([key, title]) => {
      const findings = report.body.findings?.filter(f => (f.section ?? (f.type === 'hypothesis' ? 'hypothesis' : 'statement')) === key) ?? []
      return <section key={key}><h3>{title}</h3>{!findings.length ? <p className="muted">尚缺乏依据。</p> : findings.map((finding, index) => <div className="finding" key={index}>
        <div className="finding-type">{finding.type === 'statement' ? '受访者陈述' : finding.type === 'opinion' ? '受访者观点' : '待验证假设'}</div><p>{finding.text}</p>
        <div className="citations">{finding.citations.map((citation, i) => <button onClick={() => onCitation(citation)} key={`${citation.turn_id}-${i}`}><span>证据 {citation.turn_id.slice(-6)}</span><q>{citation.quote}</q></button>)}</div>
      </div>)}</section>
    })}
    <section><h3>未回答／拒绝回答事项</h3>{report.body.unanswered?.length ? <ul>{report.body.unanswered.map(item => <li key={item}>{item}</li>)}</ul> : <p className="muted">本版本未记录其他未回答事项。</p>}</section>
  </div>
}

function ExportMenu({ id }: { id: string }) {
  const [open, setOpen] = useState(false)
  return <div className="export-menu"><button className="button ghost" onClick={() => setOpen(!open)}>导出 ▾</button>{open && <div className="export-popover">{['markdown', 'json', 'csv'].map(format => <a href={`/api/admin/sessions/${id}/export?format=${format}`} key={format} download>{format.toUpperCase()}</a>)}</div>}</div>
}

function audioMessage(turn: Turn, mode: string, failed: boolean) {
  if (failed) return '音频读取或播放失败，请检查连接或备份；文字仍可定位。'
  if (turn.audio_status === 'missing') return '音频档案缺失或损坏，仅可定位文字'
  if (turn.audio_status === 'unplayable') return '原始文件已保存，但格式或时长校验失败，无法播放'
  if (turn.role === 'assistant' && mode === 'voice') return '问题语音尚未生成或合成失败，可直接阅读问题文字'
  return turn.input_mode === 'text' ? '文字输入，无录音' : '本轮尚无已完成的音频档案'
}
