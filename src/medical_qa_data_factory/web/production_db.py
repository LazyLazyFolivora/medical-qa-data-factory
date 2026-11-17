"""PostgreSQL connections and forward migrations for Web production."""

from contextlib import closing, contextmanager
from hashlib import sha256
from importlib.resources import files
import locale
import os
from pathlib import Path
from threading import Lock

import psycopg2
from dotenv import dotenv_values
from psycopg2 import sql
from psycopg2.errors import DuplicateDatabase
from psycopg2.extensions import make_dsn, parse_dsn
from psycopg2.extras import DictCursor
from psycopg2.pool import ThreadedConnectionPool


DATABASE_URL_ENV = "MEDICAL_QA_PRODUCTION_DATABASE_URL"


def production_database_url() -> str | None:
    configured = os.environ.get(DATABASE_URL_ENV)
    if configured is not None:
        return configured or None
    return dotenv_values(".env", encoding="utf-8-sig", interpolate=False).get(DATABASE_URL_ENV) or None


@contextmanager
def postgres_connection_errors():
    try:
        yield
    except UnicodeDecodeError as error:
        # libpq can return authentication errors in the system code page on Windows.
        encoding = locale.getpreferredencoding(False)
        if os.name == "nt" and encoding.lower() == "utf-8":
            encoding = "mbcs"
        message = error.object.decode(encoding, errors="replace").strip()
        raise psycopg2.OperationalError(message) from None


class ProductionCursor(DictCursor):
    def execute(self, query, vars=None):
        super().execute(query, vars)
        return self


class ProductionDatabase:
    def __init__(self, root: str | Path, dsn: str | None = None):
        self.dsn = dsn or production_database_url()
        if not self.dsn:
            raise ValueError(f"请在项目根目录的 .env 或环境变量中设置 {DATABASE_URL_ENV}")
        identity = os.path.normcase(str(Path(root).resolve())).encode("utf-8")
        digest = sha256(identity).digest()
        self.schema = "qa_production_" + digest.hex()[:32]
        self.lock_key = int.from_bytes(digest[:8], "big", signed=True)
        self._pool = None
        self._pool_lock = Lock()

    def _checkout(self):
        with postgres_connection_errors():
            with self._pool_lock:
                if self._pool is None:
                    self._pool = ThreadedConnectionPool(1, 24, self.dsn, connect_timeout=5)
                pool = self._pool
            return pool, pool.getconn()

    def _create_missing_database(self) -> bool:
        database_name = parse_dsn(self.dsn).get("dbname")
        if not database_name or database_name == "postgres":
            return False
        with postgres_connection_errors():
            admin = psycopg2.connect(make_dsn(self.dsn, dbname="postgres"), connect_timeout=5)
        with closing(admin):
            admin.autocommit = True
            with admin.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database_name,))
                if cursor.fetchone():
                    return False
                try:
                    cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
                except DuplicateDatabase:
                    pass
        return True

    @contextmanager
    def transaction(self, *, create_schema=False):
        try:
            pool, connection = self._checkout()
        except psycopg2.OperationalError as original:
            if not create_schema:
                raise
            try:
                created = self._create_missing_database()
            except psycopg2.OperationalError:
                raise original from None
            if not created:
                raise
            pool, connection = self._checkout()
        try:
            with connection, connection.cursor(cursor_factory=ProductionCursor) as cursor:
                cursor.execute("SET LOCAL lock_timeout = '10s'")
                schema = sql.Identifier(self.schema)
                if create_schema:
                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", (self.lock_key,))
                    cursor.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema))
                cursor.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(schema))
                yield cursor
        finally:
            pool.putconn(connection)

    def close(self):
        with self._pool_lock:
            if self._pool is not None:
                self._pool.closeall()
                self._pool = None

    def acquire(self):
        with postgres_connection_errors():
            connection = psycopg2.connect(self.dsn, connect_timeout=5)
        try:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", (self.lock_key,))
                if not cursor.fetchone()[0]:
                    raise ValueError("另一个后端进程正在管理持续生产，请使用原后端")
            return connection
        except BaseException:
            connection.close()
            raise

    def migrate(self):
        """Apply packaged SQL files in order, recording versions and checksums."""
        migrations = sorted(
            (file for file in files(__package__).joinpath("migrations").iterdir() if file.name.endswith(".sql")),
            key=lambda file: file.name,
        )
        applied = []
        with self.transaction(create_schema=True) as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version text PRIMARY KEY,
                    checksum text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            installed = dict(cursor.execute("SELECT version, checksum FROM schema_migrations"))
            for migration in migrations:
                content = migration.read_text(encoding="utf-8").replace("\r\n", "\n")
                checksum = sha256(content.encode("utf-8")).hexdigest()
                existing = installed.get(migration.name)
                if existing is not None:
                    if existing != checksum:
                        raise ValueError(f"已执行的迁移文件已更改：{migration.name}")
                    continue
                cursor.execute(content)
                cursor.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (migration.name, checksum),
                )
                applied.append(migration.name)
        return applied
