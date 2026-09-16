import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { InviteManager, InviteItem } from '../components/InviteManager'
import { apiRequest } from '../lib/api'

vi.mock('../lib/api', async () => ({ ...await vi.importActual('../lib/api'), apiRequest: vi.fn() }))

test('revoking a pending invite refreshes its status and preserves completed session access', async () => {
  const pending: InviteItem = { id: 'i1', version_number: 1, status: 'available', created_at: '2026-09-16T00:00:00Z', expires_at: '2026-09-23T00:00:00Z' }
  const redeemed: InviteItem = { ...pending, id: 'i2', status: 'redeemed', session_id: 's2' }
  const request = vi.mocked(apiRequest)
  request.mockResolvedValueOnce([pending, redeemed]).mockResolvedValueOnce({ ...pending, status: 'revoked' }).mockResolvedValueOnce([{ ...pending, status: 'revoked' }, redeemed])
  const onRevoked = vi.fn()
  render(<MemoryRouter><InviteManager studyId="study1" onRevoked={onRevoked} /></MemoryRouter>)
  const button = await screen.findByRole('button', { name: '撤销邀请' })
  expect(screen.getByRole('link', { name: '查看场次' })).toHaveAttribute('href', '/sessions/s2')
  await userEvent.click(button)
  await waitFor(() => expect(screen.getByText(/研究版本 V1 · 已撤销/)).toBeInTheDocument())
  expect(screen.queryByRole('button', { name: '撤销邀请' })).not.toBeInTheDocument()
  expect(onRevoked).toHaveBeenCalledWith('i1')
  expect(request).toHaveBeenCalledWith('/api/admin/studies/study1/invites/i1/revoke', { method: 'POST', body: {} })
})
