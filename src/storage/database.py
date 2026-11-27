"""
数据库层（SQLAlchemy 2.0 异步 ORM）
统一支持 SQLite（默认）与 PostgreSQL。
凭证以 email 为唯一键。schema 兼容策略（全部保留数据）：
- 表缺少模型列：启动时 ALTER TABLE ADD COLUMN 自动补齐
- 表存在模型外的 NOT NULL 列（旧版本/其他部署创建，如 supported_models）：
  启动时自动放宽——PostgreSQL 直接 DROP NOT NULL，SQLite 重建表并拷贝
  共有列数据；写入路径遇到 NOT NULL 违反时也会触发一次自愈修复后重试
- 键结构变更（filename -> email）：旧表结构不兼容，删除重建（数据无法迁移）
"""

import asyncio
import json
import os
import random
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import (
    Boolean,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from log import log


# ====================== ORM 模型 ======================

class Base(DeclarativeBase):
    pass


class AntigravityCredential(Base):
    """Antigravity 凭证表（email 唯一键）"""
    __tablename__ = "antigravity_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)

    # 凭证结构化字段
    access_token: Mapped[str] = mapped_column(Text, default="")
    refresh_token: Mapped[str] = mapped_column(Text, default="")
    client_id: Mapped[str] = mapped_column(Text, default="")
    client_secret: Mapped[str] = mapped_column(Text, default="")
    project_id: Mapped[str] = mapped_column(Text, default="")
    expiry: Mapped[str] = mapped_column(Text, default="")

    # 状态字段
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    error_codes: Mapped[str] = mapped_column(Text, default="[]")
    error_messages: Mapped[str] = mapped_column(Text, default="[]")
    last_success: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # 模型级冷却 (JSON: {model_name: cooldown_timestamp})
    model_cooldowns: Mapped[str] = mapped_column(Text, default="{}")

    # tier 状态 (free/pro/ultra)
    tier: Mapped[Optional[str]] = mapped_column(String(32), default="pro")

    # 时间戳
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time, onupdate=time.time)

    __table_args__ = (
        Index("idx_ag_disabled", "disabled"),
    )


class ConfigItem(Base):
    """配置表"""
    __tablename__ = "config"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class UsageLog(Base):
    """请求日志表：每次 API 调用记录一条（成功与失败均记录）"""
    __tablename__ = "usage_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)

    # 请求来源
    api_format: Mapped[str] = mapped_column(String(16), default="")        # openai / gemini / anthropic
    client_ip: Mapped[str] = mapped_column(String(64), default="")
    request_id: Mapped[str] = mapped_column(String(64), default="")

    # 调用信息
    model_name: Mapped[str] = mapped_column(String(128), default="")             # 客户端请求的模型名（原始，可能带功能前缀）
    upstream_model_name: Mapped[str] = mapped_column(String(128), default="")   # 后端实际使用的真实模型名（映射后）
    credential_email: Mapped[str] = mapped_column(String(320), default="")
    is_stream: Mapped[bool] = mapped_column(Boolean, default=False)

    # 结果
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    use_time: Mapped[float] = mapped_column(Float, default=0.0)            # 耗时（秒）

    # Token 用量
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)  # prompt 中命中缓存的 token（含在 prompt_tokens 内）

    # 失败时的错误摘要
    error_message: Mapped[str] = mapped_column(Text, default="")

    # 请求体详细日志文件索引（data/request_logs 相对路径；空=未记录）
    request_log_file: Mapped[str] = mapped_column(String(512), default="")

    __table_args__ = (
        Index("idx_ul_created_model", "created_at", "model_name"),
        Index("idx_ul_created_email", "created_at", "credential_email"),
    )


# ====================== JSON 序列化辅助 ======================

def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load_json(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _brief_db_error(err: Exception) -> str:
    """提取数据库错误的简短描述。

    SQLAlchemy 的 DBAPI 错误 str() 会附带完整 SQL 语句与全部参数值
    （含 access_token / refresh_token / client_secret 等凭据明文），
    绝不能原样写入日志：这里只保留底层错误类型与消息，顶层错误描述
    在 [SQL: 标记处截断。"""
    orig = getattr(err, "orig", None)
    if orig is not None:
        return f"{type(orig).__name__}: {str(orig)[:300]}"
    return f"{type(err).__name__}: {str(err).split('[SQL:')[0].strip()[:300]}"


def _row_to_credential_dict(row: AntigravityCredential) -> Dict[str, Any]:
    """ORM 行 -> 凭证字典"""
    return {
        "access_token": row.access_token or "",
        "refresh_token": row.refresh_token or "",
        "client_id": row.client_id or "",
        "client_secret": row.client_secret or "",
        "project_id": row.project_id or "",
        "expiry": row.expiry or "",
    }


def get_cooldown_group(model_name: str) -> str:
    """
    模型 CD 分组：同家族模型共享一个冷却键
    - 名字带 argon -> "argon"
    - 名字带 gemini -> "gemini"
    - 名字带 claude 或 gpt -> "claude_gpt"
    - 其他 -> 原始模型名
    """
    lowered = (model_name or "").lower()
    if "argon" in lowered:
        return "argon"
    if "gemini" in lowered:
        return "gemini"
    if "claude" in lowered or "gpt" in lowered:
        return "claude_gpt"
    return model_name


# ====================== 数据库管理器 ======================

class Database:
    """统一数据库管理器（SQLite / PostgreSQL）"""

    def __init__(self):
        self._engine: Optional[AsyncEngine] = None
        self._session_factory: Optional[async_sessionmaker[AsyncSession]] = None
        self._initialized = False
        self._lock = asyncio.Lock()
        self._backend_type = "sqlite"

        # 内存配置缓存
        self._config_cache: Dict[str, Any] = {}

    # ---------- 初始化 ----------

    async def initialize(self) -> None:
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            postgres_uri = os.getenv("POSTGRESQL_URI", "")
            credentials_dir = os.getenv("CREDENTIALS_DIR", "./creds")
            os.makedirs(credentials_dir, exist_ok=True)

            if postgres_uri:
                # PostgreSQL（postgres:// 或 postgresql:// -> postgresql+asyncpg://）
                dsn = postgres_uri
                if dsn.startswith("postgresql://"):
                    dsn = "postgresql+asyncpg://" + dsn[len("postgresql://"):]
                elif dsn.startswith("postgres://"):
                    dsn = "postgresql+asyncpg://" + dsn[len("postgres://"):]
                elif not dsn.startswith("postgresql+"):
                    dsn = "postgresql+asyncpg://" + dsn
                self._engine = create_async_engine(dsn, pool_size=5, max_overflow=5, pool_pre_ping=True)
                self._backend_type = "postgresql"
            else:
                # SQLite 默认
                db_path = os.path.join(credentials_dir, "credentials.db")
                self._engine = create_async_engine(
                    f"sqlite+aiosqlite:///{db_path}",
                    # 连接池：多个长连接复用（默认 NullPool 每会话新建连接开销大）；
                    # WAL 模式允许读写并发，单写者由 busy_timeout 兜底
                    pool_size=5,
                    max_overflow=10,
                    pool_pre_ping=True,
                    connect_args={"timeout": 30},
                )
                # WAL + NORMAL：读写并发 + 减少每次提交的 fsync（PRAGMA 需在连接建立后执行）
                from sqlalchemy import event

                @event.listens_for(self._engine.sync_engine, "connect")
                def _set_sqlite_pragma(dbapi_conn, conn_record):
                    cursor = dbapi_conn.cursor()
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    cursor.execute("PRAGMA busy_timeout=15000")
                    cursor.execute("PRAGMA cache_size=-8000")
                    cursor.close()

                self._backend_type = "sqlite"
                log.warning(
                    "未配置 POSTGRESQL_URI：当前使用临时 SQLite 存储（"
                    f"{db_path}）。容器重启 / 重新部署 / 平台休眠唤醒后，"
                    "OAuth 新增凭证与面板配置将全部丢失！PaaS（Render Free 等）"
                    "部署请配置 POSTGRESQL_URI 指向外部 PostgreSQL 实现持久化，"
                    "或把需要长期保留的凭据放入 CREDENTIALS_JSON 环境变量"
                    "（每次启动自动重新注入）。"
                )

            self._session_factory = async_sessionmaker(
                self._engine, expire_on_commit=False, class_=AsyncSession
            )

            # schema 兼容迁移 + 建表（各步骤独立事务：PostgreSQL 中任一语句
            # 出错会中止整个事务，无法在同一事务里"尽力而为"；单步失败仅
            # 告警不阻断启动，写入路径还有 NOT NULL 违反自愈兜底）
            await self._run_full_schema_migration()

            # 标记初始化完成（供 session() 使用），再加载配置缓存
            self._initialized = True

            # 加载配置缓存
            await self._load_config_cache()

            log.info(f"Database initialized (backend={self._backend_type})")

    def _create_tables(self, sync_conn) -> None:
        """按当前 ORM 结构建表（只创建缺失的表；已存在表的结构由迁移步骤处理）"""
        Base.metadata.create_all(sync_conn)

    # ---------- schema 兼容迁移 ----------

    async def _safe_migration_step(self, step_name: str, step_fn, *args) -> Any:
        """在独立事务中执行一个迁移步骤，失败仅告警（不阻断启动）。

        PostgreSQL 中任一语句出错会中止当前事务，因此各步骤必须各自
        独立事务，才能做到"尽力而为"式迁移。"""
        try:
            async with self._engine.begin() as conn:
                return await conn.run_sync(step_fn, *args)
        except Exception as e:
            log.error(f"数据库迁移步骤「{step_name}」失败（跳过，不影响启动）: {_brief_db_error(e)}")
            return None

    async def _run_full_schema_migration(self) -> None:
        """执行全部 schema 兼容迁移（幂等，可重复调用）"""
        await self._safe_migration_step("删除旧版表结构", self._migrate_drop_legacy_table)
        await self._safe_migration_step("补充缺失列", self._migrate_add_missing_columns)
        await self._safe_migration_step("放宽多余 NOT NULL 列", self._migrate_relax_extra_not_null)
        await self._safe_migration_step("建表", self._create_tables)

    def _migrate_drop_legacy_table(self, sync_conn) -> None:
        """删除 filename 键的旧版凭证表（键结构变更，数据无法迁移）"""
        inspector = inspect(sync_conn)
        table_name = AntigravityCredential.__tablename__
        if inspector.has_table(table_name):
            existing_columns = {c["name"] for c in inspector.get_columns(table_name)}
            if "filename" in existing_columns or "email" not in existing_columns:
                log.info("检测到旧版凭证表结构（filename 键），重建为 email 键结构")
                sync_conn.execute(text(f'DROP TABLE "{table_name}"'))

    def _migrate_add_missing_columns(self, sync_conn) -> None:
        """为已存在的表补齐 ORM 模型中缺失的列（ALTER TABLE ADD COLUMN，保留数据）"""
        inspector = inspect(sync_conn)
        dialect = inspector.bind.dialect
        for orm_model in (AntigravityCredential, ConfigItem, UsageLog):
            table = orm_model.__table__
            if not inspector.has_table(table.name):
                continue
            existing_columns = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                column_type = column.type.compile(dialect)
                # 补列：有 server_default 用之；有 Python default 用字面量；否则 DEFAULT NULL
                # （补充列不强制 NOT NULL，避免旧行无值导致 ALTER 失败）
                if column.server_default is not None:
                    default_clause = f" DEFAULT {column.server_default.arg}"
                elif column.default is not None and column.default.is_scalar:
                    default_clause = f" DEFAULT {column.default.arg!r}"
                else:
                    default_clause = " DEFAULT NULL"
                sync_conn.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}{default_clause}')
                )
                log.info(f"表 {table.name} 补充列 {column.name} ({column_type})")

    def _migrate_relax_extra_not_null(self, sync_conn) -> None:
        """放宽"表里存在但 ORM 模型不认识"的 NOT NULL 列。

        这类列通常由旧版本或其他部署变体创建（如 supported_models），
        当前代码的 INSERT 不包含它们，NOT NULL 且无默认值会导致所有
        凭证写入失败（NotNullViolationError）。处理策略：
        - PostgreSQL：ALTER COLUMN DROP NOT NULL（保留数据，结构最小变更）
        - SQLite：无法直接放宽约束，在同一事务内原地重建表（rename 旧表
          -> 按当前模型建新表 -> 拷贝共有列 -> 删除旧表）
        可为空 / 已有默认值的多余列无需处理（INSERT 缺省时自动填 NULL/默认值）。
        """
        inspector = inspect(sync_conn)
        dialect_name = inspector.bind.dialect.name
        for orm_model in (AntigravityCredential, ConfigItem, UsageLog):
            table = orm_model.__table__
            if not inspector.has_table(table.name):
                continue
            model_columns = {c.name for c in table.columns}
            extra_not_null = [
                c for c in inspector.get_columns(table.name)
                if c["name"] not in model_columns
                and not c.get("nullable", True)
                and c.get("default") is None
            ]
            if not extra_not_null:
                continue
            extra_names = ", ".join(c["name"] for c in extra_not_null)
            if dialect_name == "postgresql":
                for col in extra_not_null:
                    sync_conn.execute(text(
                        f'ALTER TABLE "{table.name}" ALTER COLUMN "{col["name"]}" DROP NOT NULL'
                    ))
                log.warning(
                    f"表 {table.name} 存在模型外的 NOT NULL 列（{extra_names}，"
                    "旧版本/其他部署创建），已自动放宽为可空"
                )
            else:
                legacy_name = f"{table.name}_legacy_{int(time.time())}"
                sync_conn.execute(text(f'ALTER TABLE "{table.name}" RENAME TO "{legacy_name}"'))
                # 旧表的命名索引跟随重命名仍占用索引名，必须先删除，
                # 否则新表创建同名索引（idx_ag_disabled 等）会冲突
                for legacy_idx in inspect(sync_conn).get_indexes(legacy_name):
                    sync_conn.execute(text(f'DROP INDEX "{legacy_idx["name"]}"'))
                table.create(sync_conn)  # 按当前模型结构（含索引/唯一键）建新表
                legacy_columns = {c["name"] for c in inspect(sync_conn).get_columns(legacy_name)}
                common = [c.name for c in table.columns if c.name in legacy_columns]
                col_list = ", ".join(f'"{c}"' for c in common)
                sync_conn.execute(text(
                    f'INSERT INTO "{table.name}" ({col_list}) SELECT {col_list} FROM "{legacy_name}"'
                ))
                sync_conn.execute(text(f'DROP TABLE "{legacy_name}"'))
                log.warning(
                    f"表 {table.name} 存在模型外的 NOT NULL 列（{extra_names}），"
                    f"已重建表并迁移 {len(common)} 列数据"
                )

    async def close(self) -> None:
        if self._engine:
            await self._engine.dispose()
            self._engine = None
        self._session_factory = None
        self._initialized = False
        log.debug("Database closed")

    def _ensure_initialized(self):
        if not self._initialized or not self._session_factory:
            raise RuntimeError("Database not initialized")

    @asynccontextmanager
    async def session(self):
        """获取数据库会话（自动提交/回滚）"""
        self._ensure_initialized()
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # ---------- 凭证管理 ----------

    @property
    def backend_type(self) -> str:
        """当前数据库后端类型（sqlite / postgresql）"""
        return self._backend_type

    async def store_credential(
        self, email: str, credential_data: Dict[str, Any]
    ) -> bool:
        """存储或更新凭证（以 email 为唯一键，保留状态字段）。

        并发导入同一 email 时可能同时走到插入分支，后提交方触发唯一键
        冲突（IntegrityError）；此时回退为更新路径，保证幂等。
        表结构漂移（旧版本/其他部署创建的表缺列或多余 NOT NULL 列）会
        先自动修复表结构再重试一次。任何数据库错误都不向外抛出：记录
        脱敏后的真实错误并返回 False，由调用方决定如何呈现（避免"提示
        保存成功但实际未入库"的静默失败）。"""
        try:
            async with self.session() as s:
                await self._upsert_credential(s, email, credential_data)
            log.debug(f"Stored credential: {email}")
            return True
        except IntegrityError as e:
            # 优先自愈：多余 NOT NULL 列等表结构漂移，修复后重试；真正
            # 唯一键冲突的并发竞态走下方更新路径
            if await self._repair_and_retry(email, credential_data, e):
                return True
            # 并发竞态：另一请求已插入同一 email，改为更新
            log.warning(f"凭证写入触发唯一键冲突，回退为更新路径 {email}: {_brief_db_error(e)}")
            try:
                async with self.session() as s:
                    await self._upsert_credential(s, email, credential_data)
                log.debug(f"Stored credential (after race): {email}")
                return True
            except Exception as retry_err:
                log.error(f"写入凭证失败 {email}（重试更新路径同样失败）: {_brief_db_error(retry_err)}")
                return False
        except Exception as e:
            # NOT NULL 违反兜底自愈（个别驱动包装路径不同），其余如实记录简短错误
            if await self._repair_and_retry(email, credential_data, e):
                return True
            log.error(f"写入凭证失败 {email}: {_brief_db_error(e)}")
            return False

    async def _repair_and_retry(
        self, email: str, credential_data: Dict[str, Any], first_err: Exception
    ) -> bool:
        """表结构漂移自愈：修复后重试一次。

        覆盖两类漂移（旧版本/其他部署创建的表）：
        - 多余 NOT NULL 列（如 supported_models）→ NotNullViolationError
        - 缺少模型列 → no such column (SQLite) / does not exist (PostgreSQL)
        仅当错误确属表结构漂移时才执行修复（返回 False 表示不适用，
        由调用方走原有处理路径）。"""
        brief = _brief_db_error(first_err).lower()
        schema_drift = (
            "null value in column" in brief
            or "not null constraint" in brief
            or "no such column" in brief
            or ("column" in brief and "does not exist" in brief)
        )
        if not schema_drift:
            return False
        log.warning(f"检测到表结构与代码不一致（缺列/多余 NOT NULL 列），自动修复后重试 {email}")
        await self._run_full_schema_migration()
        try:
            async with self.session() as s:
                await self._upsert_credential(s, email, credential_data)
            log.info(f"表结构已自动修复，凭证写入成功 {email}")
            return True
        except Exception as retry_err:
            log.error(f"写入凭证失败 {email}（表结构修复后重试仍失败）: {_brief_db_error(retry_err)}")
            return False

    @staticmethod
    async def _upsert_credential(s: AsyncSession, email: str, credential_data: Dict[str, Any]) -> None:
        """在给定会话内执行凭证 upsert（不提交，由 session() 上下文统一提交）"""
        row = await s.scalar(
            select(AntigravityCredential).where(AntigravityCredential.email == email)
        )
        access_token = credential_data.get("access_token") or credential_data.get("token") or ""
        refresh_token = credential_data.get("refresh_token") or ""
        client_id = credential_data.get("client_id") or ""
        client_secret = credential_data.get("client_secret") or ""
        project_id = credential_data.get("project_id") or ""
        expiry_val = credential_data.get("expiry") or ""
        expiry = expiry_val.isoformat() if hasattr(expiry_val, "isoformat") else str(expiry_val)
        tier = credential_data.get("tier")

        if row:
            row.access_token = access_token
            row.refresh_token = refresh_token
            row.client_id = client_id
            row.client_secret = client_secret
            row.project_id = project_id
            row.expiry = expiry
            if tier:
                row.tier = tier
            row.updated_at = time.time()
        else:
            s.add(AntigravityCredential(
                email=email,
                access_token=access_token,
                refresh_token=refresh_token,
                client_id=client_id,
                client_secret=client_secret,
                project_id=project_id,
                expiry=expiry,
                tier=tier or "pro",
                last_success=time.time(),
            ))

    async def get_credential(
        self, email: str
    ) -> Optional[Dict[str, Any]]:
        """获取凭证数据"""
        async with self.session() as s:
            row = await s.scalar(
                select(AntigravityCredential).where(AntigravityCredential.email == email)
            )
            return _row_to_credential_dict(row) if row else None

    async def list_credentials(self) -> List[str]:
        """列出所有凭证 email（创建顺序）"""
        async with self.session() as s:
            rows = await s.scalars(
                select(AntigravityCredential.email).order_by(AntigravityCredential.id)
            )
            return list(rows)

    async def delete_credential(self, email: str) -> bool:
        """删除凭证"""
        async with self.session() as s:
            row = await s.scalar(
                select(AntigravityCredential).where(AntigravityCredential.email == email)
            )
            if row:
                await s.delete(row)
                return True
            return False

    async def get_next_available_credential(
        self,
        model_name: Optional[str] = None,
        exclude_emails: Optional[set] = None,
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """随机获取一个可用凭证（负载均衡 + 模型家族级冷却过滤），返回 (email, credential)

        exclude_emails：优先排除已尝试失败的凭证；若排除后无候选则回退
        到全量候选（保留单凭证场景下的重试能力）。"""
        current_time = time.time()
        cd_group = get_cooldown_group(model_name) if model_name else None
        async with self.session() as s:
            # 轻量查询：只取必要的小字段（email/冷却/禁用），避免全表加载
            # access_token/refresh_token 等大字段（凭证明文可达数 KB）
            rows = (
                await s.execute(
                    select(
                        AntigravityCredential.email,
                        AntigravityCredential.model_cooldowns,
                    )
                    .where(AntigravityCredential.disabled == False)  # noqa: E712
                    .order_by(AntigravityCredential.id)
                )
            ).all()
            if not rows:
                return None
            # Python 侧随机负载均衡（过滤模型家族冷却）
            candidates = rows
            if cd_group:
                candidates = []
                for email, model_cooldowns in rows:
                    cooldowns = _load_json(model_cooldowns, {})
                    cd = cooldowns.get(cd_group)
                    if cd is not None and current_time < cd:
                        continue
                    candidates.append((email, model_cooldowns))
            if not candidates:
                return None
            # 优先排除本轮已尝试失败的凭证（排除后为空则回退全量候选）
            if exclude_emails:
                remaining = [c for c in candidates if c[0] not in exclude_emails]
                if remaining:
                    candidates = remaining
            picked_email = random.choice(candidates)[0]
            # 只取被选中凭证的完整数据
            row = await s.scalar(
                select(AntigravityCredential).where(
                    AntigravityCredential.email == picked_email
                )
            )
            if not row or row.disabled:
                return None
            return row.email, _row_to_credential_dict(row)

    async def get_available_credential_by_email(
        self, email: str, model_name: Optional[str] = None
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        按指定 email 获取可用凭证（凭证亲和/粘性模式使用）。
        凭证不存在/被禁用/该模型家族冷却中 -> None。
        """
        if not email:
            return None
        current_time = time.time()
        cd_group = get_cooldown_group(model_name) if model_name else None
        async with self.session() as s:
            row = await s.scalar(
                select(AntigravityCredential).where(AntigravityCredential.email == email)
            )
            if not row or row.disabled:
                return None
            if cd_group:
                cooldowns = _load_json(row.model_cooldowns, {})
                cd = cooldowns.get(cd_group)
                if cd is not None and current_time < cd:
                    return None
            return row.email, _row_to_credential_dict(row)

    # ---------- 状态管理 ----------

    async def update_credential_state(
        self, email: str, state_updates: Dict[str, Any]
    ) -> bool:
        """更新凭证状态字段"""
        json_fields = {"error_codes", "error_messages", "model_cooldowns"}
        float_fields = {"last_success"}
        str_fields = {"user_email", "tier", "project_id"}
        bool_fields = {"disabled"}
        async with self.session() as s:
            values: Dict[str, Any] = {}
            for key, value in state_updates.items():
                if key in json_fields:
                    values[key] = _dump_json(value)
                elif key in float_fields:
                    values[key] = float(value) if value is not None else None
                elif key in str_fields:
                    values[key] = value
                elif key in bool_fields:
                    values[key] = bool(value)
            if not values:
                return True
            values["updated_at"] = time.time()
            result = await s.execute(
                AntigravityCredential.__table__.update()
                .where(AntigravityCredential.email == email)
                .values(**values)
            )
            return result.rowcount > 0

    async def get_credential_state(
        self, email: str
    ) -> Dict[str, Any]:
        """获取凭证状态（不含 error_messages）"""
        async with self.session() as s:
            row = await s.scalar(
                select(AntigravityCredential).where(AntigravityCredential.email == email)
            )
            if not row:
                return {}
            return {
                "disabled": bool(row.disabled),
                "error_codes": _load_json(row.error_codes, []),
                "last_success": row.last_success or time.time(),
                "email": row.email,
                "model_cooldowns": _load_json(row.model_cooldowns, {}),
                "tier": row.tier or "pro",
                "project_id": row.project_id or "",
            }

    async def get_credentials_summary(
        self,
        offset: int = 0,
        limit: Optional[int] = None,
        status_filter: str = "all",
        error_code_filter: Optional[str] = None,
        cooldown_filter: Optional[str] = None,
        tier_filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """凭证摘要（分页 + 状态筛选，内存过滤）。

        只查询摘要所需的小字段：access_token / refresh_token 等大字段
        （凭证明文可达数 KB）不参与加载，凭证量大时显著减少 IO 与内存。"""
        current_time = time.time()
        async with self.session() as s:
            total = await s.scalar(select(func.count()).select_from(AntigravityCredential)) or 0
            disabled_count = await s.scalar(
                select(func.count()).select_from(AntigravityCredential)
                .where(AntigravityCredential.disabled == True)  # noqa: E712
            ) or 0

            # 列裁剪：不加载 access_token / refresh_token / client_* 大字段
            stmt = (
                select(
                    AntigravityCredential.email,
                    AntigravityCredential.disabled,
                    AntigravityCredential.error_codes,
                    AntigravityCredential.last_success,
                    AntigravityCredential.model_cooldowns,
                    AntigravityCredential.tier,
                    AntigravityCredential.project_id,
                )
                .order_by(AntigravityCredential.id)
            )
            if status_filter == "enabled":
                stmt = stmt.where(AntigravityCredential.disabled == False)  # noqa: E712
            elif status_filter == "disabled":
                stmt = stmt.where(AntigravityCredential.disabled == True)  # noqa: E712
            rows = (await s.execute(stmt)).all()

            filter_value = None
            filter_int = None
            filter_none = False
            if error_code_filter and error_code_filter.strip().lower() != "all":
                if error_code_filter.strip().lower() == "none":
                    filter_none = True
                else:
                    filter_value = error_code_filter.strip()
                    try:
                        filter_int = int(filter_value)
                    except ValueError:
                        filter_int = None

            summaries = []
            for row in rows:
                error_codes = _load_json(row.error_codes, [])
                cooldowns = _load_json(row.model_cooldowns, {})
                active_cooldowns = {k: v for k, v in cooldowns.items() if v > current_time}

                if filter_none and error_codes:
                    continue
                if filter_value is not None:
                    match = False
                    for code in error_codes:
                        if code == filter_value or code == filter_int:
                            match = True
                            break
                        if isinstance(code, str) and filter_int is not None:
                            try:
                                if int(code) == filter_int:
                                    match = True
                                    break
                            except ValueError:
                                pass
                    if not match:
                        continue

                summary = {
                    "email": row.email,
                    "disabled": bool(row.disabled),
                    "error_codes": error_codes,
                    "last_success": row.last_success or current_time,
                    "model_cooldowns": active_cooldowns,
                    "tier": row.tier or "pro",
                    "project_id": row.project_id or "",
                }
                if tier_filter and tier_filter in ("free", "pro", "ultra"):
                    if summary["tier"] != tier_filter:
                        continue
                if cooldown_filter == "in_cooldown" and not active_cooldowns:
                    continue
                if cooldown_filter == "no_cooldown" and active_cooldowns:
                    continue
                summaries.append(summary)

            total_count = len(summaries)
            page_items = summaries[offset:offset + limit] if limit is not None else summaries[offset:]
            return {
                "items": page_items,
                "total": total_count,
                "offset": offset,
                "limit": limit,
                "stats": {
                    "total": total,
                    "normal": total - disabled_count,
                    "disabled": disabled_count,
                },
            }

    async def get_all_credentials_export(self) -> List[Dict[str, Any]]:
        """一次查询取出全部凭证的导出字段（凭据 + 状态），供面板打包导出使用。

        相比逐个 get_credential + get_credential_state 的 2N 次查询，
        这里只需 1 次全表扫描。"""
        async with self.session() as s:
            rows = (
                await s.scalars(
                    select(AntigravityCredential).order_by(AntigravityCredential.id)
                )
            ).all()
            return [
                {
                    "email": r.email,
                    "refresh_token": r.refresh_token or "",
                    "access_token": r.access_token or "",
                    "expiry": r.expiry or "",
                    "tier": r.tier or "pro",
                    "project_id": r.project_id or "",
                }
                for r in rows
            ]

    async def get_credential_errors(self, email: str) -> Dict[str, Any]:
        """获取凭证的错误信息"""
        async with self.session() as s:
            row = await s.scalar(
                select(AntigravityCredential).where(AntigravityCredential.email == email)
            )
            if row:
                return {
                    "email": email,
                    "error_codes": _load_json(row.error_codes, []),
                    "error_messages": _load_json(row.error_messages, []),
                }
            return {"email": email, "error_codes": [], "error_messages": []}

    # ---------- 模型级冷却 ----------

    async def set_model_cooldown(
        self,
        email: str,
        model_name: str,
        cooldown_until: Optional[float],
    ) -> bool:
        """设置/清除模型的冷却时间（按家族分组：argon / gemini / claude_gpt / 其他原名）"""
        cd_group = get_cooldown_group(model_name)
        async with self.session() as s:
            row = await s.scalar(
                select(AntigravityCredential).where(AntigravityCredential.email == email)
            )
            if not row:
                return False
            cooldowns = _load_json(row.model_cooldowns, {})
            if cooldown_until is None:
                cooldowns.pop(cd_group, None)
            else:
                cooldowns[cd_group] = cooldown_until
            row.model_cooldowns = _dump_json(cooldowns)
            row.updated_at = time.time()
            return True

    async def record_success(
        self, email: str, model_name: Optional[str] = None
    ) -> None:
        """成功调用后的条件写入（清除错误 + 该模型家族的CD）"""
        async with self.session() as s:
            row = await s.scalar(
                select(AntigravityCredential).where(AntigravityCredential.email == email)
            )
            if not row:
                return
            changed = False
            if _load_json(row.error_codes, []):
                row.error_codes = "[]"
                row.error_messages = "{}"
                row.last_success = time.time()
                row.updated_at = time.time()
                changed = True
            if model_name:
                cd_group = get_cooldown_group(model_name)
                cooldowns = _load_json(row.model_cooldowns, {})
                if cd_group in cooldowns:
                    cooldowns.pop(cd_group)
                    row.model_cooldowns = _dump_json(cooldowns)
                    row.updated_at = time.time()
                    changed = True
            if changed:
                log.debug(f"Recorded success for {email} (model={model_name})")

    # ---------- 配置管理（内存缓存） ----------

    async def _load_config_cache(self) -> None:
        async with self.session() as s:
            rows = (await s.scalars(select(ConfigItem))).all()
            for item in rows:
                try:
                    self._config_cache[item.key] = json.loads(item.value)
                except (json.JSONDecodeError, TypeError):
                    self._config_cache[item.key] = item.value
            log.debug(f"Loaded {len(self._config_cache)} config items")

    async def reload_config_cache(self) -> None:
        self._ensure_initialized()
        self._config_cache.clear()
        await self._load_config_cache()
        log.info("Config cache reloaded from database")

    async def set_config(self, key: str, value: Any) -> bool:
        """设置配置（写库 + 更新缓存）"""
        async with self.session() as s:
            item = await s.get(ConfigItem, key)
            serialized = _dump_json(value)
            if item:
                item.value = serialized
                item.updated_at = time.time()
            else:
                s.add(ConfigItem(key=key, value=serialized))
            self._config_cache[key] = value
            return True

    async def get_all_config(self) -> Dict[str, Any]:
        self._ensure_initialized()
        return self._config_cache.copy()

    # ---------- 请求日志（usage_log） ----------

    async def insert_usage_log(self, entry: Dict[str, Any]) -> bool:
        """写入一条请求日志（失败仅告警，不影响主流程）"""
        return await self.insert_usage_logs_batch([entry])

    async def insert_usage_logs_batch(self, entries: List[Dict[str, Any]]) -> bool:
        """批量写入请求日志（单事务；整批失败时回退为逐条写入，单条失败不影响其余）"""
        if not entries:
            return True
        try:
            async with self.session() as s:
                s.add_all([self._build_usage_log_row(entry) for entry in entries])
            return True
        except SQLAlchemyError as batch_err:
            log.warning(f"批量写入请求日志失败，回退为逐条写入: {_brief_db_error(batch_err)}")
        except (TypeError, ValueError) as build_err:
            # 个别条目字段非法导致行构建失败：逐条写入，坏条目单独告警
            log.warning(f"请求日志批量构建失败，回退为逐条写入: {build_err}")

        ok = True
        for entry in entries:
            try:
                async with self.session() as s:
                    s.add(self._build_usage_log_row(entry))
            except Exception as e:
                log.warning(f"写入请求日志失败: {_brief_db_error(e)}")
                ok = False
        return ok

    @staticmethod
    def _build_usage_log_row(entry: Dict[str, Any]) -> UsageLog:
        """把日志字典转换为 UsageLog ORM 行（字段非法时抛出，由调用方处理）"""
        return UsageLog(
            created_at=entry.get("created_at") or time.time(),
            api_format=str(entry.get("api_format", "")),
            client_ip=str(entry.get("client_ip", "")),
            request_id=str(entry.get("request_id", "")),
            model_name=str(entry.get("model_name", "")),
            upstream_model_name=str(entry.get("upstream_model_name", "")),
            credential_email=str(entry.get("credential_email", "")),
            is_stream=bool(entry.get("is_stream", False)),
            status_code=int(entry.get("status_code", 0) or 0),
            success=bool(entry.get("success", False)),
            use_time=float(entry.get("use_time", 0.0) or 0.0),
            prompt_tokens=int(entry.get("prompt_tokens", 0) or 0),
            completion_tokens=int(entry.get("completion_tokens", 0) or 0),
            total_tokens=int(entry.get("total_tokens", 0) or 0),
            cached_tokens=int(entry.get("cached_tokens", 0) or 0),
            error_message=str(entry.get("error_message", ""))[:2000],
            request_log_file=str(entry.get("request_log_file", ""))[:512],
        )

    async def query_usage_logs(
        self,
        offset: int = 0,
        limit: int = 50,
        start_timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None,
        model_name: str = "",
        credential_email: str = "",
        api_format: str = "",
        status: str = "all",  # all / success / error
        is_stream: Optional[bool] = None,
        status_code: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """分页查询请求日志，返回 (日志列表, 总数)，新 -> 旧"""
        stmt = select(UsageLog)
        count_stmt = select(func.count()).select_from(UsageLog)

        if start_timestamp:
            stmt = stmt.where(UsageLog.created_at >= start_timestamp)
            count_stmt = count_stmt.where(UsageLog.created_at >= start_timestamp)
        if end_timestamp:
            stmt = stmt.where(UsageLog.created_at <= end_timestamp)
            count_stmt = count_stmt.where(UsageLog.created_at <= end_timestamp)
        if model_name:
            stmt = stmt.where(UsageLog.model_name == model_name)
            count_stmt = count_stmt.where(UsageLog.model_name == model_name)
        if credential_email:
            stmt = stmt.where(UsageLog.credential_email == credential_email)
            count_stmt = count_stmt.where(UsageLog.credential_email == credential_email)
        if api_format:
            stmt = stmt.where(UsageLog.api_format == api_format)
            count_stmt = count_stmt.where(UsageLog.api_format == api_format)
        if status_code is not None:
            stmt = stmt.where(UsageLog.status_code == status_code)
            count_stmt = count_stmt.where(UsageLog.status_code == status_code)
        if status == "success":
            stmt = stmt.where(UsageLog.success.is_(True))
            count_stmt = count_stmt.where(UsageLog.success.is_(True))
        elif status == "error":
            stmt = stmt.where(UsageLog.success.is_(False))
            count_stmt = count_stmt.where(UsageLog.success.is_(False))
        if is_stream is not None:
            stmt = stmt.where(UsageLog.is_stream.is_(is_stream))
            count_stmt = count_stmt.where(UsageLog.is_stream.is_(is_stream))

        async with self.session() as s:
            total = (await s.execute(count_stmt)).scalar() or 0
            rows = (
                (
                    await s.execute(
                        stmt.order_by(UsageLog.created_at.desc(), UsageLog.id.desc())
                        .offset(offset)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

        items = [
            {
                "id": r.id,
                "created_at": r.created_at,
                "api_format": r.api_format or "",
                "client_ip": r.client_ip or "",
                "request_id": r.request_id or "",
                "model_name": r.model_name or "",
                "upstream_model_name": r.upstream_model_name or "",
                "credential_email": r.credential_email or "",
                "is_stream": r.is_stream,
                "status_code": r.status_code,
                "success": r.success,
                "use_time": r.use_time,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "total_tokens": r.total_tokens,
                "cached_tokens": r.cached_tokens or 0,
                "error_message": r.error_message or "",
                "request_log_file": r.request_log_file or "",
            }
            for r in rows
        ]
        return items, total

    async def usage_log_stats(
        self,
        start_timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None,
        model_name: str = "",
        credential_email: str = "",
        api_format: str = "",
    ) -> Dict[str, Any]:
        """聚合统计：总请求数 / 成功数 / 失败数 / token 汇总 / 平均耗时 / 每模型聚合"""
        conditions = []
        if start_timestamp:
            conditions.append(UsageLog.created_at >= start_timestamp)
        if end_timestamp:
            conditions.append(UsageLog.created_at <= end_timestamp)
        if model_name:
            conditions.append(UsageLog.model_name == model_name)
        if credential_email:
            conditions.append(UsageLog.credential_email == credential_email)
        if api_format:
            conditions.append(UsageLog.api_format == api_format)

        success_cond = func.cast(UsageLog.success, Integer).label("success_int")

        agg = select(
            func.count().label("total_requests"),
            func.coalesce(func.sum(success_cond), 0).label("success_count"),
            func.coalesce(func.sum(UsageLog.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(UsageLog.completion_tokens), 0).label("completion_tokens"),
            func.coalesce(func.sum(UsageLog.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(UsageLog.cached_tokens), 0).label("cached_tokens"),
            func.coalesce(func.avg(UsageLog.use_time), 0).label("avg_use_time"),
        ).where(*conditions)

        by_model = (
            select(
                UsageLog.model_name.label("model_name"),
                func.count().label("count"),
                func.coalesce(func.sum(UsageLog.total_tokens), 0).label("total_tokens"),
            )
            .where(*conditions)
            .group_by(UsageLog.model_name)
            .order_by(func.count().desc())
            .limit(50)
        )

        async with self.session() as s:
            row = (await s.execute(agg)).one()
            model_rows = (await s.execute(by_model)).all()

        return {
            "total_requests": row.total_requests or 0,
            "success_count": row.success_count or 0,
            "error_count": (row.total_requests or 0) - (row.success_count or 0),
            "prompt_tokens": row.prompt_tokens or 0,
            "completion_tokens": row.completion_tokens or 0,
            "total_tokens": row.total_tokens or 0,
            "cached_tokens": row.cached_tokens or 0,
            "avg_use_time": round(float(row.avg_use_time or 0), 2),
            "by_model": [
                {"model_name": m.model_name or "(unknown)", "count": m.count, "total_tokens": m.total_tokens}
                for m in model_rows
            ],
        }

    async def delete_usage_logs_before(self, before_timestamp: float) -> int:
        """删除指定时间之前的日志，返回删除条数"""
        async with self.session() as s:
            result = await s.execute(
                text("DELETE FROM usage_log WHERE created_at < :ts"),
                {"ts": before_timestamp},
            )
            return result.rowcount or 0


# ====================== 全局单例 ======================

_database: Optional[Database] = None
_database_lock = asyncio.Lock()


async def get_database() -> Database:
    """获取全局数据库实例"""
    global _database
    if _database is None:
        async with _database_lock:
            if _database is None:
                db = Database()
                await db.initialize()
                _database = db
    return _database


async def close_database() -> None:
    """关闭全局数据库"""
    global _database
    if _database:
        await _database.close()
        _database = None
