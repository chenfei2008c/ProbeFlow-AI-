import { render, screen } from '@testing-library/react'
import { ErrorPanel } from '../components/Common'
import { ApiError, apiRequest } from '../lib/api'

test('a legacy upload error shows the recovery action without an empty request label', async () => {
  const fetcher = async () => new Response(JSON.stringify({ detail: 'Method Not Allowed' }), { status: 405 })
  const error = await apiRequest('/api/admin/studies/import', { method: 'POST', fetcher }).catch(value => value)
  if (!(error instanceof ApiError)) throw new Error('Expected an API error')
  render(<ErrorPanel error={error} />)
  expect(screen.getByRole('alert')).toHaveTextContent('重启服务')
  expect(screen.queryByText(/请求编号/)).not.toBeInTheDocument()
})

test('a structured error keeps its explanation and usable request number', () => {
  render(<ErrorPanel error={new ApiError('DOCUMENT_INVALID', '文件无法读取', false, 'request-45', 422)} />)
  expect(screen.getByRole('alert')).toHaveTextContent('文件无法读取')
  expect(screen.getByRole('alert')).toHaveTextContent('请求编号 request-45')
})
