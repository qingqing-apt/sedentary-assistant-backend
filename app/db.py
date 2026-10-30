"""数据库连接与会话管理。"""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_config


class Base(DeclarativeBase):
    pass


def _build_url() -> str:
    url = get_config().database_url
    if url.startswith("sqlite:///") and ":memory:" not in url:
        raw = url.removeprefix("sqlite:///")
        Path(raw).parent.mkdir(parents=True, exist_ok=True)
    return url


engine = create_engine(
    _build_url(),
    connect_args={"check_same_thread": False},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  注册模型后再建表

    Base.metadata.create_all(engine)
