import { ReactNode, useEffect, useState } from 'react'
import { ApiError } from '../lib/api'
import { statusLabel, jobLabel } from '../lib/format'
import { AppMode, ArchiveMode, Job } from '../types'

export function ModeBadge({ mode }: { mode: AppMode }) {
  return <span className={`mode-badge ${mode}`}>{mode === 'mock' ? '模拟模式 · 输出不可用于质量判断' : '真实服务模式'}</span>
}

export function ArchiveNotice({ mode = 'unknown' }: { mode?: ArchiveMode }) {
  if (mode === 'live') return null
  const message = {
    mock: '模拟结果：仅用于验证界面与流程，不代表真实访谈质量。',
    mixed: '混合来源档案：包含模拟或来源未确认内容，不可作为真实质量验收依据。',
    unknown: '来源未确认：历史档案缺少来源记录，不可作为真实质量验收依据。',
  }[mode]
  return <div className="mock-warning" role="note">{message}</div>
}

export function StatusBadge({ status }: { status: string }) {
  return <span className={`status status-${status}`}>{statusLabel[status] ?? status}</span>
}

export function Loading({ label = '正在读取…' }: { label?: string }) {
  return <div className="loading"><span className="spinner" /> <span>{label}</span></div>
}

export function ErrorPanel({ error, retry }: { error: ApiError | Error; retry?: () => void }) {
  const api = error instanceof ApiError ? error : undefined
  return <div className="error-panel" role="alert">
    <div><strong>{error.message || '请求失败，请刷新页面后重试。'}</strong>{api?.requestId && api.requestId !== 'unknown' && <small>请求编号 {api.requestId}</small>}</div>
    {retry && <button className="button ghost" onClick={retry}>{api?.retryable ? '重试' : '重新读取'}</button>}
  </div>
}

export function EmptyState({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return <div className="empty-state"><div className="empty-mark">⌁</div><h3>{title}</h3><p>{children}</p>{action}</div>
}

export function JobNotice({ job, onRetry }: { job: Job; onRetry?: (acceptPossibleCharge: boolean) => void }) {
  const unknown = job.status === 'external_status_unknown'
  const [chargeAccepted, setChargeAccepted] = useState(false)
  useEffect(() => setChargeAccepted(false), [job.id, job.status])
  return <div className={`job-notice ${unknown ? 'unknown' : ''}`}>
    <div><StatusBadge status={job.status} /><strong>{jobLabel[job.kind] ?? job.kind}</strong></div>
    <p>{unknown ? '服务商可能已经处理并计费，但结果没有返回。再次发起可能产生重复费用。' : job.error_message ?? (job.status === 'succeeded' ? '处理已完成，结果已保存。' : job.status === 'cancelled' ? '任务已取消，不再继续处理。' : '任务将在后台继续，刷新页面不会丢失。')}</p>
    {onRetry && unknown && <label className="charge-confirm"><input type="checkbox" checked={chargeAccepted} onChange={event => setChargeAccepted(event.target.checked)} /><span>我了解上次请求可能已经产生费用，再次请求可能重复计费。</span></label>}
    {onRetry && (job.status === 'failed' || unknown) && <button className="button secondary" disabled={unknown && !chargeAccepted} onClick={() => onRetry(unknown)}>重试任务</button>}
  </div>
}
