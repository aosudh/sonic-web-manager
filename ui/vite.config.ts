import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8765",
        changeOrigin: true,
        configure(proxy) {
          proxy.on("proxyReq", (request, incoming) => {
            // Only bridge our exact loopback dev origin. Foreign origins must
            // remain unchanged so the backend's same-origin check rejects them.
            if (incoming.headers.origin === "http://127.0.0.1:5173") {
              request.setHeader("Origin", "http://127.0.0.1:8765");
            }
          });
        },
      },
    },
  },
  build: { target: "es2022", sourcemap: false },
});
