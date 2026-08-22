import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '')
  return {
    plugins: [react()],
    build: {
      // The vendor libraries change far less often than the app does, so keeping them in their own
      // chunks means a deploy invalidates the app chunk alone rather than the whole download.
      rollupOptions: {
        output: {
          manualChunks: {
            react: ['react', 'react-dom', 'react-router'],
            query: ['@tanstack/react-query'],
            icons: ['lucide-react'],
          },
        },
      },
      // Routes are split per page now, so anything this large again is a regression worth seeing.
      chunkSizeWarningLimit: 300,
    },
    server: {
      port: 5173,
      proxy: { '/api': { target: env.VITE_API_PROXY_TARGET || 'http://localhost:8000', changeOrigin: true } },
    },
  }
})
