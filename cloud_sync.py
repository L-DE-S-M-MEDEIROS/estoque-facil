from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from database_utils import register_database_functions


SUPABASE_URL = "https://raleparpityoscsykssk.supabase.co"
SUPABASE_KEY = "sb_publishable_YLaG2l4ORj5JuJmEsM26Vg_gC27RG_3"
TABLES = (
    "operation_types",
    "users",
    "product_groups",
    "products",
    "sku_mappings",
    "sku_mapping_products",
    "movement_batches",
    "movements",
)
SHARED_WORKSPACE_KEY = "bolsas-baby"


class CloudSyncError(RuntimeError):
    pass


class CloudSync:
    def __init__(self, folder: Path, settings: dict):
        self.folder = folder
        self.settings = settings
        self.device_id = settings.setdefault("cloud_device_id", str(uuid.uuid4()))

    @property
    def signed_in(self) -> bool:
        return bool(self.settings.get("cloud_access_token") and self.settings.get("cloud_user_id"))

    @property
    def email(self) -> str:
        return str(self.settings.get("cloud_email", ""))

    def _request(self, path: str, *, method="GET", body=None, authenticated=False, headers=None, retry=True):
        request_headers = {"apikey": SUPABASE_KEY, "Content-Type": "application/json"}
        if authenticated:
            if not self.signed_in:
                raise CloudSyncError("Entre na sua conta para sincronizar.")
            request_headers["Authorization"] = f"Bearer {self.settings['cloud_access_token']}"
        if headers:
            request_headers.update(headers)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(SUPABASE_URL + path, data=data, method=method, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if authenticated and error.code == 401 and retry and self.settings.get("cloud_refresh_token"):
                self.refresh_session()
                return self._request(path, method=method, body=body, authenticated=True, headers=headers, retry=False)
            try:
                detail = json.loads(error.read().decode("utf-8"))
                message = detail.get("msg") or detail.get("message") or detail.get("error_description")
            except (ValueError, UnicodeDecodeError):
                message = None
            raise CloudSyncError(message or f"O Supabase respondeu com erro {error.code}.") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise CloudSyncError("Não foi possível conectar ao Supabase. Verifique a internet.") from error

    def sign_in(self, email: str, password: str) -> None:
        result = self._request("/auth/v1/token?grant_type=password", method="POST", body={"email": email, "password": password})
        self._store_session(result, email)

    def sign_up(self, email: str, password: str) -> bool:
        result = self._request("/auth/v1/signup", method="POST", body={"email": email, "password": password})
        if result.get("access_token"):
            self._store_session(result, email)
            return True
        return False

    def refresh_session(self) -> None:
        result = self._request(
            "/auth/v1/token?grant_type=refresh_token",
            method="POST",
            body={"refresh_token": self.settings.get("cloud_refresh_token", "")},
            retry=False,
        )
        self._store_session(result, self.email)

    def _store_session(self, result: dict, email: str) -> None:
        user = result.get("user") or {}
        if not result.get("access_token") or not user.get("id"):
            raise CloudSyncError("O Supabase não retornou uma sessão válida.")
        self.settings.update({
            "cloud_access_token": result["access_token"],
            "cloud_refresh_token": result.get("refresh_token", ""),
            "cloud_user_id": user["id"],
            "cloud_email": email.strip().lower(),
        })

    def sign_out(self) -> None:
        for key in ("cloud_access_token", "cloud_refresh_token", "cloud_user_id", "cloud_email"):
            self.settings.pop(key, None)

    def export_payload(self, connection: sqlite3.Connection) -> dict:
        connection.commit()
        tables = {}
        for table in TABLES:
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            tables[table] = [dict(zip(columns, row, strict=True)) for row in connection.execute(f"SELECT {','.join(columns)} FROM {table}")]
        photos = {}
        for row in tables["products"]:
            source = Path(str(row.get("photo") or ""))
            if source.is_file():
                name = source.name
                photos[name] = base64.b64encode(source.read_bytes()).decode("ascii")
                row["photo"] = name
        return {
            "format": 1,
            "app": "Estoque Bolsas Baby",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "tables": tables,
            "photos": photos,
        }

    @staticmethod
    def payload_fingerprint(payload: dict) -> str:
        stable = dict(payload)
        stable.pop("exported_at", None)
        serialized = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def payload_has_user_data(payload: dict) -> bool:
        tables = payload.get("tables") or {}
        return any(tables.get(name) for name in ("products", "movements", "movement_batches", "users", "product_groups", "sku_mappings"))

    def _remember_sync(self, payload: dict, snapshot: dict) -> None:
        self.settings["cloud_last_fingerprint"] = self.payload_fingerprint(payload)
        self.settings["cloud_last_revision"] = int(snapshot.get("revision") or 1)
        self.settings["cloud_last_remote_updated_at"] = str(snapshot.get("updated_at") or "")
        self.settings.pop("cloud_local_modified_at", None)

    def _upload_payload(self, payload: dict, revision: int) -> dict:
        body = {
            "workspace_key": SHARED_WORKSPACE_KEY,
            "payload": payload,
            "revision": max(1, revision),
            "device_id": self.device_id,
            "updated_by": self.settings["cloud_user_id"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        result = self._request(
            "/rest/v1/shared_inventory_snapshot?on_conflict=workspace_key",
            method="POST",
            body=body,
            authenticated=True,
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
        )
        snapshot = result[0] if result else body
        self._remember_sync(payload, snapshot)
        return snapshot

    def upload(self, connection: sqlite3.Connection) -> dict:
        payload = self.export_payload(connection)
        remote = self.remote_snapshot()
        return self._upload_payload(payload, int((remote or {}).get("revision") or 0) + 1)

    def remote_snapshot(self) -> dict | None:
        query = urllib.parse.urlencode({
            "select": "payload,revision,updated_at,device_id,updated_by",
            "workspace_key": f"eq.{SHARED_WORKSPACE_KEY}",
        })
        rows = self._request(f"/rest/v1/shared_inventory_snapshot?{query}", authenticated=True)
        return rows[0] if rows else None

    def _download_snapshot(self, connection: sqlite3.Connection, snapshot: dict) -> str:
        payload = snapshot["payload"]
        if payload.get("format") != 1:
            raise CloudSyncError("A cópia na nuvem usa um formato incompatível.")
        tables = payload.get("tables")
        if not isinstance(tables, dict):
            raise CloudSyncError("A cópia na nuvem não contém tabelas válidas.")
        allowed_columns = {
            table: {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
            for table in TABLES
        }
        for table in TABLES:
            rows = tables.get(table, [])
            if not isinstance(rows, list):
                raise CloudSyncError("A cópia na nuvem contém uma tabela inválida.")
            for row in rows:
                if not isinstance(row, dict) or not row or not set(row).issubset(allowed_columns[table]):
                    raise CloudSyncError("A cópia na nuvem contém colunas inválidas.")
        if not isinstance(payload.get("photos", {}), dict):
            raise CloudSyncError("A cópia na nuvem contém fotos inválidas.")
        register_database_functions(connection)
        backup = self.folder / f"antes-da-sincronizacao-{datetime.now():%Y%m%d-%H%M%S}.db"
        connection.commit()
        backup_connection = sqlite3.connect(backup)
        connection.backup(backup_connection)
        backup_connection.close()
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            with connection:
                for table in reversed(TABLES):
                    connection.execute(f"DELETE FROM {table}")
                for table in TABLES:
                    rows = tables.get(table, [])
                    for row in rows:
                        values = dict(row)
                        if table == "products" and values.get("photo"):
                            values["photo"] = str(self.folder / "fotos" / Path(values["photo"]).name)
                        columns = list(values)
                        placeholders = ",".join("?" for _ in columns)
                        connection.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})", tuple(values[c] for c in columns))
            connection.execute("PRAGMA foreign_keys=ON")
            photos_folder = self.folder / "fotos"
            photos_folder.mkdir(exist_ok=True)
            for name, encoded in payload.get("photos", {}).items():
                (photos_folder / Path(name).name).write_bytes(base64.b64decode(encoded))
        except Exception:
            connection.execute("PRAGMA foreign_keys=OFF")
            backup_connection = sqlite3.connect(backup)
            backup_connection.backup(connection)
            backup_connection.close()
            connection.execute("PRAGMA foreign_keys=ON")
            raise
        self._remember_sync(payload, snapshot)
        return str(snapshot["updated_at"])

    def download(self, connection: sqlite3.Connection) -> str:
        snapshot = self.remote_snapshot()
        if not snapshot:
            raise CloudSyncError("Ainda não existe uma cópia compartilhada no Supabase.")
        return self._download_snapshot(connection, snapshot)

    def synchronize(self, connection: sqlite3.Connection, prefer_local: bool = False) -> dict:
        """Keep this device aligned with the single inventory shared by authenticated users."""
        local_payload = self.export_payload(connection)
        local_fingerprint = self.payload_fingerprint(local_payload)
        remote = self.remote_snapshot()
        if remote is None:
            snapshot = self._upload_payload(local_payload, 1)
            return {"action": "uploaded", "snapshot": snapshot}

        remote_payload = remote.get("payload") or {}
        if remote_payload.get("format") != 1:
            raise CloudSyncError("A cópia compartilhada usa um formato incompatível.")
        remote_fingerprint = self.payload_fingerprint(remote_payload)
        last_fingerprint = str(self.settings.get("cloud_last_fingerprint") or "")
        remote_revision = int(remote.get("revision") or 1)

        if local_fingerprint == remote_fingerprint:
            self._remember_sync(remote_payload, remote)
            return {"action": "unchanged", "snapshot": remote}
        if prefer_local and self.payload_has_user_data(local_payload):
            snapshot = self._upload_payload(local_payload, remote_revision + 1)
            return {"action": "uploaded", "snapshot": snapshot}
        if not self.payload_has_user_data(local_payload) and self.payload_has_user_data(remote_payload):
            updated_at = self._download_snapshot(connection, remote)
            return {"action": "downloaded", "updated_at": updated_at, "snapshot": remote}
        if last_fingerprint:
            if local_fingerprint == last_fingerprint:
                updated_at = self._download_snapshot(connection, remote)
                return {"action": "downloaded", "updated_at": updated_at, "snapshot": remote}
            if remote_fingerprint == last_fingerprint:
                snapshot = self._upload_payload(local_payload, remote_revision + 1)
                return {"action": "uploaded", "snapshot": snapshot}

        local_modified = str(self.settings.get("cloud_local_modified_at") or "")
        remote_modified = str(remote.get("updated_at") or "")
        if remote_modified and (not local_modified or remote_modified >= local_modified):
            updated_at = self._download_snapshot(connection, remote)
            return {"action": "downloaded", "updated_at": updated_at, "snapshot": remote}
        snapshot = self._upload_payload(local_payload, remote_revision + 1)
        return {"action": "uploaded", "snapshot": snapshot}
