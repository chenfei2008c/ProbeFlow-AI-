import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RenewConsent } from '../components/RenewConsent'
import { apiRequest } from '../lib/api'
import type { Detail } from '../types'

vi.mock('../lib/api', async () => ({ ...await vi.importActual('../lib/api'), apiRequest: vi.fn() }))
const detail = { session: { status: 'completed', consent_version: 'old', mode: 'voice' }, consent_version: 'new', mode: 'mock', providers: {} } as Detail
beforeEach(() => vi.mocked(apiRequest).mockReset())

test('completed interview renewal requires two fresh choices and never restarts recording', async () => {
  vi.mocked(apiRequest).mockResolvedValue({ session_id: 's1' })
  const reload = vi.fn().mockResolvedValue(undefined)
  render(<RenewConsent detail={detail} reload={reload} />)
  await userEvent.click(screen.getByRole('button', { name: '查看更新后的处理说明' }))
  const submit = screen.getByRole('button', { name: '同意更新后的处理与保存说明' })
  expect(submit).toBeDisabled()
  for (const checkbox of screen.getAllByRole('checkbox')) {
    expect(checkbox).not.toBeChecked()
    await userEvent.click(checkbox)
  }
  await userEvent.click(submit)
  expect(apiRequest).toHaveBeenCalledExactlyOnceWith('/api/participant/consent', { method: 'POST', body: { version: 'new', mode: 'voice', processing: true, permanent: true } })
  expect(reload).toHaveBeenCalledOnce()
  expect(screen.queryByRole('button', { name: /设备检查/ })).not.toBeInTheDocument()
})

test('declining updated processing does not post or delete anything', async () => {
  render(<RenewConsent detail={detail} reload={vi.fn()} />)
  await userEvent.click(screen.getByRole('button', { name: '查看更新后的处理说明' }))
  await userEvent.click(screen.getByRole('button', { name: '暂不接受更新' }))
  expect(apiRequest).not.toHaveBeenCalled()
  expect(screen.getByText(/既有档案继续保存/)).toBeInTheDocument()
})
