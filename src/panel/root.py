"""
根路由模块 - 处理控制面板主页（编译后的前端）
"""

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from log import log


# 创建路由器
router = APIRouter(tags=["root"])

# 编译后的前端目录
FRONTEND_DIST_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "dist",
)


def _safe_join_dist(path: str) -> str:
    """安全地拼接前端静态资源路径，防止路径穿越"""
    full = os.path.normpath(os.path.join(FRONTEND_DIST_DIR, path))
    dist_root = os.path.normpath(FRONTEND_DIST_DIR)
    if not full.startswith(dist_root + os.sep) and full != dist_root:
        raise HTTPException(status_code=404, detail="Not Found")
    return full


@router.get("/healthz", include_in_schema=False)
async def healthz():
    """无鉴权健康检查端点（遵循 Kubernetes / PaaS 探活命名惯例）。

    供 Render / Fly.io / k8s 等平台的 healthCheckPath 探活，以及
    出站保活（KEEPALIVE_URL）自我唤醒使用。刻意不触上游、不查数据库、
    不做鉴权——探活请求必须零依赖零副作用，否则平台会因健康检查
    失败而反复重启实例。
    """
    return JSONResponse(
        content={"status": "ok"},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/", response_class=HTMLResponse)
async def serve_control_panel():
    """提供编译后的控制面板首页"""
    index_path = os.path.join(FRONTEND_DIST_DIR, "index.html")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail="前端资源未构建，请先运行 frontend 目录下的 npm run build",
        )
    except Exception as e:
        log.error(f"加载控制面板页面失败: {e}")
        raise HTTPException(status_code=500, detail="服务器内部错误")


@router.get("/assets/{file_path:path}")
async def serve_frontend_assets(file_path: str):
    """提供前端静态资源（JS/CSS等）"""
    full_path = _safe_join_dist(os.path.join("assets", file_path))
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(full_path)


@router.get("/favicon.ico")
async def serve_favicon():
    """提供 favicon"""
    # 优先从编译后的前端目录提供
    favicon_path = os.path.join(FRONTEND_DIST_DIR, "favicon.ico")
    if os.path.isfile(favicon_path):
        return FileResponse(favicon_path, media_type="image/x-icon")
    raise HTTPException(status_code=404, detail="Not Found")
