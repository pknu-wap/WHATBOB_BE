from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str = "mysql+pymysql://watbab:watbab@localhost:3306/watbab"
    JWT_SECRET: str = "watbab-default-secret-key"
    JWT_EXPIRE_MINUTES: int = 10080
    KAKAO_REST_API_KEY: str
    KAKAO_REDIRECT_URI: str = "http://localhost:8000/api/auth/kakao/callback"
    KAKAO_CLIENT_SECRET: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()

