#!/usr/bin/env node

const { spawnSync } = require("node:child_process");

const args = process.argv.slice(2);
const isWin = process.platform === "win32";
const executable = isWin ? "tmuxx.exe" : "tmuxx";

const run = spawnSync(executable, args, {
  stdio: "inherit",
  shell: false,
});

if (run.error) {
  if (run.error.code === "ENOENT") {
    console.error(
      "tmuxx binary not found in PATH. Install with 'pipx install tmuxx' or 'pip install tmuxx'."
    );
    process.exit(127);
  }
  console.error(`Failed to run tmuxx: ${run.error.message}`);
  process.exit(1);
}

process.exit(run.status === null ? 1 : run.status);
