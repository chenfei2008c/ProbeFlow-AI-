const HARD_LIMIT_SECONDS = 90 * 60
const HARD_LIMIT_WARNING_SECONDS = 80 * 60

export type TimeBoundary = {
  level: 'normal' | 'target' | 'warning' | 'limit'
  message: string
  canExtend: boolean
  blockNewAnswer: boolean
}

export function sessionTimeBoundary(activeSeconds: number, targetSeconds: number): TimeBoundary {
  if (activeSeconds >= HARD_LIMIT_SECONDS) return {
    level: 'limit',
    message: '已达到 90 分钟安全上限。可以完成正在进行的回答，但不能开始新一轮，请结束访谈。',
    canExtend: false,
    blockNewAnswer: true,
  }
  if (activeSeconds >= targetSeconds) return {
    level: 'target',
    message: '已达到本场目标时长。可以完成当前回答；继续新一轮前请明确选择延长 10 分钟。',
    canExtend: targetSeconds < HARD_LIMIT_SECONDS,
    blockNewAnswer: true,
  }
  if (activeSeconds >= HARD_LIMIT_WARNING_SECONDS) return {
    level: 'warning',
    message: `距离 90 分钟安全上限还剩约 ${Math.max(1, Math.ceil((HARD_LIMIT_SECONDS - activeSeconds) / 60))} 分钟。请开始收尾。`,
    canExtend: targetSeconds < HARD_LIMIT_SECONDS,
    blockNewAnswer: false,
  }
  return { level: 'normal', message: '', canExtend: false, blockNewAnswer: false }
}
