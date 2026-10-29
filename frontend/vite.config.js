import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  base: "/static/generation-app/",
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/tasks": "http://127.0.0.1:8765",
      "/groups": "http://127.0.0.1:8765",
      "/static": "http://127.0.0.1:8765",
    },
  },
  test: {
    environment: "happy-dom",
  },
  build: {
    outDir: "../src/medical_qa_data_factory/web/static/generation-app",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        entryFileNames: "app.js",
        chunkFileNames: "chunks/[name]-[hash].js",
        assetFileNames: (assetInfo) =>
          assetInfo.names?.some((name) => name.endsWith(".css"))
            ? "app.css"
            : "assets/[name]-[hash][extname]",
      },
    },
  },
});
