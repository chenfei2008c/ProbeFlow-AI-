import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { StudiesPage } from '../pages/StudiesPage'
import { StudyEditorPage } from '../pages/StudyEditorPage'
import { apiRequest } from '../lib/api'
import type { Study, StudyConfig } from '../types'

vi.mock('../lib/api', async () => ({ ...await vi.importActual('../lib/api'), apiRequest: vi.fn() }))
const config = { mode: 'mock', providers: { interview: { provider: 'bailian', model: 'qwen-plus' } } }
const imported: StudyConfig = { title: '材料流转体验', objective: '理解补件经历', participant_description: '客户经理', target_minutes: 30, topics: [{ id: 'T1', title: '一次补件', research_question: '当时怎样处理？', priority: 1, evidence_type: '经历', minutes: 20 }], exclusions: '不问客户身份', glossary: [], budget_cny: 5, confirm_transcript: true, tone: '中性' }
const source = { filename: '方案.pptx', text: '调研目的：理解补件经历', sha256: 'abc', characters: 14, warnings: [] }
let study: Study

beforeEach(() => {
  vi.mocked(apiRequest).mockReset()
  study = { id: 'study1', title: '方案', archived: false, version: { ...imported, title: '方案', objective: '', topics: [] }, session_count: 0, completed_count: 0, total_cost_cny: 0, updated_at: '', import_job: { id: 'job1', kind: 'study_import', status: 'queued', created_at: '' }, import_source: source }
  vi.mocked(apiRequest).mockImplementation(async (path, options) => {
    if (path === '/api/config') return config as never
    if (path === '/api/admin/studies') return [] as never
    if (path.endsWith('/sessions') || path.endsWith('/invites')) return [] as never
    if (path === '/api/admin/studies/study1') return structuredClone(study) as never
    if (path === '/api/admin/studies/import') return { study_id: 'study1', job_id: 'job1' } as never
    if (path === '/api/admin/jobs/job1') return { ...study.import_job, status: 'succeeded', result: { study: imported, warnings: ['核对时长'], mode: 'mock' } } as never
    if (path.endsWith('/versions')) { study = { ...study, current_version_id: 'v1', version_number: 1, version: options?.body as StudyConfig }; return study as never }
    throw new Error(`unexpected request ${path}`)
  })
})

test('uploading PPT fills a research draft without manual fields and waits for publication', async () => {
  render(<MemoryRouter initialEntries={['/studies']}><Routes><Route path='/studies' element={<StudiesPage />} /><Route path='/studies/:id' element={<StudyEditorPage />} /></Routes></MemoryRouter>)
  const input = await screen.findByLabelText('选择调研方案文件')
  await userEvent.upload(input, new File(['fictional slides'], '方案.pptx', { type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation' }))
  expect(screen.getByRole('button', { name: '上传并生成研究草稿' })).toBeDisabled()
  await userEvent.click(screen.getByLabelText(/我同意按上述说明解析此方案/))
  await userEvent.click(screen.getByRole('button', { name: '上传并生成研究草稿' }))
  await waitFor(() => expect(screen.getByLabelText(/研究目标/)).toHaveValue('理解补件经历'), { timeout: 4000 })
  expect(screen.getByLabelText(/研究标题/)).toHaveValue('材料流转体验')
  expect(screen.getByRole('button', { name: '创建邀请链接' })).toBeDisabled()
  expect(screen.getByText(/模拟导入/)).toBeInTheDocument()
  expect(vi.mocked(apiRequest).mock.calls.filter(([path]) => path.endsWith('/versions'))).toHaveLength(0)
  await userEvent.click(screen.getByRole('button', { name: '发布新版本' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '创建邀请链接' })).toBeEnabled())
  expect(study.version.objective).toBe('理解补件经历')
})

test('restored import result is reviewable without overwriting an existing published design', async () => {
  study = { ...study, current_version_id: 'old', version_number: 1, version: { ...imported, objective: '原发布目标' }, import_job: { id: 'job1', kind: 'study_import', status: 'succeeded', created_at: '', result: { study: imported, warnings: [], mode: 'mock' } } }
  render(<MemoryRouter initialEntries={['/studies/study1']}><Routes><Route path='/studies/:id' element={<StudyEditorPage />} /></Routes></MemoryRouter>)
  expect(await screen.findByLabelText(/研究目标/)).toHaveValue('原发布目标')
  await userEvent.click(screen.getByRole('button', { name: '应用到当前草稿' }))
  expect(screen.getByLabelText(/研究目标/)).toHaveValue('理解补件经历')
  expect(study.version.objective).toBe('原发布目标')
})

test('upload failure keeps the selected file and reports the actual cause', async () => {
  const original = vi.mocked(apiRequest).getMockImplementation()!
  vi.mocked(apiRequest).mockImplementation(async (path, options) => {
    if (path.endsWith('/import')) throw new Error('PDF 没有可读取文字')
    return original(path, options)
  })
  render(<MemoryRouter><StudiesPage /></MemoryRouter>)
  const input = await screen.findByLabelText('选择调研方案文件')
  await userEvent.upload(input, new File(['scan'], '方案.pdf', { type: 'application/pdf' }))
  await userEvent.click(screen.getByLabelText(/我同意按上述说明解析此方案/))
  await userEvent.click(screen.getByRole('button', { name: '上传并生成研究草稿' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('PDF 没有可读取文字')
  expect((input as HTMLInputElement).files?.[0].name).toBe('方案.pdf')
})

test('uploading into a published study fills the editable draft automatically after polling', async () => {
  study = { ...study, current_version_id: 'old', version_number: 1, version: { ...imported, objective: '原发布目标' }, import_job: null }
  const original = vi.mocked(apiRequest).getMockImplementation()!
  let uploaded = false
  vi.mocked(apiRequest).mockImplementation(async (path, options) => {
    if (path === '/api/admin/studies/study1/import') {
      uploaded = true
      study.import_job = { id: 'job1', kind: 'study_import', status: 'queued', created_at: '' }
      return { study_id: 'study1', job_id: 'job1', source } as never
    }
    if (uploaded && path === '/api/admin/studies/study1') await new Promise(resolve => setTimeout(resolve, 50))
    return original(path, options)
  })
  render(<MemoryRouter initialEntries={['/studies/study1']}><Routes><Route path='/studies/:id' element={<StudyEditorPage />} /></Routes></MemoryRouter>)
  const input = await screen.findByLabelText('选择调研方案文件')
  await userEvent.upload(input, new File(['fictional slides'], '方案.pptx', { type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation' }))
  await userEvent.click(screen.getByLabelText(/我同意按上述说明解析此方案/))
  await userEvent.click(screen.getByRole('button', { name: '上传并生成研究草稿' }))
  await waitFor(() => expect(screen.getByLabelText(/研究目标/)).toHaveValue('理解补件经历'), { timeout: 4000 })
  expect(study.version.objective).toBe('原发布目标')
})

test('outline generation and document import cannot overwrite each other concurrently', async () => {
  study = { ...study, current_version_id: 'v1', version_number: 1, version: imported, import_job: { id: 'job1', kind: 'study_import', status: 'succeeded', created_at: '', result: { study: imported, mode: 'mock', warnings: [] } } }
  const original = vi.mocked(apiRequest).getMockImplementation()!
  vi.mocked(apiRequest).mockImplementation(async (path, options) => {
    if (path.endsWith('/outline')) return { job_id: 'outline1' } as never
    if (path === '/api/admin/jobs/outline1') return { id: 'outline1', kind: 'outline', status: 'running' } as never
    return original(path, options)
  })
  render(<MemoryRouter initialEntries={['/studies/study1']}><Routes><Route path='/studies/:id' element={<StudyEditorPage />} /></Routes></MemoryRouter>)
  const input = await screen.findByLabelText('选择调研方案文件')
  await userEvent.upload(input, new File(['slides'], '方案.pptx', { type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation' }))
  await userEvent.click(screen.getByLabelText(/我同意按上述说明解析此方案/))
  await userEvent.click(screen.getByRole('button', { name: 'AI 生成提纲' }))
  expect(screen.getByRole('button', { name: '上传并生成研究草稿' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '应用到当前草稿' })).toBeDisabled()
})

test('replacing an import with unknown fees requires explicit charge consent', async () => {
  study = { ...study, current_version_id: 'v1', version: imported, import_job: { id: 'job1', kind: 'study_import', status: 'external_status_unknown', created_at: '' } }
  render(<MemoryRouter initialEntries={['/studies/study1']}><Routes><Route path='/studies/:id' element={<StudyEditorPage />} /></Routes></MemoryRouter>)
  const input = await screen.findByLabelText('选择调研方案文件')
  await userEvent.upload(input, new File(['slides'], '方案.pptx', { type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation' }))
  await userEvent.click(screen.getByLabelText(/我同意按上述说明解析此方案/))
  expect(screen.getByRole('button', { name: '上传并生成研究草稿' })).toBeDisabled()
  await userEvent.click(screen.getByLabelText(/重新上传仍可能增加费用/))
  expect(screen.getByRole('button', { name: '上传并生成研究草稿' })).toBeEnabled()
})
