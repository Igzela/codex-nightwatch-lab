import fs from "node:fs";

import { getpgrp } from "./posix.js";

export function pidAlive(pid: number | null | undefined): boolean {
  if (typeof pid !== "number" || !Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
  } catch {
    return false;
  }
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
    const state = stat.split(")").at(-1)?.trim().split(/\s+/)[0];
    return state !== "Z";
  } catch {
    return false;
  }
}

export function linuxProcessIdentity(pid: number): { pid: number; starttime: string; executable: string } | null {
  if (!pidAlive(pid)) return null;
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
    const fields = stat.split(")").at(-1)?.trim().split(/\s+/) ?? [];
    const starttime = fields[19];
    const executable = fs.readlinkSync(`/proc/${pid}/exe`);
    if (!starttime) return null;
    return { pid, starttime, executable };
  } catch {
    return null;
  }
}

export function processIdentityMatches(record: { pid?: unknown; starttime?: unknown; executable?: unknown }): boolean {
  if (typeof record.pid !== "number") return false;
  const observed = linuxProcessIdentity(record.pid);
  return Boolean(observed && observed.starttime === record.starttime && observed.executable === record.executable);
}

export function processGroupAlive(pgid: number | null | undefined): boolean {
  if (typeof pgid !== "number" || !Number.isInteger(pgid) || pgid <= 1) return false;
  try {
    if (pgid === getpgrp()) return false;
  } catch {
    /* continue */
  }
  try {
    process.kill(-pgid, 0);
    return true;
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code === "ESRCH") return false;
    if (code === "EPERM") return true;
    return false;
  }
}
