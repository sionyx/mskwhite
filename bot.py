import os
import sqlite3
import logging
import argparse
import asyncio
from datetime import timedelta, timezone
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, LabeledPrice, User
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv

from database import (
    get_latest_purchase,
    get_purchase_expiration_datetime,
    get_recent_purchases,
    get_remaining_subscription_days,
    init_database,
    mark_purchase_refunded,
    save_purchase,
)
from outline_service import OutlineService, OutlineServiceError
from subscription_checks import check_overquota_subscriptions, expire_subscriptions

EXPIRATION_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_TRAFFIC_LIMIT_MB = 100 * 1000

DEFAULT_TELEGRAM_API_RETRIES = 3
DEFAULT_TELEGRAM_API_RETRY_DELAY_SECONDS = 3.0

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Полная клавиатура для администратора
admin_keyboard = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🛒 Купить доступ")],
        [KeyboardButton("🔑 Мой ключ")],
        [KeyboardButton("📊 Мои лимиты")],
        [KeyboardButton("🔐 Выдать ключ")],
        [KeyboardButton("📋 Список пользователей")],
        [KeyboardButton("🧾 Список покупок")],
        [KeyboardButton("📥 Скачать Outline")],
    ],
    resize_keyboard=True
)

# Сокращенная клавиатура для обычных пользователей
user_keyboard = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🛒 Купить доступ")],
        [KeyboardButton("🔑 Мой ключ")],
        [KeyboardButton("📊 Мои лимиты")],
        [KeyboardButton("📥 Скачать Outline")],
    ],
    resize_keyboard=True
)


DEFAULT_PAY_SUPPORT_LIMIT_MB = 100.0
DEFAULT_SUBSCRIPTION_PRICE_STARS = 100



def get_pay_support_limit_mb() -> float:
    """Возвращает лимит трафика для возврата из переменных окружения."""
    raw_value = os.getenv("PAY_SUPPORT_LIMIT_MB", str(DEFAULT_PAY_SUPPORT_LIMIT_MB)).strip()

    try:
        limit_mb = float(raw_value)
    except ValueError as error:
        raise ValueError("PAY_SUPPORT_LIMIT_MB должен быть числом") from error

    if limit_mb < 0:
        raise ValueError("PAY_SUPPORT_LIMIT_MB не может быть отрицательным")

    return limit_mb



def get_subscription_price_stars() -> int:
    """Возвращает стоимость подписки в Telegram Stars из переменных окружения."""
    raw_value = os.getenv("SUBSCRIPTION_PRICE_STARS", str(DEFAULT_SUBSCRIPTION_PRICE_STARS)).strip()

    try:
        price_stars = int(raw_value)
    except ValueError as error:
        raise ValueError("SUBSCRIPTION_PRICE_STARS должен быть целым числом") from error

    if price_stars <= 0:
        raise ValueError("SUBSCRIPTION_PRICE_STARS должен быть положительным числом")

    return price_stars



def get_traffic_limit_mb() -> float:
    """Возвращает лимит трафика подписки в мегабайтах из переменных окружения."""
    raw_value = os.getenv("TRAFFIC_LIMIT_MB", str(DEFAULT_TRAFFIC_LIMIT_MB)).strip()

    try:
        traffic_limit_mb = float(raw_value)
    except ValueError as error:
        raise ValueError("TRAFFIC_LIMIT_MB должен быть числом") from error

    if traffic_limit_mb <= 0:
        raise ValueError("TRAFFIC_LIMIT_MB должен быть положительным числом")

    return traffic_limit_mb



def get_telegram_api_retries() -> int:
    """Возвращает количество повторных попыток для запросов к Telegram API."""
    raw_value = os.getenv("TELEGRAM_API_RETRIES", str(DEFAULT_TELEGRAM_API_RETRIES)).strip()

    try:
        retries = int(raw_value)
    except ValueError as error:
        raise ValueError("TELEGRAM_API_RETRIES должен быть целым числом") from error

    if retries < 1:
        raise ValueError("TELEGRAM_API_RETRIES должен быть не меньше 1")

    return retries



def get_telegram_api_retry_delay_seconds() -> float:
    """Возвращает задержку между повторными попытками запросов к Telegram API."""
    raw_value = os.getenv(
        "TELEGRAM_API_RETRY_DELAY_SECONDS",
        str(DEFAULT_TELEGRAM_API_RETRY_DELAY_SECONDS),
    ).strip()

    try:
        delay_seconds = float(raw_value)
    except ValueError as error:
        raise ValueError("TELEGRAM_API_RETRY_DELAY_SECONDS должен быть числом") from error

    if delay_seconds < 0:
        raise ValueError("TELEGRAM_API_RETRY_DELAY_SECONDS не может быть отрицательным")

    return delay_seconds



async def telegram_api_call_with_retries(
    operation,
    *,
    operation_name: str,
    retries: int,
    retry_delay_seconds: float,
):
    """Выполняет вызов Telegram API с повторными попытками при сетевых ошибках."""
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            return await operation()
        except RetryAfter as error:
            last_error = error
            if attempt == retries:
                break

            wait_seconds = max(float(error.retry_after), retry_delay_seconds)
            logging.warning(
                "Telegram API retry_after во время %s. Попытка %s/%s, ожидание %.2f сек.",
                operation_name,
                attempt,
                retries,
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds)
        except (NetworkError, TimedOut) as error:
            last_error = error
            if attempt == retries:
                break

            logging.warning(
                "Сетевая ошибка Telegram API во время %s. Попытка %s/%s через %.2f сек.: %s",
                operation_name,
                attempt,
                retries,
                retry_delay_seconds,
                error,
            )
            await asyncio.sleep(retry_delay_seconds)

    logging.exception(
        "Не удалось выполнить %s после %s попыток",
        operation_name,
        retries,
        exc_info=last_error,
    )
    raise last_error



def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверяет, является ли пользователь администратором бота."""
    admin_user_id = context.application.bot_data.get("admin_user_id")
    user = update.effective_user

    return bool(user and admin_user_id is not None and user.id == admin_user_id)


def get_telegram_retry_settings(application: Application | None) -> tuple[int, float]:
    """Возвращает настройки повторных попыток Telegram API из bot_data."""
    if application is None:
        return DEFAULT_TELEGRAM_API_RETRIES, DEFAULT_TELEGRAM_API_RETRY_DELAY_SECONDS

    retries = application.bot_data.get("telegram_api_retries", DEFAULT_TELEGRAM_API_RETRIES)
    retry_delay_seconds = application.bot_data.get(
        "telegram_api_retry_delay_seconds",
        DEFAULT_TELEGRAM_API_RETRY_DELAY_SECONDS,
    )
    return retries, retry_delay_seconds



async def reply_text_with_retries(
    message,
    text: str,
    *,
    operation_name: str,
    **kwargs,
):
    """Отправляет reply_text с повторными попытками при сетевых ошибках Telegram API."""
    if not message:
        return None

    application = getattr(message, "_application", None)
    retries, retry_delay_seconds = get_telegram_retry_settings(application)

    return await telegram_api_call_with_retries(
        lambda: message.reply_text(text, **kwargs),
        operation_name=operation_name,
        retries=retries,
        retry_delay_seconds=retry_delay_seconds,
    )



async def send_message_with_retries(
    bot,
    chat_id: int,
    text: str,
    *,
    operation_name: str,
    **kwargs,
):
    """Отправляет send_message с повторными попытками при сетевых ошибках Telegram API."""
    application = getattr(bot, "_application", None)
    retries, retry_delay_seconds = get_telegram_retry_settings(application)

    return await telegram_api_call_with_retries(
        lambda: bot.send_message(chat_id=chat_id, text=text, **kwargs),
        operation_name=operation_name,
        retries=retries,
        retry_delay_seconds=retry_delay_seconds,
    )



async def send_invoice_with_retries(
    bot,
    *,
    operation_name: str,
    **kwargs,
):
    """Отправляет invoice с повторными попытками при сетевых ошибках Telegram API."""
    application = getattr(bot, "_application", None)
    retries, retry_delay_seconds = get_telegram_retry_settings(application)

    return await telegram_api_call_with_retries(
        lambda: bot.send_invoice(**kwargs),
        operation_name=operation_name,
        retries=retries,
        retry_delay_seconds=retry_delay_seconds,
    )



async def refund_star_payment_with_retries(
    bot,
    *,
    operation_name: str,
    **kwargs,
):
    """Выполняет refund_star_payment с повторными попытками при сетевых ошибках Telegram API."""
    application = getattr(bot, "_application", None)
    retries, retry_delay_seconds = get_telegram_retry_settings(application)

    return await telegram_api_call_with_retries(
        lambda: bot.refund_star_payment(**kwargs),
        operation_name=operation_name,
        retries=retries,
        retry_delay_seconds=retry_delay_seconds,
    )



async def answer_pre_checkout_query_with_retries(
    query,
    *,
    operation_name: str,
    **kwargs,
):
    """Отвечает на pre-checkout query с повторными попытками при сетевых ошибках Telegram API."""
    application = getattr(query, "_application", None)
    retries, retry_delay_seconds = get_telegram_retry_settings(application)

    return await telegram_api_call_with_retries(
        lambda: query.answer(**kwargs),
        operation_name=operation_name,
        retries=retries,
        retry_delay_seconds=retry_delay_seconds,
    )



async def forward_message_with_retries(
    message,
    *,
    operation_name: str,
    **kwargs,
):
    """Пересылает сообщение с повторными попытками при сетевых ошибках Telegram API."""
    application = getattr(message, "_application", None)
    retries, retry_delay_seconds = get_telegram_retry_settings(application)

    return await telegram_api_call_with_retries(
        lambda: message.forward(**kwargs),
        operation_name=operation_name,
        retries=retries,
        retry_delay_seconds=retry_delay_seconds,
    )



async def deny_admin_access(update: Update):
    """Сообщает пользователю об отсутствии доступа к административной команде."""
    await reply_text_with_retries(
        update.message,
        "⛔ Эта команда доступна только администратору.",
        operation_name="deny_admin_access.reply_text",
    )

async def issue_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создает и отправляет пользователю Outline access key без оплаты."""
    if not is_admin(update, context):
        await deny_admin_access(update)
        return
    await _issue_key(update, context)

async def _issue_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    outline_service = context.application.bot_data.get("outline_service")
    if not outline_service:
        logging.error("OutlineService не инициализирован")
        await reply_text_with_retries(
            update.message,
            "❌ Не удалось подготовить доступ Outline: сервис интеграции не настроен.",
            operation_name="_issue_key.outline_service_missing.reply_text",
        )
        return

    await reply_text_with_retries(
        update.message,
        "🔄 Создаем для вас ключ доступа Outline...",
        operation_name="_issue_key.progress.reply_text",
    )

    try:
        access_key = outline_service.create_access_key(update.effective_user)
    except OutlineServiceError as error:
        logging.exception("Ошибка при создании Outline access key: %s", error)
        await reply_text_with_retries(
            update.message,
            "❌ Не удалось создать доступ Outline. Попробуйте позже.",
            operation_name="_issue_key.outline_error.reply_text",
        )
        return

    limit_mb = context.application.bot_data["pay_support_limit_mb"]

    logging.info("Outline access key успешно создан для пользователя %s", update.effective_user.id)
    await reply_text_with_retries(
        update.message,
        "🔐 Ключ Outline успешно создан \\(нажмите, чтобы скопировать\\):\n\n"
        f"`{access_key}`"
        "\n\n🛜 Для подключения откройте приложение Outline, нажмите ➕, вставьте ключ в открывшееся окно, нажмите Подтвердить. После этого можете активировать VPN кнопкой Подключить."
        "\n\n📥 Если у вас нет приложения Outline, вы можете его скачать воспользовавшись командой /download"
        f"\n\n⏳ Вы можете проверить работу сервиса использовав до {limit_mb:.0f} МБ, и в случае проблем запросить возврат командой paysupport"
        "\n\n🤔 Если у вас остались вопросы, просто напишите их в чат с ботом — вопрос будет перенаправлен администратору и вы быстро получите ответ",
        operation_name="_issue_key.success.reply_text",
        parse_mode="MarkdownV2",
    )


async def my_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Находит и отправляет пользователю его текущий ключ Outline."""
    outline_service = context.application.bot_data.get("outline_service")
    if not outline_service:
        logging.error("OutlineService не инициализирован")
        await reply_text_with_retries(
            update.message,
            "❌ Не удалось получить ключ Outline: сервис интеграции не настроен.",
            operation_name="my_key.outline_service_missing.reply_text",
        )
        return

    await reply_text_with_retries(
        update.message,
        "🔄 Ищем ваш текущий ключ Outline...",
        operation_name="my_key.progress.reply_text",
    )

    try:
        access_key = outline_service.get_access_key_for_user(update.effective_user)
    except OutlineServiceError as error:
        logging.exception("Ошибка при поиске Outline access key: %s", error)
        await reply_text_with_retries(
            update.message,
            "❌ Не удалось получить ваш ключ Outline. Попробуйте позже.",
            operation_name="my_key.outline_error.reply_text",
        )
        return

    if not access_key:
        await reply_text_with_retries(
            update.message,
            "ℹ️ Для вашего аккаунта пока нет активного ключа Outline.",
            operation_name="my_key.not_found.reply_text",
        )
        return

    await reply_text_with_retries(
        update.message,
        "🔑 Ваш текущий ключ Outline \\(нажмите, чтобы скопировать\\):\n\n"
        f"`{access_key}`",
        operation_name="my_key.success.reply_text",
        parse_mode="MarkdownV2",
    )


async def my_limits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает пользователю оставшийся срок подписки и доступный трафик."""
    outline_service = context.application.bot_data.get("outline_service")
    if not outline_service:
        logging.error("OutlineService не инициализирован")
        await reply_text_with_retries(
            update.message,
            "❌ Не удалось получить лимиты: сервис интеграции не настроен.",
            operation_name="my_limits.outline_service_missing.reply_text",
        )
        return

    user = update.effective_user
    if not user:
        await reply_text_with_retries(
            update.message,
            "❌ Не удалось определить пользователя.",
            operation_name="my_limits.user_missing.reply_text",
        )
        return

    purchase = get_latest_purchase(user.id)
    if not purchase:
        await reply_text_with_retries(
            update.message,
            "ℹ️ У вас нет активной подписки.",
            operation_name="my_limits.no_purchase.reply_text",
        )
        return

    remaining_days = get_remaining_subscription_days(purchase["payment_datetime"])
    if remaining_days <= 0:
        expiration_datetime = get_purchase_expiration_datetime(purchase["payment_datetime"])
        await reply_text_with_retries(
            update.message,
            "ℹ️ Срок вашей подписки уже истек.\n\n"
            f"Дата окончания: {expiration_datetime.strftime('%d.%m.%Y')}",
            operation_name="my_limits.expired.reply_text",
        )
        return

    try:
        used_megabytes = outline_service.get_used_megabytes_for_user(user)
        data_limit_megabytes = outline_service.get_data_limit_megabytes_for_user(user)
    except OutlineServiceError as error:
        logging.exception("Ошибка при получении лимитов пользователя %s: %s", user.id, error)
        await reply_text_with_retries(
            update.message,
            "❌ Не удалось получить данные по лимитам. Попробуйте позже.",
            operation_name="my_limits.outline_error.reply_text",
        )
        return

    total_limit_megabytes = data_limit_megabytes
    if total_limit_megabytes is None:
        total_limit_megabytes = context.application.bot_data["traffic_limit_mb"]

    expiration_datetime = get_purchase_expiration_datetime(purchase["payment_datetime"])
    expiration_text = expiration_datetime.strftime('%d.%m.%Y')
    used_gigabytes = used_megabytes / 1000
    total_limit_gigabytes = total_limit_megabytes / 1000

    await reply_text_with_retries(
        update.message,
        "📊 Ваши лимиты:\n\n"
        f"⏳ Осталось дней: {remaining_days}\n"
        f"📅 Подписка действует до: {expiration_text}\n"
        f"📶 Использовано {used_gigabytes:.2f} ГБ из {total_limit_gigabytes:.2f} ГБ",
        operation_name="my_limits.success.reply_text",
    )


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выводит полный список пользователей сервиса Outline."""
    if not is_admin(update, context):
        await deny_admin_access(update)
        return

    outline_service = context.application.bot_data.get("outline_service")
    if not outline_service:
        logging.error("OutlineService не инициализирован")
        await reply_text_with_retries(
            update.message,
            "❌ Не удалось получить список пользователей: сервис интеграции не настроен.",
            operation_name="list_users.outline_service_missing.reply_text",
        )
        return

    await reply_text_with_retries(
        update.message,
        "🔄 Получаем список пользователей Outline...",
        operation_name="list_users.progress.reply_text",
    )

    try:
        users = outline_service.list_access_keys()
    except OutlineServiceError as error:
        logging.exception("Ошибка при получении списка пользователей Outline: %s", error)
        await reply_text_with_retries(
            update.message,
            "❌ Не удалось получить список пользователей Outline. Попробуйте позже.",
            operation_name="list_users.outline_error.reply_text",
        )
        return

    if not users:
        await reply_text_with_retries(
            update.message,
            "📋 В Outline пока нет пользователей.",
            operation_name="list_users.empty.reply_text",
        )
        return

    message = "📋 Список пользователей Outline:\n\n" + "\n".join(users)
    if len(message) <= 4096:
        await reply_text_with_retries(
            update.message,
            message,
            operation_name="list_users.single_chunk.reply_text",
        )
        return

    chunks = []
    current_chunk = "📋 Список пользователей Outline:\n\n"
    for user in users:
        candidate = f"{current_chunk}{user}\n"
        if len(candidate) > 4096:
            chunks.append(current_chunk.rstrip())
            current_chunk = f"{user}\n"
        else:
            current_chunk = candidate

    if current_chunk.strip():
        chunks.append(current_chunk.rstrip())

    for index, chunk in enumerate(chunks, start=1):
        await reply_text_with_retries(
            update.message,
            chunk,
            operation_name=f"list_users.chunk_{index}.reply_text",
        )


async def list_purchases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выводит список записей из базы покупок за последние 40 дней."""
    if not is_admin(update, context):
        await deny_admin_access(update)
        return

    purchases = get_recent_purchases(days=40)
    if not purchases:
        await reply_text_with_retries(
            update.message,
            "🧾 За последние 40 дней покупок не найдено.",
            operation_name="list_purchases.empty.reply_text",
        )
        return

    lines = ["🧾 Покупки за последние 40 дней:", ""]
    for purchase in purchases:
        lines.append("; ".join(f"{key}={value}" for key, value in purchase.items()))

    chunks = []
    current_chunk = ""
    for line in lines:
        candidate = f"{current_chunk}{line}\n"
        if len(candidate) > 4096:
            chunks.append(current_chunk.rstrip())
            current_chunk = f"{line}\n"
        else:
            current_chunk = candidate

    if current_chunk.strip():
        chunks.append(current_chunk.rstrip())

    for index, chunk in enumerate(chunks, start=1):
        await reply_text_with_retries(
            update.message,
            chunk,
            operation_name=f"list_purchases.chunk_{index}.reply_text",
        )


async def download_outline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет ссылки на скачивание клиента Outline."""
    download_text = (
        "📥 Скачать Outline:\n\n"
        "• Android: https://play.google.com/store/apps/details?id=org.outline.android.client\n"
        "• iPhone / iPad: https://apps.apple.com/app/outline-app/id1356177741\n"
        "• Windows: https://s3.amazonaws.com/outline-releases/client/windows/stable/Outline-Client.exe\n"
        "• macOS: https://apps.apple.com/ru/app/outline-secure-internet-access/id1356178125?mt=12\n"
        "• Linux: https://s3.amazonaws.com/outline-releases/client/linux/stable/Outline-Client.AppImage\n"
        "• Официальная страница: https://getoutline.org/get-started/#step-3"
    )
    await reply_text_with_retries(
        update.message,
        download_text,
        operation_name="download_outline.reply_text",
    )


async def paysupport_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает запрос на возврат платежа при малом использовании трафика."""
    outline_service = context.application.bot_data.get("outline_service")
    if not outline_service:
        logging.error("OutlineService не инициализирован")
        await reply_text_with_retries(
            update.message,
            "❌ Не удалось обработать запрос на возврат: сервис Outline недоступен.",
            operation_name="paysupport.outline_service_missing.reply_text",
        )
        return

    user = update.effective_user
    if not user:
        await reply_text_with_retries(
            update.message,
            "❌ Не удалось определить пользователя.",
            operation_name="paysupport.user_missing.reply_text",
        )
        return

    purchase = get_latest_purchase(user.id)
    if not purchase:
        await reply_text_with_retries(
            update.message,
            "ℹ️ Для вашего аккаунта не найдено оплаченных покупок, доступных для возврата.",
            operation_name="paysupport.no_purchase.reply_text",
        )
        return

    try:
        used_megabytes = outline_service.get_used_megabytes_for_user(user)
    except OutlineServiceError as error:
        logging.exception("Ошибка при получении трафика пользователя %s: %s", user.id, error)
        await reply_text_with_retries(
            update.message,
            "❌ Не удалось проверить использованный трафик. Попробуйте позже.",
            operation_name="paysupport.traffic_error.reply_text",
        )
        return

    limit_mb = context.application.bot_data["pay_support_limit_mb"]
    if used_megabytes >= limit_mb:
        await reply_text_with_retries(
            update.message,
            "ℹ️ Услуга считается предоставленной: использовано "
            f"{used_megabytes:.2f} МБ при лимите возврата {limit_mb:.2f} МБ.",
            operation_name="paysupport.limit_exceeded.reply_text",
        )
        return

    payment_id = purchase["payment_id"]

    try:
        await refund_star_payment_with_retries(
            context.bot,
            operation_name="paysupport.refund_star_payment",
            user_id=user.id,
            telegram_payment_charge_id=payment_id,
        )
        mark_purchase_refunded(payment_id)
        outline_service.delete_access_key_for_user(user)
    except sqlite3.Error as error:
        logging.exception("Ошибка при обновлении статуса платежа %s: %s", payment_id, error)
        await reply_text_with_retries(
            update.message,
            "❌ Возврат выполнен, но не удалось обновить статус в локальной базе.",
            operation_name="paysupport.sqlite_error.reply_text",
        )
        return
    except OutlineServiceError as error:
        logging.exception("Ошибка при удалении ключа Outline для пользователя %s: %s", user.id, error)
        await reply_text_with_retries(
            update.message,
            "❌ Возврат выполнен, но не удалось удалить ключ Outline.",
            operation_name="paysupport.outline_delete_error.reply_text",
        )
        return
    except Exception as error:
        logging.exception("Ошибка при возврате платежа %s: %s", payment_id, error)
        await reply_text_with_retries(
            update.message,
            "❌ Не удалось выполнить возврат платежа. Попробуйте позже.",
            operation_name="paysupport.refund_error.reply_text",
        )
        return

    await reply_text_with_retries(
        update.message,
        "✅ Возврат платежа выполнен. Доступ Outline удален.",
        operation_name="paysupport.success.reply_text",
    )


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    subscription_price_stars = context.application.bot_data["subscription_price_stars"]
    traffic_limit_mb = context.application.bot_data['traffic_limit_mb'] / 1000
    welcome_text = f"""
👋 Добро пожаловать в VPN-сервис Москва Белокаменная!

Мы предоставляем надежный VPN-доступ через Outline который работает даже в условиях "белых списков" и с российскими приложениями.

⭐ Стоимость доступа: {subscription_price_stars} Telegram Stars на месяц. Ограничение трафика {traffic_limit_mb:.0f} ГБ.

ℹ️ После покупки вы можете проверить работу сервиса и вернуть звезды, в случае, если что-то работает не так.

Нажмите кнопку ниже👇, чтобы начать покупку или получите уже оплаченный ключ.

🤔 Ой, а как все настроить? Просто как 1️⃣-2️⃣-3️⃣:

1️⃣ Купить звезды через официального бота @PremiumBot
2️⃣ Оплатить звездами подписку для бота и получить ключ
3️⃣ Установить приложение Outline (ссылки по команде /getoutline) и вставить в него ключ

🤔 Если у вас остались вопросы, просто напишите их в чат с ботом — вопрос будет перенаправлен администратору и вы быстро получите ответ.
    """.strip()

    reply_markup = admin_keyboard if is_admin(update, context) else user_keyboard
    await reply_text_with_retries(
        update.message,
        welcome_text,
        operation_name="start_handler.reply_text",
        reply_markup=reply_markup,
    )

async def buy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки покупки - отправка инвойса на стоимость из конфигурации."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    outline_service = context.application.bot_data.get("outline_service")

    active_purchase = get_latest_purchase(user.id)
    if active_purchase:
        if not outline_service:
            logging.error("OutlineService не инициализирован")
            await reply_text_with_retries(
                update.message,
                "⚠️ У вас уже есть активная покупка, но сейчас не удалось получить ключ Outline.",
                operation_name="buy_handler.active_purchase.outline_service_missing.reply_text",
            )
            return

        try:
            access_key = outline_service.get_access_key_for_user(user)
        except OutlineServiceError as error:
            logging.exception("Ошибка при получении активного ключа Outline: %s", error)
            await reply_text_with_retries(
                update.message,
                "⚠️ У вас уже есть активная покупка, но не удалось получить текущий ключ. Попробуйте позже.",
                operation_name="buy_handler.active_purchase.outline_error.reply_text",
            )
            return

        warning_text = "⚠️ У вас уже есть активная оплаченная покупка"
        if access_key:
            warning_text += f"\n\nВаш активный ключ Outline:\n\n`{access_key}`"
            await reply_text_with_retries(
                update.message,
                warning_text,
                operation_name="buy_handler.active_purchase.with_key.reply_text",
                parse_mode="MarkdownV2",
            )
            return

        await reply_text_with_retries(
            update.message,
            warning_text + "\n\nАктивный ключ не найден. Используйте команду /paysupport или обратитесь к администратору.",
            operation_name="buy_handler.active_purchase.no_key.reply_text",
        )
        return

    # Получение токена платежного провайдера из контекста
    provider_token = context.bot_data.get('payment_token')

    # Создание инвойса на стоимость из переменных окружения
    subscription_price_stars = context.application.bot_data['subscription_price_stars']
    title = "VPN-доступ через Outline"
    description = "Доступ к VPN-серверу Outline на 30 дней"
    payload = "vpn_access_purchase"
    currency = "XTR"  # Telegram Stars
    price = subscription_price_stars

    prices = [LabeledPrice("VPN доступ", price)]

    try:
        await send_invoice_with_retries(
            context.bot,
            operation_name="buy_handler.send_invoice",
            chat_id=chat_id,
            title=title,
            description=description,
            payload=payload,
            provider_token=provider_token,
            currency=currency,
            prices=prices,
            start_parameter="vpn_purchase",
        )
    except Exception as error:
        logging.error("Ошибка при отправке инвойса: %s", error)
        await reply_text_with_retries(
            update.message,
            "❌ Произошла ошибка при создании счета. Попробуйте позже.",
            operation_name="buy_handler.send_invoice_error.reply_text",
        )

async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждает pre-checkout запрос перед списанием Telegram Stars."""
    query = update.pre_checkout_query

    if query.invoice_payload != "vpn_access_purchase":
        logging.warning("Получен неизвестный payload в pre-checkout: %s", query.invoice_payload)
        await answer_pre_checkout_query_with_retries(
            query,
            operation_name="pre_checkout_handler.invalid_payload.answer",
            ok=False,
            error_message="❌ Не удалось подтвердить платеж. Попробуйте начать покупку заново.",
        )
        return

    await answer_pre_checkout_query_with_retries(
        query,
        operation_name="pre_checkout_handler.success.answer",
        ok=True,
    )
    logging.info("Pre-checkout подтвержден для пользователя %s", query.from_user.id)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает успешную оплату и создает Outline access key."""
    payment = update.message.successful_payment

    logging.info(
        "Успешная оплата: user_id=%s, payload=%s, total_amount=%s, currency=%s",
        update.effective_user.id,
        payment.invoice_payload,
        payment.total_amount,
        payment.currency,
    )

    try:
        save_purchase(
            user_id=update.effective_user.id,
            username=update.effective_user.username,
            payment_id=payment.telegram_payment_charge_id,
        )
    except sqlite3.IntegrityError:
        logging.warning(
            "Платеж уже сохранен в БД: payment_id=%s",
            payment.telegram_payment_charge_id,
        )
    except sqlite3.Error as error:
        logging.exception("Не удалось сохранить платеж в SQLite: %s", error)

    await reply_text_with_retries(
        update.message,
        "✅ Покупка подтверждена!\n\n"
        "Платеж успешно получен, создаем для вас ключ доступа Outline.",
        operation_name="successful_payment_handler.reply_text",
    )

    await _issue_key(update, context)


async def run_daily_expiration_check(application: Application) -> None:
    """Запускает ежедневные проверки истечения подписок и превышения лимита трафика."""
    while True:
        await expire_subscriptions(application)
        await check_overquota_subscriptions(application)
        await asyncio.sleep(EXPIRATION_CHECK_INTERVAL_SECONDS)



async def on_startup(application: Application) -> None:
    """Запускает фоновую задачу проверки истекших подписок."""
    application.bot_data["expiration_task"] = asyncio.create_task(
        run_daily_expiration_check(application)
    )



async def on_shutdown(application: Application) -> None:
    """Корректно останавливает фоновые задачи приложения."""
    expiration_task = application.bot_data.get("expiration_task")
    if not expiration_task:
        return

    expiration_task.cancel()
    try:
        await expiration_task
    except asyncio.CancelledError:
        logging.info("Фоновая задача проверки истекших подписок остановлена")



def format_user_mention(user: User) -> str:
    """Возвращает строку с данными пользователя для сообщений администратору."""
    username = f"@{user.username}" if user.username else "без username"
    full_name = user.full_name
    return (
        f"{full_name} ({username}, id={user.id})"
    )


async def forward_user_message_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пересылает обычные сообщения пользователей администратору."""
    message = update.effective_message
    user = update.effective_user
    admin_user_id = context.application.bot_data.get("admin_user_id")

    if not message or not user or admin_user_id is None:
        return

    if user.id == admin_user_id:
        return

    await send_message_with_retries(
        context.bot,
        admin_user_id,
        (
            "📩 Новое сообщение от пользователя\n"
            f"👤 {format_user_mention(user)}"
        ),
        operation_name="forward_user_message_to_admin.notify_admin.send_message",
    )

    forwarded_message = await forward_message_with_retries(
        message,
        chat_id=admin_user_id,
        operation_name="forward_user_message_to_admin.forward",
    )
    context.application.bot_data.setdefault("forwarded_messages", {})[
        forwarded_message.message_id
    ] = user.id

    await reply_text_with_retries(
        message,
        "✅ Ваше сообщение отправлено администратору. Ответ придет сюда от имени бота.",
        operation_name="forward_user_message_to_admin.confirm.reply_text",
    )


async def relay_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет ответ администратора пользователю, если это ответ на пересланное сообщение."""
    message = update.effective_message
    admin_user_id = context.application.bot_data.get("admin_user_id")

    if not message or not message.reply_to_message or update.effective_user is None:
        return

    if update.effective_user.id != admin_user_id:
        return

    forwarded_messages = context.application.bot_data.get("forwarded_messages", {})
    target_user_id = forwarded_messages.get(message.reply_to_message.message_id)
    if not target_user_id:
        return

    reply_text = message.text or message.caption
    if not reply_text:
        await reply_text_with_retries(
            message,
            "⚠️ Сейчас можно пересылать пользователю только текстовые ответы администратора.",
            operation_name="relay_admin_reply.non_text.reply_text",
        )
        return

    await send_message_with_retries(
        context.bot,
        target_user_id,
        (
            "💬 Ответ администратора:\n\n"
            f"{reply_text}"
        ),
        operation_name="relay_admin_reply.send_message",
    )

    await reply_text_with_retries(
        message,
        "✅ Ответ отправлен пользователю.",
        operation_name="relay_admin_reply.confirm.reply_text",
    )


async def unknown_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик неизвестных команд"""
    await reply_text_with_retries(
        update.message,
        "Извините, я не понимаю эту команду. Используйте /start для начала работы.",
        operation_name="unknown_handler.reply_text",
    )

def main():
    """Основная функция запуска бота"""
    # Парсинг аргументов командной строки
    parser = argparse.ArgumentParser(description='Запуск Telegram бота')
    parser.add_argument('--test', action='store_true', help='Запуск в тестовом режиме')
    args = parser.parse_args()
    
    # Выбор окружения
    if args.test:
        print("🚀 Запуск в ТЕСТОВОМ режиме")
        bot_token = os.getenv('TEST_BOT_TOKEN')
        payment_token = os.getenv('TEST_PAYMENT_PROVIDER_TOKEN')
        
        if not bot_token:
            logging.error("TEST_BOT_TOKEN не найден в переменных окружения")
            print("❌ Ошибка: TEST_BOT_TOKEN не найден")
            return
    else:
        print("🚀 Запуск в PRODUCTION режиме")
        bot_token = os.getenv('BOT_TOKEN')
        payment_token = os.getenv('PAYMENT_PROVIDER_TOKEN')
        
        if not bot_token:
            logging.error("BOT_TOKEN не найден в переменных окружения")
            print("❌ Ошибка: BOT_TOKEN не найден")
            return
    
    admin_user_id_raw = os.getenv('ADMIN_USER_ID')
    if not admin_user_id_raw:
        logging.error("ADMIN_USER_ID не найден в переменных окружения")
        print("❌ Ошибка: ADMIN_USER_ID не найден")
        return

    try:
        admin_user_id = int(admin_user_id_raw)
    except ValueError:
        logging.error("ADMIN_USER_ID должен быть целым числом, получено: %s", admin_user_id_raw)
        print("❌ Ошибка: ADMIN_USER_ID должен быть целым числом")
        return

    try:
        pay_support_limit_mb = get_pay_support_limit_mb()
        subscription_price_stars = get_subscription_price_stars()
        traffic_limit_mb = get_traffic_limit_mb()
        telegram_api_retries = get_telegram_api_retries()
        telegram_api_retry_delay_seconds = get_telegram_api_retry_delay_seconds()
    except ValueError as error:
        logging.error("Некорректное значение переменной окружения: %s", error)
        print(f"❌ Ошибка: {error}")
        return

    try:
        init_database()
        logging.info("SQLite база платежей инициализирована")
    except sqlite3.Error as error:
        logging.error("Не удалось инициализировать SQLite базу: %s", error)
        print("❌ Ошибка: не удалось инициализировать SQLite базу")
        return

    # Создание приложения
    application = (
        Application.builder()
        .token(bot_token)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    
    # Сохранение токена платежного провайдера в контексте бота
    application.bot_data['payment_token'] = payment_token
    application.bot_data['admin_user_id'] = admin_user_id
    application.bot_data['pay_support_limit_mb'] = pay_support_limit_mb
    application.bot_data['subscription_price_stars'] = subscription_price_stars
    application.bot_data['traffic_limit_mb'] = traffic_limit_mb
    application.bot_data['telegram_api_retries'] = telegram_api_retries
    application.bot_data['telegram_api_retry_delay_seconds'] = telegram_api_retry_delay_seconds

    try:
        application.bot_data['outline_service'] = OutlineService.from_env()
        logging.info("OutlineService успешно инициализирован")
    except OutlineServiceError as error:
        application.bot_data['outline_service'] = None
        logging.warning("OutlineService не инициализирован: %s", error)
    
    # Добавление обработчиков
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("download", download_outline))
    application.add_handler(CommandHandler("paysupport", paysupport_handler))
    application.add_handler(CommandHandler("getoutline", download_outline))
    application.add_handler(MessageHandler(filters.Text("🛒 Купить доступ"), buy_handler))
    application.add_handler(MessageHandler(filters.Text("🔑 Мой ключ"), my_key))
    application.add_handler(MessageHandler(filters.Text("📊 Мои лимиты"), my_limits))
    application.add_handler(MessageHandler(filters.Text("🔐 Выдать ключ"), issue_key))
    application.add_handler(MessageHandler(filters.Text("📋 Список пользователей"), list_users))
    application.add_handler(MessageHandler(filters.Text("🧾 Список покупок"), list_purchases))
    application.add_handler(MessageHandler(filters.Text("📥 Скачать Outline"), download_outline))
    application.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    application.add_handler(MessageHandler(filters.REPLY & filters.TEXT, relay_admin_reply))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_handler))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, forward_user_message_to_admin))
    
    # Запуск бота
    print("Бот запущен...")
    
    # Создание и запуск event loop для Python 3.14
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        application.run_polling()
    finally:
        loop.close()

if __name__ == "__main__":
    main()