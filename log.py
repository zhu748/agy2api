"""
日志模块 - 控制台输出 + 进程内环形缓冲（无状态函数风格）
- LOG_LEVEL 控制级别（debug/info/warning/error/critical，默认 info）
- ENABLE_LOG=0 彻底静默（不打印任何日志，环形缓冲仍持续记录）
- log.debug/info/warning/error/critical(message) 记录日志
- set_log_level 动态调整级别，get_log_level 读取当前级别
- 环形缓冲：面板"运行日志"页的数据源，get_buffered_logs 读取、
  clear_buffered_logs 清空、install_logging_capture 桥接 uvicorn 日志
"""

import logging
import os
import sys
import threading
from collections import deque
from datetime import datetime
from types import SimpleNamespace

# 日志级别定义
LOG_LEVELS = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}

# 缓存配置（模块加载时读取一次）
_cached_log_level: int = LOG_LEVELS["info"]
_log_enabled: bool = True

# 进程内环形日志缓冲（面板"运行日志"数据源）
# 上限 3000 条防止长时运行内存膨胀；锁保护多线程/异步 worker 并发追加
_LOG_BUFFER: deque = deque(maxlen=3000)
_LOG_BUFFER_LOCK = threading.Lock()


def _capture(level: str, message: str, source: str) -> None:
    """追加一条日志到环形缓冲（与控制台输出互不影响，永不抛错）"""
    try:
        record = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": level.lower(),
            "source": source,
            "message": str(message),
        }
        with _LOG_BUFFER_LOCK:
            _LOG_BUFFER.append(record)
    except Exception:
        # 日志捕获失败不能影响主流程
        pass


def _refresh_config():
    """从环境变量刷新缓存配置"""
    global _cached_log_level, _log_enabled
    level = os.getenv("LOG_LEVEL", "info").lower()
    _cached_log_level = LOG_LEVELS.get(level, LOG_LEVELS["info"])
    _log_enabled = os.getenv("ENABLE_LOG", "1").strip().lower() not in ("0", "false", "no", "off")


_refresh_config()


def _log(level: str, message: str):
    level_val = LOG_LEVELS.get(level.lower())
    if level_val is None:
        return

    # 始终写入环形缓冲（不受控制台开关/级别影响，面板"运行日志"需要完整记录）
    _capture(level, message, "app")

    # 最快短路：日志整体已禁用时直接返回，零开销
    if not _log_enabled:
        return

    if level_val < _cached_log_level:
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] [{level.upper()}] {message}"

    if level in ("error", "critical"):
        print(entry, file=sys.stderr)
    else:
        print(entry)


def set_log_level(level: str):
    """动态设置日志级别"""
    global _cached_log_level
    level = level.lower()
    if level not in LOG_LEVELS:
        return False
    _cached_log_level = LOG_LEVELS[level]
    return True


def get_log_level() -> str:
    """获取当前日志级别名称"""
    current = _cached_log_level
    for name, value in LOG_LEVELS.items():
        if value == current:
            return name
    return "info"


# 日志接口：log.debug/info/warning/error/critical(message)
log = SimpleNamespace(
    debug=lambda message: _log("debug", message),
    info=lambda message: _log("info", message),
    warning=lambda message: _log("warning", message),
    error=lambda message: _log("error", message),
    critical=lambda message: _log("critical", message),
)


def is_enabled(level: str = "info") -> bool:
    """判断指定级别日志当前是否会输出。

    热路径上拼接代价高的 debug 日志（如逐 chunk 原始内容），
    应先调用 log.is_enabled("debug") 再构造字符串：
        if log.is_enabled("debug"):
            log.debug(f"expensive {data}")
    """
    if not _log_enabled:
        return False
    return LOG_LEVELS.get(level, LOG_LEVELS["info"]) >= _cached_log_level


def get_buffered_logs(limit: int = 500, min_level: str = "debug") -> list[dict]:
    """读取环形缓冲日志（新→旧），按最低级别过滤。

    min_level 语义为"该级别及以上"：warning 表示 warning/error/critical。
    """
    threshold = LOG_LEVELS.get((min_level or "debug").lower(), 0)
    with _LOG_BUFFER_LOCK:
        items = [
            dict(r)
            for r in _LOG_BUFFER
            if LOG_LEVELS.get(r["level"], 0) >= threshold
        ]
    items.reverse()  # deque 按时间旧→新追加，展示取新→旧
    if limit and limit > 0:
        items = items[:limit]
    return items


def clear_buffered_logs() -> int:
    """清空环形缓冲，返回清除的条数"""
    with _LOG_BUFFER_LOCK:
        count = len(_LOG_BUFFER)
        _LOG_BUFFER.clear()
    return count


def format_buffered_logs() -> str:
    """把环形缓冲格式化为纯文本（下载/复制用），旧→新按时间排列"""
    with _LOG_BUFFER_LOCK:
        snapshot = [dict(r) for r in _LOG_BUFFER]
    lines = [
        f"[{r['ts']}] [{r['level'].upper():8s}] [{r['source']}] {r['message']}"
        for r in snapshot
    ]
    return "\n".join(lines) if lines else "(日志缓冲为空)"


class _LoggingCaptureHandler(logging.Handler):
    """标准 logging 桥接：把 uvicorn / 第三方库的 logging 记录进环形缓冲"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + "".join(
                    logging.Formatter().formatException(record.exc_info)
                )
            _capture(record.levelname, message, record.name)
        except Exception:
            # 捕获失败静默，避免日志系统自身引发错误
            pass


def install_logging_capture() -> None:
    """把标准 logging（uvicorn 访问/错误日志、第三方库）桥接到环形缓冲。

    必须在 uvicorn 完成 logging dictConfig 之后调用（放 lifespan 启动
    阶段），否则挂载的 handler 会被 uvicorn 的 logging 配置覆盖。
    """
    handler = _LoggingCaptureHandler(level=logging.INFO)
    # uvicorn 与 uvicorn.access 均 propagate=False，需分别挂载；
    # uvicorn.error 传播到 "uvicorn"，无需单独挂载
    for name in ("", "uvicorn", "uvicorn.access"):
        logger_obj = logging.getLogger(name)
        if handler not in logger_obj.handlers:
            logger_obj.addHandler(handler)


# 导出的公共接口
__all__ = [
    "log",
    "set_log_level",
    "get_log_level",
    "LOG_LEVELS",
    "is_enabled",
    "get_buffered_logs",
    "clear_buffered_logs",
    "format_buffered_logs",
    "install_logging_capture",
]
