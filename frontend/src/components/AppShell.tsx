import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { apiRequest } from '../lib/api'
import { Config } from '../types'
import { ModeBadge } from './Common'

const nav = [
  { to: '/studies', label: '研究', glyph: '◫' },
  { to: '/settings', label: '设置与诊断', glyph: '⌁' },
]

export function AppShell({ config }: { config: Config }) {
  const location = useLocation()
  const navigate = useNavigate()
  const logout = async () => { await apiRequest('/api/admin/logout', { method: 'POST', body: {} }); navigate('/login') }
  return <div className="app-shell">
    <aside className="sidebar">
      <NavLink className="brand" to="/studies"><span className="brand-mark">P</span><span>ProbeFlow<small>深度访谈工作台</small></span></NavLink>
      <nav>{nav.map(item => <NavLink className={({ isActive }) => isActive ? 'nav-item active' : 'nav-item'} to={item.to} key={item.to}><span>{item.glyph}</span>{item.label}</NavLink>)}</nav>
      <div className="sidebar-foot"><ModeBadge mode={config.mode} /><button className="text-button" onClick={logout}>退出管理端</button></div>
    </aside>
    <main className="workspace">
      <header className="topbar"><div><span className="crumb">工作台</span><strong>{location.pathname.startsWith('/settings') ? '系统设置' : location.pathname.includes('/sessions/') ? '访谈详情' : location.pathname === '/studies' ? '所有研究' : '研究设计'}</strong></div><div className="retention-pill"><span>●</span> 正式档案永久保存</div></header>
      <Outlet />
    </main>
  </div>
}
