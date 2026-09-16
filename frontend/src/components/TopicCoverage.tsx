import { Coverage } from '../types'

const labels = { not_started: '未谈及', awaiting_answer: '已提问，尚无确认回答', partial: '已讨论，仍待补充', covered: '已覆盖（模型判断）', skipped: '已跳过／拒答' }

export function TopicCoverage({ coverage, onTurn }: { coverage: Coverage; onTurn?: (id: string) => void }) {
  return <div className="topic-coverage">
    {coverage.topics.map(topic => <article className="coverage-item" key={topic.id}>
      <div className="section-heading"><strong>{topic.title}</strong><span className={`coverage-status ${topic.status}`}>{labels[topic.status]}</span></div>
      <p>{topic.confirmed_turn_ids.length} 条已确认回答{topic.unconfirmed_count > 0 ? ` · ${topic.unconfirmed_count} 条待确认` : ''}</p>
      {topic.reasons.length > 0 && <p className="muted">{topic.reasons.join('；')}</p>}
      {onTurn && <div className="button-row">{topic.confirmed_turn_ids.map((id, index) => <button className="text-button" onClick={() => onTurn(id)} key={id}>定位回答 {index + 1}</button>)}</div>}
    </article>)}
    {coverage.unresolved.length > 0 && <section><h3>仍待澄清</h3><ul>{coverage.unresolved.map((item, index) => <li key={index}>{item.text}</li>)}</ul></section>}
  </div>
}
