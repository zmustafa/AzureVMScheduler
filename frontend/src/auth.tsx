import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { api, json } from './api'
import type { User } from './types'

type AuthValue = { user: User | null; loading: boolean; login: (username: string, password: string) => Promise<void>; logout: () => Promise<void>; refresh: () => Promise<void> }
const AuthContext = createContext<AuthValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)
  const refresh = async () => { try { const data = await api<{ user: User }>('/auth/me'); setUser(data.user) } catch { setUser(null) } finally { setLoading(false) } }
  useEffect(() => { void refresh() }, [])
  const login = async (username: string, password: string) => { const data = await api<{ user: User }>('/auth/login', json('POST', { username, password })); setUser(data.user) }
  const logout = async () => { await api('/auth/logout', json('POST')); setUser(null) }
  return <AuthContext.Provider value={{ user, loading, login, logout, refresh }}>{children}</AuthContext.Provider>
}

export function useAuth() { const value = useContext(AuthContext); if (!value) throw new Error('AuthProvider is missing'); return value }

/** Permission check against the role permissions returned by /auth/me ('*' means administrator). */
export function hasPermission(user: User | null, permission: string): boolean {
  return !!user && (user.permissions?.includes('*') || user.permissions?.includes(permission))
}

export function useCan(permission: string): boolean {
  const { user } = useAuth()
  return hasPermission(user, permission)
}
