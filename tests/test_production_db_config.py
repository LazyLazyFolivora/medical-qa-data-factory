import locale

import psycopg2
import pytest

from medical_qa_data_factory.web import production_db
from medical_qa_data_factory.web.production_db import DATABASE_URL_ENV, ProductionDatabase


def test_production_database_reads_local_dotenv_without_overriding_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    file_dsn = "postgresql://file@localhost/medical_qa"
    env_dsn = "postgresql://service@localhost/medical_qa"
    (tmp_path / ".env").write_text(f"{DATABASE_URL_ENV}='{file_dsn}'\n", encoding="utf-8-sig")

    assert ProductionDatabase(tmp_path / "workspace").dsn == file_dsn

    monkeypatch.setenv(DATABASE_URL_ENV, env_dsn)
    assert ProductionDatabase(tmp_path / "workspace").dsn == env_dsn


def test_authentication_failure_keeps_localized_message(tmp_path, monkeypatch):
    message = "FATAL: 密码认证失败"
    encoded = message.encode("gb18030")

    def fail_connect(*args, **kwargs):
        encoded.decode("utf-8")

    monkeypatch.setattr(locale, "getpreferredencoding", lambda do_setlocale: "gb18030")
    monkeypatch.setattr(psycopg2, "connect", fail_connect)
    monkeypatch.setattr(production_db, "ThreadedConnectionPool", fail_connect)
    database = ProductionDatabase(tmp_path, "postgresql://localhost/medical_qa")

    with pytest.raises(psycopg2.OperationalError, match=message):
        database.acquire()
    with pytest.raises(psycopg2.OperationalError, match=message):
        database.migrate()
