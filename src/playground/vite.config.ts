import { defineConfig } from "vitest/config";
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
    // `npm run dev` proxies API calls to a running `whygraph serve` (§9.3 dev flow).
    proxy: {
      "/api": "http://localhost:8765",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
