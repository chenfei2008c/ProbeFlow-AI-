import { FormEvent, useEffect, useState } from 'react'
import { ProviderInfo, StudyConfig } from '../types'

interface Props {
  mode: 'text' | 'voice'
  providers: { asr?: ProviderInfo; interview?: ProviderInfo; tts?: ProviderInfo; report?: ProviderInfo }
  study?: StudyConfig
  mock?: boolean
  purpose?: 'start' | 'renew' | 'voice_upgrade'
  onSubmit(): void | Promise<void>
}

export function ConsentGate({ mode, providers, onSubmit, study, mock, purpose = 'start' }: Props) {
  const [processing, setProcessing] = useState(false)
  const [permanent, setPermanent] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  useEffect(() => { setProcessing(false); setPermanent(false) }, [mode])
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!processing || !permanent || submitting) return
    setSubmitting(true)
    try { await onSubmit() } finally { setSubmitting(false) }
  }
  return <form className="consent-card" onSubmit={submit}>
    <div className="eyebrow">{purpose === 'renew' ? '更新后续处理授权' : purpose === 'voice_upgrade' ? '启用语音回答之前' : '开始之前'}</div>
    <h1>你的选择始终有效</h1>
    <p className="lead">{purpose === 'renew' ? '访谈已经结束。处理说明有更新；接受新说明后，研究者可以继续处理现有资料和生成报告。此操作不会重新开始访谈。' : purpose === 'voice_upgrade' ? '语音回答需要补充同意声音处理和原始录音永久保存。当前文字草稿会保留；同意后，点击“开始回答”才会申请麦克风并录音。' : '这场访谈由 AI 主持。你可以跳过问题、随时暂停或结束；结束会保留已提交内容，撤回会删除本场资料。'}</p>
    <div className="consent-summary">
      {study && <><div><span>研究目的</span><strong>{study.objective}</strong></div>{purpose === 'start' && <div><span>预计时长</span><strong>约 {study.target_minutes} 分钟，你可以随时结束</strong></div>}</>}
      <div><span>处理方式</span><strong>{mock ? '当前为模拟测试，不向外部模型发送资料。' : mode === 'voice' ? `录音会发送至${providerName(providers.asr, '语音服务')}，文字由${providerName(providers.interview, '访谈服务')}处理；问题文字由${providerName(providers.tts, '语音合成服务')}播报，报告由${providerName(providers.report, '报告服务')}生成。` : `文字由${providerName(providers.interview, '访谈服务')}处理，报告由${providerName(providers.report, '报告服务')}生成。`}</strong></div>
      <div><span>谁能查看</span><strong>本项目研究者可查看记录与报告，你只能查看自己本场的问答。</strong></div>
      <div><span>保存期限</span><strong>永久保存，除非你撤回或研究者主动删除</strong></div>
    </div>
    <label className="check-row"><input type="checkbox" disabled={submitting} checked={processing} onChange={event => setProcessing(event.target.checked)} /><span>我同意按上述方式处理本次访谈数据{mode === 'voice' && !mock && '，包括将录音发送给语音识别服务'}。</span></label>
    <label className="check-row"><input type="checkbox" disabled={submitting} checked={permanent} onChange={event => setPermanent(event.target.checked)} /><span>我接受已提交内容、全部修订、报告与引用关系永久保存{mode === 'voice' && '，其中包括原始录音与已保存的 AI 问题音频'}。</span></label>
    <button className="button primary wide" disabled={!processing || !permanent || submitting}>{submitting ? '正在保存同意…' : purpose === 'renew' ? '同意更新后的处理与保存说明' : purpose === 'voice_upgrade' ? '同意并启用语音回答' : mode === 'voice' ? '同意并进行设备检查' : '同意并开始文字访谈'}</button>
    <p className="fine-print">原始机器转写、确认文本、全部修订、报告及引用关系均永久保存；语音模式还包括原始录音和已保存的问题音频。结束、归档或长期未访问不会自动删除。撤回会删除本系统内资料并阻止旧备份恢复，已发送给供应商的数据按其实际政策处理。</p>
    <p className="fine-print">{purpose === 'renew' ? '你可以不接受更新后的处理方式；原有档案仍按已接受的说明保存，新的处理请求会被阻止。你仍可撤回本场内容。' : '不同意任一项时无法开始。你可以关闭页面，或切换为文字模式并重新确认相应说明。'}</p>
  </form>
}

function providerName(provider: ProviderInfo | undefined, fallback: string) {
  return provider ? `${provider.provider}（${provider.model}${provider.endpoint_host ? `，${provider.endpoint_host}` : ''}）` : fallback
}
