import logging
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from outline_vpn.outline_vpn import OutlineVPN


class OutlineServiceError(Exception):
    """Ошибка интеграции с Outline API."""


@dataclass(frozen=True)
class OutlineConfig:
    api_url: str
    cert_sha256: str

    @classmethod
    def from_env(cls) -> "OutlineConfig":
        api_url = os.getenv("OUTLINE_API_URL", "").strip()
        cert_sha256 = os.getenv("OUTLINE_CERT_SHA256", "").strip()

        missing = []
        if not api_url:
            missing.append("OUTLINE_API_URL")
        if not cert_sha256:
            missing.append("OUTLINE_CERT_SHA256")

        if missing:
            raise OutlineServiceError(
                f"Не заданы обязательные переменные окружения Outline: {', '.join(missing)}"
            )

        return cls(api_url=api_url, cert_sha256=cert_sha256)


class OutlineService:
    """Сервис для работы с ключами доступа в Outline."""

    def __init__(self, config: OutlineConfig):
        self._config = config
        self._client = OutlineVPN(
            api_url=config.api_url,
            cert_sha256=config.cert_sha256,
        )

    @classmethod
    def from_env(cls) -> "OutlineService":
        return cls(OutlineConfig.from_env())

    def create_access_key(self, telegram_user) -> str:
        key_name = self._build_key_name(telegram_user)
        logging.info("Создание Outline access key для пользователя %s с именем %s", telegram_user.id, key_name)

        try:
            key = self._client.create_key(name=key_name)
        except Exception as error:
            raise OutlineServiceError("Не удалось создать access key в Outline") from error

        access_url = getattr(key, "access_url", None)
        if not access_url:
            raise OutlineServiceError("Outline API вернул ключ без access URL")

        return access_url

    def list_access_keys(self) -> list[str]:
        logging.info("Получение списка пользователей Outline")

        keys = self._get_keys()
        if not keys:
            return []

        return [self._format_key_summary(key) for key in keys]

    def get_access_key_for_user(self, telegram_user) -> Optional[str]:
        key = self.get_key_for_user(telegram_user)
        if not key:
            return None

        access_url = getattr(key, "access_url", None)
        if access_url:
            return access_url

        key_id = getattr(key, "key_id", None)
        if not key_id:
            raise OutlineServiceError("Outline API вернул ключ без ID")

        return self._build_access_url(key_id)

    def get_key_for_user(self, telegram_user):
        key_name = self._build_key_name(telegram_user)
        logging.info(
            "Поиск Outline access key для пользователя %s с именем %s",
            telegram_user.id,
            key_name,
        )

        for key in self._get_keys():
            if getattr(key, "name", None) == key_name:
                return key

        return None

    def get_used_megabytes_for_user(self, telegram_user) -> float:
        key = self.get_key_for_user(telegram_user)
        if not key:
            raise OutlineServiceError("Для пользователя не найден ключ Outline")

        used_bytes = getattr(key, "used_bytes", 0) or 0
        return used_bytes / (1024 * 1024)

    def delete_access_key_for_user(self, telegram_user) -> bool:
        key = self.get_key_for_user(telegram_user)
        if not key:
            return False

        key_id = getattr(key, "key_id", None)
        if not key_id:
            raise OutlineServiceError("Outline API вернул ключ без ID")

        try:
            self._client.delete_key(key_id)
        except Exception as error:
            raise OutlineServiceError("Не удалось удалить access key в Outline") from error

        return True

    def _get_keys(self):
        try:
            return self._client.get_keys()
        except Exception as error:
            raise OutlineServiceError("Не удалось получить список пользователей Outline") from error

    def _build_access_url(self, key_id: str) -> str:
        try:
            server_info = self._client.get_server_information()
        except Exception as error:
            raise OutlineServiceError("Не удалось получить информацию о сервере Outline") from error

        hostname = getattr(server_info, "hostname_for_access_keys", None)
        port = getattr(server_info, "port_for_new_access_keys", None)
        password = getattr(server_info, "access_key_data_limit", None)

        if not hostname or not port:
            raise OutlineServiceError("Outline API вернул неполную информацию о сервере")

        try:
            key = self._client.get_key(key_id)
        except Exception as error:
            raise OutlineServiceError("Не удалось получить данные ключа Outline") from error

        password = getattr(key, "password", None)
        method = getattr(key, "method", None)
        if not password or not method:
            raise OutlineServiceError("Outline API вернул неполные данные ключа")

        credentials = quote(f"{method}:{password}")
        return f"ss://{credentials}@{hostname}:{port}/?outline=1"

    @staticmethod
    def _format_key_summary(key) -> str:
        key_id = getattr(key, "key_id", "unknown")
        name = getattr(key, "name", None) or "без имени"
        used_bytes = getattr(key, "used_bytes", 0) or 0
        data_limit = getattr(key, "data_limit", None)
        used_megabytes = used_bytes / (1024 * 1024)

        summary = f"• ID: {key_id} | Имя: {name} | Трафик: {used_megabytes:.2f} МБ"
        if data_limit is not None:
            data_limit_megabytes = data_limit / (1024 * 1024)
            summary += f" | Лимит: {data_limit_megabytes:.2f} МБ"

        return summary

    @staticmethod
    def _build_key_name(telegram_user) -> str:
        username = getattr(telegram_user, "username", None)
        full_name = getattr(telegram_user, "full_name", None)

        preferred_name = username or full_name or f"user-{telegram_user.id}"
        normalized_name = " ".join(preferred_name.split())
        return f"tg-{telegram_user.id}-{normalized_name}"[:100]
