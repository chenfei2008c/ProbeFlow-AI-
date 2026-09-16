import { apiRequest, retryApiRequest } from '../lib/api'

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
