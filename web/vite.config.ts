import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development, proxy API calls to the gateway (via nginx on :8000) so the UI and the API
// share an origin, exactly as in production where nginx serves both.
const gateway = "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      ["/v1", "/admin", "/demo", "/healthz", "/readyz", "/metrics"].map((path) => [path, gateway]),
    ),
  },
});
