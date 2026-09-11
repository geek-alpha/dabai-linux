// Vite dev 配置 —— 阶段 0（方案 B 渐进迁移）
// 用法：
//   后端需已启动（默认 https://localhost:8000，可用 DABAI_BACKEND 环境变量覆盖）
//   npm run dev  →  http://localhost:5173
// 不改动现有 Python 部署：生产仍由 server.py 直接服务 web/ 目录。
import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const rootDir = path.dirname(fileURLToPath(import.meta.url));
const backend = process.env.DABAI_BACKEND || 'https://localhost:8000';

// /static/xxx → web/xxx：兼容 index.html 里 /static/app.ts 的既有绝对路径
// dev：中间件改写请求前缀 + resolveId 兜底（Vite 预热入口时绕过中间件直接解析）；
// build：改写 index.html 里的引用为相对路径以便打包入口识别
function staticPrefixRewrite() {
  return {
    name: 'dabai-static-prefix-rewrite',
    enforce: 'pre',
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        if (req.url && req.url.startsWith('/static/')) {
          req.url = req.url.slice('/static'.length);
        }
        next();
      });
    },
    resolveId(id) {
      if (id.startsWith('/static/')) {
        // 剥离查询串并转绝对路径，否则 fs 查找 app.ts?v=161 失败
        const clean = id.slice('/static/'.length).split('?')[0];
        return path.resolve(rootDir, 'web', clean);
      }
      return null;
    },
    transformIndexHtml(html) {
      return html.replaceAll('/static/', './');
    },
  };
}

export default defineConfig(({ command }) => ({
  root: 'web',
  // build 用相对 base：产物可挂载到任意路径（含 server.py 的 /static），资源引用全部相对化；
  // dev 保持根路径 '/'
  base: command === 'build' ? './' : '/',
  plugins: [staticPrefixRewrite()],
  // 产物输出 web/dist（`npm run build`；相对 base 下 HTML 与 JS 动态分包引用均为 ./assets/...，
  // 与 server.py 的 /static 挂载天然兼容）
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  resolve: {
    alias: {
      'three/addons/': path.resolve(rootDir, 'node_modules/three/examples/jsm/'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/ws': { target: backend, ws: true, secure: false, changeOrigin: true },
      '/api': { target: backend, secure: false, changeOrigin: true },
      '/audio': { target: backend, secure: false, changeOrigin: true },
      '/models': { target: backend, secure: false, changeOrigin: true },
      '/backgrounds': { target: backend, secure: false, changeOrigin: true },
      '/generated': { target: backend, secure: false, changeOrigin: true },
    },
  },
}));
