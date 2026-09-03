# 🌙 Nightwatch (守夜人)

<div align="center">

**专为 OpenAI Codex 设计的无损容灾、配额感知、精确会话恢复的 Linux 无人值守守护进程。**  
*零外部依赖 • 直连官方 App Server JSON-RPC • 预冻结验证门禁 • 宿主系统级原生集成*

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-brightgreen.svg)](#安装指南)
[![Platform: Linux](https://img.shields.io/badge/Platform-Linux-orange.svg)](#系统要求)
[![Tests: 205 Passing](https://img.shields.io/badge/Tests-205%20Passing-success.svg)](#测试验证)
[![Codex: 0.152.1+](https://img.shields.io/badge/OpenAI%20Codex-0.152.1%2B-purple.svg)](https://github.com/openai/codex)

[**English**](README.md) | [**中文说明**](README_CN.md)

</div>

---

## ⚡ 核心解决的痛点

你睡前给 AI 编程助手分配了一个复杂的工程重构任务，期待醒来时看到全部绿色的测试用例。然而现实往往是：
- ❌ **5 小时配额瞬间用尽**：运行 20 分钟就触发了 OpenAI 的 `Usage limit`，任务直接停摆；
- ❌ **终端断连 / 笔记本睡眠中断**：SSH 断开或系统休眠导致后台进程直接被系统回收；
- ❌ **模型幻觉假装完成**：模型在聊天中宣称*“我已经全部实现并验证完毕”*，实际上根本没有跑通测试；
- ❌ **重启丢失记忆**：重新开启新会话导致前序上下文全部丢失，浪费海量 Token 且状态分叉。

**Nightwatch (守夜人) 为解决这一整套工程可靠性问题而生。**

```text
               ┌────────────────────────────────────────────────────────┐
               │                 Nightwatch 守护进程                    │
               │  (外部受信任控制面: ~/.local/state/codex-nightwatch/)  │
               └───────────┬────────────────────────────────┬───────────┘
                           │                                │
        [1] 派生与管控子进程                 [2] 原生 JSON-RPC stdio 通信
        `codex exec --json`                  `account/rateLimits/read`
                           │                                │
                           ▼                                ▼
               ┌───────────────────────┐        ┌───────────────────────┐
               │    OpenAI Codex CLI   │        │   官方 App Server     │
               │   (工作区沙箱执行)    │        │     (配额权威源)      │
               └───────────┬───────────┘        └───────────┬───────────┘
                           │                                │
                           │ [3] 触发 5h / 周配额耗尽       │ [4] 窗口刷新到达
                           ▼                                ▼
               ┌────────────────────────────────────────────────────────┐
               │               智能重置探测与自动续跑                   │
               │        在同一个 exact Thread 上无缝恢复上下文          │
               │         严格执行用户预冻结的测试验证门禁               │
               └────────────────────────────────────────────────────────┘
```

---

## ✨ 核心特性

- 🔄 **官方 App Server 协议直连**：通过 JSON-RPC 2.0 stdio 协议直连 Codex 内部 `account/rateLimits/read`，精准获取 5h 与周配额重置 Epoch，杜绝脆弱的正则表达式或 ANSI 屏幕抓取。
- 🧵 **精确 Thread ID 级断点续传**：跨配额周期与系统重启时，严格执行 `codex exec --json resume <thread_id> -` 精准接续原会话，坚决拒绝模糊的 `--last` 猜测。
- 👥 **可选账号池**：显式选择的账号子集只会在 provider 退出后轮换；每个账号使用全局 lease，通过新的 App Server 配额会话检查，并同时受 5 小时和 weekly 限制约束。
- 🔐 **规范认证同步串行化**：规范 `codex-auth` 注册表操作在账号 lease 之后获取外部受信控制面目录上的短时内核锁，并在 provider 执行前释放；provider capsule 只保留被选账号，启动前删除全账号 staging。
- 🔒 **受信任控制面物理隔离**：核心状态与验收规则保存在 Git 工作区之外（`~/.local/state/codex-nightwatch/`，`0700` 权限），模型沙箱只能读写信箱，绝无可能篡改验收规则。
- 🧪 **冻结真实验收门禁**：任务完成（`DONE`）必须严格通过用户预冻结的 `--verify` 命令（如 `pytest -q`、`cargo test`、`git diff --check`），彻底杜绝模型幻觉。
- 🛡️ **无侵入伴随监听与自动接管（`nightwatch watch`）**：安全监听终端中正在运行的交互式 Codex 会话，在不打断前台的前提下实时统计 Token 与配额，并在触发上限或终端关闭后自动无缝接管夜间续跑。
- 🔋 **Linux 原生高可靠**：全项目基于 Python 3 标准库（零第三方 pip 依赖），原生支持 `systemctl --user` 服务化守护与 `systemd-inhibit` 阻塞系统休眠。

---

## 🚀 极速安装

### 一键安装脚本

```bash
curl -fsSL https://raw.githubusercontent.com/Igzela/codex-nightwatch-lab/master/install.sh | bash
```

*或手动克隆安装：*

```bash
git clone https://github.com/Igzela/codex-nightwatch-lab.git ~/.local/share/codex-nightwatch
~/.local/share/codex-nightwatch/nightwatch/bin/nightwatch install
```

### 环境诊断与校验

```bash
nightwatch doctor
```
```text
Nightwatch doctor: ok
Codex: codex-cli 0.152.1
Auth: ok
Quota authority: LIVE_APP_SERVER (live_app_server)
5h: 7.0% used, reset=1787866896
weekly: 1.0% used, reset=1788453696
systemd-inhibit: available
```

---

## 📖 核心使用模式

### 交互式 TUI（推荐）

在终端里不带子命令运行 Nightwatch：

```bash
cd /path/to/my-project
nightwatch
```

未选中活跃任务时，直接输入自然语言会进入新任务向导和执行预览；选中了活跃任务时，自然语言会变成发给该 exact thread 的待确认 steer 指令。任何会改变状态的操作都不会绕过预览确认。

```text
Nightwatch 0.4.0 · MULTI-THREAD CONTROL
Runs 2 · ↑/↓ select · / commands · Esc quit

▶ RUNNING             payments-retry         01a050ac-1149…
    ███████████░░░░░░░ 61%  gpt-5.6-luna · high  quota 5h 52% · week 8%
  WAIT_QUOTA          inventory-import       01a050bd-82ae…
    ███████░░░░░░░░░░░ 38%  gpt-5.6-luna · medium

Thread     01a050ac-1149… · generation 2
Agent      RUNNING · PID 18234 · resume
Next       continue current milestone
Source: trusted state + sequence-validated events

Input › natural language starts a goal (or steers an active run); / opens command palette
```

输入 `/` 会展开带说明的命令面板。主要观察入口包括 `/status`、`/plan`、`/timeline`、`/explain`、`/thread`、`/quota`、`/logs`、`/recap` 和 `/report`；`/run`、`/adopt`、`/steer`、`/resume`、`/stop` 等状态变更操作都会先显示确认预览。`/adopt` 会列出能够同时证明 PID、rollout、仓库和 exact thread 的活跃会话，手工输入 Thread ID 仅作为显式后备路径。

`/multi` 会统一展示受信任状态根目录中的全部运行。多个写入 Agent 可以并行工作在不同仓库或相互隔离的 Git worktree 中。如果目标仓库已经存在一个运行，`/run` 向导会在确认后创建 `.worktrees/<repo>/<label>`，并为它生成独立的 systemd user unit。Nightwatch 不允许两个受控写入 Agent 共用同一个工作目录。

到达 `DONE`、`BLOCKED`、`FAILED`、`STOPPED` 或 `AWAITING_ACCEPTANCE` 时，TUI 会响铃并明确显示真实终态。`/recap` 提供基于证据的短总结；`/report` 持久化包含模型、Thread、generation、里程碑、验证、配额和可信时间线的完整报告。模型叙述不会混入可信事实。

### 选择 Codex 模型和推理挡位

Nightwatch 会读取当前已安装 Codex CLI 的实时模型目录，不把容易过期的模型和挡位列表写死：

```bash
nightwatch models
nightwatch models --json
```

创建或收养任务时可以同时指定模型与挡位。它们会写入工作区外的受信任持久化状态，并用于后续每一次 exact-thread 续跑：

```bash
nightwatch run \
  --model gpt-5.6-luna \
  --reasoning-effort high \
  --verify "pytest -q" \
  "实现功能并通过完整测试"
```

不指定某一项时，继续使用 Codex 自身配置的默认值；模型与挡位是否兼容最终由本机 Codex CLI 校验。

### 可选账号池（AUTO_POOL）

任务默认是 `CURRENT_ONLY`，不会自动发现或加入所有本机账号。安装并配置独立的 `codex-auth` 后，必须在启动时明确选择账号子集：

```bash
nightwatch run \
  --account-mode auto-pool \
  --account personal \
  --account backup \
  --verify "pytest -q" \
  "实现功能并通过测试"
```

Nightwatch 只使用 `codex-auth list --skip-api --json` 获取稳定 `account_key` 和显示信息，并在工作区外的 0700 capsule 中使用账号；不会使用 codex-auth 的远程用量 API。真实可用配额始终来自每个账号上下文中新建的官方 Codex App Server `account/rateLimits/read`。5 小时与 weekly 两个窗口都必须存在且未耗尽，选择策略按较小剩余容量、5 小时剩余、weekly 剩余、reset 时间和短指纹确定性排序。

每次 App Server 探测或 Codex provider 执行前都会持有工作区外的全局账号 lease；子进程退出且刷新后的认证状态同步完成后才释放。所有账号不可用时进入 `WAIT_QUOTA`，等待最早相关 reset 后重新探测整个账号池。跨账号 exact-thread 能否保持尚未假定；在本机 Codex 版本完成安全实测前，状态会明确显示 `CONTROLLED_THREAD_HANDOFF`，使用受信任的目标、冻结验证策略、仓库/Git HEAD、里程碑和旧 Thread 审计包创建新对话，不会冒充原 Thread。缺少兼容 codex-auth 时 AUTO_POOL 安全不可用，但 CURRENT_ONLY 保持兼容。

正常 AUTO_POOL 配额耗尽只记录信息性的 `quota_cycles`，不会消耗防御性恢复预算；`recovery_failures` 记录有界的异常恢复失败。真实上游 `codex-auth` 合约已审计，并使用隔离的 `v0.3.0-alpha.11`（commit `0fde29598c2e02e28e0e8bcc33a4bb8d45d7b23a`）完成实际合约操作测试，未替换主机现有 binary。目前 live discovery 发现 3 个已存账号，但本次测试的两个非活跃账号中只有一个返回了 live App Server 配额，因此双账号生产验收仍待完成。跨账号 exact-thread 结果为 `INCONCLUSIVE`，生产行为继续使用安全的 controlled handoff。

### 模式一：夜间全自主无人值守模式

在 Git 仓库中直接启动带真实验收门禁的守护任务：

```bash
cd /path/to/my-project
nightwatch run \
  --model gpt-5.6-luna \
  --reasoning-effort high \
  --verify "pytest -q" \
  --verify "git diff --check" \
  "完成当前模块的重构并确保所有测试全部通过"
```

如果希望作为后台系统服务运行（关闭终端、注销登录不掉线）：

```bash
nightwatch run --service \
  --verify "cargo test" \
  --verify "git diff --check" \
  "重构存储引擎底层接口"
```

### 模式二：交互会话无侵入监听与夜间自动接管

如果你正在终端中与 Codex 交互，无需退出，直接另起窗口监听：

```bash
# 单次快照当前遥测信息（若同仓库存在多会话且未指定 --thread 则安全 fail-closed）
nightwatch watch --once

# 实时动态监控（多会话时可指定 --thread）
nightwatch watch [--thread <ID>]

# 开启夜间自动接管：等待前台交互进程退出后，自动无缝接管 exact thread 续跑
nightwatch watch --auto-takeover --verify "pytest -q" [--thread <ID>]
```

> **自动接管语义（Auto-Takeover Semantics）**：当配额耗尽（`used_percent >= 100%`）时，Nightwatch 标记状态为 `TAKEOVER_PENDING` 并保持纯被动监听，**严格等待原交互式 Codex 进程退出后**才启动受信任 supervisor，彻底防止多进程并发冲突与 Git 工作区竞争。

```text
============================================================
REPO         /home/user/projects/my-app
THREAD ID    01a04416-c7aa-7271-9ede-7fe2d40cf950
PROCESS      PID 466574 (ALIVE)
MODEL        gpt-5.6-luna [branch: main]
QUOTA 5H     7.0% used, reset=1787866896
QUOTA WEEKLY 1.0% used
TOKENS       total=15,947,329, input=15,882,347, output=64,982
SUBAGENTS    Copernicus (01a0442b...), Kepler (01a0442b...)
============================================================
```

### 模式三：显式收养已有的会话 Thread

直接将现存的对话 Thread 注入 Nightwatch 控制面：

```bash
nightwatch adopt --thread 01a04416-c7aa-7271-9ede-7fe2d40cf950 \
  --model gpt-5.6-luna --reasoning-effort high --verify "pytest"
nightwatch resume
```

### 如何交互与查看实时进度

`nightwatch run` 是无人值守执行模式：它启动 `codex exec --json`，通过 stdin 发送目标，并只监督这个精确 Thread；它本身不是聊天界面。另开一个终端即可操作控制面：

```bash
nightwatch status                 # 单次持久化状态快照
nightwatch status --watch         # 实时 Agent 状态、进度和里程碑
nightwatch status --json          # 机器可读快照
nightwatch log --tail 100         # Supervisor 审计日志
nightwatch report                 # 验收报告
nightwatch stop                   # 安全停止，保留状态和 Thread
nightwatch resume                 # 继续同一个 exact Thread
```

实时状态会区分 Supervisor 与 Codex 子 Agent（`AGENT RUNNING`、PID、start/resume 动作），显示所选模型和推理挡位，并展示受信任的 implemented/verified 里程碑进度，到达终态后自动退出。工作区 mailbox 中由模型写入的进度只是非受信任输入，必须被 Nightwatch 校验并纳入持久化计划后才会显示为可信进度。

如果需要正常聊天交互，继续直接使用 Codex，并在另一个终端运行 `nightwatch watch`。`watch --auto-takeover` 会在原交互进程退出后，把同一 Thread 交给无人值守 Supervisor。

TUI 只是现有持久化接口之上的显示与操作适配层。所有能力仍保留对应 CLI，便于脚本化与故障恢复；界面不会维护第二份隐藏状态。

---

## 🛠️ CLI 常用指令表

| 命令 | 说明 |
| :--- | :--- |
| `nightwatch` / `nightwatch ui` | 打开多线程交互式 Dashboard 和 `/` 命令面板 |
| `nightwatch models [--json]` | 显示本机 Codex 实时模型目录及支持的推理挡位 |
| `nightwatch run "<goal>" [--model <slug>] [--reasoning-effort <level>] [--verify <cmd>] [--service]` | 初始化并启动全新的受控自主任务（默认 `CURRENT_ONLY`） |
| `nightwatch run "<goal>" --account-mode auto-pool --account <key-or-alias> [--account <key-or-alias> ...]` | 使用明确授权的账号子集运行 |
| `nightwatch watch [--thread <id>] [--auto-takeover] [--once] [--json]` | 无侵入监听活跃交互会话；模型参数用于自动接管 |
| `nightwatch adopt --thread <id> [--model <slug>] [--reasoning-effort <level>] [--verify <cmd>]` | 将现有对话 Thread 纳入 Nightwatch 受信任控制面 |
| `nightwatch resume` | 恢复并继续当前仓库的精确 Thread 任务 |
| `nightwatch status [--watch] [--interval <秒>] [--json]` | 单次或持续查看 Agent、账号池、配额与可信里程碑进度 |
| `nightwatch log [--tail N]` | 查看人类可读的审计与执行日志 |
| `nightwatch report` | 输出/生成结构化验收报告 |
| `nightwatch stop` | 安全停止自动执行（保留现场与 Thread 状态） |
| `nightwatch doctor` | 检查 Linux、Codex CLI、认证状态、配额权威源与系统服务 |
| `nightwatch test app-server` | 实时测试与 Codex App Server 的 JSON-RPC 协议连接 |

---

## ⚖️ 架构方案深度对比

| 对比维度 | 原生 Codex CLI | Tmux / 屏幕抓取脚本 | **Nightwatch (守夜人)** |
| :--- | :---: | :---: | :---: |
| **配额恢复机制** | ❌ 需人工手动重试 | ⚠️ 正则匹配屏幕输出 | **✅ 原生 App Server stdio JSON-RPC 探测** |
| **会话状态记忆** | ❌ 重新拉起开新会话 | ⚠️ 依赖 `--last` 猜测 | **✅ 精准持久化 `thread_id` 无损续跑** |
| **验收完成门禁** | ❌ 模型自述假装完成 | ❌ 无验证门禁 | **✅ 用户预冻结命令通过才判定 DONE** |
| **控制面安全性** | ⚠️ 模型可随意修改测试 | ❌ 状态暴露在仓库内 | **✅ 外部 `~/.local/state/` 独立受信任沙箱** |
| **进程常驻形态** | ❌ 仅前台，掉线暴毙 | ⚠️ Tmux 输入注入 | **✅ user systemd 服务 + 阻塞系统睡眠** |
| **第三方依赖** | — | Node / pnpm / Tmux | **✅ 零外部依赖（纯 Python 3 标准库）** |

---

## 🔒 信任边界与安全模型

所有受信任权威数据均存放在 Git 工作区之外：

```text
~/.local/state/codex-nightwatch/<repo-name>-<repo-hash>/
├── state.json                 # 持久化状态机 (包含 generation, thread_id, status)
├── verification-policy.json   # 用户冻结的哈希绑定验证命令
├── acceptance.json            # 目标与真实验收准则
├── events.jsonl               # 仅追加的单调自增审计日志
├── account-leases/            # 全局账号生命周期锁
├── account-capsules/          # 外部临时 CODEX_HOME（认证文件不进 Git）
├── supervisor.lock            # 进程排他锁 (防止 PID 复用漏洞)
└── runs/                      # 分代脱敏 stdout/stderr 执行记录
```

代码仓库内部仅包含不可信信箱目录（`.nightwatch-agent/`），任何模型产出的验证脚本均被严格拒绝执行。

---

## 🧪 验证与自动化测试

Nightwatch 经过严密的工程验证与故障注入测试：

```bash
python3 -m unittest discover -s nightwatch/tests -v
```
```text
Ran 198 tests
OK
```

- ✅ 真实 Codex 0.152.1 App Server 实时配额 JSON-RPC 通信验证
- ✅ 真实多进程并发冲突与竞争防御实测
- ✅ SIGKILL 异常崩溃后 Linux PID 身份重校验与精确 Thread 恢复
- ✅ 符号链接穿透与 Mailbox 命令注入反制

---

## 📄 开源协议

本项目基于 [MIT License](LICENSE) 开源 © 2026 Igzela
