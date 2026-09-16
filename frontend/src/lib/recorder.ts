export const MAX_BROWSER_CACHE_BYTES = 100 * 1024 * 1024

export const RECORDER_MIME_TYPES = [
  'audio/webm;codecs=opus',
  'audio/mp4',
  'audio/webm',
  'audio/ogg;codecs=opus',
]

export function detectRecorderMimeType(supports: (type: string) => boolean = MediaRecorder.isTypeSupported) {
  const mime = RECORDER_MIME_TYPES.find(supports)
  if (!mime) throw new Error('当前浏览器没有可用的录音格式，请改用文字回答')
  return mime
}

export function stopMediaStream(stream?: MediaStream) {
  stream?.getTracks().forEach(track => track.stop())
}

export function bindRecordingSafety(onPause: (reason: string) => void) {
  const pageHide = () => onPause('页面即将关闭，录音已停止')
  const offline = () => onPause('网络已断开，录音已安全暂停')
  const hidden = () => { if (document.hidden) onPause('页面进入后台，录音已安全暂停') }
  window.addEventListener('pagehide', pageHide)
  window.addEventListener('offline', offline)
  document.addEventListener('visibilitychange', hidden)
  return () => {
    window.removeEventListener('pagehide', pageHide)
    window.removeEventListener('offline', offline)
    document.removeEventListener('visibilitychange', hidden)
  }
}

export interface StoredChunk { turnId: string; seq: number; blob: Blob; mimeType: string }
export interface ChunkStorage {
  usage(): Promise<number>
  put(chunk: StoredChunk): Promise<void>
  remove(turnId: string, seq: number): Promise<void>
  list(turnId: string): Promise<StoredChunk[]>
}
export interface ChunkAck { seq: number; sha256: string }

interface CoordinatorOptions {
  consented: boolean
  storage: ChunkStorage
  upload(chunk: StoredChunk): Promise<ChunkAck>
  onSafetyPause?: (reason: string) => void
}

export class RecordingCoordinator {
  private queue: StoredChunk[] = []
  private recorder?: MediaRecorder
  private stream?: MediaStream
  private seq = 0
  private turnId = ''
  private stopped = false
  private pending = new Set<Promise<void>>()
  private uploading?: Promise<void>
  readonly hashes: ChunkAck[] = []

  constructor(private options: CoordinatorOptions) {}

  async ensureCapacity(incomingBytes = 0) {
    if (await this.options.storage.usage() + incomingBytes >= MAX_BROWSER_CACHE_BYTES) {
      throw new Error('浏览器暂存已达到 100MB，已停止新的录音')
    }
  }

  async start(stream: MediaStream, turnId = '') {
    if (!this.options.consented) throw new Error('尚未完成语音处理与永久保存同意')
    await this.ensureCapacity()
    this.turnId = turnId
    this.stream = stream
    this.stopped = false
    this.seq = 0
    const mimeType = detectRecorderMimeType()
    this.recorder = new MediaRecorder(stream, { mimeType })
    this.recorder.addEventListener('dataavailable', (event) => {
      if (event.data.size) void this.acceptChunk(this.turnId, event.data, this.seq++).catch(error => this.safetyPause(error instanceof Error ? error.message : '录音暂存失败'))
    })
    stream.getTracks().forEach((track) => track.addEventListener('ended', () => this.safetyPause('麦克风连接已中断')))
    this.recorder.start(2000)
    return mimeType
  }

  stop() {
    this.stopped = true
    if (this.recorder?.state === 'recording') this.recorder.stop()
  }

  safetyPause(reason: string) {
    this.stop()
    stopMediaStream(this.stream)
    this.options.onSafetyPause?.(reason)
  }

  acceptChunk(turnId: string, blob: Blob, seq: number) {
    const task = (async () => {
      await this.ensureCapacity(blob.size)
      const chunk = { turnId, blob, seq, mimeType: blob.type }
      await this.options.storage.put(chunk)
      this.queue.push(chunk)
      void this.pump().catch(error => this.safetyPause(error instanceof Error ? error.message : '上传暂停，录音块仍保存在本机'))
    })()
    this.pending.add(task)
    void task.then(() => this.pending.delete(task), () => this.pending.delete(task))
    return task
  }

  async flush() {
    await Promise.all([...this.pending])
    await this.pump()
    return this.hashes
  }

  private pump(): Promise<void> {
    if (this.uploading) return this.uploading
    this.uploading = (async () => {
      this.queue.sort((a, b) => a.seq - b.seq)
      while (this.queue.length) {
        const chunk = this.queue[0]
        const ack = await this.options.upload(chunk)
        if (!this.hashes.some(item => item.seq === ack.seq)) this.hashes.push(ack)
        // Keep the persisted chunk until the full manifest has been finalized.
        this.queue.shift()
      }
    })().finally(() => { this.uploading = undefined })
    return this.uploading
  }

  async complete() {
    for (const chunk of await this.options.storage.list(this.turnId)) {
      await this.options.storage.remove(chunk.turnId, chunk.seq)
    }
  }

  async stopAndFlush() {
    if (this.recorder?.state === 'recording') {
      await new Promise<void>(resolve => {
        this.recorder!.addEventListener('stop', () => resolve(), { once: true })
        this.stop()
      })
    }
    return this.flush()
  }

  async resume(turnId: string) {
    this.turnId = turnId
    const stored = await this.options.storage.list(turnId)
    this.queue.push(...stored)
    return this.flush()
  }

  get isStopped() { return this.stopped }
  get currentTurnId() { return this.turnId }
}

const DATABASE = 'probeflow-recorder-v1'
const STORE = 'chunks'

function openDb() {
  return new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(DATABASE, 1)
    request.onupgradeneeded = () => request.result.createObjectStore(STORE, { keyPath: ['turnId', 'seq'] })
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
}

export const indexedChunkStorage: ChunkStorage = {
  async usage() {
    const chunks = await allChunks()
    return chunks.reduce((total, item) => total + item.blob.size, 0)
  },
  async put(chunk) { await tx('readwrite', store => store.put(chunk)) },
  async remove(turnId, seq) { await tx('readwrite', store => store.delete([turnId, seq])) },
  async list(turnId) { return (await allChunks()).filter(item => item.turnId === turnId).sort((a, b) => a.seq - b.seq) },
}

async function tx(mode: IDBTransactionMode, action: (store: IDBObjectStore) => IDBRequest) {
  const db = await openDb()
  return new Promise<void>((resolve, reject) => {
    const transaction = db.transaction(STORE, mode)
    action(transaction.objectStore(STORE))
    transaction.oncomplete = () => { db.close(); resolve() }
    transaction.onerror = () => { db.close(); reject(transaction.error) }
  })
}

async function allChunks() {
  const db = await openDb()
  return new Promise<StoredChunk[]>((resolve, reject) => {
    const request = db.transaction(STORE).objectStore(STORE).getAll()
    request.onsuccess = () => { db.close(); resolve(request.result as StoredChunk[]) }
    request.onerror = () => { db.close(); reject(request.error) }
  })
}

export async function sha256(blob: Blob) {
  const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer())
  return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('')
}
