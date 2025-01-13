import functools
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent
DOTENV = Path(BASE_DIR, ".env")
DOTENV_PROD = Path(BASE_DIR, "prod.env")


class AppSettings(BaseSettings):
    DEBUG: bool | None = False
    GOOGLE_API_KEY: str | None = None

    model_config = SettingsConfigDict(case_sensitive=True, env_file=DOTENV)  # pyright: ignore[reportUnannotatedClassAttribute]


@functools.cache
def get_app_settings() -> AppSettings:
    """
    We're using `cache` decorator to re-use the same AppSettings object,
    instead of reading it for each request. The AppSettings object will be
    created only once, the first time it's called. Then it will return
    the same object that was returned on the first call, again and again.
    """

    app_settings = AppSettings()
    if app_settings.DEBUG:
        return app_settings

    return AppSettings(_env_file=DOTENV_PROD, _env_file_encoding="utf-8")  # pyright: ignore[reportCallIssue]


config = get_app_settings()
