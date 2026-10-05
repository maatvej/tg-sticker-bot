"""Настройки бота: читаются из переменных окружения и файла .env в корне проекта."""

from pathlib import Path

from pydantic import PositiveInt, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# Заглушка в .env, которую нужно заменить токеном от @BotFather
BOT_TOKEN_PLACEHOLDER = "PASTE_YOUR_BOT_TOKEN_FROM_BOTFATHER"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # Токен бота, который выдаёт @BotFather
    bot_token: SecretStr = SecretStr("")
    # Сколько стикеров конвертируется одновременно (ограничивает нагрузку на CPU и память)
    max_concurrent_conversions: PositiveInt = 2

    @property
    def has_bot_token(self) -> bool:
        return self.bot_token.get_secret_value() not in ("", BOT_TOKEN_PLACEHOLDER)
