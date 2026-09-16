import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { JobNotice } from '../components/Common'

test('unknown-charge retry requires a separate unchecked acknowledgement', async () => {
  const retry = vi.fn()
  render(<JobNotice job={{ id: 'j1', kind: '生成下一问', status: 'external_status_unknown', created_at: '2026-09-16T00:00:00Z' }} onRetry={retry} />)
  const button = screen.getByRole('button', { name: '重试任务' })
  expect(button).toBeDisabled()
  const acknowledgement = screen.getByRole('checkbox', { name: /可能已经产生费用/ })
  expect(acknowledgement).not.toBeChecked()
  await userEvent.click(acknowledgement)
  await userEvent.click(button)
  expect(retry).toHaveBeenCalledWith(true)
})
