import { FormEvent, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, apiRequest } from '../lib/api'
import { Config } from '../types'
import { ErrorPanel, ModeBadge } from '../components/Common'

export function LoginPage({ config }: { config: Config }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState<ApiError>()
  const [busy, setBusy] = useState(false)
  const navigate = useNavigate()
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError(undefined)
    try { await apiRequest('/api/admin/login', { method: 'POST', body: { password } }); navigate('/studies') }
    catch (value) { setError(value as ApiError) } finally { setBusy(false) }
  }
  return <div className="login-page">
    <section className="login-aside"><div className="brand inverse"><span className="brand-mark">P</span><span>ProbeFlow<small>中文深度访谈</small></span></div><div><p className="kicker">研究者工作台</p><h1>从真实经历中，<br />找到可核查的线索。</h1><p>访谈、原始记录、修订和引用保留在同一条证据链中。</p></div><ModeBadge mode={config.mode} /></section>
    <section className="login-form-wrap"><form className="login-card" onSubmit={submit}><div className="eyebrow">安全访问</div><h2>登录管理端</h2><p>使用初始化时设置的管理员密码。</p>{error && <ErrorPanel error={error} />}<label className="field"><span>管理员密码</span><input autoFocus type="password" value={password} onChange={event => setPassword(event.target.value)} placeholder="输入密码" /></label><button className="button primary wide" disabled={busy || !password}>{busy ? '正在验证…' : '进入工作台'}</button><div className="privacy-note">管理端仅供研究者使用，受访者通过专属邀请进入。</div></form></section>
  </div>
}
