import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
  ],
  server: {
    port: 5173,
    host: true,
    proxy: {
      '/api': {
        target: 'https://127.0.0.1:8000',
        secure: false,
        changeOrigin: true,
      },
      '/ws': {
        target: 'wss://127.0.0.1:8000',
        secure: false,
        ws: true,
      },
    },
  },
})
