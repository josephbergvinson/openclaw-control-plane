// Execute the pinned native plan, changing only its full-lint shard selection.
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export function adaptNativePlan(plan) {
  if (typeof plan?.summary !== "string" || !plan.summary || !Array.isArray(plan.commands) || !plan.commands.length) {
    throw new Error("Expected a nonempty native qualification plan");
  }
  let lintCount = 0;
  const commands = plan.commands.map((command) => {
    if (!command || typeof command.name !== "string" || !command.name
        || !Array.isArray(command.args) || !command.args.every((arg) => typeof arg === "string")
        || (command.bin !== undefined && (typeof command.bin !== "string" || !command.bin))
        || (command.coreTestCheck !== undefined && !["checkBoundary", "checkTypes"].includes(command.coreTestCheck))
        || (command.env !== undefined && (!command.env || typeof command.env !== "object" || Array.isArray(command.env)))) {
      throw new Error("Unsupported native command shape; refusing an incomplete alternate runner");
    }
    if (command.name !== "lint") return command;
    lintCount += 1;
    if (command.bin !== undefined || command.coreTestCheck !== undefined || command.args.length !== 1 || command.args[0] !== "lint") {
      throw new Error("Expected the native full-lint package command");
    }
    return { ...command, args: [...command.args, "--split-core", "--threads=1"] };
  });
  if (lintCount > 1) throw new Error("Native plan repeats its full-lint command");
  return commands;
}

function publicCommand(command) {
  return {
    name: command.name,
    bin: command.bin ?? "pnpm",
    args: command.args,
    environmentPassedUnchanged: true,
    environmentValuesRecorded: false,
    ...(command.coreTestCheck ? { coreTestCheck: command.coreTestCheck } : {}),
  };
}

export async function executeNativePlan(plan, {
  createPnpmManagedCommand,
  runManagedCommand,
  resolveLocalCheckEnv,
  coreTestCheck,
  record = () => {},
}) {
  const commands = adaptNativePlan(plan);
  if (commands.some((command) => command.coreTestCheck && typeof coreTestCheck?.[command.coreTestCheck] !== "function")) {
    throw new Error("Native core-test callbacks are unavailable; refusing alternate process execution");
  }
  record({ kind: "plan", original: plan.commands.map(publicCommand), adapted: commands.map(publicCommand) });
  for (const [index, command] of commands.entries()) {
    const managed = command.bin ? command : createPnpmManagedCommand(command);
    record({ kind: "started", index, ...publicCommand(command),
      executionKind: command.coreTestCheck ? "native-callback" : "managed-command",
      ...(!command.coreTestCheck ? { actualBin: managed.bin, actualArgs: managed.args } : {}) });
    const exitCode = command.coreTestCheck
      ? await coreTestCheck[command.coreTestCheck]()
      : await runManagedCommand({ bin: managed.bin, args: managed.args, env: managed.env ?? resolveLocalCheckEnv() });
    if (!Number.isInteger(exitCode) || exitCode < 0 || exitCode > 255) {
      throw new Error("Native managed command returned an invalid exit status");
    }
    record({ kind: "completed", index, name: command.name, exitCode });
    if (exitCode !== 0) return exitCode;
  }
  return 0;
}

async function main() {
  const [manifestFile, evidenceDirectory, ...extra] = process.argv.slice(2);
  if (!manifestFile || !evidenceDirectory || extra.length) {
    throw new Error("Usage: run-native-reference-checks.mjs RUNTIME_MANIFEST EVIDENCE_DIRECTORY (run from reconstructed checkout)");
  }
  const evidence = path.resolve(evidenceDirectory);
  mkdirSync(evidence, { recursive: false });
  const write = (name, value) => writeFileSync(path.join(evidence, name), `${JSON.stringify(value, null, 2)}\n`);
  const results = [];
  let exitCode = 1;
  let failure;
  write("status.json", { status: "running", completed: false });
  try {
    const manifest = JSON.parse(readFileSync(manifestFile, "utf8"));
    const base = manifest.upstream?.commit;
    const expectedTree = manifest.source?.normalizedTree;
    if (!/^[0-9a-f]{40}$/.test(base ?? "") || !/^[0-9a-f]{40}$/.test(expectedTree ?? "")) {
      throw new Error("Runtime manifest has invalid source identities");
    }
    const git = (...args) => execFileSync("git", args, { encoding: "utf8" }).trim();
    const source = { base, head: git("rev-parse", "HEAD"), tree: git("rev-parse", "HEAD^{tree}") };
    if (source.tree !== expectedTree || git("status", "--porcelain", "--untracked-files=no")) {
      throw new Error("Reconstructed tracked source differs from the pinned manifest");
    }
    write("source.json", source);
    const nativeImport = (name) => import(pathToFileURL(path.resolve("scripts", name)).href);
    const { listChangedPathsFromGit, detectChangedLanesForPaths, getChangedCoreTestPaths } = await nativeImport("changed-lanes.mts");
    const { createChangedCheckPlan, createPnpmManagedCommand } = await nativeImport("check-changed.mts");
    const { runManagedCommand } = await nativeImport("lib/managed-child-process.mts");
    const { resolveLocalCheckEnv } = await nativeImport("lib/local-check-runtime.mts");
    const paths = listChangedPathsFromGit({ base, head: "HEAD" });
    const lanes = detectChangedLanesForPaths({ paths, base, head: "HEAD" });
    const childEnv = resolveLocalCheckEnv(process.env);
    const plan = createChangedCheckPlan(lanes, { base, head: "HEAD", env: childEnv });
    let coreTestCheck;
    if (plan.commands.some((command) => command.coreTestCheck)) {
      const corePaths = getChangedCoreTestPaths(lanes);
      if (!corePaths) throw new Error("Native core-test selection is unavailable");
      const { createChangedCoreTestCheck } = await nativeImport("run-tsgo-core-test-shards.mts");
      const { createSparseTsgoSkipEnv } = await nativeImport("lib/tsgo-sparse-guard.mts");
      coreTestCheck = createChangedCoreTestCheck(corePaths, createSparseTsgoSkipEnv(childEnv));
    }
    write("changed-paths.json", paths);
    exitCode = await executeNativePlan(plan, {
      createPnpmManagedCommand,
      runManagedCommand,
      resolveLocalCheckEnv,
      coreTestCheck,
      record(event) {
        if (event.kind === "plan") {
          write("command-plan.json", event);
          return;
        }
        results.push(event);
        write("command-results.json", results);
        console.error(`[reference:native] ${event.kind} ${event.index + 1}: ${event.name}${event.exitCode === undefined ? "" : ` (exit ${event.exitCode})`}`);
      },
    });
  } catch (error) {
    failure = error instanceof Error ? error.message : "Native qualification failed";
    console.error(failure);
    exitCode = 1;
  } finally {
    write("status.json", { status: exitCode === 0 ? "passed" : "failed", completed: true, exitCode, ...(failure ? { failure } : {}) });
  }
  process.exitCode = exitCode;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
