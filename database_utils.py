from __future__ import annotations

import sqlite3
import unicodedata


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
