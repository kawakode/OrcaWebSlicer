import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built bundle is served by the API itself, so the app always talks to its
// own origin. Only the dev server needs to be told where the API is.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
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
