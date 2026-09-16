import { bindRecordingSafety, RecordingCoordinator, detectRecorderMimeType, MAX_BROWSER_CACHE_BYTES, stopMediaStream } from '../lib/recorder'

test('recorder refuses to start before current voice consent', async () => {
  const coordinator = new RecordingCoordinator({ consented: false, storage: { usage: async () => 0, put: vi.fn(), remove: vi.fn(), list: vi.fn(async () => []) }, upload: vi.fn() })
  await expect(coordinator.start({} as MediaStream)).rejects.toThrow('尚未完成语音处理与永久保存同意')
})

test('recorder chooses the first format actually supported by this browser', () => {
  const supported = new Set(['audio/mp4'])
  expect(detectRecorderMimeType((type) => supported.has(type))).toBe('audio/mp4')
})

test('chunks upload while recording but remain durable until whole-turn finalize', async () => {
  const order: string[] = []
  const storage = {
    usage: async () => 0,
    put: async (chunk: { seq: number }) => { order.push(`persist-${chunk.seq}`) },
    remove: async (_turnId: string, seq: number) => { order.push(`remove-${seq}`) },
    list: async () => [],
  }
  const upload = async (chunk: { seq: number }) => { order.push(`upload-${chunk.seq}`); return { seq: chunk.seq, sha256: `hash-${chunk.seq}` } }
  const coordinator = new RecordingCoordinator({ consented: true, storage, upload })
  await coordinator.acceptChunk('turn-1', new Blob(['first']), 0)
  await coordinator.acceptChunk('turn-1', new Blob(['second']), 1)
  await coordinator.flush()
  expect(order.indexOf('persist-0')).toBeLessThan(order.indexOf('upload-0'))
  expect(order.indexOf('persist-1')).toBeLessThan(order.indexOf('upload-1'))
  expect(order.indexOf('upload-0')).toBeLessThan(order.indexOf('upload-1'))
  expect(order.some(item => item.startsWith('remove'))).toBe(false)
  expect(coordinator.hashes).toEqual([{ seq: 0, sha256: 'hash-0' }, { seq: 1, sha256: 'hash-1' }])
})

test('refresh after acknowledged upload reconstructs the complete finalize manifest', async () => {
  const chunks = new Map<number, { turnId: string; seq: number; blob: Blob; mimeType: string }>()
  const storage = { usage: async () => 0, put: async (chunk: { turnId: string; seq: number; blob: Blob; mimeType: string }) => { chunks.set(chunk.seq, chunk) }, remove: async (_id: string, seq: number) => { chunks.delete(seq) }, list: async () => [...chunks.values()] }
  const upload = vi.fn(async (chunk: { seq: number }) => ({ seq: chunk.seq, sha256: `hash-${chunk.seq}` }))
  const before = new RecordingCoordinator({ consented: true, storage, upload })
  await before.acceptChunk('turn-1', new Blob(['first']), 0)
  await before.flush()
  expect(chunks.size).toBe(1)
  const after = new RecordingCoordinator({ consented: true, storage, upload })
  expect(await after.resume('turn-1')).toEqual([{ seq: 0, sha256: 'hash-0' }])
  await after.complete()
  expect(chunks.size).toBe(0)
})

test('flush waits for an in-flight IndexedDB write before uploading the final chunk', async () => {
  let finishPersist!: () => void
  const persisted = new Promise<void>(resolve => { finishPersist = resolve })
  const upload = vi.fn(async (chunk: { seq: number }) => ({ seq: chunk.seq, sha256: 'final-hash' }))
  const coordinator = new RecordingCoordinator({
    consented: true,
    storage: { usage: async () => 0, put: async () => persisted, remove: vi.fn(), list: vi.fn(async () => []) },
    upload,
  })
  const accepting = coordinator.acceptChunk('turn-1', new Blob(['final']), 0)
  const flushing = coordinator.flush()
  await Promise.resolve()
  expect(upload).not.toHaveBeenCalled()
  finishPersist()
  await Promise.all([accepting, flushing])
  expect(upload).toHaveBeenCalledTimes(1)
})

test('cache limit blocks another recording before browser storage exceeds 100MB', async () => {
  const coordinator = new RecordingCoordinator({ consented: true, storage: { usage: async () => MAX_BROWSER_CACHE_BYTES, put: vi.fn(), remove: vi.fn(), list: vi.fn(async () => []) }, upload: vi.fn() })
  await expect(coordinator.ensureCapacity()).rejects.toThrow('浏览器暂存已达到 100MB')
})

test('resume restores persisted chunks and uploads them in sequence order', async () => {
  const uploaded: number[] = []
  const stored = [
    { turnId: 'turn-1', seq: 2, blob: new Blob(['c']), mimeType: 'audio/webm' },
    { turnId: 'turn-1', seq: 0, blob: new Blob(['a']), mimeType: 'audio/webm' },
    { turnId: 'turn-1', seq: 1, blob: new Blob(['b']), mimeType: 'audio/webm' },
  ]
  const coordinator = new RecordingCoordinator({ consented: true, storage: { usage: async () => 3, put: vi.fn(), remove: vi.fn(), list: async () => stored }, upload: async chunk => { uploaded.push(chunk.seq); return { seq: chunk.seq, sha256: `hash-${chunk.seq}` } } })
  await coordinator.resume('turn-1')
  expect(uploaded).toEqual([0, 1, 2])
})

test('releasing a microphone stops every media track', () => {
  const tracks = [{ stop: vi.fn() }, { stop: vi.fn() }]
  stopMediaStream({ getTracks: () => tracks } as unknown as MediaStream)
  expect(tracks.every(track => track.stop.mock.calls.length === 1)).toBe(true)
})

test('page hide and network loss trigger recorder safety pause', () => {
  const pause = vi.fn()
  const unbind = bindRecordingSafety(pause)
  window.dispatchEvent(new Event('pagehide'))
  window.dispatchEvent(new Event('offline'))
  expect(pause).toHaveBeenNthCalledWith(1, '页面即将关闭，录音已停止')
  expect(pause).toHaveBeenNthCalledWith(2, '网络已断开，录音已安全暂停')
  unbind()
})
