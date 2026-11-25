"""
后台任务引用管理

asyncio.create_task 返回的 Task 只被事件循环弱引用，fire-and-forget
任务若不保存引用，可能在执行中途被垃圾回收而静默消失（CPython 官方文档
明确要求保存结果引用）。本模块用模块级集合持有后台任务引用，完成后自动移除。
"""

import asyncio
from typing import Coroutine

# 后台任务引用集合（防止被 GC 中途取消）
_background_tasks: set = set()


def create_background_task(coro: Coroutine) -> asyncio.Task:
    """创建 fire-and-forget 后台任务并持有引用，防止被垃圾回收"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
