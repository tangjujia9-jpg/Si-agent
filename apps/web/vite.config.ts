import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig(({ mode }) => {
  const backend = loadEnv(mode, '.', 'SI_').SI_API_URL || 'http://127.0.0.1:8000';
  return {
  plugins: [react()],
  server: { proxy: { '/api': backend, '/ready': backend } },
  };
});
