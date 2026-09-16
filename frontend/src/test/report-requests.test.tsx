import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { SessionDetailPage } from '../pages/SessionDetailPage'
import { useApi } from '../hooks/useApi'
import { apiRequest } from '../lib/api'
import type { Detail, Job } from '../types'

vi.mock('../hooks/useApi', () => ({ useApi: vi.fn() }))
vi.mock('../lib/api', async () => ({ ...await vi.importActual('../lib/api'), apiRequest: vi.fn() }))
const stored: Job = { id: 'old-report', kind: 'report', status: 'external_status_unknown', created_at: '2026-09-16T00:00:00Z' }
const reload = vi.fn().mockResolvedValue(undefined)

function open(job = stored) {
  const provider = { provider: 'mock', model: 'mock' }
  const detail: Detail = { session: { id: 's1', study_id: 'study1', participant_code: 'P-FICTIONAL', status: 'completed', mode: 'text', consent_version: 'v1', processing_consent: true, permanent_consent: true, retention: 'permanent', created_at: stored.created_at, active_seconds: 1, budget_cny: 5, spent_cny: 0, reserved_cny: 0 }, consent_version: 'v1', mode: 'mock', providers: { asr: provider, interview: provider, tts: provider, report: provider }, study: { title: '虚构研究', objective: '软件验证', participant_description: '', target_minutes: 60, topics: [], exclusions: '', glossary: [], budget_cny: 5, confirm_transcript: true, tone: '中性' }, turns: [], reports: [], jobs: [job] }
  vi.mocked(useApi).mockReturnValue({ data: detail, error: undefined, loading: false, reload, setData: vi.fn() })
  return render(<MemoryRouter initialEntries={['/sessions/s1']}><Routes><Route path="/sessions/:id" element={<SessionDetailPage />} /></Routes></MemoryRouter>)
}

beforeEach(() => { vi.mocked(apiRequest).mockReset(); reload.mockClear() })
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

test('refresh still requires explicit confirmation before replacing an unknown report', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  open()
  await userEvent.click(screen.getByRole('button', { name: '生成新报告' }))
  expect(confirm).toHaveBeenCalledOnce()
  expect(apiRequest).not.toHaveBeenCalled()
  confirm.mockReturnValue(true)
  vi.mocked(apiRequest).mockResolvedValue({ job_id: 'new-report' })
  await userEvent.click(screen.getByRole('button', { name: '生成新报告' }))
  expect(apiRequest).toHaveBeenCalledWith('/api/admin/sessions/s1/reports', { method: 'POST', body: { accept_possible_charge: true } })
  expect(screen.getByRole('button', { name: '生成新报告' })).toBeDisabled()
})

test('unknown report retry requires an unchecked charge acknowledgment and reuses its job', async () => {
  open()
  const retry = screen.getByRole('button', { name: '重试任务' })
  const checkbox = screen.getByRole('checkbox', { name: /重复计费/ })
  expect(checkbox).not.toBeChecked()
  expect(retry).toBeDisabled()
  vi.mocked(apiRequest).mockResolvedValue({ job_id: 'old-report' })
  await userEvent.click(checkbox)
  await userEvent.click(retry)
  expect(apiRequest).toHaveBeenCalledExactlyOnceWith('/api/admin/sessions/s1/reports', { method: 'POST', body: { retry_job_id: 'old-report', accept_possible_charge: true } })
})

test('opening a page with a persisted running report resumes polling without starting a new job', async () => {
  vi.useFakeTimers()
  vi.mocked(apiRequest).mockResolvedValue({ ...stored, status: 'succeeded' })
  open({ ...stored, status: 'running' })
  expect(screen.getByRole('button', { name: '生成新报告' })).toBeDisabled()
  await act(async () => { await vi.advanceTimersByTimeAsync(1600) })
  expect(apiRequest).toHaveBeenCalledExactlyOnceWith('/api/admin/jobs/old-report')
  expect(reload).toHaveBeenCalledOnce()
})
