import assert from "node:assert/strict";
import test from "node:test";
import { adaptNativePlan, executeNativePlan } from "../scripts/run-native-reference-checks.mjs";

function fixture() {
  const env = { CI: "true", GOMAXPROCS: "2", PRIVATE_TEST_TOKEN: "never-record-this-value" };
  return {
    summary: "all",
    commands: [
      { name: "typecheck all", args: ["tsgo:all"], env },
      { name: "lint", args: ["lint"], env },
      { name: "runtime import cycles", args: ["check:import-cycles"], env },
      { name: "lint test root changed files", bin: "node", args: ["scripts/run-oxlint.mjs", "test/example.test.ts"], env },
    ],
  };
}

function fakeRuntime({ failAt = -1, exitCode = 143, throwAt = -1 } = {}) {
  const calls = [], packages = [], events = [];
  return {
    calls, packages, events,
    resolveLocalCheckEnv() { return { CI: "true", NATIVE_DEFAULT: "fixture" }; },
    createPnpmManagedCommand(command) {
      packages.push(command);
      return { ...command, bin: "corepack", args: ["pnpm", ...command.args] };
    },
    async runManagedCommand(command) {
      calls.push(command);
      if (calls.length - 1 === throwAt) throw new Error("fixture execution error");
      return calls.length - 1 === failAt ? exitCode : 0;
    },
    record(event) { events.push(event); },
  };
}

test("preserves every command, tail, argument and environment except the full-lint flags", async () => {
  const plan = fixture(), original = structuredClone(plan), runtime = fakeRuntime();
  assert.equal(await executeNativePlan(plan, runtime), 0);
  assert.deepEqual(plan, original);
  assert.deepEqual(runtime.calls.map(({ bin, args }) => ({ bin, args })), [
    { bin: "corepack", args: ["pnpm", "tsgo:all"] },
    { bin: "corepack", args: ["pnpm", "lint", "--split-core", "--threads=1"] },
    { bin: "corepack", args: ["pnpm", "check:import-cycles"] },
    { bin: "node", args: ["scripts/run-oxlint.mjs", "test/example.test.ts"] },
  ]);
  for (let index = 0; index < plan.commands.length; index++) {
    assert.equal(runtime.calls[index].env, plan.commands[index].env);
  }
  assert.equal(runtime.packages.length, 3);
  assert.equal(runtime.events.filter((event) => event.kind === "completed").length, 4);
  assert.equal(JSON.stringify(runtime.events).includes("never-record-this-value"), false);
  assert.deepEqual(runtime.events[0].original.map(({ args }) => args), original.commands.map(({ args }) => args));
  assert.deepEqual(runtime.events.filter(({ kind }) => kind === "started").map(({ actualArgs }) => actualArgs), runtime.calls.map(({ args }) => args));
});

test("rejects empty, duplicate, malformed and unsupported callback plans before executing anything", async () => {
  const malformed = [
    { ...fixture(), summary: "" },
    { summary: "all", commands: [] },
    { ...fixture(), commands: [...fixture().commands, { name: "lint", args: ["lint"] }] },
    { ...fixture(), commands: [{ name: "lint", args: ["lint", "--only=core"] }] },
    { ...fixture(), commands: [{ name: "lint", bin: "pnpm", args: ["lint"] }] },
    { ...fixture(), commands: [{ name: "lint", args: ["lint"], coreTestCheck: "checkTypes" }] },
    { ...fixture(), commands: [{ name: "lint", args: [null] }] },
    { ...fixture(), commands: [{ name: "lint", args: ["lint"], env: [] }] },
  ];
  for (const plan of malformed) {
    const runtime = fakeRuntime();
    await assert.rejects(executeNativePlan(plan, runtime));
    assert.equal(runtime.calls.length, 0);
    assert.equal(runtime.packages.length, 0);
  }
});

test("preserves a narrow native plan and its shared core-test callback owner", async () => {
  const plan = { summary: "core-tests", commands: [
    { name: "core boundary", args: ["native-boundary"], coreTestCheck: "checkBoundary" },
    { name: "core types", args: ["native-types"], coreTestCheck: "checkTypes" },
    { name: "tail check", args: ["check:import-cycles"] },
  ] };
  const runtime = fakeRuntime(), callbacks = [];
  runtime.coreTestCheck = {
    async checkBoundary() { callbacks.push("boundary"); return 0; },
    async checkTypes() { callbacks.push("types"); return 0; },
  };
  const original = structuredClone(plan);
  assert.equal(await executeNativePlan(plan, runtime), 0);
  assert.deepEqual(plan, original);
  assert.deepEqual(callbacks, ["boundary", "types"]);
  assert.deepEqual(runtime.calls.map(({ args }) => args), [["pnpm", "check:import-cycles"]]);
  assert.deepEqual(runtime.events.filter(({ kind }) => kind === "completed").map(({ exitCode }) => exitCode), [0, 0, 0]);
  assert.equal(runtime.events.filter(({ executionKind }) => executionKind === "native-callback").length, 2);
});

test("missing or failed native callbacks cannot fall back to process execution", async () => {
  const plan = { summary: "core-tests", commands: [
    { name: "core boundary", args: ["native-boundary"], coreTestCheck: "checkBoundary" },
    { name: "tail check", args: ["check:import-cycles"] },
  ] };
  const missing = fakeRuntime();
  await assert.rejects(executeNativePlan(plan, missing), /callbacks are unavailable/);
  assert.equal(missing.calls.length, 0);
  const failed = fakeRuntime();
  failed.coreTestCheck = { async checkBoundary() { return 143; } };
  assert.equal(await executeNativePlan(plan, failed), 143);
  assert.equal(failed.calls.length, 0);
  assert.deepEqual(failed.events.filter(({ kind }) => kind === "completed").map(({ exitCode }) => exitCode), [143]);
});

test("preserves native failure exit and does not run or report unexecuted tail checks", async () => {
  const runtime = fakeRuntime({ failAt: 1, exitCode: 143 });
  assert.equal(await executeNativePlan(fixture(), runtime), 143);
  assert.equal(runtime.calls.length, 2);
  assert.deepEqual(runtime.events.filter(({ kind }) => kind === "completed").map(({ exitCode }) => exitCode), [0, 143]);
  assert.equal(runtime.events.some(({ kind, index }) => kind === "started" && index === 2), false);
});

test("propagates managed-execution exceptions and refuses invalid exit values", async () => {
  const throwing = fakeRuntime({ throwAt: 1 });
  await assert.rejects(executeNativePlan(fixture(), throwing), /fixture execution error/);
  assert.equal(throwing.calls.length, 2);
  assert.equal(throwing.events.filter(({ kind }) => kind === "completed").length, 1);
  for (const exitCode of [undefined, -1, 256, "0"]) {
    const runtime = fakeRuntime({ failAt: 0, exitCode });
    if (exitCode === undefined) runtime.runManagedCommand = async () => undefined;
    await assert.rejects(executeNativePlan(fixture(), runtime), /invalid exit status/);
  }
});

test("evidence failure prevents further native commands instead of silently losing results", async () => {
  const runtime = fakeRuntime();
  runtime.record = (event) => { if (event.kind === "completed") throw new Error("fixture evidence write failed"); };
  await assert.rejects(executeNativePlan(fixture(), runtime), /evidence write failed/);
  assert.equal(runtime.calls.length, 1);
});

test("adaptation preserves original command references and environments", () => {
  const plan = fixture(), adapted = adaptNativePlan(plan);
  assert.equal(adapted.length, plan.commands.length);
  assert.equal(adapted[0], plan.commands[0]);
  assert.equal(adapted[2], plan.commands[2]);
  assert.equal(adapted[3], plan.commands[3]);
  assert.notEqual(adapted[1], plan.commands[1]);
  assert.equal(adapted[1].env, plan.commands[1].env);
});

test("explicit binary without an environment receives the native default resolver", async () => {
  const plan = fixture(), runtime = fakeRuntime();
  delete plan.commands[3].env;
  assert.equal(await executeNativePlan(plan, runtime), 0);
  assert.deepEqual(runtime.calls[3].env, { CI: "true", NATIVE_DEFAULT: "fixture" });
});
