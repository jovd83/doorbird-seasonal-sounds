from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# The one place this default lives. The Dockerfile and docker-compose.yml pass
# TZ through from the deployment rather than restating a value of their own.
DEFAULT_TIMEZONE = "Europe/Brussels"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    admin_username: str = Field("admin", alias="ADMIN_USERNAME")
    # Either of these supplies the login password. The hash is preferred: it
    # keeps plaintext out of the environment and out of `docker inspect`.
    # `app.security` normalises whichever is set into a bcrypt hash at startup.
    admin_password: str = Field("", alias="ADMIN_PASSWORD")
    admin_password_hash: str = Field("", alias="ADMIN_PASSWORD_HASH")
    secret_key: str = Field(..., alias="SECRET_KEY")
    fernet_key: str = Field(..., alias="FERNET_KEY")
    data_dir: Path = Field(Path("/data"), alias="DATA_DIR")
    daily_run_hour: int = Field(3, alias="DAILY_RUN_HOUR")
    daily_run_minute: int = Field(15, alias="DAILY_RUN_MINUTE")
    timezone: str = Field(DEFAULT_TIMEZONE, alias="TZ")

    # Mark the session cookie `Secure`. On by default: the only reason to turn
    # it off is the plain-HTTP LAN deployment this app is usually run as, and
    # that should be a deliberate choice rather than the silent default.
    session_https_only: bool = Field(False, alias="SESSION_HTTPS_ONLY")
    # How long a login lasts. Two weeks was Starlette's implicit default.
    session_max_age_seconds: int = Field(60 * 60 * 24 * 7, alias="SESSION_MAX_AGE_SECONDS")

    # How long audit rows are kept. 0 disables pruning entirely.
    audit_retention_days: int = Field(365, alias="AUDIT_RETENTION_DAYS")

    # Largest MP3 the upload forms will accept, in bytes.
    max_upload_bytes: int = Field(2 * 1024 * 1024, alias="MAX_UPLOAD_BYTES")

    # FastAPI's /docs, /redoc and /openapi.json need no login and publish the
    # whole route list, including the ring webhook. Off unless asked for.
    enable_api_docs: bool = Field(False, alias="ENABLE_API_DOCS")

    # --- ring-chime mode -------------------------------------------------
    # Watch each device's monitor.cgi stream and play today's MP3 through the
    # door station's own speaker on every ring. This is the mode that works
    # against the published LAN API.
    ring_chime_enabled: bool = Field(True, alias="RING_CHIME_ENABLED")
    ring_debounce_seconds: float = Field(8.0, alias="RING_DEBOUNCE_SECONDS")
    chime_max_seconds: float = Field(15.0, alias="CHIME_MAX_SECONDS")
    chime_gain_db: float = Field(0.0, alias="CHIME_GAIN_DB")
    # Try the (undocumented, administration-only) customsound.cgi upload as
    # well. Off by default because it needs the factory administration user.
    button_sound_upload_enabled: bool = Field(False, alias="BUTTON_SOUND_UPLOAD_ENABLED")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "doorbird.db"

    @property
    def mp3_dir(self) -> Path:
        return self.data_dir / "mp3"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def ulaw_dir(self) -> Path:
        return self.data_dir / "ulaw"


settings = Settings()


def ensure_data_dirs() -> None:
    """Create the data directories. Called from `init_db`, not at import.

    Importing this module used to create four directories as a side effect, so
    any test, CLI script or tool that merely touched `app.config` wrote to the
    filesystem -- which is exactly why `tests/conftest.py` needs its careful,
    load-bearing note about assigning DATA_DIR before the first import.
    """
    for directory in (settings.data_dir, settings.mp3_dir,
                      settings.log_dir, settings.ulaw_dir):
        directory.mkdir(parents=True, exist_ok=True)
