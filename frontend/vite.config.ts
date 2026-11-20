// @ts-expect-error -- Node built-in; the frontend doesn't install @types/node for one config line
import { realpathSync } from "node:fs";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

declare const process: { cwd(): string; chdir(dir: string): void };

// Started through a junction or symlink (e.g. C:\yuuhi\int3132\PhosphoChat -> the real folder),
// Vite's dependency optimizer compares esbuild's real paths with paths relative to the linked
// working directory, finds nothing and crashes ("Cannot read properties of undefined (reading
// 'imports')"). Work from the real folder instead.
const realCwd = realpathSync.native(process.cwd());
if (realCwd !== process.cwd()) process.chdir(realCwd);

export default defineConfig({
  root: realCwd,
  plugins: [react()],
  // Cytoscape (~500 kB) is split into its own chunk and only loaded when a network is shown.
  build: { chunkSizeWarningLimit: 600 },
  // Pre-bundle every dependency at startup. Otherwise the lazily loaded network view makes Vite
  // discover cytoscape mid-session and re-optimize, which crashes Vite 6.0.x
  // ("Cannot read properties of undefined (reading 'imports')").
  optimizeDeps: {
    include: ["react", "react-dom/client", "react-router-dom", "react-markdown", "remark-gfm",
              "cytoscape", "cytoscape-fcose"],
  },
  server: {
    port: 5173,
    // changeOrigin keeps Host = localhost:8000; the browser's Origin header stays
    // http://localhost:5173, so the backend's same-origin check would refuse POSTs.
    // Map only this dev server's own origin to the target; any other site's Origin is passed
    // through unchanged so the backend still refuses it.
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on("proxyReq", (req) => {
            const origin = String(req.getHeader("origin") ?? "");
            if (/^http:\/\/(localhost|127\.0\.0\.1):5173$/.test(origin)) {
              req.setHeader("origin", "http://localhost:8000");
            }
          });
        },
      },
    },
  },
});
