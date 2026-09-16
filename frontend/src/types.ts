export type AppMode = 'mock' | 'live'
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'external_status_unknown' | 'cancelled'

export interface ProviderInfo { provider: string; model: string; region?: string; available?: boolean; configured?: boolean; name?: string }
export interface ProviderMap { asr: ProviderInfo; interview: ProviderInfo; tts: ProviderInfo; report: ProviderInfo }
export interface Config { mode: AppMode; consent_version: string; providers: ProviderMap; admin_initialized: boolean }
export interface Topic { id: string; title: string; research_question: string; priority: number; evidence_type: string; minutes: number }
export interface StudyConfig {
  title: string
  objective: string
  participant_description: string
  target_minutes: 15 | 30 | 45 | 60
  topics: Topic[]
  exclusions: string
  glossary: string[]
  budget_cny: number
  confirm_transcript: boolean
  tone: string
}
export interface Study { id: string; title: string; archived: boolean; current_version_id?: string; version_number?: number; version: StudyConfig; session_count: number; completed_count: number; total_cost_cny: number; updated_at: string }
export interface Session { id: string; study_id: string; status: string; participant_code: string; mode: 'text' | 'voice'; consent_version?: string; processing_consent: boolean; permanent_consent: boolean; active_seconds: number; target_seconds?: number; budget_cny: number; spent_cny: number; reserved_cny: number; pause_reason?: string; created_at: string; ended_at?: string; retention: 'permanent' }
export interface Revision { id: string; text: string; source: string; created_at: string }
export interface Turn { id: string; seq: number; role: 'assistant' | 'participant'; status: string; input_mode: 'text' | 'voice'; text: string; revision_id?: string; revisions: Revision[]; audio_asset_id?: string; audio_status?: string; action?: string; topic_id?: string; created_at: string; confirmed: boolean; played_complete?: boolean }
export interface Citation { turn_id: string; revision_id: string; start: number; end: number; quote: string }
export interface Report { id: string; version: number; status: string; source_updated: boolean; body: { summary?: string; findings?: Array<{ type: string; text: string; citations: Citation[] }>; limitations?: string[]; unanswered?: string[] }; markdown: string; citations: Citation[]; created_at: string }
export interface Job { id: string; kind: string; status: JobStatus; error_code?: string; error_message?: string; result?: unknown; created_at: string }
export interface Detail { session: Session; study: StudyConfig; turns: Turn[]; reports?: Report[]; jobs: Job[]; memory?: { topics: unknown[]; unresolved: unknown[] }; mode: AppMode; consent_version: string; providers: ProviderMap }
export interface ApiEvent { seq: number; type: string; payload: unknown; created_at: string }
export interface Diagnostics { mode: AppMode; providers: Record<string, unknown>; ffmpeg_available: boolean; recent_errors: Array<Record<string, unknown>>; status?: string; primary_bytes?: number; formal_bytes?: number; temp_bytes?: number; backup_bytes?: number; free_bytes?: number; minimum_free_bytes?: number; deletion_tombstones?: number | null; last_backup?: Record<string, unknown> | string | null; storage?: Record<string, unknown>; backup?: Record<string, unknown> }
export interface Usage { month_spent_cny: number; month_reserved_cny: number; monthly_limit_cny: number; entries: Array<Record<string, unknown>> }
