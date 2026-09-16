import { exchangeInviteFragment } from '../lib/invite'

test('invite exchange erases the fragment immediately and never persists the token', async () => {
  history.replaceState({}, '', '/join#token=secret-invite')
  const calls: string[] = []
  await exchangeInviteFragment(async (_path, options) => {
    calls.push((options.body as { token: string }).token)
    return { session_id: 'session-1' }
  })
  expect(calls).toEqual(['secret-invite'])
  expect(location.hash).toBe('')
  expect(localStorage.length).toBe(0)
  expect(sessionStorage.length).toBe(0)
})
