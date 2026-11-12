import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const CLI = path.join(ROOT, "src", "cli.ts");

describe("cli help", () => {
  it("prints usage and exits 2 when invoked with no args on non-tty", () => {
    const result = spawnSync(process.execPath, ["--import", "tsx", CLI], {
      cwd: ROOT,
      encoding: "utf8",
      env: { ...process.env },
      stdio: ["pipe", "pipe", "pipe"],
    });
    assert.equal(result.status, 2);
    const output = `${result.stdout ?? ""}${result.stderr ?? ""}`;
    assert.match(output, /Usage:\s*nightwatch/i);
    assert.match(output, /Commands:/i);
  });

  it("prints usage for --help", () => {
    const result = spawnSync(process.execPath, ["--import", "tsx", CLI, "--help"], {
      cwd: ROOT,
      encoding: "utf8",
      env: { ...process.env },
      stdio: ["pipe", "pipe", "pipe"],
    });
    assert.equal(result.status, 0);
    assert.match(result.stdout ?? "", /Usage:\s*nightwatch/i);
  });
});
