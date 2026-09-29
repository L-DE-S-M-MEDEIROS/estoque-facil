from __future__ import annotations

import sqlite3
import unicodedata


INVENTORY_TABLES = (
    "operation_types", "users", "product_groups", "products", "sku_mappings",
    "sku_mapping_products", "movement_batches", "movements", "monthly_stock_counts",
)


def initialize_sync_tracking(connection: sqlite3.Connection) -> None:
    """Keep unsent changes in the same transaction as the inventory itself.

    This device-local journal is deliberately excluded from cloud payloads.
    Triggers also cover writes by another connection or an older app process.
    """
    connection.execute("""CREATE TABLE IF NOT EXISTS local_sync_state (
        id INTEGER PRIMARY KEY CHECK(id=1),
        revision INTEGER NOT NULL DEFAULT 0,
        synced_revision INTEGER NOT NULL DEFAULT 0
    )""")
    connection.execute("INSERT OR IGNORE INTO local_sync_state(id) VALUES(1)")
    for table in INVENTORY_TABLES:
        for action in ("INSERT", "UPDATE", "DELETE"):
            # Ignore no-op updates during schema initialization and refreshes.
            condition = ""
            if action == "UPDATE":
                columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]
                condition = "WHEN " + " OR ".join(f'OLD."{column}" IS NOT NEW."{column}"' for column in columns)
            connection.execute(f"""CREATE TRIGGER IF NOT EXISTS track_sync_{table}_{action.lower()}
                AFTER {action} ON {table} {condition}
                BEGIN
                    UPDATE local_sync_state SET revision=revision+1 WHERE id=1;
                END""")


def get_sync_state(connection: sqlite3.Connection) -> tuple[int, int]:
    row = connection.execute("SELECT revision,synced_revision FROM local_sync_state WHERE id=1").fetchone()
    return int(row[0]), int(row[1])


def has_pending_sync(connection: sqlite3.Connection) -> bool:
    revision, synced_revision = get_sync_state(connection)
    return revision != synced_revision


def acknowledge_sync(connection: sqlite3.Connection, revision: int) -> None:
    """Acknowledge only the revision actually sent, never a newer local write."""
    connection.execute("""UPDATE local_sync_state
        SET synced_revision=MAX(synced_revision, MIN(revision, ?)) WHERE id=1""", (revision,))


def normalize_identity_text(value: object) -> str:
    """Normalize user-facing identifiers for reliable duplicate comparisons."""

    decomposed = unicodedata.normalize("NFKD", str(value or "").casefold())
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return " ".join(without_accents.split())


def register_database_functions(connection: sqlite3.Connection) -> None:
    connection.create_function(
        "normalize_identity_text",
        1,
        normalize_identity_text,
        deterministic=True,
    )


def configure_database_connection(connection: sqlite3.Connection) -> None:
    """Apply the safety and responsiveness settings used by every app connection."""

    register_database_functions(connection)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")


def database_integrity_errors(connection: sqlite3.Connection) -> list[str]:
    """Return concise SQLite integrity errors without mutating the database."""

    errors = [
        str(row[0])
        for row in connection.execute("PRAGMA quick_check")
        if str(row[0]).lower() != "ok"
    ]
    errors.extend(
        f"chave estrangeira inválida em {row[0]} (linha {row[1]})"
        for row in connection.execute("PRAGMA foreign_key_check")
    )
    return errors
