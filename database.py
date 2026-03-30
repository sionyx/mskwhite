import sqlite3
from datetime import datetime, timezone

DB_PATH = "payments.db"


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


def get_expired_purchases(expires_before: datetime, db_path: str = DB_PATH) -> list[sqlite3.Row]:
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
