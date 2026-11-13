import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// https://vitejs.dev/config/
export default defineConfig(async () => ({
    plugins: [react()],
    clearScreen: false,
    build: { rollupOptions: { input: { main: 'index.html', manual: 'manual.html' } } },
    server: {
        port: 1420,
        strictPort: true,
        watch: {
            ignored: ["**/src-tauri/**"],
        },
    },
}));
