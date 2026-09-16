import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { App } from './App'
import { ErrorPanel, Loading } from './components/Common'
import { apiRequest } from './lib/api'
import { Config } from './types'
import './styles.css'

function Bootstrap() {
  const [config, setConfig] = useState<Config>()
  const [error, setError] = useState<Error>()
  const load = async () => { setError(undefined); try { setConfig(await apiRequest<Config>('/api/config')) } catch (value) { setError(value as Error) } }
  useEffect(() => { void load() }, [])
  if (error) return <div className="center-page cream"><ErrorPanel error={error} retry={load} /></div>
  if (!config) return <div className="center-page cream"><Loading label="正在启动 ProbeFlow…" /></div>
  return <App config={config} />
}

createRoot(document.getElementById('root')!).render(<StrictMode><BrowserRouter><Bootstrap /></BrowserRouter></StrictMode>)
