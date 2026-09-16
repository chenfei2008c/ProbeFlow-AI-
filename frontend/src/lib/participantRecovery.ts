import { Job, Turn } from '../types'

export function manualTranscriptTurnId(job: Job | undefined, turns: Turn[]) {
  if (!job || job.kind !== 'asr' || !['failed', 'external_status_unknown'].includes(job.status)) return undefined
  const fromJob = (job.result as { turn_id?: string } | undefined)?.turn_id
  if (fromJob) return fromJob
  return [...turns].reverse().find(turn => turn.role === 'participant' && !turn.confirmed && ['recording', 'uploading', 'transcribing'].includes(turn.status))?.id
}
