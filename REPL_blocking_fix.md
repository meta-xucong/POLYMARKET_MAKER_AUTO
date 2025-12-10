# REPL 阻塞原因与最小改造思路（不改现有功能）

## 现象
后台挂单子进程与 REPL 共用同一个终端/标准输入，子进程一旦尝试读 stdin（例如 `input()`、`select` 轮询或 `read`），会占用终端缓冲区，导致前台 REPL 在回车后没有机会读取到数据，看起来像“卡死/阻塞”。

## 根因
- 主进程在同一 `screen` 会话中启动 REPL，并在内部再启动挂单子进程；
- 子进程默认继承父进程的文件描述符（含 stdin），控制终端只有一个；
- 当任一子进程对 stdin 进行读操作或阻塞式 `select`，会抢占终端输入，前台 REPL 自然读不到数据。

## 最小化改造思路（保持 REPL，避免输入争用）
仅需在**启动子进程**时让它们与 REPL 的 stdin 解耦，无需改业务逻辑：

1. **对子进程禁用 stdin**：
   - 启动挂单脚本时（`subprocess.Popen`），显式传入 `stdin=subprocess.DEVNULL`（或打开 `/dev/null`）。
   - 若是 `multiprocessing.Process`，在子进程入口处将 `sys.stdin` 重定向到 `/dev/null`，或在 `spawn` 出来后立刻关闭 `0` 号 fd。

2. **可选：为子进程开启新会话**（进一步隔离 TTY）：
   - 在 `Popen` 时增加 `start_new_session=True`（或 `preexec_fn=os.setsid`）。
   - 这样子进程即使尝试重新获取终端，也不会占用 REPL 的控制终端。

3. **保持 REPL 逻辑不变**：
   - 只需在创建/重启挂单子进程的代码路径里插入上述 stdin 处理，不改交互命令、策略逻辑；
   - 现有日志/状态文件输出路径不变，只是把子进程的输入彻底断开。

## 改造示例（示意，不必立即改代码）
- `subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=..., stderr=..., start_new_session=True)`
- 或在子进程入口：
  ```python
  import os, sys
  sys.stdin = open(os.devnull)
  os.close(0)
  ```

## 为什么比“另开终端+--command”更符合需求
- 仍然在同一 `screen` 中使用交互式 REPL；
- 只需少量启动参数/上下文调整即可解除 stdin 竞争；
- 不改变现有功能和操作习惯，后台挂单与 REPL 共存且互不阻塞。
