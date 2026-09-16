import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, apiRequest, retryApiRequest } from '../lib/api'
import { duration, actionLabel, pauseLabel } from '../lib/format'
import { exchangeInviteFragment } from '../lib/invite'
import { bindRecordingSafety, detectRecorderMimeType, indexedChunkStorage, RecordingCoordinator, sha256, stopMediaStream, StoredChunk } from '../lib/recorder'
import { confirmWithdrawal, manualTranscriptTurnId } from '../lib/participantRecovery'
import { sessionTimeBoundary } from '../lib/sessionSafety'
import { Config, Detail, Job, Turn } from '../types'
import { ConsentGate } from '../components/ConsentGate'
import { RenewConsent } from '../components/RenewConsent'
import { WithdrawalComplete } from '../components/WithdrawalComplete'
import { ArchiveNotice, ErrorPanel, JobNotice, Loading, ModeBadge, StatusBadge } from '../components/Common'

type Screen = 'opening' | 'consent' | 'device' | 'interview' | 'finished' | 'withdrawn'

export function ParticipantPage({ config: initialConfig }: { config: Config }) {
  const [screen, setScreen] = useState<Screen>('opening')
  const [detail, setDetail] = useState<Detail>()
  const config = detail ? { ...initialConfig, mode: detail.mode, consent_version: detail.consent_version, providers: detail.providers } : initialConfig
  const [mode, setMode] = useState<'voice' | 'text'>('voice')
  const [error, setError] = useState<ApiError | Error>()
  const [syncIssue, setSyncIssue] = useState('')
  const [micStream, setMicStream] = useState<MediaStream>()
  const [withdrawnTurnIds, setWithdrawnTurnIds] = useState<string[]>([])
  const cursor = useRef(0)

  const load = useCallback(async () => {
    const next = await apiRequest<Detail>('/api/participant/session')
    setDetail(next)
    if (['finalizing', 'completed', 'withdrawn'].includes(next.session.status)) setScreen('finished')
    else if (next.session.processing_consent && next.session.permanent_consent && next.session.consent_version === next.consent_version) setScreen(current => current === 'opening' || current === 'consent' ? (next.session.mode === 'voice' && next.session.status === 'ready' ? 'device' : 'interview') : current)
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
  const finishScreen = (withdrawn = false, activeTurnId?: string) => {
    releaseMic()
    if (withdrawn) {
      setWithdrawnTurnIds([...new Set([...(detail?.turns.map(turn => turn.id) ?? []), ...(activeTurnId ? [activeTurnId] : [])])])
      setDetail(undefined)
    }
    setScreen(withdrawn ? 'withdrawn' : 'finished')
  }
  if (screen === 'withdrawn') return <ParticipantFrame config={config}><WithdrawalComplete turnIds={withdrawnTurnIds} /></ParticipantFrame>
  if (screen === 'finished' && detail) return <ParticipantFrame config={config}><Finished detail={detail} reload={load} onWithdrawn={() => finishScreen(true)} /></ParticipantFrame>
  if (screen === 'consent') return <ParticipantFrame config={config}><div className="consent-wrap">{error && <ErrorPanel error={error} />}<div className="mode-switch"><button className={mode === 'voice' ? 'active' : ''} onClick={() => setMode('voice')}>语音访谈</button><button className={mode === 'text' ? 'active' : ''} onClick={() => setMode('text')}>文字访谈</button></div><ConsentGate key={config.consent_version} mode={mode} providers={config.providers} study={detail?.study} mock={config.mode === 'mock'} onSubmit={consent} /></div></ParticipantFrame>
  if (screen === 'device') return <ParticipantFrame config={config}><section className="device-check"><div className="device-icon">◉</div><div className="eyebrow">设备检查</div><h1>先确认麦克风可用</h1><p>浏览器会请求麦克风权限，但现在不会录音。每轮只有在你点击“开始回答”后才会录制。</p>{error && <ErrorPanel error={error} />}<button className="button primary wide" onClick={checkMic}>检查麦克风</button><button className="button ghost wide" onClick={async () => { setMode('text'); setScreen('consent') }}>改用文字回答</button><div className="safety-line"><span>✓</span> 未开始回答前不会录制任何声音</div></section></ParticipantFrame>
  return <ParticipantFrame config={config}>{detail ? <Interview detail={detail} micStream={micStream} requestMic={requestMic} releaseMic={releaseMic} syncIssue={syncIssue} reload={load} onFinished={finishScreen} /> : <Loading />}</ParticipantFrame>
}

function ParticipantFrame({ config, children }: { config: Config; children: React.ReactNode }) {
  return <div className="participant-page"><header className="participant-header"><div className="brand"><span className="brand-mark">P</span><span>ProbeFlow</span></div><ModeBadge mode={config.mode} /></header><main>{children}</main><footer>AI 主持 · 你可以跳过任何问题、暂停或结束访谈</footer></div>
}

function Interview({ detail, micStream, requestMic, releaseMic, syncIssue, reload, onFinished }: { detail: Detail; micStream?: MediaStream; requestMic: () => Promise<MediaStream>; releaseMic: () => void; syncIssue: string; reload: () => Promise<void>; onFinished: (withdrawn?: boolean, activeTurnId?: string) => void }) {
  const [text, setText] = useState('')
  const [answerMode, setAnswerMode] = useState(detail.session.mode)
  const [answerDraft, setAnswerDraft] = useState({ text: '', questionId: '' })
  const [voiceUpgrade, setVoiceUpgrade] = useState(false)
  const [recording, setRecording] = useState(false)
  const [recordSeconds, setRecordSeconds] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [audioFailedFor, setAudioFailedFor] = useState<string>()
  const [warning, setWarning] = useState('')
  const [error, setError] = useState<ApiError | Error>()
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState<Turn>()
  const [manualText, setManualText] = useState('')
  const coordinator = useRef<RecordingCoordinator | undefined>(undefined)
  const audio = useRef<HTMLAudioElement>(null)
  const latestQuestion = [...detail.turns].reverse().find(turn => turn.role === 'assistant')
  const pendingTranscript = [...detail.turns].reverse().find(turn => turn.role === 'participant' && turn.status === 'confirming')
  const pendingAnswer = [...detail.turns].reverse().find(turn => turn.role === 'participant' && ['recording', 'uploading', 'transcribing', 'confirming'].includes(turn.status))
  const unfinished = pendingAnswer && ['recording', 'uploading'].includes(pendingAnswer.status) ? pendingAnswer : undefined
  const latestJob = detail.jobs.at(-1)
  const currentJob = latestJob && ['queued', 'running', 'failed', 'external_status_unknown'].includes(latestJob.status) ? latestJob : undefined
  const processing = detail.jobs.some(job => ['queued', 'running'].includes(job.status))
  const modeBlocked = Boolean(busy || recording || processing || confirming || pendingAnswer)
  const targetSeconds = detail.session.target_seconds ?? detail.study.target_minutes * 60
  const timeBoundary = sessionTimeBoundary(detail.session.active_seconds, targetSeconds)
  const manualTurnId = manualTranscriptTurnId(currentJob, detail.turns)
  const micReady = Boolean(micStream?.getAudioTracks().some(track => track.readyState === 'live'))

  useEffect(() => { if (pendingTranscript) { setConfirming(pendingTranscript); setText(pendingTranscript.text) } }, [pendingTranscript?.id])
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
    if (busy || recording || pendingAnswer || processing) return
    if (detail.session.mode !== 'voice') { setVoiceUpgrade(true); return }
    setBusy(true); setError(undefined); setWarning(''); setRecordSeconds(0)
    try {
      const mimeType = detectRecorderMimeType()
      coordinator.current = new RecordingCoordinator({ consented: detail.session.processing_consent && detail.session.permanent_consent, storage: indexedChunkStorage, upload, onSafetyPause: reason => { setWarning(reason); setRecording(false); void control('pause', { reason }).catch(value => setError(value as Error)) } })
      await coordinator.current.ensureCapacity()
      const stream = micReady && micStream ? micStream : await requestMic()
      const created = await apiRequest<{ turn_id: string }>('/api/participant/turns', { method: 'POST', body: { input_mode: 'voice', mime_type: mimeType } })
      await coordinator.current.start(stream, created.turn_id); setRecording(true)
    } catch (value) {
      coordinator.current?.stop(); releaseMic(); setError(value as Error)
      await reload().catch(() => undefined)
    } finally { setBusy(false) }
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
    event.preventDefault(); if (!answerDraft.text.trim() || busy || pendingAnswer) return; setBusy(true); setError(undefined)
    try {
      const submitted = answerDraft.text.trim()
      const result = await apiRequest<{ turn_id: string; job_id?: string }>('/api/participant/turns', { method: 'POST', body: { input_mode: 'text', text: submitted } })
      setAnswerDraft({ text: '', questionId: '' })
      if (result.job_id) await reload()
      else { setText(submitted); setConfirming({ id: result.turn_id, text: submitted } as Turn) }
    }
    catch (value) { setError(value as ApiError) } finally { setBusy(false) }
  }
  const confirm = async () => {
    if (!confirming || !text.trim()) return; setBusy(true)
    try { await apiRequest(`/api/participant/turns/${confirming.id}/confirm`, { method: 'POST', body: { text: text.trim() } }); setConfirming(undefined); setText(''); await reload() }
    catch (value) { setError(value as ApiError) } finally { setBusy(false) }
  }
  const stopPlayback = async () => { audio.current?.pause(); setPlaying(false); if (latestQuestion) await control('playback_done', { turn_id: latestQuestion.id, played_complete: false }) }
  const chooseAnswerMode = async (next: 'text' | 'voice') => {
    if (modeBlocked || next === answerMode) return
    setBusy(true); setError(undefined)
    try {
      if (playing) await stopPlayback()
      releaseMic()
      if (next === 'voice' && detail.session.mode !== 'voice') setVoiceUpgrade(true)
      else setAnswerMode(next)
    } catch (value) { setError(value as Error) } finally { setBusy(false) }
  }
  const enableVoice = async () => {
    setBusy(true); setError(undefined)
    try {
      await apiRequest('/api/participant/consent', { method: 'POST', body: { version: detail.consent_version, mode: 'voice', processing: true, permanent: true } })
      await reload(); setAnswerMode('voice'); setVoiceUpgrade(false)
    } catch (value) { setError(value as Error) } finally { setBusy(false) }
  }
  const restartUnfinished = async () => {
    if (!unfinished || busy || recording || !window.confirm('这一轮录音尚未完整提交。重新回答将放弃本设备暂存的这一轮录音；已正式保存的录音和文字不受影响。确定重新回答吗？')) return
    setBusy(true); setError(undefined)
    try {
      const stopped = coordinator.current?.stopAndPreserve()
      releaseMic()
      await stopped
      await control('rerecord', { turn_id: unfinished.id })
      for (const chunk of await indexedChunkStorage.list(unfinished.id)) await indexedChunkStorage.remove(unfinished.id, chunk.seq)
    } catch (value) { setError(value as Error) } finally { setBusy(false) }
  }
  const resumeUpload = async () => {
    if (!unfinished) return
    setBusy(true); setError(undefined)
    try {
      await coordinator.current?.stopAndPreserve()
      if (detail.session.status === 'paused') await control('resume')
      const resumed = new RecordingCoordinator({ consented: true, storage: indexedChunkStorage, upload })
      const chunks = await resumed.resume(unfinished.id)
      if (!chunks.length) throw new Error('没有找到可恢复的录音块，请重新回答')
      await apiRequest(`/api/participant/turns/${unfinished.id}/finalize`, { method: 'POST', body: { chunks } })
      await resumed.complete()
      await reload()
    } catch (value) { setError(value as Error) } finally { setBusy(false) }
  }
  const finish = async (withdraw = false) => {
    if (busy || !(withdraw ? confirmWithdrawal() : window.confirm('确定结束访谈并保留已提交内容吗？'))) return
    setError(undefined)
    try {
      if (withdraw) {
        setBusy(true)
        const stopped = coordinator.current?.stopAndPreserve()
        releaseMic(); setRecording(false)
        await stopped
        await apiRequest('/api/participant/control', { method: 'POST', body: { action: 'withdraw' } })
        onFinished(true, coordinator.current?.currentTurnId)
      } else {
        if (recording && !await finishRecording()) return
        setBusy(true)
        await control('end'); releaseMic(); onFinished()
      }
    } catch (value) { setError(value as Error) } finally { setBusy(false) }
  }
  const retry = async (job: Job, accept: boolean) => { await control('retry', { job_id: job.id, accept_possible_charge: accept }) }
  const pauseInterview = async () => {
    setBusy(true)
    try {
      coordinator.current?.stop(); releaseMic(); setRecording(false)
      await control('pause', { reason: 'user' })
    } catch (value) { setError(value as Error) } finally { setBusy(false) }
  }
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
    {detail.turns.length > 0 && <ArchiveNotice mode={detail.archive_mode} />}
    {syncIssue && <div className="warning-banner" role="status">{syncIssue}</div>}
    {manualTurnId && <section className="answer-card"><h2>改为手动录入</h2><p>识别失败后，原始录音仍保留。你可以输入并确认本轮内容；这不会重新调用语音识别。上次请求的未知费用仍保留记录。</p><textarea className="transcript-editor" aria-label="手动录入本轮回答" rows={5} value={manualText} onChange={event => setManualText(event.target.value)} /><button className="button primary" disabled={busy || !manualText.trim()} onClick={confirmManual}>确认手动文字</button></section>}
    <div className="interview-meta"><div><span className="live-dot" />{paused ? '访谈已暂停' : '访谈进行中'}</div><div>{duration(detail.session.active_seconds)} <small>/ {duration(targetSeconds)}</small></div></div>
    <div className="time-progress"><span style={{ width: `${Math.min(100, detail.session.active_seconds / targetSeconds * 100)}%` }} /></div>
    {error && <ErrorPanel error={error} retry={reload} />}{warning && <div className="warning-banner">{warning}</div>}{timeBoundary.level !== 'normal' && <div className={`time-boundary ${timeBoundary.level}`}><div><strong>{timeBoundary.level === 'limit' ? '访谈时间已到上限' : timeBoundary.level === 'warning' ? '请开始收尾' : '目标时长已到'}</strong><p>{timeBoundary.message}</p></div>{timeBoundary.canExtend && <button className="button secondary" onClick={() => control('extend')}>延长 10 分钟</button>}</div>}{unfinished && !recording && !busy && <div className="recovery-banner"><div><strong>这一轮录音尚未完成</strong><p>请先恢复上传，或确认后重新回答。已有的正式录音和文字继续保存。</p></div><div className="button-row"><button className="button secondary" onClick={resumeUpload}>恢复上传</button><button className="button ghost" onClick={restartUnfinished}>重新回答这一轮</button></div></div>}{currentJob && <JobNotice job={currentJob} onRetry={(accept) => retry(currentJob, accept)} />}
    <section className="question-card"><div className="question-label"><span>AI 当前问题</span>{latestQuestion?.action && <small>{actionLabel[latestQuestion.action] ?? latestQuestion.action}</small>}</div><h1>{latestQuestion?.text || '准备好后，我们会从你的实际经历开始。'}</h1>{latestQuestion?.audio_asset_id && latestQuestion.audio_status === 'available' && audioFailedFor !== latestQuestion.id && <div className="audio-controls"><audio ref={audio} src={`/api/media/${latestQuestion.audio_asset_id}`} onError={() => { setPlaying(false); setAudioFailedFor(latestQuestion.id); setWarning('问题音频读取失败，请根据屏幕上的文字继续。') }} onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} onEnded={() => { setPlaying(false); void control('playback_done', { turn_id: latestQuestion.id, played_complete: true }) }} /><button className="button secondary" onClick={() => audio.current?.play().catch(() => setWarning('音频无法播放，请根据屏幕上的问题文字继续。'))}>{playing ? '正在播放…' : latestQuestion.played_complete ? '↻ 再听一次' : '▶ 播放问题'}</button>{playing && <button className="text-button" onClick={stopPlayback}>停止播放并回答</button>}</div>}</section>
    <section className="answer-mode-panel" aria-label="回答方式">
      <div className="mode-switch" role="group" aria-label="选择回答方式">
        <button className={answerMode === 'voice' ? 'active' : ''} aria-pressed={answerMode === 'voice'} disabled={modeBlocked || voiceUpgrade} onClick={() => chooseAnswerMode('voice')}>语音回答</button>
        <button className={answerMode === 'text' ? 'active' : ''} aria-pressed={answerMode === 'text'} disabled={modeBlocked || voiceUpgrade} onClick={() => chooseAnswerMode('text')}>文字回答</button>
      </div>
      <p className="fine-print">切换会保留当前文字草稿，已提交内容继续永久保存。</p>
      {modeBlocked && !voiceUpgrade && <p className="fine-print" role="status">{recording ? '请先停止并提交本轮录音。' : unfinished ? '请先恢复上传或选择重新回答。' : confirming ? '请先确认这一轮文字。' : '当前内容正在处理，请稍候。'}</p>}
      {answerMode === 'voice' && answerDraft.text && <p className="fine-print">你的文字草稿仍保留，切回文字回答可继续编辑。</p>}
    </section>
    {voiceUpgrade ? <section className="voice-upgrade">
      <ConsentGate key={detail.consent_version} mode="voice" providers={detail.providers} study={detail.study} mock={detail.mode === 'mock'} purpose="voice_upgrade" onSubmit={enableVoice} />
      <button className="button ghost wide" disabled={busy} onClick={() => setVoiceUpgrade(false)}>继续使用文字回答</button>
    </section> : paused ? <section className="answer-card centered">
      <div className="device-icon small">Ⅱ</div><h2>已暂停</h2><p>{pauseLabel[detail.session.pause_reason ?? ''] ?? detail.session.pause_reason ?? '你的内容已保存。准备好后可以继续。'}</p><button className="button primary" disabled={busy} onClick={() => control('resume')}>继续访谈</button>
    </section> : confirming ? <section className="answer-card">
      <div className="answer-heading"><div><span className="step">✓</span><div><h2>确认这一轮文字</h2><p>请修正识别错误。确认后才会生成下一问。</p></div></div><StatusBadge status="confirming" /></div>
      <textarea className="transcript-editor" aria-label="确认这一轮文字" rows={7} value={text} onChange={e => setText(e.target.value)} />
      <div className="button-row end"><button className="button ghost" onClick={rerecord} disabled={busy}>重新回答</button><button className="button primary" onClick={confirm} disabled={busy || !text.trim()}>{busy ? '正在保存…' : '确认并继续'}</button></div>
    </section> : <section className="answer-card">
      <div className="answer-heading"><div><span className="step">↳</span><div>
        <h2>{timeBoundary.blockNewAnswer ? (timeBoundary.canExtend ? '请选择结束或延长' : '请结束访谈') : processing || !latestQuestion ? '正在准备下一步' : '轮到你回答'}</h2>
        <p>{timeBoundary.blockNewAnswer ? '可以提交已经开始的回答，但不能再开始新一轮。' : processing || !latestQuestion ? '页面会自动同步处理结果，请不要重复提交。' : answerMode === 'voice' ? '点击开始后录音；说完再提交。AI 播放期间不会录音。' : '写下你的回答，之后仍可确认或修改。'}</p>
      </div></div></div>
      {answerMode === 'voice' ? <div className="record-zone">
        {recording ? <><div className="recording-orb"><span /><strong>{duration(recordSeconds)}</strong><small>正在录音</small></div><button className="button danger wide" onClick={finishRecording}>说完了，安全提交</button></> :
          <button className="record-button" onClick={startRecording} disabled={modeBlocked || playing || !latestQuestion || timeBoundary.blockNewAnswer}><span>●</span><strong>{timeBoundary.blockNewAnswer ? (timeBoundary.canExtend ? '请先选择延长' : '已到 90 分钟上限') : pendingAnswer ? '先处理未完成的回答' : processing || !latestQuestion ? '请稍候' : playing ? '先停止问题播放' : busy ? '正在保存…' : '开始回答'}</strong><small>{playing ? '使用上方“停止播放并回答”' : '点击后才开始录音'}</small></button>}
      </div> : <form onSubmit={submitText}>
        {answerDraft.text && answerDraft.questionId !== latestQuestion?.id && <p className="warning-banner">保留了上一问题的文字草稿，提交前请核对当前问题。</p>}
        <textarea className="transcript-editor" aria-label="文字回答草稿" rows={7} value={answerDraft.text} onChange={e => setAnswerDraft({ text: e.target.value, questionId: answerDraft.text ? answerDraft.questionId : latestQuestion?.id ?? '' })} placeholder="写下你的回答…" disabled={modeBlocked || !latestQuestion || timeBoundary.blockNewAnswer} />
        <button className="button primary wide" disabled={modeBlocked || !latestQuestion || !answerDraft.text.trim() || timeBoundary.blockNewAnswer}>{busy ? '正在提交…' : '提交回答'}</button>
      </form>}
    </section>}

    <div className="interview-actions"><button className="button ghost" disabled={busy} onClick={pauseInterview}>暂停</button><button className="button ghost" disabled={busy || recording || Boolean(unfinished)} onClick={() => control('skip')}>跳过这题</button><button className="text-button danger-text" disabled={busy} onClick={() => finish(false)}>结束并保留</button><button className="text-button danger-text" disabled={busy} onClick={() => finish(true)}>撤回并删除</button></div>
  </div>
}

function Finished({ detail, reload, onWithdrawn }: { detail: Detail; reload: () => Promise<void>; onWithdrawn: () => void }) {
  const [error, setError] = useState<Error>()
  const [busy, setBusy] = useState(false)
  const withdraw = async () => {
    if (busy || !confirmWithdrawal()) return
    setBusy(true); setError(undefined)
    try {
      await apiRequest('/api/participant/control', { method: 'POST', body: { action: 'withdraw' } })
      onWithdrawn()
    } catch (value) { setError(value as Error) } finally { setBusy(false) }
  }
  const withdrawn = detail.session.status === 'withdrawn'
  return <section className="finished-card"><div className="finish-mark">{withdrawn ? '×' : '✓'}</div><div className="eyebrow">访谈已{withdrawn ? '撤回' : '结束'}</div><h1>{withdrawn ? '本场资料正在按撤回流程删除' : '感谢你分享这些经历'}</h1><p>{withdrawn ? '新的处理请求已停止，访问凭证已撤销。供应商侧数据依其实际政策处理。' : '你已提交的内容会按说明永久保存。结束访谈不会自动删除资料。'}</p>{!busy && <RenewConsent detail={detail} reload={reload} />}{error && <ErrorPanel error={error} />}{!withdrawn && <><button className="button ghost" disabled={busy} onClick={reload}>检查处理状态</button><button className="text-button danger-text" disabled={busy} onClick={withdraw}>{busy ? '正在撤回…' : '撤回并删除本场内容'}</button></>}</section>
}
