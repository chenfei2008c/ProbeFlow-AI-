export const money = (value?: number) => `¥${Number(value ?? 0).toFixed(2)}`
export const dateTime = (value?: string) => value ? new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) : '—'
export const duration = (seconds = 0) => `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${Math.floor(seconds % 60).toString().padStart(2, '0')}`

export const statusLabel: Record<string, string> = {
  pending_consent: '等待同意', ready: '准备开始', in_progress: '进行中', paused: '已暂停', finalizing: '正在收尾', completed: '已完成', withdrawn: '已撤回', expired: '已过期', deleted: '已删除',
  queued: '排队中', running: '处理中', succeeded: '已完成', failed: '失败', external_status_unknown: '外部状态未知', cancelled: '已取消',
  ready_to_record: '可以回答', recording: '录音中', uploading: '正在安全上传', transcribing: '正在转写', confirming: '等待确认', planning: '正在整理下一问', synthesizing: '正在准备语音', speaking: '正在播放',
}

export const actionLabel: Record<string, string> = {
  ask_background: '了解背景', ask_example: '了解具体经历', probe_detail: '追问细节', probe_explanation: '了解个人解释',
  clarify: '澄清含义', contrast: '比较情境', verify_summary: '核对理解', transition: '转换主题', close: '准备结束',
}
export const jobLabel: Record<string, string> = { decide: '准备下一问', asr: '语音转写', tts: '生成问题语音', report: '生成报告', outline: '生成提纲', summary: '整理工作记忆' }
export const pauseLabel: Record<string, string> = { user: '你已主动暂停', user_paused: '你已主动暂停', heartbeat_lost: '连接中断，已暂停新的处理', budget_exceeded: '预算不足，请联系研究者追加预算', provider_error: '服务处理失败，请查看提示后重试或改用手动输入' }
