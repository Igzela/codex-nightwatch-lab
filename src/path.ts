import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export class NightPath {
  constructor(public readonly raw: string) {}

  static home(): NightPath {
    return new NightPath(os.homedir());
  }

  toString(): string {
    return this.raw;
  }

  get parent(): NightPath {
    return new NightPath(path.dirname(this.raw));
  }

  get name(): string {
    return path.basename(this.raw);
  }

  join(...parts: string[]): NightPath {
    return new NightPath(path.join(this.raw, ...parts));
  }

  expanduser(): NightPath {
    if (this.raw === "~") return NightPath.home();
    if (this.raw.startsWith("~/")) return new NightPath(path.join(os.homedir(), this.raw.slice(2)));
    return this;
  }

  resolve(): NightPath {
    return new NightPath(path.resolve(this.expanduser().raw));
  }

  exists(): boolean {
    try {
      fs.lstatSync(this.raw);
      return true;
    } catch {
      return false;
    }
  }

  isFile(): boolean {
    try {
      return fs.statSync(this.raw).isFile();
    } catch {
      return false;
    }
  }

  isDir(): boolean {
    try {
      return fs.statSync(this.raw).isDirectory();
    } catch {
      return false;
    }
  }

  isSymlink(): boolean {
    try {
      return fs.lstatSync(this.raw).isSymbolicLink();
    } catch {
      return false;
    }
  }

  readText(): string {
    return fs.readFileSync(this.raw, "utf8");
  }

  writeText(text: string): void {
    fs.writeFileSync(this.raw, text, "utf8");
  }

  readBytes(): Buffer {
    return fs.readFileSync(this.raw);
  }

  writeBytes(data: Buffer | string): void {
    fs.writeFileSync(this.raw, data);
  }

  mkdir(options?: { parents?: boolean; existOk?: boolean; mode?: number }): void {
    try {
      fs.mkdirSync(this.raw, { recursive: options?.parents ?? false, mode: options?.mode });
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (options?.existOk && (code === "EEXIST" || options.parents)) return;
      throw error;
    }
  }

  chmod(mode: number): void {
    fs.chmodSync(this.raw, mode);
  }

  unlink(): void {
    fs.unlinkSync(this.raw);
  }

  rename(dest: NightPath): void {
    fs.renameSync(this.raw, dest.raw);
  }

  iterdir(): NightPath[] {
    return fs.readdirSync(this.raw).map((name) => this.join(name));
  }

  lstat(): fs.Stats {
    return fs.lstatSync(this.raw);
  }

  stat(): fs.Stats {
    return fs.statSync(this.raw);
  }

  readlink(): string {
    return fs.readlinkSync(this.raw);
  }

  open(flags: fs.OpenMode = "a+"): TextFile {
    if (!this.exists()) this.parent.mkdir({ parents: true, existOk: true });
    return new TextFile(fs.openSync(this.raw, flags), this);
  }
}

export class TextFile {
  private position = 0;

  constructor(
    public readonly fd: number,
    public readonly path: NightPath,
  ) {}

  seek(offset: number): void {
    this.position = offset;
  }

  truncate(length = 0): void {
    fs.ftruncateSync(this.fd, length);
    if (this.position > length) this.position = length;
  }

  write(text: string): void {
    const buffer = Buffer.from(text);
    fs.writeSync(this.fd, buffer, 0, buffer.length, this.position);
    this.position += buffer.length;
  }

  flush(): void {
    fs.fsyncSync(this.fd);
  }

  fileno(): number {
    return this.fd;
  }

  close(): void {
    fs.closeSync(this.fd);
  }

  read(): string {
    const stat = fs.fstatSync(this.fd);
    const buffer = Buffer.alloc(stat.size);
    fs.readSync(this.fd, buffer, 0, stat.size, 0);
    return buffer.toString("utf8");
  }
}
