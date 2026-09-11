#!/usr/bin/env node
"use strict";

// Bind the responsible Node process and isolated Python interpreter in launchd.
// Installer-rendered environment supplies the adopter's explicit Python path.
const { spawn } = require("node:child_process");
const { isAbsolute } = require("node:path");
const PYTHON = process.env.OPENCLAW_INTERNAL_PYTHON || "";
if (!isAbsolute(PYTHON) || /[<>$~\r\n\0]/.test(PYTHON)) {
  process.stderr.write(JSON.stringify({ result: "blocked", error: "resolved absolute OPENCLAW_INTERNAL_PYTHON is required" }) + "\n");
  process.exit(64);
}
const argumentsForGuard = process.argv.slice(2);

if (argumentsForGuard.length === 0) {
  process.stderr.write(
    `${JSON.stringify({ result: "blocked", error: "guard arguments are missing" })}\n`,
  );
  process.exit(64);
}

const child = spawn(PYTHON, ["-I", ...argumentsForGuard], {
  shell: false,
  stdio: "inherit",
});
let launcherFailed = false;

const signalNumbers = new Map([
  ["SIGHUP", 1],
  ["SIGINT", 2],
  ["SIGQUIT", 3],
  ["SIGKILL", 9],
  ["SIGTERM", 15],
]);

for (const signal of ["SIGHUP", "SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    if (child.exitCode === null && child.signalCode === null) {
      try {
        child.kill(signal);
      } catch (error) {
        if (error.code !== "ESRCH") {
          process.stderr.write(
            `${JSON.stringify({
              result: "blocked",
              error: `guard signal relay failed: ${error.code || error.name}`,
            })}\n`,
          );
          launcherFailed = true;
          process.exitCode = 70;
        }
      }
    }
  });
}

child.once("error", (error) => {
  launcherFailed = true;
  process.stderr.write(
    `${JSON.stringify({
      result: "blocked",
      error: `guard spawn failed: ${error.code || error.name}`,
    })}\n`,
  );
  process.exitCode = 70;
});

child.once("exit", (code, signal) => {
  if (launcherFailed) {
    process.exitCode = 70;
    return;
  }
  if (signal !== null) {
    process.exitCode = 128 + (signalNumbers.get(signal) || 0);
    return;
  }
  process.exitCode = Number.isInteger(code) ? code : 70;
});
