import { useCallback, useEffect, useState } from 'react'
import { indexedChunkStorage } from '../lib/recorder'

export function WithdrawalComplete({ turnIds }: { turnIds: string[] }) {
  const [busy, setBusy] = useState(true)
  const [failed, setFailed] = useState(false)
  const clearLocalAudio = useCallback(async () => {
    setBusy(true); setFailed(false)
    const results = await Promise.allSettled(turnIds.map(async id => {
      const chunks = await indexedChunkStorage.list(id)
      const removals = await Promise.allSettled(chunks.map(chunk => indexedChunkStorage.remove(id, chunk.seq)))
      if (removals.some(result => result.status === 'rejected')) throw new Error('Local cleanup incomplete')
    }))
    setFailed(results.some(result => result.status === 'rejected'))
    setBusy(false)
  }, [turnIds])
  useEffect(() => { void clearLocalAudio() }, [clearLocalAudio])
  return <section className="finished-card">
    <h1>本场访谈已撤回</h1>
    <p>服务器已删除本场访谈资料，并撤销访问凭证。供应商侧数据依其实际政策处理。</p>
    {busy ? <p role="status">正在清理本机录音暂存…</p> : failed ? <div className="warning-banner" role="alert">
      <p>本机录音暂存未能全部清理，可能仍留在此浏览器中。服务器撤回已完成，无需再次撤回；请在关闭页面前重试清理。</p>
      <button className="button secondary" onClick={clearLocalAudio}>重新清理本机暂存</button>
    </div> : <p role="status">本场在此浏览器中的录音暂存已清理。</p>}
  </section>
}
