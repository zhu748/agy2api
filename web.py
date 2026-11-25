"""
Main Web Integration - Integrates all routers and modules
集合router并开启主服务
"""

import json
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

from src.keepalive import keepalive_service
from src.tasks import create_background_task

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

    # 环境变量凭据注入（CREDENTIALS_JSON / CREDENTIALS_JSON_2..N）：
    # 无持久磁盘的 PaaS 部署（Render Free 等）重启后凭据全丢，凭据放在
    # 环境变量里每次启动自动重建（同 email 覆盖，幂等注入）。导入含
    # OAuth 刷新等网络请求，放后台任务避免阻塞启动。
    try:
        _inject_credentials_from_env()
    except Exception as e:
        log.warning(f"环境变量凭据注入调度失败（不影响启动）: {e}")

    # 出站保活（KEEPALIVE_URL，未配置时不启动、零开销，见 src/keepalive.py）：
    # 定期 GET 目标 URL 自我唤醒，防 Render 等免费平台冻结闲置实例。
    try:
        await keepalive_service.start()
    except Exception as e:
        log.warning(f"保活服务启动失败（不影响主服务）: {type(e).__name__} - {e}")

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

    # 停止出站保活任务（幂等；未启动时为空操作）
    try:
        await keepalive_service.stop()
    except Exception as e:
        log.debug(f"停止保活任务时出错: {e}")

    log.info("AGY2API 主服务已停止")


def _collect_env_credentials() -> list[dict]:
    """收集环境变量中的凭据 JSON（CREDENTIALS_JSON + 编号变量 _2..N）。

    单个变量放 JSON 数组（多条凭据）或 JSON 对象（单条）；编号变量
    CREDENTIALS_JSON_2、CREDENTIALS_JSON_3 ... 逐条追加，适合无法
    传入超长环境变量的平台分批注入。解析失败的变量跳过并记日志。
    """
    payloads: list[dict] = []
    names = ["CREDENTIALS_JSON"] + [
        f"CREDENTIALS_JSON_{i}" for i in range(2, 51)
    ]
    for name in names:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as e:
            log.warning(f"{name} 不是合法 JSON，已跳过: {e}")
            continue
        if isinstance(data, dict):
            payloads.append(data)
        elif isinstance(data, list):
            payloads.extend(item for item in data if isinstance(item, dict))
        else:
            log.warning(f"{name} 必须是 JSON 对象或数组，已跳过")
    return payloads


def _inject_credentials_from_env() -> None:
    """启动期凭据注入调度（fire-and-forget 后台任务，不阻塞启动）。

    每条凭据走面板同款导入链路（_import_single_credential）：缺失字段
    自动补齐、同 email 覆盖，天然幂等——重启即重建，适配无持久磁盘的
    PaaS（Render Free 等）部署。
    """
    payloads = _collect_env_credentials()
    if not payloads:
        return

    log.info(f"检测到环境变量凭据注入: {len(payloads)} 条，后台导入中")

    async def _run_import() -> None:
        # 循环依赖（panel.creds -> config -> ... 链上模块在 lifespan 阶段
        # 可能尚未完全就绪），延迟导入
        from src.panel.creds import _import_single_credential

        ok = 0
        for data in payloads:
            try:
                result = await _import_single_credential(data)
                if result.get("status") == "success":
                    ok += 1
                else:
                    log.warning(
                        f"环境变量凭据导入失败 ({result.get('email', 'unknown')}): "
                        f"{result.get('message', '未知原因')}"
                    )
            except Exception as e:
                log.warning(f"环境变量凭据导入异常: {type(e).__name__} - {e}")
        log.info(f"环境变量凭据注入完成: 成功 {ok}/{len(payloads)}")

    create_background_task(_run_import())


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


async def _resolve_server_bind_address() -> tuple:
    """
    解析服务绑定地址：环境变量优先，其次数据库配置，最后默认值。

    在临时事件循环中读取数据库配置后立即关闭数据库引擎——异步引擎
    绑定在创建它的事件循环上，若保留到 uvicorn 的新循环会报
    "attached to a different loop"。关闭后应用启动时（lifespan）会
    在正确的循环中重新初始化。
    """
    import config

    host = port = None
    try:
        host = await config.get_server_host()
        port = await config.get_server_port()
    finally:
        try:
            from src.storage.database import close_database
            await close_database()
        except Exception:
            pass
    return host, port


def main():
    """主启动函数：uvicorn 直接运行 ASGI 应用"""
    import asyncio
    import uvicorn

    workers = int(os.environ.get("WORKERS", 1))

    # 绑定地址：环境变量 > 面板(数据库)配置 > 默认值。
    # 此前面板保存的 host/port 仅存库未被使用，重启后始终回落环境变量，
    # 现在让面板配置真正生效。
    try:
        host, port = asyncio.run(_resolve_server_bind_address())
    except Exception as e:
        log.warning(f"读取数据库中的 host/port 配置失败，使用环境变量/默认值: {e}")
        host = os.environ.get("HOST", "127.0.0.1")
        port = int(os.environ.get("PORT", 7861))

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
