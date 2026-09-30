import { configDefaults, defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";

// The built bundle is packed into the Python wheel (§9). Output straight into
// the package so `importlib`/FileResponse can serve it; `base: "/"` keeps asset
// URLs absolute, which the SPA catch-all in `serve/app.py` serves.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/",
  resolve: {
    // `@/` -> src/, mirrored by `paths` in tsconfig.json.
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    // From src/playground/, `..` is src/, so this resolves to src/whygraph/serve/static.
    outDir: fileURLToPath(new URL("../whygraph/serve/static", import.meta.url)),
    emptyOutDir: true,
  },
  server: {
    // `npm run dev` proxies API and MCP calls to the dev portal (`make dev`).
    // changeOrigin rewrites Host to the portal's own (else its guard answers
    // 421); the forwarded Origin (http://localhost:5173) is allowed by the
    // WHYGRAPH_DEV_ORIGINS that `make dev` sets (else 403).
    proxy: {
      "/api": { target: "http://127.0.0.1:8765", changeOrigin: true },
      "/mcp": { target: "http://127.0.0.1:8765", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    // The Playwright suite (`make e2e`) lives in e2e/ and is not a Vitest suite.
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
