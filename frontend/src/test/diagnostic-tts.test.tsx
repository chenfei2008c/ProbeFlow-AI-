import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DiagnosticTts } from '../components/DiagnosticTts'

test('diagnostic TTS polls the job and renders the resulting authenticated audio', async () => {
  const request = vi.fn()
    .mockResolvedValueOnce({ job_id: 'job-1' })
    .mockResolvedValueOnce({ id: 'job-1', kind: 'tts', status: 'running', created_at: '2026-09-16T00:00:00Z' })
    .mockResolvedValueOnce({ id: 'job-1', kind: 'tts', status: 'succeeded', result: { audio_asset_id: 'audio-1' }, created_at: '2026-09-16T00:00:00Z' })
  render(<DiagnosticTts mode="mock" request={request} pollIntervalMs={0} />)
  await userEvent.click(screen.getByRole('button', { name: '创建试听任务' }))
  const audio = await screen.findByLabelText('中文语音试听结果')
  expect(audio).toHaveAttribute('src', '/api/media/audio-1')
  expect(screen.getByText('这是模拟提示音，不代表中文播报能力已通过。')).toBeInTheDocument()
})

test('diagnostic TTS shows the job failure in Chinese instead of leaving a job id', async () => {
  const request = vi.fn()
    .mockResolvedValueOnce({ job_id: 'job-2' })
    .mockResolvedValueOnce({ id: 'job-2', kind: 'tts', status: 'failed', error_message: '语音服务暂时不可用', created_at: '2026-09-16T00:00:00Z' })
  render(<DiagnosticTts mode="live" request={request} pollIntervalMs={0} />)
  await userEvent.click(screen.getByRole('button', { name: '创建试听任务' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('试听失败：语音服务暂时不可用')
})
