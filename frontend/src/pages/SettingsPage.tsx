import { dateTime, money } from '../lib/format'
import { Diagnostics, Usage } from '../types'
import { ErrorPanel, Loading, ModeBadge, StatusBadge } from '../components/Common'
import { useApi } from '../hooks/useApi'
import { DiagnosticTts } from '../components/DiagnosticTts'

export function SettingsPage() {
  const diagnostics = useApi<Diagnostics>('/api/admin/diagnostics')
  const usage = useApi<Usage>('/api/admin/usage')
  if (diagnostics.loading || usage.loading) return <div className="page"><Loading label="正在检查运行环境…" /></div>
  if (!diagnostics.data || !usage.data) return <div className="page">{(diagnostics.error || usage.error) && <ErrorPanel error={(diagnostics.error || usage.error)!} retry={() => { void diagnostics.reload(); void usage.reload() }} />}</div>
  const { data: diag } = diagnostics, { data: bill } = usage
  const spent = Number(bill.month_spent_cny), reserved = Number(bill.month_reserved_cny), limit = Number(bill.monthly_limit_cny)
  const percent = Math.min(100, (spent + reserved) / Math.max(0.01, limit) * 100)
  return <div className="page settings-page"><div className="page-heading"><div><div className="eyebrow">运行环境</div><h1>设置与诊断</h1><p>这里显示实际配置与运行状态，不替代真实接口或设备验收。</p></div><button className="button ghost" onClick={() => { void diagnostics.reload(); void usage.reload() }}>重新检查</button></div>
    <div className="settings-grid"><section className="panel provider-panel"><div className="section-heading"><div><h2>供应商角色</h2><p>配置名经过脱敏，密钥不会回显。</p></div><ModeBadge mode={diag.mode} /></div><div className="provider-list">{Object.entries(diag.providers ?? {}).map(([role, value]) => { const provider = value as Record<string, unknown>; return <div className="provider-row" key={role}><div className="provider-icon">{role.slice(0, 1).toUpperCase()}</div><div><strong>{{ asr: '语音识别', interview: '访谈决策', tts: '语音合成', report: '报告生成' }[role] ?? role}</strong><span>{String(provider.name ?? provider.model ?? '未配置')}</span></div><span className="status">{diag.mode === 'mock' ? '模拟' : provider.configured ? '已配置 · 连接未验证' : '未配置凭证'}</span></div>})}</div><DiagnosticTts mode={diag.mode} /></section>
      <section className="panel budget-panel"><div className="section-heading"><div><h2>本月使用</h2><p>未知计费项保留预算预占。</p></div><strong>{money(spent + reserved)} / {money(limit)}</strong></div><div className="budget-ring" style={{ '--percent': `${percent * 3.6}deg` } as React.CSSProperties}><div><strong>{Math.round(percent)}%</strong><span>已用 + 预占</span></div></div><dl className="usage-breakdown"><div><dt>已结算</dt><dd>{money(spent)}</dd></div><div><dt>仍预占</dt><dd>{money(reserved)}</dd></div><div><dt>剩余额度</dt><dd>{money(Math.max(0, limit - spent - reserved))}</dd></div></dl><p className="fine-print">应用侧限额无法替代供应商账户控制；状态未知的请求可能已经产生费用。</p></section>
    </div>
    <div className="settings-grid lower"><section className="panel"><div className="section-heading"><div><h2>存储与备份</h2><p>正式档案不会通过自动清理释放空间。</p></div></div><div className="storage-list">{storageRows(diag).map(([name, value]) => <div key={name}><span>{name}</span><strong>{formatStorage(value)}</strong></div>)}</div><div className="retention-box"><strong>正式档案：永久</strong><p>结束、归档和长期未访问不会删除录音、文字修订、报告或引用。</p></div><div className="backup-status"><span>最近备份</span><strong>{backupLabel(diag.last_backup ?? diag.backup)}</strong><small>{diag.deletion_tombstones == null ? '' : `${diag.deletion_tombstones} 条删除清单记录`}</small></div></section>
      <section className="panel"><div className="section-heading"><div><h2>运行检查</h2><p>用于定位故障，不代表真人访谈已验收。</p></div></div><div className="check-list"><div><span>FFmpeg 音频工具</span><StatusBadge status={diag.ffmpeg_available ? 'succeeded' : 'failed'} /></div><div><span>运行模式</span><strong>{diag.mode === 'mock' ? '模拟' : '真实'}</strong></div></div><h3 className="subheading">最近错误</h3>{diag.recent_errors?.length ? <div className="error-log">{diag.recent_errors.map((item, index) => <div key={index}><strong>{String(item.code ?? 'ERROR')}</strong><span>{String(item.message ?? '')}</span><small>{String(item.request_id ?? '')}</small></div>)}</div> : <p className="muted">没有记录到最近错误。</p>}</section></div>
  </div>
}

function formatStorage(value: unknown) {
  const bytes = typeof value === 'number' ? value : Number((value as Record<string, unknown>)?.bytes ?? 0)
  if (!Number.isFinite(bytes)) return String(value)
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024).toFixed(1)} KB`
}

function storageRows(diag: Diagnostics): Array<[string, unknown]> {
  if (diag.storage && Object.keys(diag.storage).length) return Object.entries(diag.storage).map(([name, value]) => [({ archive: '正式档案', temporary: '临时文件', backup: '备份', available: '可用空间' } as Record<string, string>)[name] ?? name, value])
  return [['正式档案', diag.formal_bytes ?? 0], ['临时文件', diag.temp_bytes ?? 0], ['备份', diag.backup_bytes ?? 0], ['可用空间', diag.free_bytes ?? 0]]
}

function backupLabel(value: unknown) {
  if (!value) return '未运行'
  if (typeof value === 'string') return value
  const record = value as Record<string, unknown>
  const label = ({ ok: '备份成功', failed: '备份失败', never: '尚未备份' } as Record<string, string>)[String(record.status)] ?? '已有记录'
  return `${label}${record.created_at ? ` · ${dateTime(String(record.created_at))}` : ''}${record.code ? ` · ${record.code}` : ''}`
}
