import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ParticipantPage } from '../pages/ParticipantPage'
import { apiRequest } from '../lib/api'
import { indexedChunkStorage, RecordingCoordinator } from '../lib/recorder'
import type { Config, Detail } from '../types'

vi.mock('../lib/api', async () => ({ ...await vi.importActual('../lib/api'), apiRequest: vi.fn() }))
const provider = { provider: 'mock', model: 'mock' }
const config: Config = { mode: 'mock', consent_version: 'v1', admin_initialized: true, providers: { asr: provider, interview: provider, tts: provider, report: provider } }
const cacheChunk = { turnId: 'answer1', seq: 0, blob: new Blob(['fictional audio']), mimeType: 'audio/webm' }
let requestWithdrawal: () => Promise<unknown>

async function open(status: 'in_progress' | 'completed', mode: 'text' | 'voice' = 'text') {
  const detail: Detail = {
    session: { id: 's1', study_id: 'study1', participant_code: 'P-FICTIONAL', status, mode, consent_version: 'v1', processing_consent: true, permanent_consent: true, retention: 'permanent', created_at: '2026-09-16T00:00:00Z', active_seconds: 1, budget_cny: 5, spent_cny: 0, reserved_cny: 0 },
    consent_version: 'v1', mode: 'mock', providers: config.providers,
    study: { title: '虚构研究', objective: '软件验证', participant_description: '', target_minutes: 60, topics: [], exclusions: '', glossary: [], budget_cny: 5, confirm_transcript: true, tone: '中性' },
    turns: [
      { id: 'q1', seq: 1, role: 'assistant', status: 'ready_to_record', input_mode: 'text', text: '请描述一次虚构经历。', revisions: [], confirmed: true, created_at: '2026-09-16T00:00:00Z' },
      { id: 'answer1', seq: 2, role: 'participant', status: 'confirmed', input_mode: 'voice', text: '虚构已确认回答。', revisions: [], confirmed: true, created_at: '2026-09-16T00:00:01Z' },
    ], jobs: [],
  }
  vi.mocked(apiRequest).mockImplementation(async (path, options) => {
    if (path === '/api/participant/session') return structuredClone(detail) as never
    if (path.startsWith('/api/participant/events')) return { events: [], cursor: 0 } as never
    if (path === '/api/participant/turns') return { turn_id: 'late-turn' } as never
    if (path === '/api/participant/control' && (options?.body as { action: string }).action === 'withdraw') return await requestWithdrawal() as never
    throw new Error(`unexpected request ${path}`)
  })
  render(<ParticipantPage config={config} />)
  return screen.findByRole('button', { name: status === 'completed' ? '撤回并删除本场内容' : '撤回并删除' })
}

const withdrawalRequests = () => vi.mocked(apiRequest).mock.calls.filter(([, options]) => options?.method === 'POST' && (options.body as { action?: string }).action === 'withdraw')
beforeEach(() => {
  vi.mocked(apiRequest).mockReset()
  requestWithdrawal = async () => ({ status: 'withdrawn' })
  vi.spyOn(indexedChunkStorage, 'list').mockImplementation(async id => id === 'answer1' ? [cacheChunk] : [])
  vi.spyOn(indexedChunkStorage, 'remove').mockResolvedValue(undefined)
})
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe.each(['in_progress', 'completed'] as const)('%s withdrawal', status => {
  test.each([1, 2])('cancelling confirmation %i sends no deletion request and preserves browser audio', async cancelAt => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    if (cancelAt === 2) confirm.mockReturnValueOnce(true)
    await userEvent.click(await open(status))
    expect(withdrawalRequests()).toHaveLength(0)
    expect(indexedChunkStorage.remove).not.toHaveBeenCalled()
    expect(screen.queryByRole('heading', { name: '本场访谈已撤回' })).not.toBeInTheDocument()
  })

  test('blocks repeat withdrawal while awaiting the server and only then clears local audio', async () => {
    let complete!: (value: unknown) => void
    requestWithdrawal = () => new Promise(resolve => { complete = resolve })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const button = await open(status)
    await userEvent.click(button)
    expect(button).toBeDisabled()
    expect(indexedChunkStorage.remove).not.toHaveBeenCalled()
    await userEvent.click(button)
    expect(withdrawalRequests()).toEqual([['/api/participant/control', { method: 'POST', body: { action: 'withdraw' } }]])
    await act(async () => complete({ status: 'withdrawn' }))
    expect(await screen.findByRole('heading', { name: '本场访谈已撤回' })).toBeInTheDocument()
    expect(indexedChunkStorage.remove).toHaveBeenCalledExactlyOnceWith('answer1', 0)
  })

  test('a failed withdrawal preserves data, shows failure and allows an explicitly confirmed retry', async () => {
    requestWithdrawal = async () => { throw new Error('撤回请求未被接受') }
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const button = await open(status)
    await userEvent.click(button)
    expect(await screen.findByText('撤回请求未被接受')).toBeInTheDocument()
    expect(button).toBeEnabled()
    expect(indexedChunkStorage.remove).not.toHaveBeenCalled()
    requestWithdrawal = async () => ({ status: 'withdrawn' })
    await userEvent.click(button)
    expect(await screen.findByRole('heading', { name: '本场访谈已撤回' })).toBeInTheDocument()
    expect(confirm).toHaveBeenCalledTimes(4)
    expect(indexedChunkStorage.remove).toHaveBeenCalledExactlyOnceWith('answer1', 0)
  })

  test('browser cleanup failure does not misreport a successful server withdrawal or repeat it', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(indexedChunkStorage.remove).mockRejectedValueOnce(new Error('浏览器暂存不可写'))
    await userEvent.click(await open(status))
    expect(await screen.findByRole('heading', { name: '本场访谈已撤回' })).toBeInTheDocument()
    expect(screen.getByText(/本机录音暂存未能全部清理/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '重新清理本机暂存' }))
    expect(await screen.findByText('本场在此浏览器中的录音暂存已清理。')).toBeInTheDocument()
    expect(withdrawalRequests()).toHaveLength(1)
    expect(screen.queryByText(/本机录音暂存未能全部清理/)).not.toBeInTheDocument()
  })
})

test('active withdrawal settles final local audio and includes a new turn not yet returned by polling', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  vi.stubGlobal('MediaRecorder', { isTypeSupported: () => true })
  const stopMic = vi.fn()
  const track = { readyState: 'live', stop: stopMic }
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: async () => ({ getTracks: () => [track], getAudioTracks: () => [track] }) } })
  vi.spyOn(indexedChunkStorage, 'usage').mockResolvedValue(0)
  vi.spyOn(RecordingCoordinator.prototype, 'start').mockResolvedValue('audio/webm')
  vi.spyOn(RecordingCoordinator.prototype, 'currentTurnId', 'get').mockReturnValue('late-turn')
  let settle!: () => void
  vi.spyOn(RecordingCoordinator.prototype, 'stopAndPreserve').mockImplementation(() => new Promise(resolve => { settle = resolve }))
  vi.mocked(indexedChunkStorage.list).mockImplementation(async id => id === 'late-turn' ? [{ ...cacheChunk, turnId: id }] : [])
  const button = await open('in_progress', 'voice')
  await userEvent.click(screen.getByRole('button', { name: /开始回答/ }))
  await screen.findByText('正在录音')
  await userEvent.click(button)
  expect(button).toBeDisabled()
  expect(stopMic).toHaveBeenCalled()
  expect(withdrawalRequests()).toHaveLength(0)
  await act(async () => settle())
  expect(await screen.findByRole('heading', { name: '本场访谈已撤回' })).toBeInTheDocument()
  expect(indexedChunkStorage.remove).toHaveBeenCalledExactlyOnceWith('late-turn', 0)
})
