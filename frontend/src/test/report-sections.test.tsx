import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ReportView } from '../pages/SessionDetailPage'
import { TopicCoverage } from '../components/TopicCoverage'
import type { Coverage, Report } from '../types'

const coverage: Coverage = { topics: [
  { id: 'T1', title: '经历', status: 'skipped', confirmed_turn_ids: ['a1'], unconfirmed_count: 0, reasons: ['受访者主动跳过'] },
  { id: 'T2', title: '建议', status: 'not_started', confirmed_turn_ids: [], unconfirmed_count: 0, reasons: [] },
], unresolved: [] }

test('report puts observed limitations before background and preserves citations inside sections', async () => {
  const citation = { turn_id: 'a1', revision_id: 'r1', start: 0, end: 6, quote: '我负责核对。' }
  const report: Report = { id: 'rep', version: 1, status: 'ready', source_updated: false, archive_mode: 'mock', created_at: '2026-09-16T00:00:00Z', markdown: '', citations: [citation], body: {
    schema_version: 2, background: { title: '虚构研究', objective: '了解流程' }, coverage,
    limitations: ['访谈提前结束；部分录音档案文件缺失。'], unanswered: ['建议：未谈及'],
    findings: [{ section: 'role', type: 'statement', text: '受访者称其负责核对。', citations: [citation] }],
  } }
  const onCitation = vi.fn()
  render(<ReportView report={report} onCitation={onCitation} />)
  const limits = screen.getByRole('heading', { name: '研究局限' })
  expect(limits.compareDocumentPosition(screen.getByRole('heading', { name: '研究背景' })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  for (const title of ['受访者自述角色', '具体事件', '主要陈述', '受访者对原因的解释', '待验证假设', '受访者建议', '未回答／拒绝回答事项']) expect(screen.getByRole('heading', { name: title })).toBeInTheDocument()
  expect(screen.getAllByText('尚缺乏依据。')).toHaveLength(5)
  await userEvent.click(screen.getByRole('button', { name: /证据 a1/ }))
  expect(onCitation).toHaveBeenCalledWith(citation)
})

test('coverage keeps skipped and untouched topics distinct and links available answers', async () => {
  const onTurn = vi.fn()
  render(<TopicCoverage coverage={coverage} onTurn={onTurn} />)
  expect(screen.getByText('已跳过／拒答')).toBeInTheDocument()
  expect(screen.getByText('未谈及')).toBeInTheDocument()
  expect(screen.getByText('受访者主动跳过')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '定位回答 1' }))
  expect(onTurn).toHaveBeenCalledWith('a1')
})
