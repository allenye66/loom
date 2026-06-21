import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// Dev: Vite on :5173 proxies /api to the loom server (:8787).
// Prod: `vite build` -> dist/, which the loom server serves at / (same origin).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8787', ws: true } },
  },
  build: { outDir: 'dist', emptyOutDir: true },
});
