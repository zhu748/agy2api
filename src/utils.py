import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Dict, Optional

import httpx

from config import get_api_password, get_panel_password, verify_panel_password
from fastapi import Depends, HTTPException, Header, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from log import log

# HTTP Bearer security scheme
security = HTTPBearer()

# ====================== OAuth Configuration ======================

# Antigravity CLI 客户端仿真常量
_DEFAULT_ANTIGRAVITY_CLI_VERSION = "1.5.16"

# 版本来源：官方 CLI 发布清单（与 antigravity.google/cli/install.sh 同源）。
# 版本号与平台无关，任取一份即可；两份用于容灾。
_CLI_MANIFEST_URLS = (
    "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/windows_amd64.json",
    "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_amd64.json",
)


def _get_latest_antigravity_cli_version() -> str:
    """从官方发布清单获取最新 Antigravity CLI 版本；全部失败时返回默认版本。"""
    for url in _CLI_MANIFEST_URLS:
        try:
            response = httpx.get(url, timeout=10.0)
            response.raise_for_status()
            manifest = response.json()
        except (httpx.HTTPError, ValueError) as error:
            log.debug(f"获取 Antigravity CLI 版本清单失败（{url}）：{error}")
            continue

        version = manifest.get("version") if isinstance(manifest, dict) else None
        if isinstance(version, str) and version.strip():
            return version.strip().lstrip("vV")

    log.warning(
        f"官方版本清单均不可用，使用默认版本 {_DEFAULT_ANTIGRAVITY_CLI_VERSION}"
    )
    return _DEFAULT_ANTIGRAVITY_CLI_VERSION


# CLI 版本：默认值立即可用，最新版由后台线程获取后热替换。
# 不在 import 时同步请求网络（避免启动被阻塞最多 10 秒）。
ANTIGRAVITY_CLI_VERSION = _DEFAULT_ANTIGRAVITY_CLI_VERSION

# UA 构造常量（对齐官方 CLI v1.5.16 实测抓包格式）
ANTIGRAVITY_CLI_OS_TYPE = "windows"
ANTIGRAVITY_CLI_ARCH = "amd64"
_ANTIGRAVITY_CLI_BUILD_CL = "1028571191"  # 官方 1.5.16 构建的 changelist 号


def get_antigravity_user_agent(purpose: Optional[str] = None) -> str:
    """返回 Antigravity CLI 仿真 User-Agent（版本由后台线程异步刷新）。

    对齐官方 CLI v1.5.16 实测格式：
        antigravity/cli/1.5.16 (aidev_client; os_type=windows; arch=amd64;
        cl=1028571191; auth_method=oauth-personal[; purpose=<purpose>])

    auth_method 与官方一致地按登录方式取值（oauth-personal / business /
    aicode / gemini_api_key / adc）；本项目走个人 Google OAuth，固定为
    oauth-personal。purpose 为空时省略该段（与官方 generateContent 请求
    的 UA 一致；模型可用性探测等用途会携带 purpose）。
    """
    parts = [
        "aidev_client",
        f"os_type={ANTIGRAVITY_CLI_OS_TYPE}",
        f"arch={ANTIGRAVITY_CLI_ARCH}",
        f"cl={_ANTIGRAVITY_CLI_BUILD_CL}",
        "auth_method=oauth-personal",
    ]
    if purpose:
        parts.append(f"purpose={purpose}")
    joined = "; ".join(parts)
    return f"antigravity/cli/{ANTIGRAVITY_CLI_VERSION} ({joined})"


def _refresh_cli_version_in_background() -> None:
    """后台线程获取最新 CLI 版本，完成后替换默认值"""

    def _worker():
        global ANTIGRAVITY_CLI_VERSION
        version = _get_latest_antigravity_cli_version()
        if version != ANTIGRAVITY_CLI_VERSION:
            ANTIGRAVITY_CLI_VERSION = version
            log.info(f"当前 Antigravity CLI 版本已更新为: {version}")
        else:
            log.info(f"当前 Antigravity CLI 版本: {version}")

    threading.Thread(target=_worker, daemon=True, name="antigravity-cli-version").start()


_refresh_cli_version_in_background()

# Antigravity OAuth Configuration
# 从环境变量读取（.env 会被自动加载），环境变量优先。
# 默认值为官方 CLI v1.5.18 (linux-x64) 所用的 Google OAuth client 凭据，
# 2026-11-27 从官方二进制提取，已经 Google token 端点与端到端授权验证。
# 该凭据随官方 CLI 公开分发，属公开常量而非机密。
ANTIGRAVITY_CLIENT_ID = os.getenv(
    "ANTIGRAVITY_CLIENT_ID",
    "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com",
)
ANTIGRAVITY_CLIENT_SECRET = os.getenv(
    "ANTIGRAVITY_CLIENT_SECRET",
    "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",
)
ANTIGRAVITY_SCOPES = [
    'https://www.googleapis.com/auth/cloud-platform',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    'https://www.googleapis.com/auth/cclog',
    'https://www.googleapis.com/auth/experimentsandconfigs',
    # 2026-11 起官方 CLI 授权 URL 新增的两个 scope（实测抓包确认）
    'https://www.googleapis.com/auth/aicode',
    'openid',
]

# 回调服务器配置
CALLBACK_HOST = "localhost"

# ====================== Model Helper Functions ======================

# 抗截断模型前缀
_ANTI_TRUNCATION_PREFIX = "抗截断-"


def is_anti_truncation_model(model_name: str) -> bool:
    """Check if model name indicates anti-truncation should be used."""
    return model_name.startswith(_ANTI_TRUNCATION_PREFIX)


def get_base_model_from_feature_model(model_name: str) -> str:
    """Get base model name from feature model name."""
    if model_name.startswith(_ANTI_TRUNCATION_PREFIX):
        return model_name[len(_ANTI_TRUNCATION_PREFIX) :]
    return model_name


# ====================== Authentication Functions ======================

async def authenticate_flexible(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    access_token: Optional[str] = Header(None, alias="access_token"),
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    x_anthropic_auth_token: Optional[str] = Header(None, alias="x-anthropic-auth-token"),
    anthropic_auth_token: Optional[str] = Header(None, alias="anthropic-auth-token"),
    key: Optional[str] = Query(None)
) -> str:
    """
    统一的灵活认证函数，支持多种认证方式

    此函数可以直接用作 FastAPI 的 Depends 依赖

    支持的认证方式:
        - URL 参数: key
        - HTTP 头部: Authorization (Bearer token)
        - HTTP 头部: x-api-key
        - HTTP 头部: access_token
        - HTTP 头部: x-goog-api-key
        - HTTP 头部: x-anthropic-auth-token
        - HTTP 头部: anthropic-auth-token

    Args:
        request: FastAPI Request 对象
        authorization: Authorization 头部值（自动注入）
        x_api_key: x-api-key 头部值（自动注入）
        access_token: access_token 头部值（自动注入）
        x_goog_api_key: x-goog-api-key 头部值（自动注入）
        x_anthropic_auth_token: x-anthropic-auth-token 头部值（自动注入）
        anthropic_auth_token: anthropic-auth-token 头部值（自动注入）
        key: URL 参数 key（自动注入）

    Returns:
        验证通过的token

    Raises:
        HTTPException: 认证失败时抛出异常

    使用示例:
        @router.post("/endpoint")
        async def endpoint(token: str = Depends(authenticate_flexible)):
            # token 已验证通过
            pass
    """
    password = await get_api_password()
    token = None
    auth_method = None

    # 1. 尝试从 URL 参数 key 获取（Google 官方标准方式）
    if key:
        token = key
        auth_method = "URL parameter 'key'"

    # 2. 尝试从 x-goog-api-key 头部获取（Google API 标准方式）
    elif x_goog_api_key:
        token = x_goog_api_key
        auth_method = "x-goog-api-key header"

    # 3. 尝试从 x-anthropic-auth-token 头部获取（Anthropic 标准方式）
    elif x_anthropic_auth_token:
        token = x_anthropic_auth_token
        auth_method = "x-anthropic-auth-token header"

    # 4. 尝试从 anthropic-auth-token 头部获取（Anthropic 替代方式）
    elif anthropic_auth_token:
        token = anthropic_auth_token
        auth_method = "anthropic-auth-token header"

    # 5. 尝试从 x-api-key 头部获取
    elif x_api_key:
        token = x_api_key
        auth_method = "x-api-key header"

    # 6. 尝试从 access_token 头部获取
    elif access_token:
        token = access_token
        auth_method = "access_token header"

    # 7. 尝试从 Authorization 头部获取
    elif authorization:
        if not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication scheme. Use 'Bearer <token>'",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = authorization[7:]  # 移除 "Bearer " 前缀
        auth_method = "Authorization Bearer header"

    # 检查是否提供了任何认证凭据
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials. Use 'key' URL parameter, 'x-goog-api-key', 'x-anthropic-auth-token', 'anthropic-auth-token', 'x-api-key', 'access_token' header, or 'Authorization: Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # 验证 token（常量时间比较，避免时序侧信道泄露密码前缀信息）
    if not secrets.compare_digest(token.encode("utf-8"), password.encode("utf-8")):
        log.debug(f"Authentication failed using {auth_method}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="密码错误"
        )
    
    log.debug(f"Authentication successful using {auth_method}")
    return token


# 为了保持向后兼容，保留旧函数名作为别名
authenticate_bearer = authenticate_flexible
authenticate_gemini_flexible = authenticate_flexible


# ====================== Panel Authentication Functions ======================

# 面板会话令牌：无状态 HMAC 签名令牌（跨 worker/重启均有效）。
# 载荷为过期时刻，密钥为当前存储的面板密码（哈希串或明文）——
# 密码变更后旧令牌自动全部失效，无需服务端集中吊销。
# 默认 24 小时有效期。
_PANEL_SESSION_TTL_SECONDS = 24 * 3600
_PANEL_SESSION_PREFIX = b"panel-session:"


def _panel_session_key(stored_password: str) -> bytes:
    """会话签名密钥：取存储的面板密码原文（哈希串或明文）作 HMAC 密钥。"""
    return stored_password.encode("utf-8")


def _sign_panel_session(stored_password: str, expiry: int) -> str:
    payload = _PANEL_SESSION_PREFIX + str(expiry).encode()
    signature = hmac.new(
        _panel_session_key(stored_password), payload, hashlib.sha256
    ).digest()
    token = base64.urlsafe_b64encode(payload + b"." + signature).decode()
    return token.rstrip("=")


async def issue_panel_session_token() -> str:
    """签发一个新的面板会话令牌（24 小时有效，与密码解耦且改密即失效）。"""
    stored_password = await get_panel_password()
    expiry = int(time.time()) + _PANEL_SESSION_TTL_SECONDS
    return _sign_panel_session(stored_password, expiry)


async def _is_valid_panel_session(token: str) -> bool:
    """校验签名令牌：签名匹配且未过期。"""
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload, _, signature = raw.rpartition(b".")
        if not payload or not signature:
            return False
        if not payload.startswith(_PANEL_SESSION_PREFIX):
            return False
        stored_password = await get_panel_password()
        expected = hmac.new(
            _panel_session_key(stored_password), payload, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            return False
        expiry = int(payload[len(_PANEL_SESSION_PREFIX) :])
        return expiry >= int(time.time())
    except (ValueError, TypeError, UnicodeError):
        return False


async def verify_panel_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """
    控制面板令牌验证函数（签名会话令牌优先，密码直传兼容）

    优先校验 Bearer 令牌是否为服务端签发的有效面板会话（无状态 HMAC
    签名，多 worker / 重启后仍有效）；否则回退为以面板密码直接作为
    令牌的旧用法（常量时间比较），兼容既有部署与脚本。面板密码变更
    后旧会话令牌自动失效。

    Args:
        credentials: HTTPAuthorizationCredentials 自动注入

    Returns:
        验证通过的token

    Raises:
        HTTPException: 令牌无效或密码错误时抛出401异常
    """
    token = credentials.credentials
    if token and await _is_valid_panel_session(token):
        return token
    if token and await verify_panel_password(token):
        return token
    raise HTTPException(status_code=401, detail="令牌无效或密码错误")
