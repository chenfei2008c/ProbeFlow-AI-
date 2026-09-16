import { sessionTimeBoundary } from '../lib/sessionSafety'

test('session time boundary warns ten minutes before the hard limit', () => {
  expect(sessionTimeBoundary(80 * 60, 90 * 60)).toEqual(expect.objectContaining({ level: 'warning', canExtend: false }))
})

test('session time boundary blocks starting another answer at 90 minutes', () => {
  expect(sessionTimeBoundary(90 * 60, 90 * 60)).toEqual(expect.objectContaining({ level: 'limit', blockNewAnswer: true, canExtend: false }))
})

test('target duration can be extended by ten minutes before the hard limit', () => {
  expect(sessionTimeBoundary(60 * 60, 60 * 60)).toEqual(expect.objectContaining({ level: 'target', canExtend: true, blockNewAnswer: true }))
  expect(sessionTimeBoundary(80 * 60, 80 * 60)).toEqual(expect.objectContaining({ level: 'target', canExtend: true, blockNewAnswer: true }))
})
