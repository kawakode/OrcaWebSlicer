import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built bundle is served by the API itself, so the app always talks to its
// own origin. Only the dev server needs to be told where the API is.
export default defineConfig({
  plugins: [react()],
  // three.js alone is most of the bundle; splitting it would only add a request.
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 1024 },
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.ORCA_WEB_API_URL ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
