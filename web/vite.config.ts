import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { resolveQuantosApiTarget } from "./config/proxy.ts";

const proxy = {
  "/v1": { target: resolveQuantosApiTarget(), changeOrigin: false },
};

export default defineConfig({
  plugins: [react()],
  server: { host: "127.0.0.1", port: 5173, proxy },
  preview: { host: "127.0.0.1", port: 4173, proxy },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    css: true,
  },
});
