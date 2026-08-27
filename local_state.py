from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


UI_DEFAULTS: dict[str, Any] = {
    "theme": "Light",
    "window_state": "zoomed",
    "window_geometry": "",
    "last_page": "stock",
    "counter_name": "",
    "stock_search": "",
    "count_search": "",
    "count_filter": "todos",
    "count_products_expanded": False,
    "movement_products_expanded": False,
    "movement_operation": "Entrada",
    "movement_user": "",
    "movement_section": "new",
    "quick_stock_action": "Defeito",
    "quick_stock_user": "",
    "kit_conversion_mode": "Montagem",
    "kit_conversion_user": "",
    "history_filter": "Todas as operações",
}

CLOUD_SESSION_KEYS = frozenset(
    {
        "cloud_access_token",
        "cloud_refresh_token",
        "cloud_user_id",
        "cloud_email",
        "cloud_device_id",
        "cloud_last_fingerprint",
        "cloud_last_revision",
        "cloud_last_remote_updated_at",
        "cloud_local_modified_at",
    }
)


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _text(value: Any, fallback: str = "", maximum: int = 240) -> str:
    return str(value if value is not None else fallback)[:maximum]


def sanitize_preferences(values: dict[str, Any]) -> dict[str, Any]:
    clean = dict(UI_DEFAULTS)
    clean["theme"] = values.get("theme") if values.get("theme") in ("Light", "Dark") else "Light"
    clean["window_state"] = values.get("window_state") if values.get("window_state") in ("normal", "zoomed") else "zoomed"
    clean["window_geometry"] = _text(values.get("window_geometry"), maximum=80)
    clean["last_page"] = values.get("last_page") if values.get("last_page") in ("stock", "movements", "defect_return", "kit_conversion", "simulation", "count", "registration", "settings") else "stock"
    clean["counter_name"] = _text(values.get("counter_name"), maximum=100)
    clean["stock_search"] = _text(values.get("stock_search"))
    clean["count_search"] = _text(values.get("count_search"))
    clean["count_filter"] = values.get("count_filter") if values.get("count_filter") in ("todos", "pendentes", "verificados") else "todos"
    clean["count_products_expanded"] = values.get("count_products_expanded") is True
    clean["movement_products_expanded"] = values.get("movement_products_expanded") is True
    clean["movement_operation"] = _text(values.get("movement_operation"), "Entrada", 100) or "Entrada"
    clean["movement_user"] = _text(values.get("movement_user"), maximum=100)
    clean["movement_section"] = values.get("movement_section") if values.get("movement_section") in ("new", "history") else "new"
    clean["quick_stock_action"] = values.get("quick_stock_action") if values.get("quick_stock_action") in ("Defeito", "Devolução") else "Defeito"
    clean["quick_stock_user"] = _text(values.get("quick_stock_user"), maximum=100)
    saved_kit_mode = values.get("kit_conversion_mode")
    clean["kit_conversion_mode"] = "Desmontagem" if saved_kit_mode in ("Desmontagem", "Desmembramento") else "Montagem"
    clean["kit_conversion_user"] = _text(values.get("kit_conversion_user"), maximum=100)
    clean["history_filter"] = _text(values.get("history_filter"), "Todas as operações", 100) or "Todas as operações"
    return clean


class LocalPreferences:
    """Persist only device-local UI state for the current operating-system user."""

    def __init__(self, path: Path, legacy: dict[str, Any] | None = None):
        self.path = path
        stored = read_json_object(path)
        if isinstance(stored.get("preferences"), dict):
            stored = stored["preferences"]
        source = stored if path.exists() else (legacy or {})
        self.values = sanitize_preferences(source)
        self.save()

    def save(self) -> None:
        clean = sanitize_preferences(self.values)
        self.values.clear()
        self.values.update(clean)
        atomic_write_json(self.path, {"format": 1, "preferences": self.values})


class LocalCloudSession:
    """Persist Supabase authentication locally without accepting UI preferences."""

    def __init__(self, path: Path, legacy: dict[str, Any] | None = None):
        self.path = path
        stored = read_json_object(path)
        if isinstance(stored.get("session"), dict):
            stored = stored["session"]
        source = stored if path.exists() else (legacy or {})
        self.values = {key: source[key] for key in CLOUD_SESSION_KEYS if key in source}
        self.save()

    def save(self) -> None:
        clean = {key: self.values[key] for key in CLOUD_SESSION_KEYS if key in self.values}
        self.values.clear()
        self.values.update(clean)
        atomic_write_json(self.path, {"format": 1, "session": self.values})


def sanitize_simulation(values: dict[str, Any]) -> dict[str, Any]:
    """Accept only a local operation and positive product quantities."""

    operation = values.get("operation") if values.get("operation") in ("entrada", "saida") else "saida"
    items: list[dict[str, int | float]] = []
    positions: dict[int, int] = {}
    source = values.get("items") if isinstance(values.get("items"), list) else []
    for raw_item in source[:1000]:
        if not isinstance(raw_item, dict):
            continue
        try:
            product_id = int(raw_item.get("product_id", 0))
            quantity = float(raw_item.get("quantity", 0))
        except (TypeError, ValueError, OverflowError):
            continue
        if product_id <= 0 or quantity <= 0 or not math.isfinite(quantity):
            continue
        item = {"product_id": product_id, "quantity": quantity}
        if product_id in positions:
            items[positions[product_id]] = item
        else:
            positions[product_id] = len(items)
            items.append(item)
    return {"operation": operation, "items": items}


class LocalSimulationDraft:
    """Persist a device-local simulation without mixing it into cloud data."""

    def __init__(self, path: Path):
        self.path = path
        stored = read_json_object(path)
        if isinstance(stored.get("simulation"), dict):
            stored = stored["simulation"]
        self.values = sanitize_simulation(stored)
        self.save()

    def save(self) -> None:
        clean = sanitize_simulation(self.values)
        self.values.clear()
        self.values.update(clean)
        atomic_write_json(self.path, {"format": 1, "simulation": self.values})
