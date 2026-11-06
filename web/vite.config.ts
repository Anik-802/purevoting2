import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // dev only; the compose "web" service (nginx.conf) serves the same /v1/ paths on WEB_PORT
  server: { port: 5173, proxy: { "/v1": "http://127.0.0.1:8000" } },
});
