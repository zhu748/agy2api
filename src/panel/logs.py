"""
运行日志路由模块 - 面板内直接查看服务运行日志（进程内环形缓冲）

数据源为 log.py 的环形缓冲：应用自身的 log.* 输出 + 通过
install_logging_capture 桥接进来的 uvicorn/标准 logging 记录。
凭证写入失败、OAuth 异常等此前只能去 Render 控制台翻的错误，
现在可在面板"运行日志"页直接看到。
"""

from fastapi import APIRouter, Depends, Query, Response

from log import clear_buffered_logs, format_buffered_logs, get_buffered_logs
from src.utils import verify_panel_token


# 创建路由器（vite dev 代理已预置 /logs -> 后端）
router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("/recent")
async def recent_logs(
    limit: int = Query(500, ge=1, le=3000),
    level: str = Query("all"),
    token: str = Depends(verify_panel_token),
):
    """读取最近的运行日志（新→旧）

    level 为最低级别语义：all/debug 返回全部，warning 返回
    warning/error/critical，以此类推；关键字搜索由前端本地过滤。
    """
    min_level = "debug" if level in ("all", "") else level
    items = get_buffered_logs(limit=limit, min_level=min_level)
    return {
        "items": items,
        "total": len(items),
        "max_size": 3000,
        "level": level,
    }


@router.post("/clear")
async def clear_logs(token: str = Depends(verify_panel_token)):
    """清空运行日志缓冲"""
    cleared = clear_buffered_logs()
    return {"message": f"已清空 {cleared} 条运行日志", "cleared": cleared}


@router.get("/download")
async def download_logs(token: str = Depends(verify_panel_token)):
    """下载全部运行日志（纯文本，旧→新）"""
    content = format_buffered_logs()
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=agy2api-runtime-logs.txt"
        },
    )
