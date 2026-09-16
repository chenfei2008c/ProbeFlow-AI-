import { apiRequest } from './api'

type Exchange = (path: string, options: { method: string; body: { token: string } }) => Promise<{ session_id: string }>

export async function exchangeInviteFragment(exchange: Exchange = apiRequest) {
  const fragment = new URLSearchParams(location.hash.slice(1))
  const token = fragment.get('token')
  history.replaceState(history.state, '', `${location.pathname}${location.search}`)
  if (!token) throw new Error('邀请链接缺少凭证')
  return exchange('/api/participant/exchange', { method: 'POST', body: { token } })
}
