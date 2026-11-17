"""Configuration, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database. Either a full URL, or the individual POSTGRES_* parts (as used by the
    # postgres image), from which the URL is assembled.
    database_url: str | None = None
    postgres_user: str = "soundable"
    postgres_password: SecretStr = SecretStr("soundable")
    postgres_db: str = "soundable"
    postgres_host: str = "db"
    postgres_port: int = 5432

    music_dir: Path = Path("/data/music")

    # Own API: admin password (hashed with argon2 at startup) and the key used to sign
    # short-lived stream URLs.
    admin_password: SecretStr
    secret_key: SecretStr = Field(min_length=32)

    # Subsonic API: must be plaintext because Subsonic token auth is md5(password + salt).
    subsonic_user: str = "admin"
    subsonic_password: SecretStr

    # Cloudflare (free/pro) rejects request bodies > 100 MB, so stay below that.
    max_upload_mb: int = 95
    stream_url_ttl_seconds: int = 6 * 3600

    # Login rate limit: max failed attempts per client IP within the window.
    login_max_failures: int = 10
    login_window_seconds: int = 15 * 60

    ytdlp_cookies_file: Path | None = None

    log_level: str = "INFO"

    @field_validator("ytdlp_cookies_file", mode="before")
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        return None if v == "" else v

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        pw = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
