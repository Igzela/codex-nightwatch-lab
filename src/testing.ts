import fs from "node:fs";

export function crashHook(point: string): void {
  if (process.env.NIGHTWATCH_ENABLE_TEST_CRASH_HOOKS !== "1" || process.env.NIGHTWATCH_TEST_CRASH_POINT !== point) return;
  const once = process.env.NIGHTWATCH_TEST_CRASH_ONCE_FILE;
  if (once) {
    try {
      const fd = fs.openSync(once, "wx", 0o600);
      fs.closeSync(fd);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") return;
      throw error;
    }
  }
  process.kill(process.pid, "SIGKILL");
}
