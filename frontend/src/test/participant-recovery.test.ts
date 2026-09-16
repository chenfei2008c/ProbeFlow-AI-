import { manualTranscriptTurnId } from '../lib/participantRecovery'
import { Job, Turn } from '../types'

test('ASR failure uses the original turn id returned by the job', () => {
  const job = { id: 'j1', kind: 'asr', status: 'failed', result: { turn_id: 'turn-from-job' }, created_at: '' } as Job
  expect(manualTranscriptTurnId(job, [])).toBe('turn-from-job')
})

test('ASR unknown state falls back to the current unconfirmed participant turn', () => {
  const job = { id: 'j1', kind: 'asr', status: 'external_status_unknown', created_at: '' } as Job
  const turns = [{ id: 'turn-current', role: 'participant', status: 'transcribing', confirmed: false }] as Turn[]
  expect(manualTranscriptTurnId(job, turns)).toBe('turn-current')
})

test('non-ASR failures do not open manual transcript recovery', () => {
  const job = { id: 'j1', kind: 'decide', status: 'failed', created_at: '' } as Job
  expect(manualTranscriptTurnId(job, [])).toBeUndefined()
})
