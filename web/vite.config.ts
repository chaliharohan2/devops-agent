import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the UI talks to both agents through the Vite proxy:
//   /api/v1/*  -> server agent  (default http://127.0.0.1:8080)
//   /api/*     -> local agent   (default http://127.0.0.1:7600)
const SERVER = process.env.DEVOPS_SERVER_URL ?? "http://127.0.0.1:8080";
const LOCAL = process.env.DEVOPS_LOCAL_URL ?? "http://127.0.0.1:7600";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api/v1": { target: SERVER, changeOrigin: true },
      "/api": { target: LOCAL, changeOrigin: false },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
