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
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import Customize_fliter_blacklist as filter_script

# =====================
# 配置与常量
# =====================
PROJECT_ROOT = Path(__file__).resolve().parent
MAKER_ROOT = PROJECT_ROOT / "POLYMARKET_MAKER"

DEFAULT_GLOBAL_CONFIG = {
    "topics_poll_sec": 10.0,
    "command_poll_sec": 1.0,
    "max_concurrent_tasks": 2,
    "log_dir": str(MAKER_ROOT / "logs" / "autorun"),
    "data_dir": str(MAKER_ROOT / "data"),
    "handled_topics_path": str(MAKER_ROOT / "data" / "handled_topics.json"),
    "filter_output_path": str(MAKER_ROOT / "data" / "topics_filtered.json"),
    "filter_params_path": str(MAKER_ROOT / "config" / "filter_params.json"),
}


def _topic_id_from_entry(entry: Any) -> str:
    """从筛选结果条目中提取 topic_id/slug，兼容字符串或 dict。"""

    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("slug") or entry.get("topic_id") or "").strip()
    return str(entry).strip()


def _safe_topic_filename(topic_id: str) -> str:
    return topic_id.replace("/", "_").replace("\\", "_")


def _load_json_file(path: Path) -> Dict[str, Any]:
    """读取 JSON 配置，不存在则返回空 dict。"""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as exc:  # pragma: no cover - 粗略校验
            raise RuntimeError(f"无法解析 JSON 配置: {path}: {exc}") from exc


def _dump_json_file(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_handled_topics(path: Path) -> set[str]:
    """读取历史已处理话题集合，空文件或字段缺失则返回空集合。"""

    data = _load_json_file(path)
    topics = data.get("topics") or data.get("handled_topics")
    if topics is None:
        return set()
    if not isinstance(topics, list):  # pragma: no cover - 容错
        print(f"[WARN] handled_topics 文件格式异常，已忽略: {path}")
        return set()
    return {str(t) for t in topics}


def write_handled_topics(path: Path, topics: set[str]) -> None:
    """写入最新的已处理话题集合。"""

    payload = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total": len(topics),
        "topics": sorted(topics),
    }
    _dump_json_file(path, payload)


def compute_new_topics(latest: List[Any], handled: set[str]) -> List[str]:
    """从最新筛选结果中筛出尚未处理的话题列表。"""

    result: List[str] = []
    for entry in latest:
        topic_id = _topic_id_from_entry(entry)
        if topic_id and topic_id not in handled:
            result.append(topic_id)
    return result


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
    filter_params_path: Path = field(
        default_factory=lambda: Path(DEFAULT_GLOBAL_CONFIG["filter_params_path"])
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
            filter_params_path=Path(merged.get("filter_params_path", cls.filter_params_path)),
        )

    def ensure_dirs(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class HighlightConfig:
    max_hours: Optional[float] = 72.0
    ask_min: Optional[float] = 0.80
    ask_max: Optional[float] = 0.99
    min_total_volume: Optional[float] = 20000.0
    max_ask_diff: Optional[float] = 0.2

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "HighlightConfig":
        data = data or {}
        return cls(
            max_hours=data.get("max_hours", cls.max_hours),
            ask_min=data.get("ask_min", cls.ask_min),
            ask_max=data.get("ask_max", cls.ask_max),
            min_total_volume=data.get("min_total_volume", cls.min_total_volume),
            max_ask_diff=data.get("max_ask_diff", cls.max_ask_diff),
        )

    def apply_to_filter(self) -> None:
        if self.max_hours is not None:
            filter_script.HIGHLIGHT_MAX_HOURS = float(self.max_hours)
        if self.ask_min is not None:
            filter_script.HIGHLIGHT_ASK_MIN = float(self.ask_min)
        if self.ask_max is not None:
            filter_script.HIGHLIGHT_ASK_MAX = float(self.ask_max)
        if self.min_total_volume is not None:
            filter_script.HIGHLIGHT_MIN_TOTAL_VOLUME = float(self.min_total_volume)
        if self.max_ask_diff is not None:
            filter_script.HIGHLIGHT_MAX_ASK_DIFF = float(self.max_ask_diff)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_hours": self.max_hours,
            "ask_min": self.ask_min,
            "ask_max": self.ask_max,
            "min_total_volume": self.min_total_volume,
            "max_ask_diff": self.max_ask_diff,
        }


@dataclass
class FilterConfig:
    min_end_hours: float = filter_script.DEFAULT_MIN_END_HOURS
    max_end_days: int = 5
    gamma_window_days: int = 2
    gamma_min_window_hours: int = 1
    legacy_end_days: int = filter_script.DEFAULT_LEGACY_END_DAYS
    allow_illiquid: bool = False
    skip_orderbook: bool = False
    no_rest_backfill: bool = False
    books_batch_size: int = 200
    only: str = ""
    highlight: HighlightConfig = field(default_factory=HighlightConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FilterConfig":
        data = data or {}
        highlight_conf = HighlightConfig.from_dict(data.get("highlight"))
        return cls(
            min_end_hours=float(data.get("min_end_hours", cls.min_end_hours)),
            max_end_days=int(data.get("max_end_days", cls.max_end_days)),
            gamma_window_days=int(data.get("gamma_window_days", cls.gamma_window_days)),
            gamma_min_window_hours=int(data.get("gamma_min_window_hours", cls.gamma_min_window_hours)),
            legacy_end_days=int(data.get("legacy_end_days", cls.legacy_end_days)),
            allow_illiquid=bool(data.get("allow_illiquid", cls.allow_illiquid)),
            skip_orderbook=bool(data.get("skip_orderbook", cls.skip_orderbook)),
            no_rest_backfill=bool(data.get("no_rest_backfill", cls.no_rest_backfill)),
            books_batch_size=int(data.get("books_batch_size", cls.books_batch_size)),
            only=str(data.get("only", cls.only)),
            highlight=highlight_conf,
        )

    def to_filter_kwargs(self) -> Dict[str, Any]:
        return {
            "min_end_hours": self.min_end_hours,
            "max_end_days": self.max_end_days,
            "gamma_window_days": self.gamma_window_days,
            "gamma_min_window_hours": self.gamma_min_window_hours,
            "legacy_end_days": self.legacy_end_days,
            "allow_illiquid": self.allow_illiquid,
            "skip_orderbook": self.skip_orderbook,
            "no_rest_backfill": self.no_rest_backfill,
            "books_batch_size": self.books_batch_size,
            "only": self.only,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.to_filter_kwargs(),
            "highlight": self.highlight.to_dict(),
        }

    def apply_highlight(self) -> None:
        self.highlight.apply_to_filter()


@dataclass
class TopicTask:
    topic_id: str
    status: str = "pending"
    start_time: float = field(default_factory=time.time)
    last_heartbeat: Optional[float] = None
    notes: List[str] = field(default_factory=list)
    process: Optional[subprocess.Popen] = None
    log_path: Optional[Path] = None
    config_path: Optional[Path] = None

    def heartbeat(self, message: str) -> None:
        self.last_heartbeat = time.time()
        self.notes.append(message)

    def is_running(self) -> bool:
        return bool(self.process) and (self.process.poll() is None)


class AutoRunManager:
    def __init__(
        self,
        global_config: GlobalConfig,
        strategy_defaults: Dict[str, Any],
        filter_config: FilterConfig,
    ):
        self.config = global_config
        self.strategy_defaults = strategy_defaults
        self.filter_config = filter_config
        self.stop_event = threading.Event()
        self.command_queue: "queue.Queue[str]" = queue.Queue()
        self.tasks: Dict[str, TopicTask] = {}
        self.latest_topics: List[Dict[str, Any]] = []
        self.topic_details: Dict[str, Dict[str, Any]] = {}
        self.handled_topics: set[str] = set()
        self.pending_topics: List[str] = []

    # ========== 核心循环 ==========
    def run_loop(self) -> None:
        self.config.ensure_dirs()
        self._load_handled_topics()
        print(f"[INIT] autorun start | poll={self.config.topics_poll_sec}s")
        self._refresh_topics()
        while not self.stop_event.is_set():
            self._process_commands()
            self._tick_once()
            time.sleep(self.config.topics_poll_sec)
        print("[DONE] autorun stopped")

    def _tick_once(self) -> None:
        self._poll_tasks()
        self._schedule_pending_topics()
        for topic_id, task in list(self.tasks.items()):
            status = task.status
            note = task.notes[-1] if task.notes else "idle"
            print(f"[RUN] topic={topic_id} status={status} last_note={note}")

        if self.latest_topics:
            topics_preview = ", ".join(
                [_topic_id_from_entry(t) for t in self.latest_topics[:5]]
            )
            print(
                f"[FILTER] 当前筛选话题数={len(self.latest_topics)} "
                f"preview={topics_preview}"
            )

    def _poll_tasks(self) -> None:
        for task in list(self.tasks.values()):
            proc = task.process
            if not proc:
                continue
            rc = proc.poll()
            if rc is None:
                task.status = "running"
                task.last_heartbeat = time.time()
                continue
            if task.status not in {"stopped", "exited", "error"}:
                task.status = "exited" if rc == 0 else "error"
            task.heartbeat(f"process finished rc={rc}")

    def _schedule_pending_topics(self) -> None:
        running = sum(1 for t in self.tasks.values() if t.is_running())
        while (
            self.pending_topics
            and running < max(1, int(self.config.max_concurrent_tasks))
        ):
            topic_id = self.pending_topics.pop(0)
            if topic_id in self.tasks and self.tasks[topic_id].is_running():
                continue
            self._start_topic_process(topic_id)
            running = sum(1 for t in self.tasks.values() if t.is_running())

    def _build_run_config(self, topic_id: str) -> Dict[str, Any]:
        base = self.strategy_defaults.get("default", {}) or {}
        topic_overrides = (self.strategy_defaults.get("topics") or {}).get(
            topic_id, {}
        )
        merged = {**base, **topic_overrides}

        topic_info = self.topic_details.get(topic_id, {})
        merged.setdefault(
            "market_url",
            f"https://polymarket.com/market/{topic_id}",
        )
        merged.setdefault("topic_id", topic_id)
        if topic_info.get("title"):
            merged.setdefault("topic_name", topic_info.get("title"))
        if topic_info.get("yes_token"):
            merged.setdefault("yes_token", topic_info.get("yes_token"))
        if topic_info.get("no_token"):
            merged.setdefault("no_token", topic_info.get("no_token"))
        if topic_info.get("end_time"):
            merged.setdefault("end_time", topic_info.get("end_time"))
        return merged

    def _start_topic_process(self, topic_id: str) -> None:
        config_data = self._build_run_config(topic_id)
        cfg_path = self.config.data_dir / f"run_params_{_safe_topic_filename(topic_id)}.json"
        _dump_json_file(cfg_path, config_data)

        log_path = self.config.log_dir / f"autorun_{_safe_topic_filename(topic_id)}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            log_file = log_path.open("a", encoding="utf-8")
        except OSError as exc:  # pragma: no cover - 文件系统异常
            print(f"[ERROR] 无法创建日志文件 {log_path}: {exc}")
            return

        cmd = [
            sys.executable,
            str(MAKER_ROOT / "Volatility_arbitrage_run.py"),
            str(cfg_path),
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            log_file.close()
        except Exception as exc:  # pragma: no cover - 子进程异常
            print(f"[ERROR] 启动 topic={topic_id} 失败: {exc}")
            log_file.close()
            return

        task = self.tasks.get(topic_id) or TopicTask(topic_id=topic_id)
        task.process = proc
        task.config_path = cfg_path
        task.log_path = log_path
        task.status = "running"
        task.heartbeat("started")
        self.tasks[topic_id] = task
        print(f"[START] topic={topic_id} pid={proc.pid} log={log_path}")

    # ========== 历史记录 ==========
    def _load_handled_topics(self) -> None:
        self.handled_topics = read_handled_topics(self.config.handled_topics_path)
        if self.handled_topics:
            preview = ", ".join(sorted(self.handled_topics)[:5])
            print(
                f"[INIT] 已加载历史话题 {len(self.handled_topics)} 个 preview={preview}"
            )
        else:
            print("[INIT] 尚无历史处理话题记录")

    def _update_handled_topics(self, new_topics: List[str]) -> None:
        if not new_topics:
            return
        self.handled_topics.update(new_topics)
        write_handled_topics(self.config.handled_topics_path, self.handled_topics)

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
        if cmd == "refresh":
            self._refresh_topics()
            return
        print(f"[WARN] 未识别命令: {cmd}")

    def _print_status(self) -> None:
        if not self.tasks:
            print("[RUN] 当前无运行中的话题")
            return
        for topic_id, task in self.tasks.items():
            hb = task.last_heartbeat
            hb_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(hb)) if hb else "-"
            pid_text = str(task.process.pid) if task.process else "-"
            log_name = task.log_path.name if task.log_path else "-"
            print(
                f"[RUN] topic={topic_id} status={task.status} "
                f"start={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(task.start_time))} "
                f"pid={pid_text} hb={hb_text} notes={len(task.notes)} "
                f"log={log_name}"
            )

    def _stop_topic(self, topic_id: str) -> None:
        task = self.tasks.get(topic_id)
        if not task:
            print(f"[WARN] topic {topic_id} 不在运行列表中")
            return
        if task.process and task.is_running():
            try:
                task.process.terminate()
            except Exception as exc:  # pragma: no cover - 终止异常
                print(f"[WARN] 无法终止 topic {topic_id}: {exc}")
        task.status = "stopped"
        task.heartbeat("stopped by user")
        print(f"[CHOICE] stop topic={topic_id}")

    def _refresh_topics(self) -> None:
        try:
            self.latest_topics = run_filter_once(
                self.filter_config, self.config.filter_output_path
            )
            self.topic_details = {
                _topic_id_from_entry(item): item
                for item in self.latest_topics
                if _topic_id_from_entry(item)
            }
            new_topics = compute_new_topics(self.latest_topics, self.handled_topics)
            if new_topics:
                preview = ", ".join(new_topics[:5])
                print(
                    f"[INCR] 新话题 {len(new_topics)} 个，将更新历史记录 preview={preview}"
                )
                self._update_handled_topics(new_topics)
                for topic_id in new_topics:
                    if topic_id in self.pending_topics:
                        continue
                    if topic_id in self.tasks and self.tasks[topic_id].is_running():
                        continue
                    self.pending_topics.append(topic_id)
            else:
                print("[INCR] 无新增话题")
        except Exception as exc:  # pragma: no cover - 网络/外部依赖
            print(f"[ERROR] 筛选流程失败：{exc}")
            self.latest_topics = []

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
        default=MAKER_ROOT / "config" / "global_config.json",
        help="全局调度配置 JSON 路径",
    )
    parser.add_argument(
        "--strategy-config",
        type=Path,
        default=MAKER_ROOT / "config" / "strategy_defaults.json",
        help="策略参数模板 JSON 路径",
    )
    parser.add_argument(
        "--filter-config",
        type=Path,
        default=MAKER_ROOT / "config" / "filter_params.json",
        help="筛选参数配置 JSON 路径",
    )
    parser.add_argument(
        "--no-repl",
        action="store_true",
        help="禁用交互式命令循环，仅按配置运行",
    )
    return parser.parse_args(argv)


def load_configs(
    args: argparse.Namespace,
) -> tuple[GlobalConfig, Dict[str, Any], FilterConfig]:
    global_conf_raw = _load_json_file(args.global_config)
    strategy_conf_raw = _load_json_file(args.strategy_config)
    filter_conf_raw = _load_json_file(args.filter_config)
    return (
        GlobalConfig.from_dict(global_conf_raw),
        strategy_conf_raw,
        FilterConfig.from_dict(filter_conf_raw),
    )


def run_filter_once(filter_conf: FilterConfig, output_path: Path) -> List[Dict[str, Any]]:
    """调用筛选脚本，落盘 JSON，并返回话题列表。"""

    filter_conf.apply_highlight()
    result = filter_script.collect_filter_results(**filter_conf.to_filter_kwargs())

    topics: List[Dict[str, Any]] = []
    for ms in result.chosen:
        topics.append(
            {
                "slug": ms.slug,
                "title": ms.title,
                "yes_token": ms.yes.token_id,
                "no_token": ms.no.token_id,
                "end_time": ms.end_time.isoformat() if ms.end_time else None,
                "liquidity": ms.liquidity,
                "total_volume": ms.totalVolume,
            }
        )

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "params": filter_conf.to_dict(),
        "total_markets": result.total_markets,
        "candidates": len(result.candidates),
        "chosen": len(result.chosen),
        "rejected": len(result.rejected),
        "highlights": len(result.highlights),
        "topics": topics,
    }
    _dump_json_file(output_path, payload)
    print(f"[FILTER] 已写入筛选结果到 {output_path}，共 {len(topics)} 个话题")
    return topics


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    global_conf, strategy_conf, filter_conf = load_configs(args)

    manager = AutoRunManager(global_conf, strategy_conf, filter_conf)

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
