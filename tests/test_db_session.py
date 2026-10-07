from app.db.session import vector_database_url


def test_vector_database_url_uses_psycopg_for_asyncpg_url() -> None:
    assert vector_database_url("postgresql+asyncpg://user:pass@localhost/db") == (
        "postgresql+psycopg://user:pass@localhost/db"
    )


def test_vector_database_url_keeps_psycopg_url() -> None:
    url = "postgresql+psycopg://user:pass@localhost/db"
    assert vector_database_url(url) == url
