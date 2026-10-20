import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// 开发机连机器人时，后端不在本机：用 ROBOT_GUI_BACKEND 指向机器人地址。
// 不设置时默认本机 8000（机器人本机部署的也是这个地址）。
const backend = process.env.ROBOT_GUI_BACKEND ?? "http://127.0.0.1:8000";
const websocketBackend = backend.replace(/^http/, "ws");

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": backend,
      "/ws": {
        target: websocketBackend,
        ws: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
  },
});
