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
    rollupOptions: {
      output: {
        // Markdown 渲染栈（react-markdown + unified 生态 + highlight.js）体积远大于应用
        // 本体（约 +120KB gzip），单独拆成 chunk：入口保持精简、构建产物不再触发
        // 500KB 警告。桌面端是本地加载，多一个并行请求没有代价。
        // 取舍见 plans/enhancement/03-markdown-rendering.md §7。
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          // React 运行时单独一块（几乎不随业务变化，可长期缓存）
          if (/[\\/]node_modules[\\/](react|react-dom|scheduler)[\\/]/.test(id)) return 'react'
          // 其余第三方依赖（Markdown / 高亮栈占大头）合成一块，避免单文件过大
          return 'vendor'
        },
      },
    },
  },
})
