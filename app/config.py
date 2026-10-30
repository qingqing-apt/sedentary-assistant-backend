"""应用配置。全部通过环境变量或 .env 注入，代码里不出现任何密钥。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "久坐活动助手"
    database_url: str = "sqlite:///./data/app.db"
    app_timezone: str = "Asia/Shanghai"

    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout: float = 20.0

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key)


@lru_cache
def get_config() -> AppConfig:
    return AppConfig()
