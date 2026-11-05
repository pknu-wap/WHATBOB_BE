from urllib.parse import quote, unquote
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str = "mysql+pymysql://watbab:watbab@localhost:3306/watbab"
    JWT_SECRET: str = "watbab-default-secret-key"
    JWT_EXPIRE_MINUTES: int = 10080
    FRONTEND_BASE_URL: str = "https://whatbobfe-app.vercel.app"
    ROOM_TTL_SECONDS: int = 60 * 60
    ROOM_HOST_DISCONNECT_GRACE_SECONDS: int = 60
    ROOM_CLEANUP_INTERVAL_SECONDS: int = 60
    KAKAO_REST_API_KEY: str
    KAKAO_REDIRECT_URI: str = "https://whatbobfe-app.vercel.app/auth/callback"
    KAKAO_CLIENT_SECRET: str | None = None
    LLM_API_KEY: str | None = None
    LLM_API_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    LLM_MODEL: str = "gemini-2.0-flash"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def encode_special_chars_in_db_password(cls, v: str) -> str:
        """비밀번호에 '@' 등 특수문자가 포함될 때 URL 파싱이 깨지지 않도록 자동 인코딩합니다."""
        if not isinstance(v, str) or "://" not in v:
            return v
        scheme, rest = v.split("://", 1)
        if "@" not in rest:
            return v
        userinfo, hostpart = rest.rsplit("@", 1)
        if ":" not in userinfo:
            return v
        user, raw_pwd = userinfo.split(":", 1)
        encoded_pwd = quote(unquote(raw_pwd), safe="")
        return f"{scheme}://{user}:{encoded_pwd}@{hostpart}"


settings = Settings()
