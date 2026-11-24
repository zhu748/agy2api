"""
Main Web Integration - Integrates all routers and modules
集合router并开启主服务
"""

import os
from contextlib import asynccontextmanager

# 加载项目根目录的 .env（须在导入 src.utils 等读取环境变量的模块之前执行）
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from log import log

# Import managers and utilities
# credential_manager 已重构为无状态函数模块（无需初始化/关闭）

# Import all routers
from src.router.antigravity.openai import router as antigravity_openai_router
from src.router.antigravity.gemini import router as antigravity_gemini_router
from src.router.antigravity.anthropic import router as antigravity_anthropic_router
from src.router.antigravity.model_list import router as antigravity_model_list_router
from src.panel import router as panel_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    log.info("启动 AGY2API 主服务")

    # 启动期安全自检：弱密码/暴露面组合告警
    try:
        import config as _config

        _weak_passwords = {"pwd", "password", "123456", "admin", ""}
        _api_pw = await _config.get_api_password()
        _panel_pw = await _config.get_panel_password()
        _host = await _config.get_server_host()
        _weak_hits = [
            name
            for name, value in (("API_PASSWORD", _api_pw), ("PANEL_PASSWORD", _panel_pw))
            if value in _weak_passwords
        ]
        if _weak_hits:
            log.warning("=" * 60)
            log.warning("安全告警：检测到弱/默认密码（" + ", ".join(_weak_hits) + "）")
            log.warning(
                "请立即修改 .env 或面板中的密码后再对外使用；"
                "默认密码叠加公网监听会导致面板与 API 完全暴露。"
            )
            if _host not in ("127.0.0.1", "localhost", "::1"):
                log.warning(
                    f"当前 HOST={_host} 正在监听非回环地址，弱密码 + 对外监听"
                    "组合风险极高，请立即改密或将 HOST 改为 127.0.0.1。"
                )
            log.warning("=" * 60)
    except Exception as e:
        log.warning(f"启动期安全自检失败（不影响启动）: {e}")

    # 初始化配置缓存（优先执行）
    try:
        import config
        await config.init_config()
        log.info("配置缓存初始化成功")
    except Exception as e:
        log.error(f"配置缓存初始化失败: {e}")

    # 预热数据库连接（提前暴露配置问题）
    try:
        from src.storage.database import get_database
        await get_database()
        log.info("数据库连接就绪")
    except Exception as e:
        log.error(f"数据库初始化失败: {e}")

    # 初始化会话凭证缓存（Redis 可选，未配置时使用内存后端）
    try:
        import os as os_module
        from src import session_cache

        redis_url = os_module.getenv("REDIS_URL", "")
        if redis_url:
            await session_cache.init_redis(redis_url)
        else:
            log.info("未配置 REDIS_URL，会话凭证缓存使用内存后端")
    except Exception as e:
        log.warning(f"会话凭证缓存初始化失败（使用内存后端）: {e}")

    # OAuth回调服务器将在需要时按需启动

    yield

    # 清理资源
    log.info("开始关闭 AGY2API 主服务")

    # 关闭凭证管理器与数据库
    try:
        from src.usage_log import flush_usage_logs
        await flush_usage_logs()
        log.info("请求日志队列已落库")
    except Exception as e:
        log.error(f"落库请求日志时出错: {e}")

    try:
        from src.storage.database import close_database
        await close_database()
        log.info("数据库已关闭")
    except Exception as e:
        log.error(f"关闭数据库时出错: {e}")

    # 关闭共享 httpx 客户端（释放连接池）
    try:
        from src.httpx_client import close_shared_clients
        await close_shared_clients()
        log.info("HTTP 客户端已关闭")
    except Exception as e:
        log.error(f"关闭 HTTP 客户端时出错: {e}")

    # 关闭会话凭证缓存
    try:
        from src import session_cache
        await session_cache.close()
    except Exception as e:
        log.debug(f"关闭会话缓存时出错: {e}")

    log.info("AGY2API 主服务已停止")


class IPWhitelistMiddleware:
    """
    IP 白名单中间件（纯 ASGI，覆盖面板与全部 API 路由）

    配置 ip_whitelist 非空时，仅允许命中白名单的客户端 IP 访问，
    其余请求直接返回 403。白名单为空时不做任何限制（热更新即时生效）。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        import config

        try:
            enabled, rules = await config.get_ip_whitelist_rules()
        except Exception as e:
            # 配置读取失败时放行，避免锁定全部流量
            log.error(f"读取 IP 白名单配置失败，已放行本次请求: {e}")
            await self.app(scope, receive, send)
            return

        if enabled:
            client_ip = (scope.get("client") or (None, None))[0]
            if not client_ip or not config.ip_in_whitelist(client_ip, rules):
                log.warning(f"IP 白名单拦截请求: {client_ip} -> {scope.get('path', '')}")
                headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
                accept = headers.get("accept", "")
                response: JSONResponse | PlainTextResponse
                if "application/json" in accept or scope["path"].startswith(("/v1/", "/config/")):
                    response = JSONResponse(
                        status_code=403,
                        content={"error": {"message": "IP not allowed", "code": 403}},
                    )
                else:
                    response = PlainTextResponse("403 Forbidden: IP not allowed", status_code=403)
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


class PathPrefixMiddleware:
    """
    路径前缀剥离中间件（纯 ASGI）

    反代保留路径前缀时（如 location /AGY2API/ 搭配不带 URI 的 proxy_pass），
    请求路径形如 /AGY2API/xxx。配置 PATH_PREFIX 后，中间件在路由匹配前
    剥掉该前缀；未配置时请求原样下发。
    """

    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                scope["path"] = path[len(self.prefix):] or "/"
                raw_path = scope.get("raw_path")
                if isinstance(raw_path, bytes) and raw_path.startswith(
                    self.prefix.encode("ascii")
                ):
                    scope["raw_path"] = raw_path[len(self.prefix):] or b"/"
        await self.app(scope, receive, send)


# 创建FastAPI应用
app = FastAPI(
    title="AGY2API",
    description="Antigravity API proxy with OpenAI / Gemini / Claude compatibility",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS 中间件：默认不启用（仅同源访问，面板与文档均不受影响）。
# 需要跨域访问时通过 CORS_ALLOW_ORIGINS 显式放行，逗号分隔，例如：
#   CORS_ALLOW_ORIGINS=https://panel.example.com,https://app.example.com
# 安全说明：不再默认放行任意来源 —— 与开放鉴权端点叠加时，
# 任意网页均可读取响应体（含 /creds/download-all 凭证导出）。
_cors_origins = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",")
    if origin.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    log.info(f"CORS 已启用，允许来源: {_cors_origins}")
else:
    log.info("CORS 未配置，仅允许同源访问（需跨域时设置 CORS_ALLOW_ORIGINS）")

# IP 白名单中间件（先于 CORS 与所有路由执行）
app.add_middleware(IPWhitelistMiddleware)

# 路径前缀剥离中间件（配置 PATH_PREFIX 时启用，先于白名单与所有路由执行，
# 使反代保留前缀的请求按剥掉前缀后的路径匹配路由）
path_prefix = os.environ.get("PATH_PREFIX", "").rstrip("/")
if path_prefix:
    app.add_middleware(PathPrefixMiddleware, prefix=path_prefix)
    log.info(f"已启用路径前缀剥离: {path_prefix}")

# 挂载路由器
# Antigravity路由 - 处理OpenAI格式请求并转换为Antigravity API
app.include_router(antigravity_openai_router, prefix="", tags=["Antigravity OpenAI API"])

# Antigravity路由 - 处理Gemini格式请求并转换为Antigravity API
app.include_router(antigravity_gemini_router, prefix="", tags=["Antigravity Gemini API"])

# Antigravity模型列表路由 - 处理Gemini格式的模型列表请求
app.include_router(antigravity_model_list_router, prefix="", tags=["Antigravity Model List"])

# Antigravity Anthropic Messages 路由 - Anthropic Messages 格式兼容
app.include_router(antigravity_anthropic_router, prefix="", tags=["Antigravity Anthropic Messages"])

# Panel路由 - 包含认证、凭证管理和控制面板功能
app.include_router(panel_router, prefix="", tags=["Panel Interface"])

# 静态文件路由 - 服务docs目录下的文件（目录不存在时跳过）
if os.path.isdir("docs"):
    app.mount("/docs", StaticFiles(directory="docs"), name="docs")


def main():
    """主启动函数：uvicorn 直接运行 ASGI 应用"""
    import uvicorn

    port = int(os.environ.get("PORT", 7861))
    host = os.environ.get("HOST", "127.0.0.1")
    workers = int(os.environ.get("WORKERS", 1))

    log.info("=" * 60)
    log.info("启动 AGY2API")
    log.info("=" * 60)
    log.info(f"控制面板: http://127.0.0.1:{port}")
    if workers > 1:
        log.info(f"Worker 数量: {workers}")
    log.info("=" * 60)

    uvicorn.run(
        "web:app",
        host=host,
        port=port,
        workers=workers if workers > 1 else None,
        log_level="info",
    )


if __name__ == "__main__":
    main()
