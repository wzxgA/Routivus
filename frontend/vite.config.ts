import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 后端默认监听 127.0.0.1:18765（见 routivus/server/config.py）。
// 需要指向其他地址时设置 ROUTIVUS_SERVER_URL 环境变量即可。
const target = process.env.ROUTIVUS_SERVER_URL ?? 'http://127.0.0.1:18765'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5183,
    strictPort: false,
    proxy: {
      '/api': { target, changeOrigin: true, ws: true },
      '/healthz': { target, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
