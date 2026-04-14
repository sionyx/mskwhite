import os
import sqlite3
import logging
import argparse
import asyncio
from datetime import timedelta, timezone
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
        "\n\n🛜 Для подключения откройте приложение Outline, нажмите ➕, вставьте ключ в открывшееся окно, нажмите Подтвердить. После этого можете активировать VPN кнопкой Подключить."
        "\n\n📥 Если у вас нет приложения Outline, вы можете его скачать воспользовавшись командой /download"
        f"\n\n⏳ Вы можете проверить работу сервиса использовав до {limit_mb:.0f} МБ, и в случае проблем запросить возврат командой paysupport",
        f"\n\n🤔 Если у вас остались вопросы, просто напишите их в чат с ботом — вопрос будет перенаправлен администратору и вы быстро получите ответ",
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


async def my_limits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает пользователю оставшийся срок подписки и доступный трафик."""
    outline_service = context.application.bot_data.get("outline_service")
    if not outline_service:
        logging.error("OutlineService не инициализирован")
        await update.message.reply_text(
            "❌ Не удалось получить лимиты: сервис интеграции не настроен."
        )
        return

    user = update.effective_user
    if not user:
        await update.message.reply_text("❌ Не удалось определить пользователя.")
        return

    purchase = get_latest_purchase(user.id)
    if not purchase:
        await update.message.reply_text(
            "ℹ️ У вас нет активной подписки."
        )
        return

    remaining_days = get_remaining_subscription_days(purchase["payment_datetime"])
    if remaining_days <= 0:
        expiration_datetime = get_purchase_expiration_datetime(purchase["payment_datetime"])
        await update.message.reply_text(
            "ℹ️ Срок вашей подписки уже истек.\n\n"
            f"Дата окончания: {expiration_datetime.strftime('%d.%m.%Y')}"
        )
        return

    try:
        used_megabytes = outline_service.get_used_megabytes_for_user(user)
        data_limit_megabytes = outline_service.get_data_limit_megabytes_for_user(user)
    except OutlineServiceError as error:
        logging.exception("Ошибка при получении лимитов пользователя %s: %s", user.id, error)
        await update.message.reply_text(
            "❌ Не удалось получить данные по лимитам. Попробуйте позже."
        )
        return

    total_limit_megabytes = data_limit_megabytes
    if total_limit_megabytes is None:
        total_limit_megabytes = context.application.bot_data["traffic_limit_mb"]

    expiration_datetime = get_purchase_expiration_datetime(purchase["payment_datetime"])
    expiration_text = expiration_datetime.strftime('%d.%m.%Y')
    used_gigabytes = used_megabytes / 1000
    total_limit_gigabytes = total_limit_megabytes / 1000

    await update.message.reply_text(
        "📊 Ваши лимиты:\n\n"
        f"⏳ Осталось дней: {remaining_days}\n"
        f"📅 Подписка действует до: {expiration_text}\n"
        f"📶 Использовано {used_gigabytes:.2f} ГБ из {total_limit_gigabytes:.2f} ГБ"
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


async def list_purchases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выводит список записей из базы покупок за последние 40 дней."""
    if not is_admin(update, context):
        await deny_admin_access(update)
        return

    purchases = get_recent_purchases(days=40)
    if not purchases:
        await update.message.reply_text("🧾 За последние 40 дней покупок не найдено.")
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

    for chunk in chunks:
        await update.message.reply_text(chunk)


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
    traffic_limit_mb = context.application.bot_data['traffic_limit_mb'] / 1000
    welcome_text = f"""
👋 Добро пожаловать в VPN-сервис Москва Белокаменная!

Мы предоставляем надежный VPN-доступ через Outline который работает в условиях "белых списков".

⭐ Стоимость доступа: {subscription_price_stars} Telegram Stars на месяц. Ограничение трафика {traffic_limit_mb:.0f} ГБ.

Нажмите кнопку ниже, чтобы начать покупку или получите уже оплаченный ключ.

🤔 Если у вас остались вопросы, просто напишите их в чат с ботом — вопрос будет перенаправлен администратору и вы быстро получите ответ.
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

    await context.bot.send_message(
        chat_id=admin_user_id,
        text=(
            "📩 Новое сообщение от пользователя\n"
            f"👤 {format_user_mention(user)}"
        ),
    )

    forwarded_message = await message.forward(chat_id=admin_user_id)
    context.application.bot_data.setdefault("forwarded_messages", {})[
        forwarded_message.message_id
    ] = user.id

    await message.reply_text(
        "✅ Ваше сообщение отправлено администратору. Ответ придет сюда от имени бота."
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
        await message.reply_text(
            "⚠️ Сейчас можно пересылать пользователю только текстовые ответы администратора."
        )
        return

    await context.bot.send_message(
        chat_id=target_user_id,
        text=(
            "💬 Ответ администратора:\n\n"
            f"{reply_text}"
        ),
    )

    await message.reply_text("✅ Ответ отправлен пользователю.")


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