"""
Конфигурация бота — читает переменные из .env файла.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки приложения (загружаются из .env)."""

    # Telegram
    bot_token: str

    # Supabase
    supabase_url: str
    supabase_key: str

    # OpenRouter
    openrouter_api_key: str

    # Replicate
    replicate_api_token: str

    # YouTube Data API
    youtube_api_key: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


# Глобальный экземпляр настроек
settings = Settings()
