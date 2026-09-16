import { useEffect, useRef, useState } from 'react'
import { ApiOptions, apiRequest } from '../lib/api'
import { AppMode, Job } from '../types'

type RequestFn = (path: string, options?: ApiOptions) => Promise<unknown>

export function DiagnosticTts({ mode, request = apiRequest as RequestFn, pollIntervalMs = 900 }: { mode: AppMode; request?: RequestFn; pollIntervalMs?: number }) {
  const [text, setText] = useState('你好，这是 ProbeFlow 的中文语音检查。')
  const [status, setStatus] = useState('')
  const [error, setError] = useState('')
  const [audioAssetId, setAudioAssetId] = useState('')
  const [busy, setBusy] = useState(false)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  const run = async () => {
    setBusy(true); setError(''); setAudioAssetId(''); setStatus('正在创建试听任务…')
    try {
      const created = await request('/api/admin/diagnostics/tts', { method: 'POST', body: { text } }) as { job_id: string }
      setStatus('语音正在生成，请稍候…')
      while (mounted.current) {
        if (pollIntervalMs) await new Promise(resolve => setTimeout(resolve, pollIntervalMs))
        const job = await request(`/api/admin/jobs/${created.job_id}`) as Job
        if (job.status === 'queued' || job.status === 'running') continue
        if (job.status === 'succeeded') {
          const assetId = (job.result as { audio_asset_id?: string } | undefined)?.audio_asset_id
          if (!assetId) throw new Error('任务完成但没有返回可播放音频')
          if (mounted.current) { setAudioAssetId(assetId); setStatus('试听音频已准备') }
          return
        }
        const prefix = job.status === 'external_status_unknown' ? '试听状态未知，可能已经产生费用' : '试听失败'
        throw new Error(`${prefix}：${job.error_message || '请查看最近错误后重试'}`)
      }
    } catch (value) {
      if (mounted.current) { setError(value instanceof Error ? value.message : '试听失败：未知错误'); setStatus('') }
    } finally { if (mounted.current) setBusy(false) }
  }

  return <div className="tts-check">
    <label className="field"><span>中文语音试听</span><textarea rows={3} value={text} maxLength={100} onChange={event => setText(event.target.value)} /></label>
    <button className="button secondary" onClick={run} disabled={busy || !text.trim()}>{busy ? '正在生成…' : '创建试听任务'}</button>
    {status && <p aria-live="polite">{status}</p>}
    {error && <p className="tts-error" role="alert">{error.startsWith('试听') ? error : `试听失败：${error}`}</p>}
    {audioAssetId && <audio aria-label="中文语音试听结果" controls preload="none" src={`/api/media/${audioAssetId}`} />}
    <small>{mode === 'mock' && audioAssetId ? '这是模拟提示音，不代表中文播报能力已通过。' : mode === 'mock' ? '模拟模式只会返回提示音，不代表中文播报能力。' : '请实际播放并检查普通话是否清晰自然。'}</small>
  </div>
}
