import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
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
