import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ConsentGate } from '../components/ConsentGate'

test('voice interview stays locked until both mandatory unchecked consents are selected', async () => {
  const user = userEvent.setup()
  render(<ConsentGate mode="voice" providers={{ asr: { provider: '百炼', model: '语音识别' }, interview: { provider: '百炼', model: '访谈模型' } }} onSubmit={vi.fn()} />)
  const start = screen.getByRole('button', { name: '同意并进行设备检查' })
  const boxes = screen.getAllByRole('checkbox')
  expect(boxes).toHaveLength(2)
  expect(boxes[0]).not.toBeChecked()
  expect(boxes[1]).not.toBeChecked()
  expect(start).toBeDisabled()
  await user.click(boxes[0])
  expect(start).toBeDisabled()
  await user.click(boxes[1])
  expect(start).toBeEnabled()
})
