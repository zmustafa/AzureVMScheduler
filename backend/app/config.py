from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "Azure VM Scheduler"
    environment: str = "development"
    data_dir: Path = ROOT_DIR / ".data"
    database_url: str | None = None
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = "ChangeMe!12345"
    session_hours: int = 12
    oidc_state_ttl_seconds: int = 600
    import_preview_ttl_seconds: int = 900
    allowed_return_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    password_min_length: int = Field(default=12, ge=10, le=128)
    password_require_upper: bool = True
    password_require_lower: bool = True
    password_require_number: bool = True
    password_require_symbol: bool = True
    enable_real_azure_starts: bool = False
    #: Deliberately separate from starts: a wrong start costs money, a wrong stop causes an outage.
    enable_real_azure_stops: bool = False
    scheduler_poll_seconds: int = 15
    scheduler_lease_seconds: int = 900
    scheduler_max_concurrency: int = 4
    scheduler_claim_batch: int = Field(default=50, ge=1, le=1000)
    scheduler_start_concurrency: int = Field(default=12, ge=1, le=200)
    scheduler_monitor_concurrency: int = Field(default=40, ge=1, le=500)
    azure_subscription_concurrency: int = Field(default=8, ge=1, le=100)
    azure_start_max_retries: int = Field(default=4, ge=0, le=10)
    azure_discovery_max_results: int = Field(default=500, ge=1, le=5000)
    default_timezone: str = "America/New_York"
    vm_monitor_timeout_seconds: int = 600
    vm_monitor_interval_seconds: int = 15
    fernet_key: str | None = None
    #: Set true only when a trusted reverse proxy (Azure Container Apps ingress) terminates the
    #: connection. Off a trusted proxy X-Forwarded-For is attacker-controlled.
    trust_forwarded_headers: bool = False
    #: How many proxies sit in front of this process. The client address is read that many entries
    #: from the *right* of X-Forwarded-For, because each hop appends. Container Apps ingress is one
    #: hop; add one for every additional proxy (Front Door, an nginx sidecar) or the allowlist can
    #: be fooled by a client-supplied header.
    forwarded_hops: int = Field(default=1, ge=1, le=10)
    #: Break-glass for the IP allowlist: CIDRs that are always allowed, and a hard kill switch.
    #: Deliberately environment-only — an administrator locked out of the UI can restore access
    #: with a container restart, without database surgery.
    ip_allowlist_bootstrap: str = ""
    ip_allowlist_disabled: bool = False
    app_base_url: str = "http://127.0.0.1:5173"
    connector_http_timeout_seconds: float = Field(default=20.0, ge=1.0, le=120.0)
    smtp_timeout_seconds: float = Field(default=20.0, ge=1.0, le=120.0)
    delivery_concurrency: int = Field(default=4, ge=1, le=50)
    delivery_max_attempts: int = Field(default=5, ge=1, le=20)
    delivery_retry_base_seconds: int = Field(default=30, ge=1, le=3600)
    delivery_retry_max_seconds: int = Field(default=900, ge=1, le=86400)
    delivery_poll_seconds: int = Field(default=30, ge=5, le=3600)
    notification_queue_size: int = Field(default=2000, ge=10, le=100000)

    @property
    def base_url(self) -> str:
        return self.app_base_url.rstrip("/")

    @property
    def return_origins(self) -> set[str]:
        return {item.strip().rstrip("/") for item in self.allowed_return_origins.split(",") if item.strip()}

    @property
    def bootstrap_networks(self) -> list[str]:
        return [item.strip() for item in self.ip_allowlist_bootstrap.split(",") if item.strip()]

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{(self.data_dir / 'azureops.db').as_posix()}"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
