import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, apiRequest } from '../lib/api'
import { dateTime } from '../lib/format'
import { useApi } from '../hooks/useApi'
import { ErrorPanel, Loading } from './Common'

export interface InviteItem {
  id: string; version_number: number; session_id?: string
  status: 'available' | 'redeemed' | 'revoked' | 'expired'
  created_at: string; expires_at: string
}

export function InviteManager({ studyId, refreshKey, onRevoked }: { studyId: string; refreshKey?: string; onRevoked?: (id: string) => void }) {
  const path = `/api/admin/studies/${studyId}/invites`
  const { data: invites, loading, error, reload } = useApi<InviteItem[]>(path, [refreshKey])
  const [busy, setBusy] = useState<string>()
  const [actionError, setActionError] = useState<ApiError>()
  const labels = { available: '待兑换', redeemed: '已兑换', revoked: '已撤销', expired: '已到期' }
  const revoke = async (id: string) => {
    setBusy(id); setActionError(undefined)
    try {
      await apiRequest(`${path}/${id}/revoke`, { method: 'POST', body: {} })
      onRevoked?.(id); await reload()
    } catch (value) { setActionError(value as ApiError) } finally { setBusy(undefined) }
  }
  return <section className="panel">
    <div className="section-heading"><div><h2>邀请管理</h2><p>链接仅创建时显示。撤销待兑换邀请不会删除档案；更换已兑换的访问凭证，请进入场次创建恢复邀请。</p></div><button className="text-button" onClick={reload}>刷新邀请</button></div>
    {(error || actionError) && <ErrorPanel error={(error || actionError)!} retry={reload} />}
    {loading ? <Loading label="正在读取邀请…" /> : !invites?.length ? <p className="muted">尚未创建邀请。</p> : <div className="invite-list">{invites.map(invite => <article className="invite-row" key={invite.id}>
      <div><strong>研究版本 V{invite.version_number} · {labels[invite.status]}</strong><p>创建于 {dateTime(invite.created_at)} · 有效至 {dateTime(invite.expires_at)}</p></div>
      <div className="button-row">{invite.session_id && <Link className="button ghost" to={`/sessions/${invite.session_id}`}>查看场次</Link>}{invite.status === 'available' && <button className="button ghost danger-text" disabled={!!busy} onClick={() => revoke(invite.id)}>{busy === invite.id ? '正在撤销…' : '撤销邀请'}</button>}</div>
    </article>)}</div>}
  </section>
}
