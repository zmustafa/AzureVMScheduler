function cookie(name: string) {
  return document.cookie.split('; ').find((item) => item.startsWith(`${name}=`))?.split('=').slice(1).join('=')
}

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase()
  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = cookie('azureops_csrf')
    if (csrf) headers.set('X-CSRF-Token', decodeURIComponent(csrf))
  }
  const response = await fetch(`/api${path}`, { ...init, headers, credentials: 'include' })
  if (!response.ok) {
    let message = response.statusText
    try {
      const data = await response.json()
      message = Array.isArray(data.detail) ? data.detail.map((item: { msg?: string }) => item.msg ?? String(item)).join(', ') : data.detail ?? message
    } catch { /* keep status text */ }
    throw new ApiError(response.status, message)
  }
  return response.status === 204 ? (undefined as T) : response.json()
}

export const json = (method: string, body?: unknown): RequestInit => ({ method, body: body === undefined ? undefined : JSON.stringify(body) })
