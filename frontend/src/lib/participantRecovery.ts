import { Job, Turn } from '../types'

export function confirmWithdrawal() {
  return window.confirm('撤回会删除本场已提交的录音、文字、全部修订、报告与引用，并停止关联任务。确定继续吗？')
    && window.confirm('请再次确认：撤回后访问凭证失效，本场内容无法恢复；结束访谈不会删除资料。确定撤回并删除吗？')
}

export function manualTranscriptTurnId(job: Job | undefined, turns: Turn[]) {
  if (!job || job.kind !== 'asr' || !['failed', 'external_status_unknown'].includes(job.status)) return undefined
  const fromJob = (job.result as { turn_id?: string } | undefined)?.turn_id
  if (fromJob) return fromJob
  return [...turns].reverse().find(turn => turn.role === 'participant' && !turn.confirmed && ['recording', 'uploading', 'transcribing'].includes(turn.status))?.id
}
