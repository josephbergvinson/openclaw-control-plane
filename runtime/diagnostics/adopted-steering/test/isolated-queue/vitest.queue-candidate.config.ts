import { fileURLToPath } from "node:url";
import { defineConfig } from "../../node_modules/vitest/dist/config.js";
import baselineConfig from "./vitest.queue-unit-dormant-boundary.config.ts";

// Reuse the proven native baseline configuration in a fresh process and cache.
export default defineConfig({
  ...baselineConfig,
  cacheDir: fileURLToPath(new URL(".vite-queue-candidate", import.meta.url)),
  test: {
    ...baselineConfig.test,
    name: "isolated-adopted-drain-candidate",
    include: [
      "adopted-drain-steering.unit-dormant-boundary.test.ts",
      "adopted-drain-steering.owner-fifo.test.ts",
    ],
  },
});
