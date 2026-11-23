from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False, hide_input_in_errors=True
    )

    azure_endpoint: str = ""
    azure_subscription_id: str = ""
    azure_api_key: SecretStr = SecretStr("")
    live_deployment: str = "gpt-live-1"
    responses_deployment: str = "gpt-6-luna"
    decision_deployment: str = "ms-d-1"
    mcp_faq_url: str = "http://127.0.0.1:8101/mcp"
    mcp_repair_url: str = "http://127.0.0.1:8102/mcp"
    mcp_verification_url: str = "http://127.0.0.1:8103/mcp"
    mcp_faq_key: SecretStr = SecretStr("")
    mcp_repair_key: SecretStr = SecretStr("")
    mcp_verification_key: SecretStr = SecretStr("")
    table_endpoint: str = ""
    table_name: str = "LiveDemo"
    state_backend: str = "table"
    sqlite_path: str = ".build/demo.sqlite"
    intent_threshold: float = Field(default=0.70, ge=0, le=1)
    mcp_catalog_ttl_seconds: int = Field(default=300, ge=1, le=3600)
    session_ttl_seconds: int = Field(default=1800, ge=60, le=7200)
    max_sessions: int = Field(default=4, ge=1, le=20)
    allow_private_container: bool = False
    auth_required: bool = False
    auth_username: str = "admin"
    auth_password_hash: SecretStr = SecretStr("")
    auth_session_seconds: int = Field(default=8 * 3600, ge=60, le=8 * 3600)
    app_origin: str = (
        "http://localhost:8000,http://127.0.0.1:8000,http://localhost:5173,http://127.0.0.1:5173"
    )

    @model_validator(mode="after")
    def validate_authentication(self) -> "Settings":
        if self.allow_private_container and not self.auth_required:
            raise ValueError("Container hosting requires AUTH_REQUIRED=true")
        if self.auth_required:
            from .login import validate_password_hash

            if not self.auth_username.strip() or len(self.auth_username) > 128:
                raise ValueError("Configure a valid AUTH_USERNAME")
            validate_password_hash(self.auth_password_hash.get_secret_value())
        return self

    @field_validator("azure_endpoint", "table_endpoint")
    @classmethod
    def validate_azure_url(cls, value: str) -> str:
        if not value:
            return value
        url = urlparse(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError("Azure endpoints must be HTTPS resource origins, not project URLs")
        if not url.hostname.endswith((".azure.com", ".windows.net")):
            raise ValueError("Expected an Azure endpoint")
        return value.rstrip("/")

    @field_validator("mcp_faq_url", "mcp_repair_url", "mcp_verification_url")
    @classmethod
    def validate_mcp_url(cls, value: str) -> str:
        url = urlparse(value)
        if (
            url.scheme not in ("https", "http")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or (url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1"))
        ):
            raise ValueError("MCP URL must use HTTPS or loopback HTTP, without credentials")
        return value

    @property
    def origins(self) -> set[str]:
        return set(self.app_origin.split(","))

    @property
    def mcp_servers(self) -> dict[str, tuple[str, str]]:
        return {
            "faq": (self.mcp_faq_url, self.mcp_faq_key.get_secret_value()),
            "repair": (self.mcp_repair_url, self.mcp_repair_key.get_secret_value()),
            "verification": (self.mcp_verification_url, self.mcp_verification_key.get_secret_value()),
        }


@lru_cache
def settings() -> Settings:
    return Settings()
