import { useEffect, useState } from 'react'
import { Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { apiRequest } from './lib/api'
import { Config } from './types'
import { AppShell } from './components/AppShell'
import { Loading } from './components/Common'
import { LoginPage } from './pages/LoginPage'
import { ParticipantPage } from './pages/ParticipantPage'
import { SessionDetailPage } from './pages/SessionDetailPage'
import { SettingsPage } from './pages/SettingsPage'
import { StudiesPage } from './pages/StudiesPage'
import { StudyEditorPage } from './pages/StudyEditorPage'

export function App({ config }: { config: Config }) {
  return <Routes>
    <Route path="/login" element={config.admin_initialized ? <LoginPage config={config} /> : <AdminNotInitialized />} />
    <Route path="/join" element={<ParticipantPage config={config} />} />
    <Route element={<RequireAdmin><AppShell config={config} /></RequireAdmin>}>
      <Route path="/studies" element={<StudiesPage />} />
      <Route path="/studies/:id" element={<StudyEditorPage />} />
      <Route path="/sessions/:id" element={<SessionDetailPage />} />
      <Route path="/settings" element={<SettingsPage />} />
    </Route>
    <Route path="*" element={<Navigate to="/studies" replace />} />
  </Routes>
}

function RequireAdmin({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<'checking' | 'ready'>('checking')
  const navigate = useNavigate()
  useEffect(() => { apiRequest('/api/admin/me').then(() => setState('ready')).catch(() => navigate('/login', { replace: true })) }, [navigate])
  return state === 'ready' ? children : <div className="center-page"><Loading label="正在验证管理会话…" /></div>
}

function AdminNotInitialized() {
  return <div className="center-page cream"><section className="setup-card"><div className="brand"><span className="brand-mark">P</span><span>ProbeFlow</span></div><div className="setup-icon">⌘</div><div className="eyebrow">首次运行</div><h1>先创建管理员密码</h1><p>ProbeFlow 没有公开注册入口。请在项目目录打开终端，运行以下命令并按提示设置密码：</p><code>./scripts/probeflow init-admin</code><p className="fine-print">密码会在终端安全读取，不会显示或写入前端。完成后刷新此页面。</p><button className="button primary" onClick={() => location.reload()}>我已完成，重新检查</button></section></div>
}
