import { render, screen } from '@testing-library/react'
import { ModeBadge } from '../components/Common'
import { ReportView } from '../pages/SessionDetailPage'
import type { ArchiveMode, Report } from '../types'

const report: Report = { id: 'r1', version: 1, status: 'ready', source_updated: false, body: { summary: '归档报告' }, markdown: '', citations: [], created_at: '2026-09-16T00:00:00Z' }

test.each<[ArchiveMode | undefined, string]>([
  ['mock', '模拟结果'], ['mixed', '混合来源档案'], ['unknown', '来源未确认'], [undefined, '来源未确认'],
])('report retains %s origin even under a live runtime badge', (archive_mode, label) => {
  render(<><ModeBadge mode="live" /><ReportView report={{ ...report, archive_mode }} onCitation={vi.fn()} /></>)
  expect(screen.getByText('真实服务模式')).toBeInTheDocument()
  expect(screen.getByRole('note')).toHaveTextContent(label)
})

test('live report is not relabeled mock after switching the runtime to mock', () => {
  render(<><ModeBadge mode="mock" /><ReportView report={{ ...report, archive_mode: 'live' }} onCitation={vi.fn()} /></>)
  expect(screen.queryByRole('note')).not.toBeInTheDocument()
})
