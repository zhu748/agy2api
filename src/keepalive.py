"""
出站保活服务（移植自同作者 aistudio-api 的 outbound_keepalive 模块，适配本项目约定）

定期向配置的 URL 发送 GET 请求，典型用途：
- 保活本服务自己的公网部署：Render / HuggingFace Space 等免费平台会
  冻结闲置实例，把 URL 填成自己的 ``https://你的域名/healthz`` 即可
  定期自我唤醒（本服务已提供无鉴权的 ``/healthz`` 端点，见 panel/root.py）；
- 或驱动外部监控端点（UptimeRobot 等）。

配置（环境变量，启动时读取一次）：
- ``KEEPALIVE_URL``：保活目标 URL；留空 = 不启动任何任务
  （零开销），必须以 http:// 或 https:// 开头。
- ``KEEPALIVE_INTERVAL``：请求间隔秒数，默认 60，下限 5。
- ``KEEPALIVE_ALLOW_PRIVATE``：允许私网 / 环回保活目标，
  默认拒绝（防内网探测），仅自我保活 / 内网监控场景显式开启。

保活请求**直连**出站、不走出站代理：目标通常是公网监控端点或本服务
自身，跟随上游代理反而会在代理不可用时让保活一并失效。单请求超时
30 秒；失败只记日志，绝不影响服务本身。

与在途请求的流式心跳 / 非流式空白保活是两个不同层面的机制，本模块
只服务于实例存活（防 PaaS 平台冻结闲置实例）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import os

import httpx

from log import log

# 单次保活请求的超时（秒）。
_REQUEST_TIMEOUT_SECONDS = 30.0
# 间隔下限（秒）：防止误配成秒级高频请求打爆保活目标。
_MIN_INTERVAL_SECONDS = 5
_MAX_REDIRECTS = 5

# 已知的云元数据域名（静态可判）：域名型元数据端点无法用地址段判段。
_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata.goog"})


def _keepalive_host_allowed(host: str, allow_private: bool = False) -> bool:
    """保活目标的地址段裁决。

    云元数据 / 链路本地段（169.254.169.254、metadata.google.internal 等）
    无论如何都不是合法保活目标：会话被伪造后可借保活任务盲探元数据端点
    收割凭据。环回与私网段需显式开启 allow_private：被劫持的管理会话
    可用保活任务对内网管理面稳定重放 GET 并经日志回读状态码（探测
    oracle），默认拒绝、自我保活用户显式确认后放行。
    """
    if host.lower() in _METADATA_HOSTS:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return True  # 普通域名无法静态判段；解析交给出站层
    if (
        addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return False
    if addr.is_loopback or addr.is_private:
        return allow_private
    return True


def validate_keepalive_url(url: str, allow_private: bool = False) -> bool:
    """校验保活 URL 的静态合法性（scheme / 主机名 / 端口 / 凭据与地址段）。"""
    if not url:
        return True
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError):
        return False
    if not (parsed.scheme in ("http", "https") and bool(parsed.host)):
        return False
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        return False
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        return False
    # 拒绝内嵌凭据（userinfo）的 URL：保活没有鉴权语义，凭据只会在
    # 启动/运行日志里原样泄露（GET {url}）。
    authority = url.split("://", 1)[-1].split("/", 1)[0]
    if "@" in authority:
        return False
    # 链路本地 / 云元数据地址段拒绝（见 _keepalive_host_allowed）。
    # 重定向目标经本函数复检，同一口径；私网/环回需 allow_private
    # 显式放行。
    return _keepalive_host_allowed(parsed.host, allow_private=allow_private)


def _env_bool(name: str, default: bool = False) -> bool:
    """读取布尔环境变量（true/1/yes/on 为真）。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


class OutboundKeepAliveService:
    """保活服务：定期向指定 URL 发送 GET 请求（未配置 URL 时零开销）。"""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()

    @staticmethod
    def _current_config() -> tuple[str, int]:
        """读取当前生效配置（环境变量 > 默认值）。"""
        url = (os.getenv("KEEPALIVE_URL") or "").strip()
        try:
            interval = int(os.getenv("KEEPALIVE_INTERVAL") or "60")
        except (TypeError, ValueError, OverflowError):
            interval = 60
        return url, interval

    @staticmethod
    def _allow_private() -> bool:
        return _env_bool("KEEPALIVE_ALLOW_PRIVATE", default=False)

    async def _run(self, url: str, interval: int) -> None:
        allow_private = self._allow_private()
        log.info(f"保活任务启动: GET {url}, 间隔 {interval}s")
        try:
            # 客户端随任务生命周期创建/关闭：取消时 async with 保证连接池
            # 不悬挂。直连出站（trust_env=False），不跟随 PROXY 环境代理：
            # 保活目标通常是本服务自身或公网监控端点，代理不可用时保活
            # 不应一并失效。
            async with httpx.AsyncClient(
                timeout=_REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                while True:
                    try:
                        # Only response headers are needed. A large or endless
                        # body must not consume memory or hold the task forever.
                        async with asyncio.timeout(_REQUEST_TIMEOUT_SECONDS):
                            current = httpx.URL(url)
                            for redirect in range(_MAX_REDIRECTS + 1):
                                async with client.stream("GET", current) as response:
                                    if response.has_redirect_location:
                                        if redirect == _MAX_REDIRECTS:
                                            raise ValueError("保活重定向次数超过上限。")
                                        current = current.join(
                                            response.headers["location"]
                                        )
                                        if not validate_keepalive_url(
                                            str(current), allow_private=allow_private
                                        ):
                                            raise ValueError("保活重定向地址无效。")
                                        continue
                                    log.info(
                                        f"保活 GET {url} -> {response.status_code}"
                                    )
                                    break
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        # 单次失败（网络抖动/目标临时不可用）只记日志，下一轮照常。
                        log.warning(f"保活 GET {url} 失败: {type(e).__name__} - {e}")
                    await asyncio.sleep(interval)
        finally:
            log.info("保活任务已停止")

    async def start(self) -> None:
        """按当前配置启动保活任务；未配置 URL / 配置非法时不启动。"""
        async with self._lifecycle_lock:
            self._start()

    def _start(self) -> None:
        if self._task and not self._task.done():
            # 已有任务在运行，不重复启动。
            return

        url, interval = self._current_config()
        if not url:
            # 未配置：静默跳过，不在每次启动时给不使用保活的用户刷日志。
            return
        if not validate_keepalive_url(
            url, allow_private=self._allow_private()
        ):
            if validate_keepalive_url(url, allow_private=True):
                log.warning(
                    "保活目标是私网/环回地址：被劫持的管理会话可借保活任务探测内网。"
                    "确属自我保活/内网监控场景时，请设置 KEEPALIVE_ALLOW_PRIVATE=1 后重启。"
                )
            else:
                log.warning("保活 URL 必须是包含主机名的合法 HTTP(S) 地址，保活未启动")
            return
        if interval < _MIN_INTERVAL_SECONDS:
            log.warning(
                f"保活间隔 {interval}s 低于下限 {_MIN_INTERVAL_SECONDS}s，保活未启动"
            )
            return
        self._task = asyncio.create_task(
            self._run(url, interval), name="keepalive_service"
        )

    async def stop(self) -> None:
        """停止保活任务（幂等；未运行时为空操作）。"""
        async with self._lifecycle_lock:
            await self._stop()

    async def _stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @property
    def is_running(self) -> bool:
        """当前保活任务是否在运行。"""
        return self._task is not None and not self._task.done()


# 全局保活服务实例（web.py lifespan 启停共用）。
keepalive_service = OutboundKeepAliveService()
