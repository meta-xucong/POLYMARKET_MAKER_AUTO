"""
poly_maker_autorun
-------------------

基础骨架：配置加载、主循环、命令/交互入口。
后续步骤将补充筛选对接、历史去重、子进程调度等能力。
"""
from __future__ import annotations

import argparse
import json
import queue
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# =====================
# 配置与常量
# =====================
DEFAULT_GLOBAL_CONFIG = {
    "topics_poll_sec": 10.0,
    "command_poll_sec": 1.0,
    "max_concurrent_tasks": 2,
    "log_dir": "logs/autorun",
    "data_dir": "data",
    "handled_topics_path": "data/handled_topics.json",
    "filter_output_path": "data/topics_filtered.json",
}


def _load_json_file(path: Path) -> Dict[str, Any]:
    """读取 JSON 配置，不存在则返回空 dict。"""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as exc:  # pragma: no cover - 粗略校验
            raise RuntimeError(f"无法解析 JSON 配置: {path}: {exc}") from exc


@dataclass
class GlobalConfig:
    topics_poll_sec: float = DEFAULT_GLOBAL_CONFIG["topics_poll_sec"]
    command_poll_sec: float = DEFAULT_GLOBAL_CONFIG["command_poll_sec"]
    max_concurrent_tasks: int = DEFAULT_GLOBAL_CONFIG["max_concurrent_tasks"]
    log_dir: Path = field(default_factory=lambda: Path(DEFAULT_GLOBAL_CONFIG["log_dir"]))
    data_dir: Path = field(default_factory=lambda: Path(DEFAULT_GLOBAL_CONFIG["data_dir"]))
    handled_topics_path: Path = field(
        default_factory=lambda: Path(DEFAULT_GLOBAL_CONFIG["handled_topics_path"])
    )
    filter_output_path: Path = field(
        default_factory=lambda: Path(DEFAULT_GLOBAL_CONFIG["filter_output_path"])
    )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GlobalConfig":
        merged = {**DEFAULT_GLOBAL_CONFIG, **(data or {})}
        return cls(
            topics_poll_sec=float(merged.get("topics_poll_sec", cls.topics_poll_sec)),
            command_poll_sec=float(merged.get("command_poll_sec", cls.command_poll_sec)),
            max_concurrent_tasks=int(merged.get("max_concurrent_tasks", cls.max_concurrent_tasks)),
            log_dir=Path(merged.get("log_dir", cls.log_dir)),
            data_dir=Path(merged.get("data_dir", cls.data_dir)),
            handled_topics_path=Path(merged.get("handled_topics_path", cls.handled_topics_path)),
            filter_output_path=Path(merged.get("filter_output_path", cls.filter_output_path)),
        )

    def ensure_dirs(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class TopicTask:
    topic_id: str
    status: str = "pending"
    start_time: float = field(default_factory=time.time)
    last_heartbeat: Optional[float] = None
    notes: List[str] = field(default_factory=list)

    def heartbeat(self, message: str) -> None:
        self.last_heartbeat = time.time()
        self.notes.append(message)


class AutoRunManager:
    def __init__(self, global_config: GlobalConfig, strategy_defaults: Dict[str, Any]):
        self.config = global_config
        self.strategy_defaults = strategy_defaults
        self.stop_event = threading.Event()
        self.command_queue: "queue.Queue[str]" = queue.Queue()
        self.tasks: Dict[str, TopicTask] = {}

    # ========== 核心循环 ==========
    def run_loop(self) -> None:
        self.config.ensure_dirs()
        print(f"[INIT] autorun start | poll={self.config.topics_poll_sec}s")
        while not self.stop_event.is_set():
            self._process_commands()
            self._tick_once()
            time.sleep(self.config.topics_poll_sec)
        print("[DONE] autorun stopped")

    def _tick_once(self) -> None:
        """占位：后续补充筛选、调度、心跳等逻辑。"""
        for topic_id, task in list(self.tasks.items()):
            status = task.status
            note = task.notes[-1] if task.notes else "idle"
            print(f"[RUN] topic={topic_id} status={status} last_note={note}")

    # ========== 命令处理 ==========
    def enqueue_command(self, command: str) -> None:
        self.command_queue.put(command)

    def _process_commands(self) -> None:
        while True:
            try:
                cmd = self.command_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_command(cmd.strip())

    def _handle_command(self, cmd: str) -> None:
        if not cmd:
            return
        if cmd in {"quit", "exit"}:
            print("[CHOICE] exit requested")
            self.stop_event.set()
            return
        if cmd == "list":
            self._print_status()
            return
        if cmd.startswith("stop "):
            _, topic_id = cmd.split(" ", 1)
            self._stop_topic(topic_id.strip())
            return
        print(f"[WARN] 未识别命令: {cmd}")

    def _print_status(self) -> None:
        if not self.tasks:
            print("[RUN] 当前无运行中的话题")
            return
        for topic_id, task in self.tasks.items():
            hb = task.last_heartbeat
            hb_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(hb)) if hb else "-"
            print(
                f"[RUN] topic={topic_id} status={task.status} "
                f"start={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(task.start_time))} "
                f"hb={hb_text} notes={len(task.notes)}"
            )

    def _stop_topic(self, topic_id: str) -> None:
        task = self.tasks.get(topic_id)
        if not task:
            print(f"[WARN] topic {topic_id} 不在运行列表中")
            return
        task.status = "stopped"
        task.heartbeat("stopped by user")
        print(f"[CHOICE] stop topic={topic_id}")

    # ========== 入口方法 ==========
    def command_loop(self) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    cmd = input("poly> ")
                except EOFError:
                    cmd = "exit"
                self.enqueue_command(cmd)
                time.sleep(self.config.command_poll_sec)
        except KeyboardInterrupt:
            print("\n[WARN] Ctrl+C detected, stopping...")
            self.stop_event.set()


# =====================
# CLI 入口
# =====================

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket maker autorun")
    parser.add_argument(
        "--global-config",
        type=Path,
        default=Path("config/global_config.json"),
        help="全局调度配置 JSON 路径",
    )
    parser.add_argument(
        "--strategy-config",
        type=Path,
        default=Path("config/strategy_defaults.json"),
        help="策略参数模板 JSON 路径",
    )
    parser.add_argument(
        "--no-repl",
        action="store_true",
        help="禁用交互式命令循环，仅按配置运行",
    )
    return parser.parse_args(argv)


def load_configs(args: argparse.Namespace) -> tuple[GlobalConfig, Dict[str, Any]]:
    global_conf_raw = _load_json_file(args.global_config)
    strategy_conf_raw = _load_json_file(args.strategy_config)
    return GlobalConfig.from_dict(global_conf_raw), strategy_conf_raw


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    global_conf, strategy_conf = load_configs(args)

    manager = AutoRunManager(global_conf, strategy_conf)

    def _handle_sigterm(signum: int, frame: Any) -> None:  # pragma: no cover - 信号处理不可测
        print(f"\n[WARN] signal {signum} received, exiting...")
        manager.stop_event.set()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    worker = threading.Thread(target=manager.run_loop, daemon=True)
    worker.start()

    if args.no_repl:
        try:
            while worker.is_alive():
                time.sleep(global_conf.command_poll_sec)
        except KeyboardInterrupt:
            print("\n[WARN] Ctrl+C detected, stopping...")
            manager.stop_event.set()
    else:
        manager.command_loop()

    worker.join()


if __name__ == "__main__":
    main()
