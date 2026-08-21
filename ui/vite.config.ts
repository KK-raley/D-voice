import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8642",
      "/ws": { target: "ws://127.0.0.1:8642", ws: true },
    },
  },
});
