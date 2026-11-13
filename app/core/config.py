from urllib.parse import quote, unquote
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str = "mysql+pymysql://watbab:watbab@localhost:3306/watbab"
    JWT_SECRET: str = Field(min_length=32)
    JWT_ISSUER: str = Field(default="whatbob-auth", min_length=1)
    JWT_AUDIENCE: str = Field(default="whatbob-api", min_length=1)
    JWT_CLOCK_SKEW_SECONDS: int = Field(default=0, ge=0, le=60)
    AUTH_COOKIE_NAME: str = Field(default="__Secure-whatbob_auth", pattern=r"^__Secure-[A-Za-z0-9_-]+$")
    AUTH_NICKNAME_ATTEMPTS: int = Field(default=20, ge=1, le=100)
    FRONTEND_BASE_URL: str = "https://whatbobfe-app.vercel.app"
    ROOM_TTL_SECONDS: int = 60 * 60
    ROOM_HOST_DISCONNECT_GRACE_SECONDS: int = 60
    ROOM_MEMBER_DISCONNECT_GRACE_SECONDS: int = 60
    ROOM_CLEANUP_INTERVAL_SECONDS: int = 60
    PROFILE_IMAGE_UPLOAD_DIR: str = ".data/profile-images"
    PROFILE_IMAGE_URL_PREFIX: str = "/uploads/profile-images"
    PROFILE_IMAGE_MAX_BYTES: int = 5 * 1024 * 1024
    PROFILE_IMAGE_MAX_PIXELS: int = 12_000_000
    PROFILE_IMAGE_MAX_SIDE: int = 512
    PROFILE_IMAGE_ORPHAN_GRACE_SECONDS: int = 300
    PERSONAL_MODEL_ARTIFACT_PATH: str = ".data/models/personal_logistic_active.json"
    TRANSITION_PREPARE_COOKIE_NAME: str = Field(
        default="__Secure-whatbob_transition",
        pattern=r"^__Secure-[A-Za-z0-9_-]+$",
    )
    TRANSITION_PREPARE_SECONDS: int = 300
    TRANSITION_RECOVERY_SECONDS: int = 24 * 60 * 60
    KAKAO_REST_API_KEY: str
    KAKAO_REDIRECT_URI: str = "https://whatbobfe-app.vercel.app/auth/callback"
    KAKAO_CLIENT_SECRET: str | None = None
    LLM_API_KEY: str | None = None
    LLM_API_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    LLM_MODEL: str = "gemini-2.0-flash"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                      hide_input_in_errors=True)

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
