import koffi from "koffi";

const libc = koffi.load("libc.so.6");
const flockNative = libc.func("int flock(int fd, int operation)");
const errnoLocation = libc.func("int* __errno_location()");
const getpgrpNative = libc.func("int getpgrp()");

export function getpgrp(): number {
  return getpgrpNative() as number;
}

export const LOCK_SH = 1;
export const LOCK_EX = 2;
export const LOCK_UN = 8;
export const LOCK_NB = 4;

export class WouldBlock extends Error {
  readonly code = "EAGAIN";
  readonly errno = 11;

  constructor() {
    super("flock would block");
    this.name = "WouldBlock";
  }
}

export function flock(fd: number, operation: number): void {
  const rc = flockNative(fd, operation) as number;
  if (rc === 0) return;
  const errno = koffi.decode(errnoLocation(), "int") as number;
  if (errno === 11 || errno === 35) throw new WouldBlock();
  const error = new Error(`flock failed (${errno})`) as NodeJS.ErrnoException;
  error.errno = errno;
  throw error;
}
