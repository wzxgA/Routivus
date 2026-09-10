import { useEffect, useState } from 'react'

export type ProjectView = 'chat' | 'notes'

export type Route =
  | { kind: 'home' }
  | { kind: 'notes' }
  | { kind: 'config' }
  | { kind: 'project'; projectId: string; view: ProjectView; sessionId: string | null }

export const HOME: Route = { kind: 'home' }
export const GLOBAL_NOTES: Route = { kind: 'notes' }
export const CONFIG: Route = { kind: 'config' }

export function projectRoute(
  projectId: string,
  view: ProjectView = 'chat',
  sessionId: string | null = null,
): Route {
  return { kind: 'project', projectId, view, sessionId }
}

export function parseRoute(hash: string): Route {
  const raw = hash.replace(/^#\/?/, '').replace(/\/+$/, '')
  if (!raw) return HOME
  const parts = raw.split('/').filter(Boolean)
  if (parts[0] === 'notes') return GLOBAL_NOTES
  if (parts[0] === 'config') return CONFIG
  if (parts[0] === 'p' && parts[1]) {
    const projectId = decodeURIComponent(parts[1])
    if (parts[2] === 'notes') return { kind: 'project', projectId, view: 'notes', sessionId: null }
    if (parts[2] === 's' && parts[3]) {
      return { kind: 'project', projectId, view: 'chat', sessionId: decodeURIComponent(parts[3]) }
    }
    return { kind: 'project', projectId, view: 'chat', sessionId: null }
  }
  return HOME
}

export function routeToHash(route: Route): string {
  switch (route.kind) {
    case 'notes':
      return '#/notes'
    case 'config':
      return '#/config'
    case 'project': {
      const base = `#/p/${encodeURIComponent(route.projectId)}`
      if (route.view === 'notes') return `${base}/notes`
      return route.sessionId ? `${base}/s/${encodeURIComponent(route.sessionId)}` : base
    }
    default:
      return '#/'
  }
}

export function navigate(route: Route): void {
  const next = routeToHash(route)
  if (window.location.hash === next) return
  window.location.hash = next
}

/** 不新增历史记录地改写地址（用于自动补全 sessionId 等派生路由）。 */
export function replaceRoute(route: Route): void {
  const next = routeToHash(route)
  if (window.location.hash === next) return
  window.location.replace(next)
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash))
  useEffect(() => {
    const handler = () => setRoute(parseRoute(window.location.hash))
    window.addEventListener('hashchange', handler)
    return () => window.removeEventListener('hashchange', handler)
  }, [])
  return route
}
