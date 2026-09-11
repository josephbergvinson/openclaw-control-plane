import { fileURLToPath } from "node:url";
import { defineConfig } from "../../node_modules/vitest/dist/config.js";
import { sharedVitestConfig } from "../../test/vitest/vitest.shared.config.ts";
const compiledSupportName = "openclaw:compiled-subprocesses";
if (sharedVitestConfig.plugins.filter((plugin) => plugin.name === compiledSupportName).length !== 1) {
  throw new Error("Expected exactly one native compiled-child support plugin");
}
export default defineConfig({
  ...sharedVitestConfig,
  // No case executes a child backend or consumes compiled child artifacts.
  // Preserve the schema plugin and every native test/alias/pool/setup option.
  plugins: sharedVitestConfig.plugins.filter((plugin) => plugin.name !== compiledSupportName),
  root: fileURLToPath(new URL(".", import.meta.url)),
  cacheDir: fileURLToPath(new URL(".vite-queue-unit", import.meta.url)),
  resolve: {
    ...sharedVitestConfig.resolve,
    // Resolve the test API to the same installed Vitest package as the native
    // runner; the API is not mocked.
    alias: [
      { find: /^vitest$/, replacement: fileURLToPath(new URL("../../node_modules/vitest/dist/index.js", import.meta.url)) },
      ...sharedVitestConfig.resolve.alias,
    ],
  },
  test: {
    ...sharedVitestConfig.test,
    name: "isolated-adopted-drain-unit-dormant-boundary",
    include: ["adopted-drain-steering.unit-dormant-boundary.test.ts"],
    dir: fileURLToPath(new URL(".", import.meta.url)),
    maxWorkers: 1,
    fileParallelism: false,
    testTimeout: 15000,
    hookTimeout: 15000,
    isolate: true,
  },
});
