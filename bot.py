import os
import sqlite3
import logging
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, LabeledPrice, User
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv

from outline_service import OutlineService, OutlineServiceError

SUBSCRIPTION_DURATION = timedelta(days=30)
EXPIRATION_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_TRAFFIC_LIMIT_MB = 100 * 1024

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
        [KeyboardButton("🔐 Выдать ключ")],
        [KeyboardButton("📋 Список пользователей")],
        [KeyboardButton("📥 Скачать Outline")],
    ],
    resize_keyboard=True
)

# Сокращенная клавиатура для обычных пользователей
user_keyboard = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🛒 Купить доступ")],
        [KeyboardButton("🔑 Мой ключ")],
        [KeyboardButton("📥 Скачать Outline")],
    ],
    resize_keyboard=True
)


DB_PATH = "payments.db"
DEFAULT_PAY_SUPPORT_LIMIT_MB = 100.0
DEFAULT_SUBSCRIPTION_PRICE_STARS = 100


def init_database(db_path: str = DB_PATH) -> None:
    """Создает локальную SQLite базу и таблицу покупок при необходимости."""
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_datetime TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                payment_id TEXT NOT NULL UNIQUE,
                transaction_type TEXT NOT NULL
            )
            """
        )
        connection.commit()



def save_purchase(
    user_id: int,
    username: str | None,
    payment_id: str,
    transaction_type: str = "purchase",
    db_path: str = DB_PATH,
) -> None:
    """Сохраняет информацию об успешном платеже в SQLite."""
    payment_datetime = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO purchases (
                payment_datetime,
                user_id,
                username,
                payment_id,
                transaction_type
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (payment_datetime, user_id, username, payment_id, transaction_type),
        )
        connection.commit()



def get_latest_purchase(user_id: int, db_path: str = DB_PATH) -> sqlite3.Row | None:
    """Возвращает последний платеж пользователя со статусом purchase."""
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT id, payment_datetime, user_id, username, payment_id, transaction_type
            FROM purchases
            WHERE user_id = ? AND transaction_type = 'purchase'
            ORDER BY payment_datetime DESC, id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()



def mark_purchase_refunded(payment_id: str, db_path: str = DB_PATH) -> None:
    """Помечает платеж как возвращенный."""
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE purchases
            SET transaction_type = 'refunded'
            WHERE payment_id = ?
            """,
            (payment_id,),
        )
        connection.commit()



def get_expired_purchases(
    expires_before: datetime,
    db_path: str = DB_PATH,
) -> list[sqlite3.Row]:
    """Возвращает активные покупки, срок действия которых истек."""
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT id, payment_datetime, user_id, username, payment_id, transaction_type
            FROM purchases
            WHERE transaction_type = 'purchase'
              AND payment_datetime <= ?
            ORDER BY payment_datetime ASC, id ASC
            """,
            (expires_before.isoformat(),),
        ).fetchall()



def mark_purchase_expired(payment_id: str, db_path: str = DB_PATH) -> None:
    """Помечает платеж как истекший."""
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE purchases
            SET transaction_type = 'expired'
            WHERE payment_id = ?
            """,
            (payment_id,),
        )
        connection.commit()



def mark_purchase_overquota(payment_id: str, db_path: str = DB_PATH) -> None:
    """Помечает платеж как исчерпавший лимит трафика."""
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE purchases
            SET transaction_type = 'overquota'
            WHERE payment_id = ?
            """,
            (payment_id,),
        )
        connection.commit()



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



def build_telegram_user(user_id: int, username: str | None) -> User:
    """Создает минимальный объект пользователя Telegram для сервисных операций."""
    return User(
        id=user_id,
        first_name=username or f"user-{user_id}",
        is_bot=False,
        username=username,
    )



def format_user_label(user_id: int, username: str | None) -> str:
    """Формирует строку пользователя для логов и уведомлений администратору."""
    if username:
        return f"@{username} \\(ID: {user_id}\\)"

    return f"ID: {user_id}"



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



def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверяет, является ли пользователь администратором бота."""
    admin_user_id = context.application.bot_data.get("admin_user_id")
    user = update.effective_user

    return bool(user and admin_user_id is not None and user.id == admin_user_id)


async def deny_admin_access(update: Update):
    """Сообщает пользователю об отсутствии доступа к административной команде."""
    await update.message.reply_text("⛔ Эта команда доступна только администратору.")

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
        await update.message.reply_text(
            "❌ Не удалось подготовить доступ Outline: сервис интеграции не настроен."
        )
        return

    await update.message.reply_text("🔄 Создаем для вас ключ доступа Outline...")

    try:
        access_key = outline_service.create_access_key(update.effective_user)
    except OutlineServiceError as error:
        logging.exception("Ошибка при создании Outline access key: %s", error)
        await update.message.reply_text(
            "❌ Не удалось создать доступ Outline. Попробуйте позже."
        )
        return

    limit_mb = context.application.bot_data["pay_support_limit_mb"]

    logging.info("Outline access key успешно создан для пользователя %s", update.effective_user.id)
    await update.message.reply_text(
        "🔐 Ключ Outline успешно создан \\(нажмите, чтобы скопировать\\):\n\n"
        f"`{access_key}`"
        "\n\nДля подключения откройте приложение Outline, нажмите ➕, вставьте ключ в открывшееся окно, нажмите Подтвердить. После этого можете активировать VPN кнопкой Подключить."
        "\n\nЕсли у вас нет приложения Outline, вы можете его скачать воспользовавшись командой /download"
        f"\n\nВы можете проверить работу сервиса использовав до {limit_mb:.0f} МБ, и в случае проблем запросить возврат командой paysupport",
        parse_mode="MarkdownV2",
    )


async def my_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Находит и отправляет пользователю его текущий ключ Outline."""
    outline_service = context.application.bot_data.get("outline_service")
    if not outline_service:
        logging.error("OutlineService не инициализирован")
        await update.message.reply_text(
            "❌ Не удалось получить ключ Outline: сервис интеграции не настроен."
        )
        return

    await update.message.reply_text("🔄 Ищем ваш текущий ключ Outline...")

    try:
        access_key = outline_service.get_access_key_for_user(update.effective_user)
    except OutlineServiceError as error:
        logging.exception("Ошибка при поиске Outline access key: %s", error)
        await update.message.reply_text(
            "❌ Не удалось получить ваш ключ Outline. Попробуйте позже."
        )
        return

    if not access_key:
        await update.message.reply_text(
            "ℹ️ Для вашего аккаунта пока нет активного ключа Outline."
        )
        return

    await update.message.reply_text(
        "🔑 Ваш текущий ключ Outline \\(нажмите, чтобы скопировать\\):\n\n"
        f"`{access_key}`",
        parse_mode="MarkdownV2",
    )


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выводит полный список пользователей сервиса Outline."""
    if not is_admin(update, context):
        await deny_admin_access(update)
        return

    outline_service = context.application.bot_data.get("outline_service")
    if not outline_service:
        logging.error("OutlineService не инициализирован")
        await update.message.reply_text(
            "❌ Не удалось получить список пользователей: сервис интеграции не настроен."
        )
        return

    await update.message.reply_text("🔄 Получаем список пользователей Outline...")

    try:
        users = outline_service.list_access_keys()
    except OutlineServiceError as error:
        logging.exception("Ошибка при получении списка пользователей Outline: %s", error)
        await update.message.reply_text(
            "❌ Не удалось получить список пользователей Outline. Попробуйте позже."
        )
        return

    if not users:
        await update.message.reply_text("📋 В Outline пока нет пользователей.")
        return

    message = "📋 Список пользователей Outline:\n\n" + "\n".join(users)
    if len(message) <= 4096:
        await update.message.reply_text(message)
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

    for chunk in chunks:
        await update.message.reply_text(chunk)


async def download_outline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет ссылки на скачивание клиента Outline."""
    download_text = (
        "📥 Скачать Outline:\n\n"
        "• Android: https://play.google.com/store/apps/details?id=org.outline.android.client\n"
        "• iPhone / iPad: https://apps.apple.com/app/outline-app/id1356177741\n"
        "• Windows: https://s3.amazonaws.com/outline-releases/client/windows/stable/Outline-Client.exe\n"
        "• macOS: https://s3.amazonaws.com/outline-releases/client/darwin/stable/Outline-Client.dmg\n"
        "• Linux: https://s3.amazonaws.com/outline-releases/client/linux/stable/Outline-Client.AppImage\n"
        "• Официальная страница: https://getoutline.org/get-started/#step-3"
    )
    await update.message.reply_text(download_text)


async def paysupport_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает запрос на возврат платежа при малом использовании трафика."""
    outline_service = context.application.bot_data.get("outline_service")
    if not outline_service:
        logging.error("OutlineService не инициализирован")
        await update.message.reply_text(
            "❌ Не удалось обработать запрос на возврат: сервис Outline недоступен."
        )
        return

    user = update.effective_user
    if not user:
        await update.message.reply_text("❌ Не удалось определить пользователя.")
        return

    purchase = get_latest_purchase(user.id)
    if not purchase:
        await update.message.reply_text(
            "ℹ️ Для вашего аккаунта не найдено оплаченных покупок, доступных для возврата."
        )
        return

    try:
        used_megabytes = outline_service.get_used_megabytes_for_user(user)
    except OutlineServiceError as error:
        logging.exception("Ошибка при получении трафика пользователя %s: %s", user.id, error)
        await update.message.reply_text(
            "❌ Не удалось проверить использованный трафик. Попробуйте позже."
        )
        return

    limit_mb = context.application.bot_data["pay_support_limit_mb"]
    if used_megabytes >= limit_mb:
        await update.message.reply_text(
            "ℹ️ Услуга считается предоставленной: использовано "
            f"{used_megabytes:.2f} МБ при лимите возврата {limit_mb:.2f} МБ."
        )
        return

    payment_id = purchase["payment_id"]

    try:
        await context.bot.refund_star_payment(
            user_id=user.id,
            telegram_payment_charge_id=payment_id,
        )
        mark_purchase_refunded(payment_id)
        outline_service.delete_access_key_for_user(user)
    except sqlite3.Error as error:
        logging.exception("Ошибка при обновлении статуса платежа %s: %s", payment_id, error)
        await update.message.reply_text(
            "❌ Возврат выполнен, но не удалось обновить статус в локальной базе."
        )
        return
    except OutlineServiceError as error:
        logging.exception("Ошибка при удалении ключа Outline для пользователя %s: %s", user.id, error)
        await update.message.reply_text(
            "❌ Возврат выполнен, но не удалось удалить ключ Outline."
        )
        return
    except Exception as error:
        logging.exception("Ошибка при возврате платежа %s: %s", payment_id, error)
        await update.message.reply_text(
            "❌ Не удалось выполнить возврат платежа. Попробуйте позже."
        )
        return

    await update.message.reply_text(
        "✅ Возврат платежа выполнен. Доступ Outline удален."
    )


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    subscription_price_stars = context.application.bot_data["subscription_price_stars"]
    welcome_text = f"""
👋 Добро пожаловать в VPN-сервис Москва Белокаменная!

Мы предоставляем надежный VPN-доступ через Outline который работает в условиях "белых списков".

⭐ Стоимость доступа: {subscription_price_stars} Telegram Stars на месяц. Ограничение трафика 100 ГБ.

Нажмите кнопку ниже, чтобы начать покупку или получите уже оплаченный ключ:
    """.strip()

    reply_markup = admin_keyboard if is_admin(update, context) else user_keyboard
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def buy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки покупки - отправка инвойса на стоимость из конфигурации."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    outline_service = context.application.bot_data.get("outline_service")

    active_purchase = get_latest_purchase(user.id)
    if active_purchase:
        if not outline_service:
            logging.error("OutlineService не инициализирован")
            await update.message.reply_text(
                "⚠️ У вас уже есть активная покупка, но сейчас не удалось получить ключ Outline."
            )
            return

        try:
            access_key = outline_service.get_access_key_for_user(user)
        except OutlineServiceError as error:
            logging.exception("Ошибка при получении активного ключа Outline: %s", error)
            await update.message.reply_text(
                "⚠️ У вас уже есть активная покупка, но не удалось получить текущий ключ. Попробуйте позже."
            )
            return

        warning_text = "⚠️ У вас уже есть активная оплаченная покупка"
        if access_key:
            warning_text += f"\n\nВаш активный ключ Outline:\n\n`{access_key}`"
            await update.message.reply_text(warning_text, parse_mode="MarkdownV2")
            return

        await update.message.reply_text(
            warning_text + "\n\nАктивный ключ не найден. Используйте команду /paysupport или обратитесь к администратору."
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
        await context.bot.send_invoice(
            chat_id=chat_id,
            title=title,
            description=description,
            payload=payload,
            provider_token=provider_token,
            currency=currency,
            prices=prices,
            start_parameter="vpn_purchase"
        )
    except Exception as e:
        logging.error(f"Ошибка при отправке инвойса: {e}")
        await update.message.reply_text("❌ Произошла ошибка при создании счета. Попробуйте позже.")

async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждает pre-checkout запрос перед списанием Telegram Stars."""
    query = update.pre_checkout_query

    if query.invoice_payload != "vpn_access_purchase":
        logging.warning("Получен неизвестный payload в pre-checkout: %s", query.invoice_payload)
        await query.answer(ok=False, error_message="❌ Не удалось подтвердить платеж. Попробуйте начать покупку заново.")
        return

    await query.answer(ok=True)
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

    await update.message.reply_text(
        "✅ Покупка подтверждена!\n\n"
        "Платеж успешно получен, создаем для вас ключ доступа Outline."
    )

    await _issue_key(update, context)


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



async def unknown_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик неизвестных команд"""
    await update.message.reply_text("Извините, я не понимаю эту команду. Используйте /start для начала работы.")

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
    application.add_handler(MessageHandler(filters.Text("🛒 Купить доступ"), buy_handler))
    application.add_handler(MessageHandler(filters.Text("🔑 Мой ключ"), my_key))
    application.add_handler(MessageHandler(filters.Text("🔐 Выдать ключ"), issue_key))
    application.add_handler(MessageHandler(filters.Text("📋 Список пользователей"), list_users))
    application.add_handler(MessageHandler(filters.Text("📥 Скачать Outline"), download_outline))
    application.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_handler))
    
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