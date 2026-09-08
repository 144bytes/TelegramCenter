import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // relative asset URLs so the bundle works from any mount point
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // one JS file and one CSS file keeps the PyInstaller bundle simple
    assetsInlineLimit: 4096,
  },
});
