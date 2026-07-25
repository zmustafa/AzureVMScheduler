import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { AuthProvider } from './auth'
import { TimeProvider } from './lib/time'
import './index.css'

const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 15_000, retry: 1 }, mutations: { retry: 0 } } })
createRoot(document.getElementById('root')!).render(<StrictMode><QueryClientProvider client={queryClient}><BrowserRouter><AuthProvider><TimeProvider><App/></TimeProvider></AuthProvider></BrowserRouter></QueryClientProvider></StrictMode>)
