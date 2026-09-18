export interface ApiFailure {
  code: string
  message: string
  retryable: boolean
  request_id: string
}

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public retryable: boolean,
    public requestId: string,
    public status: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>

export interface ApiOptions {
  method?: string
  body?: unknown
  headers?: HeadersInit
  idempotencyKey?: string
  fetcher?: Fetcher
  raw?: boolean
}

export async function retryApiRequest<T>(path: string, options: ApiOptions, retry: { attempts?: number; delayMs?: number } = {}) {
  const attempts = retry.attempts ?? 3
  const delayMs = retry.delayMs ?? 450
  const idempotencyKey = options.idempotencyKey ?? createIdempotencyKey()
  let lastError: unknown
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try { return await apiRequest<T>(path, { ...options, idempotencyKey }) }
    catch (error) {
      lastError = error
      const retryable = error instanceof TypeError || (error instanceof ApiError && error.retryable)
      if (!retryable || attempt === attempts - 1) throw error
      if (delayMs) await new Promise(resolve => setTimeout(resolve, delayMs * (attempt + 1)))
    }
  }
  throw lastError
}

export function createIdempotencyKey() {
  return crypto.randomUUID()
}

export async function apiRequest<T>(path: string, options: ApiOptions = {}): Promise<T> {
  const method = (options.method ?? 'GET').toUpperCase()
  const headers = new Headers(options.headers)
  headers.set('X-ProbeFlow-Client', 'web')
  if (!['GET', 'HEAD'].includes(method)) {
    headers.set('Idempotency-Key', options.idempotencyKey ?? createIdempotencyKey())
  }
  const binaryBody = options.body instanceof Blob || options.body instanceof ArrayBuffer
  if (options.body !== undefined && !binaryBody) headers.set('Content-Type', 'application/json')
  const response = await (options.fetcher ?? fetch)(path, {
    method,
    headers,
    credentials: 'include',
    body: options.body === undefined ? undefined : binaryBody ? options.body as BodyInit : JSON.stringify(options.body),
  })
  if (!response.ok) {
    const failure: ApiFailure = {
      code: 'HTTP_ERROR',
      message: response.status === 405 ? '当前服务不支持此操作（405），请重启服务后刷新页面再试。' : `请求失败（${response.status}）`,
      retryable: response.status >= 500,
      request_id: response.headers.get('X-Request-ID') ?? 'unknown',
    }
    try {
      const payload: unknown = await response.json()
      if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
        const data = payload as Record<string, unknown>
        const nonempty = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
        if (nonempty(data.code)) failure.code = data.code
        if (nonempty(data.message)) failure.message = data.message
        else if (response.status !== 405 && nonempty(data.detail)) failure.message = data.detail
        if (typeof data.retryable === 'boolean') failure.retryable = data.retryable
        if (nonempty(data.request_id)) failure.request_id = data.request_id
      }
    } catch { /* Non-JSON responses retain the HTTP status and safe fallback. */ }
    throw new ApiError(failure.code, failure.message, failure.retryable, failure.request_id, response.status)
  }
  if (options.raw) return response as T
  if (response.status === 204) return undefined as T
  return await response.json() as T
}
