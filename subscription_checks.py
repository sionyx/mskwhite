import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram import User
from telegram.ext import Application

from database import (
    SUBSCRIPTION_DURATION_DAYS,
    get_expired_purchases,
    mark_purchase_expired,
    mark_purchase_overquota,
)
from outline_service import OutlineServiceError

SUBSCRIPTION_DURATION = timedelta(days=SUBSCRIPTION_DURATION_DAYS)


def build_telegram_user(user_id: int, username: str | None) -> User:
    """Создает минимальный объект пользователя Telegram для сервисных операций."""
    return User(
        id=user_id,
        first_name=username or f"user-{user_id}",
        is_bot=False,
        username=username,
    )


async def notify_admin_key_revoked(
    application: Application,
    user_id: int,
    username: str | None,
    reason: str,
) -> None:
    """Отправляет администратору уведомление об отзыве ключа пользователя."""
    admin_user_id = application.bot_data.get("admin_user_id")
    if not admin_user_id:
        logging.warning("Не удалось отправить уведомление администратору: admin_user_id не задан")
        return

    user_label = format_user_label(user_id, username)
    await application.bot.send_message(
        chat_id=admin_user_id,
        text=(
            "🔒 Ключ пользователя отозван\n\n"
            f"Пользователь: {user_label}\n"
            f"Причина: {reason}"
        ),
        parse_mode="MarkdownV2",
    )


def format_user_label(user_id: int, username: str | None) -> str:
    """Формирует строку пользователя для логов и уведомлений администратору."""
    if username:
        return f"@{username} \\(ID: {user_id}\\)"

    return f"ID: {user_id}"


async def expire_subscriptions(application: Application) -> None:
    """Находит истекшие подписки, уведомляет пользователей и удаляет их ключи Outline."""
    outline_service = application.bot_data.get("outline_service")
    if not outline_service:
        logging.warning("Проверка истекших подписок пропущена: OutlineService не инициализирован")
        return

    expires_before = datetime.now(timezone.utc) - SUBSCRIPTION_DURATION

    try:
        expired_purchases = get_expired_purchases(expires_before)
    except sqlite3.Error as error:
        logging.exception("Не удалось получить список истекших подписок: %s", error)
        return

    if not expired_purchases:
        logging.info("Истекших подписок не найдено")
        return

    logging.info("Найдено %s истекших подписок", len(expired_purchases))

    for purchase in expired_purchases:
        user_id = purchase["user_id"]
        username = purchase["username"]
        payment_id = purchase["payment_id"]
        telegram_user = build_telegram_user(user_id, username)

        try:
            await application.bot.send_message(
                chat_id=user_id,
                text=(
                    "⏰ Срок вашей подписки истек.\n\n"
                    "Текущий ключ Outline деактивирован. Чтобы продолжить пользоваться сервисом, "
                    "оформите новую покупку."
                ),
            )
            outline_service.delete_access_key_for_user(telegram_user)
            mark_purchase_expired(payment_id)
            await notify_admin_key_revoked(
                application=application,
                user_id=user_id,
                username=username,
                reason="истек срок подписки",
            )
            logging.info("Подписка пользователя %s помечена как expired", user_id)
        except sqlite3.Error as error:
            logging.exception(
                "Не удалось пометить покупку %s как expired: %s",
                payment_id,
                error,
            )
        except OutlineServiceError as error:
            logging.exception(
                "Не удалось удалить ключ Outline для пользователя %s: %s",
                user_id,
                error,
            )
        except Exception as error:
            logging.exception(
                "Не удалось обработать истечение подписки пользователя %s: %s",
                user_id,
                error,
            )


async def check_overquota_subscriptions(application: Application) -> None:
    """Находит пользователей с превышением лимита трафика, уведомляет их и удаляет ключи Outline."""
    outline_service = application.bot_data.get("outline_service")
    if not outline_service:
        logging.warning("Проверка превышения лимита трафика пропущена: OutlineService не инициализирован")
        return

    try:
        active_purchases = get_expired_purchases(datetime.now(timezone.utc) + timedelta(days=365 * 100))
    except sqlite3.Error as error:
        logging.exception("Не удалось получить список активных подписок для проверки трафика: %s", error)
        return

    if not active_purchases:
        logging.info("Активных подписок для проверки трафика не найдено")
        return

    overquota_count = 0

    for purchase in active_purchases:
        user_id = purchase["user_id"]
        username = purchase["username"]
        payment_id = purchase["payment_id"]
        telegram_user = build_telegram_user(user_id, username)

        try:
            used_megabytes = outline_service.get_used_megabytes_for_user(telegram_user)
            traffic_limit_mb = outline_service.get_data_limit_megabytes_for_user(telegram_user)
            if traffic_limit_mb is None:
                traffic_limit_mb = application.bot_data["traffic_limit_mb"]

            if used_megabytes < traffic_limit_mb:
                continue

            await application.bot.send_message(
                chat_id=user_id,
                text=(
                    "📶 Лимит трафика по вашей подписке исчерпан.\n\n"
                    "Текущий ключ Outline деактивирован. Чтобы продолжить пользоваться сервисом, "
                    "оформите новую покупку."
                ),
            )
            outline_service.delete_access_key_for_user(telegram_user)
            mark_purchase_overquota(payment_id)
            await notify_admin_key_revoked(
                application=application,
                user_id=user_id,
                username=username,
                reason="превышен лимит трафика",
            )
            overquota_count += 1
            logging.info("Подписка пользователя %s помечена как overquota", user_id)
        except sqlite3.Error as error:
            logging.exception(
                "Не удалось пометить покупку %s как overquota: %s",
                payment_id,
                error,
            )
        except OutlineServiceError as error:
            logging.exception(
                "Не удалось проверить или удалить ключ Outline для пользователя %s: %s",
                user_id,
                error,
            )
        except Exception as error:
            logging.exception(
                "Не удалось обработать превышение лимита пользователя %s: %s",
                user_id,
                error,
            )

    if overquota_count == 0:
        logging.info("Пользователей с превышением лимита трафика не найдено")
