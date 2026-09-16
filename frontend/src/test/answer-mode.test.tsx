import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ParticipantPage } from '../pages/ParticipantPage'
import { apiRequest } from '../lib/api'
import { indexedChunkStorage, RecordingCoordinator } from '../lib/recorder'
import type { Config, Detail, Turn } from '../types'

vi.mock('../lib/api', async () => ({ ...await vi.importActual('../lib/api'), apiRequest: vi.fn() }))
const provider = { provider: 'mock', model: 'mock' }
const config: Config = { mode: 'mock', consent_version: 'v1', admin_initialized: true, providers: { asr: provider, interview: provider, tts: provider, report: provider } }
const question: Turn = { id: 'q1', seq: 1, role: 'assistant', status: 'ready_to_record', input_mode: 'text', text: '请描述一次虚构经历。', revisions: [], confirmed: true, created_at: '2026-09-16T00:00:00Z' }
let current: Detail
const mic = vi.fn()
const stop = vi.fn()

function writes() { return vi.mocked(apiRequest).mock.calls.filter(([, options]) => options?.method === 'POST') }
async function open(mode: 'text' | 'voice' = 'text', extraTurns: Turn[] = []) {
  current = { session: { id: 's1', study_id: 'study1', participant_code: 'P-FICTIONAL', status: 'in_progress', mode, consent_version: 'v1', processing_consent: true, permanent_consent: true, retention: 'permanent', created_at: question.created_at, active_seconds: 1, budget_cny: 5, spent_cny: 0, reserved_cny: 0 }, consent_version: 'v1', mode: 'mock', providers: config.providers, study: { title: '虚构研究', objective: '软件验证', participant_description: '', target_minutes: 60, topics: [], exclusions: '', glossary: [], budget_cny: 5, confirm_transcript: true, tone: '中性' }, turns: [{ ...question }, ...extraTurns], jobs: [] }
  vi.mocked(apiRequest).mockImplementation(async (path, options) => {
    if (path === '/api/participant/session') return structuredClone(current) as never
    if (path.startsWith('/api/participant/events')) return { events: [], cursor: 0 } as never
    const body = options?.body as Record<string, unknown>
    if (path === '/api/participant/consent') { current.session.mode = body.mode as 'voice'; return { session_id: 's1' } as never }
    if (path === '/api/participant/turns') {
      current.turns.push({ id: 'answer1', seq: 2, role: 'participant', status: body.input_mode === 'voice' ? 'recording' : 'confirming', input_mode: body.input_mode as 'voice' | 'text', text: String(body.text ?? ''), revisions: [], confirmed: false, created_at: question.created_at })
      return { turn_id: 'answer1' } as never
    }
    if (path.endsWith('/confirm')) {
      current.turns.at(-1)!.status = 'confirmed'
      current.turns.push({ ...question, id: 'q2', seq: 3, text: '然后发生了什么？' })
      return { job_id: 'decide2' } as never
    }
    if (path === '/api/participant/control') {
      if (body.action === 'rerecord') current.turns.find(t => t.id === body.turn_id)!.status = 'superseded'
      if (body.action === 'pause') current.session.status = 'paused'
      return {} as never
    }
    throw new Error(`unexpected request ${path}`)
  })
  render(<ParticipantPage config={config} />)
  await screen.findByText(question.text)
}

beforeEach(() => {
  vi.mocked(apiRequest).mockReset(); mic.mockReset(); stop.mockReset()
  vi.spyOn(indexedChunkStorage, 'list').mockResolvedValue([])
  vi.spyOn(indexedChunkStorage, 'usage').mockResolvedValue(0)
  vi.spyOn(indexedChunkStorage, 'remove').mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: mic } })
  vi.stubGlobal('MediaRecorder', { isTypeSupported: () => true })
  const track = { readyState: 'live', stop, addEventListener: vi.fn() }
  mic.mockResolvedValue({ getTracks: () => [track], getAudioTracks: () => [track] })
})
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

test('voice upgrade requires fresh consent, cancels without requests and keeps the text draft', async () => {
  await open()
  await userEvent.type(screen.getByPlaceholderText('写下你的回答…'), '未提交的文字草稿')
  await userEvent.click(screen.getByRole('button', { name: '语音回答' }))
  expect(screen.getByRole('button', { name: '同意并启用语音回答' })).toBeDisabled()
  expect(mic).not.toHaveBeenCalled(); expect(writes()).toHaveLength(0)
  await userEvent.click(screen.getAllByRole('checkbox')[0])
  await userEvent.click(screen.getByRole('button', { name: '继续使用文字回答' }))
  expect(screen.getByPlaceholderText('写下你的回答…')).toHaveValue('未提交的文字草稿')
  await userEvent.click(screen.getByRole('button', { name: '语音回答' }))
  for (const box of screen.getAllByRole('checkbox')) { expect(box).not.toBeChecked(); await userEvent.click(box) }
  await userEvent.click(screen.getByRole('button', { name: '同意并启用语音回答' }))
  expect(writes()).toEqual([['/api/participant/consent', { method: 'POST', body: { version: 'v1', mode: 'voice', processing: true, permanent: true } }]])
  expect(mic).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: '语音回答' })).toHaveAttribute('aria-pressed', 'true')
  await userEvent.click(screen.getByRole('button', { name: '文字回答' }))
  expect(screen.getByPlaceholderText('写下你的回答…')).toHaveValue('未提交的文字草稿')
})

test('an active voice session can answer in text after microphone failure without losing permission or starting ASR', async () => {
  await open('voice')
  expect(mic).not.toHaveBeenCalled()
  mic.mockRejectedValue(new Error('麦克风不可用'))
  await userEvent.click(screen.getByRole('button', { name: /开始回答/ }))
  await screen.findByText('麦克风不可用')
  await userEvent.click(screen.getByRole('button', { name: '文字回答' }))
  await userEvent.type(screen.getByPlaceholderText('写下你的回答…'), '这轮改用文字回答。')
  await userEvent.click(screen.getByRole('button', { name: '提交回答' }))
  expect(writes()).toEqual([['/api/participant/turns', { method: 'POST', body: { input_mode: 'text', text: '这轮改用文字回答。' } }]])
  expect(screen.getByRole('textbox')).toHaveValue('这轮改用文字回答。')
  expect(current.session.mode).toBe('voice')
})

test('mode switches stop playback and preserve a draft through a separate voice transcription', async () => {
  await open('voice')
  current.turns[0].audio_asset_id = 'audio1'; current.turns[0].audio_status = 'available'
  await act(async () => { window.dispatchEvent(new Event('online')) })
  const audio = document.querySelector('audio')!
  const pause = vi.spyOn(audio, 'pause').mockImplementation(() => {})
  fireEvent.play(audio)
  await userEvent.click(screen.getByRole('button', { name: '文字回答' }))
  expect(pause).toHaveBeenCalled()
  expect(apiRequest).toHaveBeenCalledWith('/api/participant/control', { method: 'POST', body: { action: 'playback_done', turn_id: 'q1', played_complete: false } })
  await userEvent.type(screen.getByPlaceholderText('写下你的回答…'), '保留这份草稿')
  await userEvent.click(screen.getByRole('button', { name: '语音回答' }))
  current.turns.push({ ...question, id: 'voice1', seq: 2, role: 'participant', input_mode: 'voice', status: 'confirming', text: '另一次语音转写', confirmed: false })
  await act(async () => { window.dispatchEvent(new Event('online')) })
  expect(await screen.findByDisplayValue('另一次语音转写')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '文字回答' })).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: '确认并继续' }))
  await userEvent.click(screen.getByRole('button', { name: '文字回答' }))
  expect(screen.getByPlaceholderText('写下你的回答…')).toHaveValue('保留这份草稿')
  expect(screen.getByText(/保留了上一问题的文字草稿/)).toBeInTheDocument()
})

test('unfinished recording cannot be silently switched away or discarded', async () => {
  const chunk = { turnId: 'partial1', seq: 0, blob: new Blob(['fictional audio']), mimeType: 'audio/webm' }
  vi.mocked(indexedChunkStorage.list).mockResolvedValue([chunk])
  await open('voice', [{ ...question, id: 'partial1', role: 'participant', input_mode: 'voice', status: 'uploading', text: '', confirmed: false }])
  expect(await screen.findByRole('button', { name: '恢复上传' })).toBeEnabled()
  expect(screen.getByRole('button', { name: '文字回答' })).toBeDisabled()
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  await userEvent.click(screen.getByRole('button', { name: '重新回答这一轮' }))
  expect(writes()).toHaveLength(0); expect(indexedChunkStorage.remove).not.toHaveBeenCalled()
  confirm.mockReturnValue(true)
  await userEvent.click(screen.getByRole('button', { name: '重新回答这一轮' }))
  expect(apiRequest).toHaveBeenCalledWith('/api/participant/control', { method: 'POST', body: { action: 'rerecord', turn_id: 'partial1' } })
  expect(indexedChunkStorage.remove).toHaveBeenCalledWith('partial1', 0)
  expect(screen.getByRole('button', { name: '文字回答' })).toBeEnabled()
})

test('failure after creating a voice turn releases the mic and allows explicit rerecord before fallback', async () => {
  await open('voice')
  vi.spyOn(RecordingCoordinator.prototype, 'start').mockRejectedValue(new Error('录音器启动失败'))
  await userEvent.click(screen.getByRole('button', { name: /开始回答/ }))
  expect(await screen.findByText('录音器启动失败')).toBeInTheDocument()
  expect(stop).toHaveBeenCalled()
  expect(screen.getByRole('button', { name: '文字回答' })).toBeDisabled()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  await userEvent.click(screen.getByRole('button', { name: '重新回答这一轮' }))
  await userEvent.click(screen.getByRole('button', { name: '文字回答' }))
  expect(screen.getByPlaceholderText('写下你的回答…')).toBeEnabled()
})

test('switching input mode is disabled while recording', async () => {
  await open('voice')
  vi.spyOn(RecordingCoordinator.prototype, 'start').mockResolvedValue('audio/webm')
  await userEvent.click(screen.getByRole('button', { name: /开始回答/ }))
  expect(await screen.findByText('正在录音')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '文字回答' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '语音回答' })).toBeDisabled()
  const stopRecorder = vi.spyOn(RecordingCoordinator.prototype, 'stop')
  await userEvent.click(screen.getByRole('button', { name: '暂停' }))
  expect(stopRecorder).toHaveBeenCalled()
  expect(stop).toHaveBeenCalled()
  expect(await screen.findByRole('button', { name: '恢复上传' })).toBeEnabled()
  expect(screen.getByRole('button', { name: '文字回答' })).toBeDisabled()
  expect(screen.queryByText('正在录音')).not.toBeInTheDocument()
})

test('failed voice authorization retains the draft and never opens the microphone', async () => {
  await open()
  await userEvent.type(screen.getByPlaceholderText('写下你的回答…'), '授权失败也要保留的草稿')
  const fallback = vi.mocked(apiRequest).getMockImplementation()!
  vi.mocked(apiRequest).mockImplementation((path, options) => {
    if (path === '/api/participant/consent') return Promise.reject(new Error('同意说明已经更新，请重试'))
    return fallback(path, options)
  })
  await userEvent.click(screen.getByRole('button', { name: '语音回答' }))
  for (const box of screen.getAllByRole('checkbox')) await userEvent.click(box)
  await userEvent.click(screen.getByRole('button', { name: '同意并启用语音回答' }))
  expect(await screen.findByText('同意说明已经更新，请重试')).toBeInTheDocument()
  expect(mic).not.toHaveBeenCalled()
  expect(current.session.mode).toBe('text')
  await userEvent.click(screen.getByRole('button', { name: '继续使用文字回答' }))
  expect(screen.getByPlaceholderText('写下你的回答…')).toHaveValue('授权失败也要保留的草稿')
})
