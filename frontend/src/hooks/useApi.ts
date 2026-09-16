import { DependencyList, useCallback, useEffect, useState } from 'react'
import { ApiError, apiRequest } from '../lib/api'

export function useApi<T>(path: string | null, deps: DependencyList = []) {
  const [data, setData] = useState<T>()
  const [error, setError] = useState<ApiError>()
  const [loading, setLoading] = useState(Boolean(path))
  const load = useCallback(async () => {
    if (!path) return
    setLoading(true); setError(undefined)
    try { setData(await apiRequest<T>(path)) } catch (value) { setError(value as ApiError) } finally { setLoading(false) }
  }, [path, ...deps]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { void load() }, [load])
  return { data, setData, error, loading, reload: load }
}
