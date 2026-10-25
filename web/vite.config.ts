import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The browser app is built into web/dist, which the Python server serves. In development, Vite runs on 127.0.0.1:5173
// and forwards /api to the Python server on 127.0.0.1:8501 (the Origin check there allows a loopback page).
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: "http://127.0.0.1:8501", changeOrigin: false } },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2022",
    assetsInlineLimit: 0, // the page's CSP has no data: for fonts, so nothing is inlined
    modulePreload: { polyfill: false },
    cssCodeSplit: false,
    sourcemap: false,
  },
  test: { environment: "node", include: ["src/**/*.test.{ts,tsx}"] },
});
