import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { apiRequest } from '../lib/api'
import { dateTime, money } from '../lib/format'
import { Study, StudyConfig } from '../types'
import { EmptyState, ErrorPanel, Loading, StatusBadge } from '../components/Common'
import { useApi } from '../hooks/useApi'

export const blankStudy: StudyConfig = {
  title: '未命名研究', objective: '了解受访者在真实情境中的经历、行为和判断依据。', participant_description: '', target_minutes: 60,
  topics: [
    { id: crypto.randomUUID(), title: '背景与角色', research_question: '受访者在相关事项中承担什么角色？', priority: 1, evidence_type: '实际职责', minutes: 10 },
    { id: crypto.randomUUID(), title: '一次具体经历', research_question: '最近一次相关经历是怎样发生的？', priority: 1, evidence_type: '具体事件与过程', minutes: 25 },
    { id: crypto.randomUUID(), title: '不同情境与判断', research_question: '哪些情境与前述经历不同？受访者如何理解这种差别？', priority: 2, evidence_type: '对照案例与个人解释', minutes: 15 },
    { id: crypto.randomUUID(), title: '核对与建议', research_question: '哪些理解需要修正？受访者还有什么补充？', priority: 2, evidence_type: '修正与建议', minutes: 10 },
  ],
  exclusions: '', glossary: [], budget_cny: 5, confirm_transcript: true, tone: '中立、清晰、尊重边界',
}

export function StudiesPage() {
  const { data, error, loading, reload } = useApi<Study[]>('/api/admin/studies')
  const [archived, setArchived] = useState(false)
  const [creating, setCreating] = useState(false)
  const navigate = useNavigate()
  const studies = (data ?? []).filter(study => study.archived === archived)
  const create = async () => { setCreating(true); try { const study = await apiRequest<Study>('/api/admin/studies', { method: 'POST', body: blankStudy }); navigate(`/studies/${study.id}`) } finally { setCreating(false) } }
  return <div className="page">
    <div className="page-heading"><div><div className="eyebrow">研究空间</div><h1>把访谈设计成一条证据链</h1><p>每次发布都会冻结版本，进行中的访谈不会被后续修改。</p></div><button className="button primary" onClick={create} disabled={creating}><span>＋</span>{creating ? '正在创建…' : '新建研究'}</button></div>
    <div className="segmented"><button className={!archived ? 'active' : ''} onClick={() => setArchived(false)}>进行中</button><button className={archived ? 'active' : ''} onClick={() => setArchived(true)}>已归档</button></div>
    {loading && <Loading label="正在读取研究…" />}{error && <ErrorPanel error={error} retry={reload} />}
    {!loading && !error && !studies.length && <EmptyState title={archived ? '还没有归档研究' : '从第一个研究开始'} action={!archived && <button className="button secondary" onClick={create}>创建研究</button>}>定义目标、主题与边界，然后生成可以人工编辑的访谈提纲。</EmptyState>}
    <div className="study-grid">{studies.map(study => <Link className="study-card" to={`/studies/${study.id}`} key={study.id}><div className="study-card-top"><StatusBadge status={study.archived ? 'archived' : 'ready'} /><span className="card-arrow">↗</span></div><h2>{study.title}</h2><p>{study.version?.objective || '尚未填写研究目标'}</p><div className="progress-track"><span style={{ width: `${study.session_count ? study.completed_count / study.session_count * 100 : 0}%` }} /></div><div className="study-metrics"><div><strong>{study.completed_count}/{study.session_count}</strong><span>完成场次</span></div><div><strong>{money(study.total_cost_cny)}</strong><span>估算支出</span></div><div><strong>{dateTime(study.updated_at)}</strong><span>最近更新</span></div></div></Link>)}</div>
  </div>
}
