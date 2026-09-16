import { useState } from 'react'
import { apiRequest } from '../lib/api'
import { Detail } from '../types'
import { ConsentGate } from './ConsentGate'
import { ErrorPanel } from './Common'

export function RenewConsent({ detail, reload }: { detail: Detail; reload: () => Promise<void> }) {
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<Error>()
  if (detail.session.status !== 'completed' || detail.session.consent_version === detail.consent_version) return null
  const renew = async () => {
    try {
      await apiRequest('/api/participant/consent', { method: 'POST', body: { version: detail.consent_version, mode: detail.session.mode, processing: true, permanent: true } })
      setOpen(false); await reload()
    } catch (value) { setError(value as Error) }
  }
  return <div className="consent-renewal">{error && <ErrorPanel error={error} />}{open ? <><ConsentGate key={detail.consent_version} mode={detail.session.mode} providers={detail.providers} study={detail.study} mock={detail.mode === 'mock'} purpose="renew" onSubmit={renew} /><button className="text-button" onClick={() => setOpen(false)}>暂不接受更新</button></> : <><p>处理说明有更新。你可以选择是否授权后续处理，既有档案继续保存。</p><button className="button secondary" onClick={() => setOpen(true)}>查看更新后的处理说明</button></>}</div>
}
