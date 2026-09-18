import { ApiError, apiRequest, retryApiRequest } from '../lib/api'

test('write requests carry the web client and caller idempotency key', async () => {
  const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    expect(init?.credentials).toBe('include')
    expect(new Headers(init?.headers).get('X-ProbeFlow-Client')).toBe('web')
    expect(new Headers(init?.headers).get('Idempotency-Key')).toBe('same-operation')
    return new Response(JSON.stringify({ id: 's1' }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  })
  await apiRequest('/api/admin/studies', { method: 'POST', body: { title: '研究' }, idempotencyKey: 'same-operation', fetcher })
  expect(fetcher).toHaveBeenCalledTimes(1)
})

test('a retryable write reuses one idempotency key across network attempts', async () => {
  const keys: string[] = []
  const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    keys.push(new Headers(init?.headers).get('Idempotency-Key') ?? '')
    if (keys.length === 1) throw new TypeError('network offline')
    return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  })
  await retryApiRequest('/api/participant/turns/t1/chunks/0', { method: 'PUT', body: new Blob(['a']), fetcher, idempotencyKey: 'chunk-operation' }, { attempts: 2, delayMs: 0 })
  expect(keys).toEqual(['chunk-operation', 'chunk-operation'])
})

test('structured API failures keep retry and request context', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ code: 'PROVIDER_TIMEOUT', message: '外部状态未知', retryable: false, request_id: 'req-7' }), { status: 504, headers: { 'Content-Type': 'application/json' } }))
  const error = await apiRequest('/api/admin/usage', { fetcher }).catch((value) => value)
  expect(error).toEqual(expect.objectContaining({ code: 'PROVIDER_TIMEOUT', message: '外部状态未知', retryable: false, requestId: 'req-7' }))
})

test('an old server without the upload route reports an actionable HTTP 405 error', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ detail: 'Method Not Allowed' }), { status: 405 }))
  const error = await apiRequest('/api/admin/studies/import', { method: 'POST', fetcher }).catch(value => value)
  if (!(error instanceof ApiError)) throw new Error('Expected an API error')
  expect(error).toEqual(expect.objectContaining({ code: 'HTTP_ERROR', status: 405, retryable: false }))
  expect(error.message).toContain('405')
  expect(error.message).toContain('重启服务')
})

test.each([null, [], {}, { message: '', retryable: 'false', request_id: 17 }, { detail: [{ msg: 'invalid' }] }].map(payload => [payload]))('malformed errors retain safe defaults: %j', async payload => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify(payload), { status: 502, headers: { 'X-Request-ID': 'header-42' } }))
  const error = await apiRequest('/api/admin/studies/import', { fetcher }).catch(value => value)
  expect(error).toEqual(expect.objectContaining({ code: 'HTTP_ERROR', message: '请求失败（502）', retryable: true, requestId: 'header-42', status: 502 }))
})

test('a plain detail error preserves its explanation and header request ID', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ detail: '请求过于频繁' }), { status: 429, headers: { 'X-Request-ID': 'header-43' } }))
  const error = await apiRequest('/api/admin/studies/import', { fetcher }).catch(value => value)
  expect(error).toEqual(expect.objectContaining({ message: '请求过于频繁', requestId: 'header-43' }))
})

test('an HTML gateway error retains the status without rendering HTML', async () => {
  const fetcher = vi.fn(async () => new Response('<h1>Bad gateway</h1>', { status: 502 }))
  const error = await apiRequest('/api/admin/studies/import', { fetcher }).catch(value => value)
  if (!(error instanceof ApiError)) throw new Error('Expected an API error')
  expect(error.message).toBe('请求失败（502）')
})
