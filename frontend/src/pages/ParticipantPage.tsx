import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, apiRequest, retryApiRequest } from '../lib/api'
import { duration, actionLabel, pauseLabel } from '../lib/format'
import { exchangeInviteFragment } from '../lib/invite'
import { bindRecordingSafety, detectRecorderMimeType, indexedChunkStorage, RecordingCoordinator, sha256, stopMediaStream, StoredChunk } from '../lib/recorder'
import { manualTranscriptTurnId } from '../lib/participantRecovery'
import { sessionTimeBoundary } from '../lib/sessionSafety'
import { Config, Detail, Job, Turn } from '../types'
import { ConsentGate } from '../components/ConsentGate'
import { ErrorPanel, JobNotice, Loading, ModeBadge, StatusBadge } from '../components/Common'

type Screen = 'opening' | 'consent' | 'device' | 'interview' | 'finished' | 'withdrawn'

export function ParticipantPage({ config: initialConfig }: { config: Config }) {
  const [screen, setScreen] = useState<Screen>('opening')
  const [detail, setDetail] = useState<Detail>()
  const config = detail ? { ...initialConfig, mode: detail.mode, consent_version: detail.consent_version, providers: detail.providers } : initialConfig
  const [mode, setMode] = useState<'voice' | 'text'>('voice')
  const [error, setError] = useState<ApiError | Error>()
  const [syncIssue, setSyncIssue] = useState('')
  const [micStream, setMicStream] = useState<MediaStream>()
  const cursor = useRef(0)

  const load = useCallback(async () => {
    const next = await apiRequest<Detail>('/api/participant/session')
    setDetail(next)
    if (['finalizing', 'completed', 'withdrawn'].includes(next.session.status)) setScreen('finished')
    else if (next.session.processing_consent && next.session.permanent_consent && next.session.consent_version === next.consent_version) setScreen(current => current === 'opening' || current === 'consent' ? (next.session.mode === 'voice' ? 'device' : 'interview') : current)
    else {
      setMicStream(current => { stopMediaStream(current); return undefined })
      if (next.session.consent_version) setMode(next.session.mode)
      setScreen('consent')
    }
  }, [])

  useEffect(() => {
    let active = true
    const open = async () => {
      try {
        if (location.hash.includes('token=')) await exchangeInviteFragment()
        if (active) await load()
      } catch (value) { if (active) { setError(value as Error); setScreen('consent') } }
    }
    void open()
    return () => { active = false }
  }, [load])

  useEffect(() => {
    if (screen !== 'interview') return
    let stopped = false
    let timer: number
    const poll = async () => {
      try {
        const events = await apiRequest<{ events: Array<{ seq: number }>; cursor: number }>(`/api/participant/events?after=${cursor.current}`)
        setSyncIssue('')
        cursor.current = events.cursor
        if (events.events.length) await load()
      } catch { setSyncIssue('连接暂时中断。已保存的内容不会丢失；恢复网络后页面会自动同步，请不要重复提交。') }
      if (!stopped) timer = window.setTimeout(poll, detail?.jobs.some(job => ['queued', 'running'].includes(job.status)) ? 1000 : 4000)
    }
    void poll()
    return () => { stopped = true; clearTimeout(timer) }
  }, [screen, detail?.jobs, load])

  useEffect(() => {
    const online = () => { setSyncIssue(''); void load().catch(() => setSyncIssue('连接仍未恢复，请稍后再试。')) }
    window.addEventListener('online', online)
    return () => window.removeEventListener('online', online)
  }, [load])

  useEffect(() => () => stopMediaStream(micStream), [micStream])

  const consent = async () => {
    try {
      await apiRequest('/api/participant/consent', { method: 'POST', body: { version: config.consent_version, mode, processing: true, permanent: true } })
      await load(); setScreen(mode === 'voice' ? 'device' : 'interview')
    } catch (value) { setError(value as ApiError) }
  }
  const requestMic = useCallback(async () => {
    stopMediaStream(micStream)
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    setMicStream(stream)
    return stream
  }, [micStream])
  const releaseMic = useCallback(() => { setMicStream(current => { stopMediaStream(current); return undefined }) }, [])
  const checkMic = async () => {
    try { await requestMic(); setScreen('interview') }
    catch { setError(new Error('无法访问麦克风。请在浏览器地址栏的网站设置中允许麦克风，或改用文字回答。')) }
  }
  if (screen === 'opening') return <ParticipantFrame config={config}><Loading label="正在安全打开邀请…" /></ParticipantFrame>
  const finishScreen = (withdrawn = false) => { releaseMic(); if (withdrawn) setDetail(undefined); setScreen(withdrawn ? 'withdrawn' : 'finished') }
  if (screen === 'withdrawn') return <ParticipantFrame config={config}><section className="finished-card"><h1>本场访谈已撤回</h1><p>已删除本系统内的访谈资料，并撤销访问凭证。供应商侧数据依其实际政策处理。</p></section></ParticipantFrame>
  if (screen === 'finished' && detail) return <ParticipantFrame config={config}><Finished detail={detail} reload={load} onWithdrawn={() => finishScreen(true)} /></ParticipantFrame>
  if (screen === 'consent') return <ParticipantFrame config={config}><div className="consent-wrap">{error && <ErrorPanel error={error} />}<div className="mode-switch"><button className={mode === 'voice' ? 'active' : ''} onClick={() => setMode('voice')}>语音访谈</button><button className={mode === 'text' ? 'active' : ''} onClick={() => setMode('text')}>文字访谈</button></div><ConsentGate key={config.consent_version} mode={mode} providers={config.providers} study={detail?.study} mock={config.mode === 'mock'} onSubmit={consent} /></div></ParticipantFrame>
  if (screen === 'device') return <ParticipantFrame config={config}><section className="device-check"><div className="device-icon">◉</div><div className="eyebrow">设备检查</div><h1>先确认麦克风可用</h1><p>浏览器会请求麦克风权限，但现在不会录音。每轮只有在你点击“开始回答”后才会录制。</p>{error && <ErrorPanel error={error} />}<button className="button primary wide" onClick={checkMic}>检查麦克风</button><button className="button ghost wide" onClick={async () => { setMode('text'); setScreen('consent') }}>改用文字回答</button><div className="safety-line"><span>✓</span> 未开始回答前不会录制任何声音</div></section></ParticipantFrame>
  return <ParticipantFrame config={config}>{detail ? <Interview detail={detail} micStream={micStream} requestMic={requestMic} releaseMic={releaseMic} syncIssue={syncIssue} reload={load} onFinished={finishScreen} /> : <Loading />}</ParticipantFrame>
}

function ParticipantFrame({ config, children }: { config: Config; children: React.ReactNode }) {
  return <div className="participant-page"><header className="participant-header"><div className="brand"><span className="brand-mark">P</span><span>ProbeFlow</span></div><ModeBadge mode={config.mode} /></header><main>{children}</main><footer>AI 主持 · 你可以跳过任何问题、暂停或结束访谈</footer></div>
}

function Interview({ detail, micStream, requestMic, releaseMic, syncIssue, reload, onFinished }: { detail: Detail; micStream?: MediaStream; requestMic: () => Promise<MediaStream>; releaseMic: () => void; syncIssue: string; reload: () => Promise<void>; onFinished: (withdrawn?: boolean) => void }) {
  const [text, setText] = useState('')
  const [recording, setRecording] = useState(false)
  const [recordSeconds, setRecordSeconds] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [audioFailedFor, setAudioFailedFor] = useState<string>()
  const [warning, setWarning] = useState('')
  const [error, setError] = useState<ApiError | Error>()
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState<Turn>()
  const [recoverable, setRecoverable] = useState<Turn>()
  const [manualText, setManualText] = useState('')
  const coordinator = useRef<RecordingCoordinator | undefined>(undefined)
  const audio = useRef<HTMLAudioElement>(null)
  const latestQuestion = [...detail.turns].reverse().find(turn => turn.role === 'assistant')
  const pendingTranscript = [...detail.turns].reverse().find(turn => turn.role === 'participant' && turn.status === 'confirming')
  const latestJob = detail.jobs.at(-1)
  const currentJob = latestJob && ['queued', 'running', 'failed', 'external_status_unknown'].includes(latestJob.status) ? latestJob : undefined
  const processing = currentJob && ['queued', 'running'].includes(currentJob.status)
  const targetSeconds = detail.session.target_seconds ?? detail.study.target_minutes * 60
  const timeBoundary = sessionTimeBoundary(detail.session.active_seconds, targetSeconds)
  const manualTurnId = manualTranscriptTurnId(currentJob, detail.turns)
  const micReady = Boolean(micStream?.getAudioTracks().some(track => track.readyState === 'live'))

  useEffect(() => { if (pendingTranscript) { setConfirming(pendingTranscript); setText(pendingTranscript.text) } }, [pendingTranscript?.id])
  useEffect(() => {
    const unfinished = [...detail.turns].reverse().find(turn => turn.role === 'participant' && ['recording', 'uploading'].includes(turn.status))
    if (!unfinished) { setRecoverable(undefined); return }
    void indexedChunkStorage.list(unfinished.id).then(chunks => setRecoverable(chunks.length ? unfinished : undefined)).catch(() => undefined)
  }, [detail.turns])
  const control = useCallback(async (action: string, extra: Record<string, unknown> = {}) => { await apiRequest('/api/participant/control', { method: 'POST', body: { action, ...extra } }); await reload() }, [reload])

  useEffect(() => {
    const safetyPause = async (reason: string) => {
      if (recording) coordinator.current?.safetyPause(reason)
      releaseMic()
      setRecording(false); setWarning(reason)
      try { await control('pause', { reason }) } catch { /* server will reconcile heartbeat */ }
    }
    return bindRecordingSafety(reason => void safetyPause(reason))
  }, [recording, control, micStream, releaseMic])

  useEffect(() => {
    if (!recording) return
    const timer = window.setInterval(() => setRecordSeconds(value => value + 1), 1000)
    return () => clearInterval(timer)
  }, [recording])
  useEffect(() => {
    if (recordSeconds === 180) setWarning('已经录了 3 分钟。你可以继续，也可以先提交这一段。')
    if (recordSeconds >= 600 && recording) { setWarning('已达到单次 10 分钟安全上限，正在保存并停止本轮。'); void finishRecording() }
  }, [recordSeconds, recording]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const timer = window.setInterval(() => { if (!document.hidden && navigator.onLine) void apiRequest('/api/participant/heartbeat', { method: 'POST', body: {} }).then(reload).catch(() => undefined) }, 10000)
    return () => clearInterval(timer)
  }, [reload])

  const upload = async (chunk: StoredChunk) => {
    const hash = await sha256(chunk.blob)
    return retryApiRequest<{ seq: number; sha256: string }>(`/api/participant/turns/${chunk.turnId}/chunks/${chunk.seq}`, { method: 'PUT', body: chunk.blob, headers: { 'Content-Type': chunk.mimeType, 'X-Chunk-SHA256': hash }, idempotencyKey: `${chunk.turnId}:${chunk.seq}:${hash}` })
  }
  const startRecording = async () => {
    setError(undefined); setWarning(''); setRecordSeconds(0)
    try {
      const stream = micReady && micStream ? micStream : await requestMic()
      const mimeType = detectRecorderMimeType()
      const created = await apiRequest<{ turn_id: string }>('/api/participant/turns', { method: 'POST', body: { input_mode: 'voice', mime_type: mimeType } })
      coordinator.current = new RecordingCoordinator({ consented: detail.session.processing_consent && detail.session.permanent_consent, storage: indexedChunkStorage, upload, onSafetyPause: reason => { setWarning(reason); setRecording(false); void control('pause', { reason }) } })
      await coordinator.current.start(stream, created.turn_id); setRecording(true)
    } catch (value) { setError(value as Error) }
  }
  const finishRecording = async () => {
    if (!coordinator.current || busy) return false
    setRecording(false); setBusy(true)
    try {
      const chunks = await coordinator.current.stopAndFlush()
      const actualTurnId = coordinator.current.currentTurnId
      if (!actualTurnId) throw new Error('未找到本轮上传记录，请刷新后恢复')
      await apiRequest(`/api/participant/turns/${actualTurnId}/finalize`, { method: 'POST', body: { chunks: [...chunks].sort((a, b) => a.seq - b.seq) } })
      await coordinator.current.complete()
      releaseMic()
      await reload()
      return true
    } catch (value) { setError(value as Error); return false } finally { releaseMic(); setBusy(false) }
  }
  const submitText = async (event: FormEvent) => {
    event.preventDefault(); if (!text.trim()) return; setBusy(true); setError(undefined)
    try { const result = await apiRequest<{ turn_id: string; job_id?: string }>('/api/participant/turns', { method: 'POST', body: { input_mode: 'text', text: text.trim() } }); if (result.job_id) { setText(''); await reload() } else setConfirming({ id: result.turn_id, text: text.trim() } as Turn) }
    catch (value) { setError(value as ApiError) } finally { setBusy(false) }
  }
  const confirm = async () => {
    if (!confirming || !text.trim()) return; setBusy(true)
    try { await apiRequest(`/api/participant/turns/${confirming.id}/confirm`, { method: 'POST', body: { text: text.trim() } }); setConfirming(undefined); setText(''); await reload() }
    catch (value) { setError(value as ApiError) } finally { setBusy(false) }
  }
  const stopPlayback = async () => { audio.current?.pause(); setPlaying(false); if (latestQuestion) await control('playback_done', { turn_id: latestQuestion.id, played_complete: false }) }
  const resumeUpload = async () => {
    if (!recoverable) return
    setBusy(true); setError(undefined)
    try {
      if (detail.session.status === 'paused') await control('resume')
      const resumed = new RecordingCoordinator({ consented: true, storage: indexedChunkStorage, upload })
      const chunks = await resumed.resume(recoverable.id)
      if (!chunks.length) throw new Error('没有找到可恢复的录音块，请重新回答')
      await apiRequest(`/api/participant/turns/${recoverable.id}/finalize`, { method: 'POST', body: { chunks } })
      await resumed.complete()
      setRecoverable(undefined); await reload()
    } catch (value) { setError(value as Error) } finally { setBusy(false) }
  }
  const finish = async (withdraw = false) => {
    const prompt = withdraw ? '撤回会删除本场已提交的录音、文字、修订、报告与引用。确定继续吗？' : '确定结束访谈并保留已提交内容吗？'
    if (!window.confirm(prompt)) return
    if (withdraw && !window.confirm('请再次确认：撤回后本场内容将不可访问。')) return
    try {
      if (withdraw) {
        coordinator.current?.stop(); releaseMic(); setRecording(false)
        await apiRequest('/api/participant/control', { method: 'POST', body: { action: 'withdraw' } })
        for (const turn of detail.turns) for (const chunk of await indexedChunkStorage.list(turn.id)) await indexedChunkStorage.remove(turn.id, chunk.seq)
        onFinished(true)
      } else {
        if (recording && !await finishRecording()) return
        await control('end'); releaseMic(); onFinished()
      }
    } catch (value) { setError(value as Error) }
  }
  const retry = async (job: Job, accept: boolean) => { await control('retry', { job_id: job.id, accept_possible_charge: accept }) }
  const paused = detail.session.status === 'paused'
  const rerecord = async () => {
    if (!confirming) return
    await control('rerecord', { turn_id: confirming.id })
    setConfirming(undefined); setText('')
  }
  const confirmManual = async () => {
    if (!manualTurnId || !manualText.trim()) return
    setBusy(true)
    try {
      await apiRequest(`/api/participant/turns/${manualTurnId}/confirm`, { method: 'POST', body: { text: manualText.trim() } })
      setManualText(''); setWarning('手动文字已保存。准备好后点击继续访谈。'); await reload()
    } catch (value) { setError(value as Error) } finally { setBusy(false) }
  }
  return <div className="interview-shell">
    {syncIssue && <div className="warning-banner" role="status">{syncIssue}</div>}
    {manualTurnId && <section className="answer-card"><h2>改为手动录入</h2><p>识别失败后，原始录音仍保留。你可以输入并确认本轮内容；这不会重新调用语音识别。上次请求的未知费用仍保留记录。</p><textarea className="transcript-editor" aria-label="手动录入本轮回答" rows={5} value={manualText} onChange={event => setManualText(event.target.value)} /><button className="button primary" disabled={busy || !manualText.trim()} onClick={confirmManual}>确认手动文字</button></section>}
    <div className="interview-meta"><div><span className="live-dot" />{paused ? '访谈已暂停' : '访谈进行中'}</div><div>{duration(detail.session.active_seconds)} <small>/ {duration(targetSeconds)}</small></div></div>
    <div className="time-progress"><span style={{ width: `${Math.min(100, detail.session.active_seconds / targetSeconds * 100)}%` }} /></div>
    {error && <ErrorPanel error={error} retry={reload} />}{warning && <div className="warning-banner">{warning}</div>}{timeBoundary.level !== 'normal' && <div className={`time-boundary ${timeBoundary.level}`}><div><strong>{timeBoundary.level === 'limit' ? '访谈时间已到上限' : timeBoundary.level === 'warning' ? '请开始收尾' : '目标时长已到'}</strong><p>{timeBoundary.message}</p></div>{timeBoundary.canExtend && <button className="button secondary" onClick={() => control('extend')}>延长 10 分钟</button>}</div>}{recoverable && <div className="recovery-banner"><div><strong>发现尚未完成的录音上传</strong><p>录音块仍保存在这台设备，可按原顺序继续上传。若容器因异常中断无法解码，服务端会明确提示重新录音。</p></div><button className="button secondary" onClick={resumeUpload} disabled={busy}>恢复上传</button></div>}{currentJob && <JobNotice job={currentJob} onRetry={(accept) => retry(currentJob, accept)} />}
    <section className="question-card"><div className="question-label"><span>AI 当前问题</span>{latestQuestion?.action && <small>{actionLabel[latestQuestion.action] ?? latestQuestion.action}</small>}</div><h1>{latestQuestion?.text || '准备好后，我们会从你的实际经历开始。'}</h1>{latestQuestion?.audio_asset_id && latestQuestion.audio_status === 'available' && audioFailedFor !== latestQuestion.id && <div className="audio-controls"><audio ref={audio} src={`/api/media/${latestQuestion.audio_asset_id}`} onError={() => { setPlaying(false); setAudioFailedFor(latestQuestion.id); setWarning('问题音频读取失败，请根据屏幕上的文字继续。') }} onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} onEnded={() => { setPlaying(false); void control('playback_done', { turn_id: latestQuestion.id, played_complete: true }) }} /><button className="button secondary" onClick={() => audio.current?.play().catch(() => setWarning('音频无法播放，请根据屏幕上的问题文字继续。'))}>{playing ? '正在播放…' : latestQuestion.played_complete ? '↻ 再听一次' : '▶ 播放问题'}</button>{playing && <button className="text-button" onClick={stopPlayback}>停止播放并回答</button>}</div>}</section>
    {paused ? <section className="answer-card centered"><div className="device-icon small">Ⅱ</div><h2>已暂停</h2><p>{pauseLabel[detail.session.pause_reason ?? ''] ?? detail.session.pause_reason ?? '你的内容已保存。准备好后可以继续。'}</p><button className="button primary" onClick={() => control('resume')}>继续访谈</button></section> : confirming ? <section className="answer-card"><div className="answer-heading"><div><span className="step">✓</span><div><h2>确认这一轮文字</h2><p>请修正识别错误。确认后才会生成下一问。</p></div></div><StatusBadge status="confirming" /></div><textarea className="transcript-editor" rows={7} value={text} onChange={e => setText(e.target.value)} /><div className="button-row end"><button className="button ghost" onClick={rerecord}>重新回答</button><button className="button primary" onClick={confirm} disabled={busy || !text.trim()}>{busy ? '正在保存…' : '确认并继续'}</button></div></section> : <section className="answer-card"><div className="answer-heading"><div><span className="step">↳</span><div><h2>{timeBoundary.blockNewAnswer ? (timeBoundary.canExtend ? '请选择结束或延长' : '请结束访谈') : processing || !latestQuestion ? '正在准备下一步' : '轮到你回答'}</h2><p>{timeBoundary.blockNewAnswer ? '可以提交已经开始的回答，但不能再开始新一轮。' : processing || !latestQuestion ? '页面会自动同步处理结果，请不要重复提交。' : detail.session.mode === 'voice' ? '点击开始后录音；说完再提交。AI 播放期间不会录音。' : '写下你的回答，之后仍可确认或修改。'}</p></div></div></div>{detail.session.mode === 'voice' ? <div className="record-zone">{recording ? <><div className="recording-orb"><span /><strong>{duration(recordSeconds)}</strong><small>正在录音</small></div><button className="button danger wide" onClick={finishRecording}>说完了，安全提交</button></> : <button className="record-button" onClick={startRecording} disabled={busy || playing || processing || !latestQuestion || timeBoundary.blockNewAnswer}><span>●</span><strong>{timeBoundary.blockNewAnswer ? (timeBoundary.canExtend ? '请先选择延长' : '已到 90 分钟上限') : processing || !latestQuestion ? '请稍候' : playing ? '先停止问题播放' : busy ? '正在保存…' : '开始回答'}</strong><small>{playing ? '使用上方“停止播放并回答”' : '点击后才开始录音'}</small></button>}</div> : <form onSubmit={submitText}><textarea className="transcript-editor" rows={7} value={text} onChange={e => setText(e.target.value)} placeholder="写下你的回答…" disabled={Boolean(processing || !latestQuestion || timeBoundary.blockNewAnswer)} /><button className="button primary wide" disabled={busy || processing || !latestQuestion || !text.trim() || timeBoundary.blockNewAnswer}>{busy ? '正在提交…' : '提交回答'}</button></form>}</section>}
    <div className="interview-actions"><button className="button ghost" onClick={() => control('pause', { reason: 'user' })}>暂停</button><button className="button ghost" onClick={() => control('skip')}>跳过这题</button><button className="text-button danger-text" disabled={busy} onClick={() => finish(false)}>结束并保留</button><button className="text-button danger-text" onClick={() => finish(true)}>撤回并删除</button></div>
  </div>
}

function Finished({ detail, reload, onWithdrawn }: { detail: Detail; reload: () => Promise<void>; onWithdrawn: () => void }) {
  const [error, setError] = useState<Error>()
  const withdraw = async () => {
    if (!window.confirm('撤回将删除本场已提交内容，即使访谈已结束也无法恢复。确定撤回吗？')) return
    try {
      await apiRequest('/api/participant/control', { method: 'POST', body: { action: 'withdraw' } })
      for (const turn of detail.turns) for (const chunk of await indexedChunkStorage.list(turn.id)) await indexedChunkStorage.remove(turn.id, chunk.seq)
      onWithdrawn()
    } catch (value) { setError(value as Error) }
  }
  const withdrawn = detail.session.status === 'withdrawn'
  return <section className="finished-card"><div className="finish-mark">{withdrawn ? '×' : '✓'}</div><div className="eyebrow">访谈已{withdrawn ? '撤回' : '结束'}</div><h1>{withdrawn ? '本场资料正在按撤回流程删除' : '感谢你分享这些经历'}</h1><p>{withdrawn ? '新的处理请求已停止，访问凭证已撤销。供应商侧数据依其实际政策处理。' : '你已提交的内容会按说明永久保存。结束访谈不会自动删除资料。'}</p>{error && <ErrorPanel error={error} />}{!withdrawn && <><button className="button ghost" onClick={reload}>检查处理状态</button><button className="text-button danger-text" onClick={withdraw}>撤回并删除本场内容</button></>}</section>
}
