import { ConsentRecord } from '../types'
import { dateTime } from '../lib/format'

const roles: Record<string, string> = { asr: '录音识别', interview: '访谈文字', tts: '问题播报', report: '报告生成' }

export function ConsentHistory({ records }: { records: ConsentRecord[] }) {
  return <details className="panel consent-history">
    <summary>同意与保存记录 · {records.length} 次</summary>
    <p className="muted">按接受时间保留历史。这里展示当时接受的说明，不随当前服务配置改变。</p>
    {!records.length && <p>尚无已接受的同意记录，不能开始处理。</p>}
    {records.map(record => <article className="coverage-item" key={record.id}>
      <div className="section-heading"><strong>{record.mode === 'voice' ? '语音访谈' : '文字访谈'}</strong><time dateTime={record.created_at}>{dateTime(record.created_at)}</time></div>
      <p>说明版本：<span>{record.version}</span></p>
      <p>数据处理：{record.processing ? '已同意' : '未同意'} · 永久保存：{record.permanent ? '已接受' : '未接受'}</p>
      <p>保存策略：{record.retention === 'permanent' ? '永久保存，可主动删除或撤回' : record.retention}</p>
      {record.snapshot?.mode && <p>{record.snapshot.mode === 'mock' ? '模拟模式，不向外部模型发送资料' : '真实服务处理模式'}</p>}
      {record.snapshot?.providers && Object.keys(record.snapshot.providers).length ? <ul>{Object.entries(record.snapshot.providers).map(([role, provider]) => <li key={role}>{roles[role] ?? role}：{provider.provider} · {provider.model}{provider.endpoint_host && ` · ${provider.endpoint_host}`}{provider.region && ` · ${provider.region}`}</li>)}</ul> : <p className="muted">该历史记录未保存处理方快照，不能按当前配置推断。</p>}
    </article>)}
  </details>
}
