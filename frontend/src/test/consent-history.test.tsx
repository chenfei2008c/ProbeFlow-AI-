import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ConsentHistory } from '../components/ConsentHistory'

test('history shows each accepted mode and stored provider snapshot, including legacy gaps', async () => {
  render(<ConsentHistory records={[
    { id: 'a', version: 'notice-old', mode: 'text', processing: true, permanent: true, retention: 'permanent', created_at: '2026-09-15T08:00:00Z', snapshot: {} },
    { id: 'b', version: 'notice-new', mode: 'voice', processing: true, permanent: true, retention: 'permanent', created_at: '2026-09-16T08:00:00Z', snapshot: { notice_version: 'V1.1', mode: 'mock', retention: 'permanent', providers: { asr: { provider: '测试处理方', model: '旧模型', region: 'cn-beijing', endpoint_host: 'accepted.invalid' } } } },
  ]} />)
  await userEvent.click(screen.getByText('同意与保存记录 · 2 次'))
  expect(screen.getByText('文字访谈')).toBeVisible()
  expect(screen.getByText('语音访谈')).toBeVisible()
  expect(screen.getByText('notice-old')).toBeVisible()
  expect(screen.getByText('notice-new')).toBeVisible()
  expect(screen.getByText('该历史记录未保存处理方快照，不能按当前配置推断。')).toBeVisible()
  expect(screen.getByText('模拟模式，不向外部模型发送资料')).toBeVisible()
  expect(screen.getByText(/测试处理方.*旧模型.*accepted.invalid/)).toBeVisible()
})
