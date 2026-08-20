from __future__ import annotations

import base64
import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path


SUPABASE_URL = "https://raleparpityoscsykssk.supabase.co"
SUPABASE_KEY = "sb_publishable_YLaG2l4ORj5JuJmEsM26Vg_gC27RG_3"
TABLES = (
    "operation_types",
    "users",
    "product_groups",
    "products",
    "movement_batches",
    "movements",
)


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
            tables[table] = [dict(zip(columns, row)) for row in connection.execute(f"SELECT {','.join(columns)} FROM {table}")]
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

    def upload(self, connection: sqlite3.Connection) -> dict:
        payload = self.export_payload(connection)
        body = {
            "owner_id": self.settings["cloud_user_id"],
            "payload": payload,
            "device_id": self.device_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        result = self._request(
            "/rest/v1/inventory_snapshots?on_conflict=owner_id",
            method="POST",
            body=body,
            authenticated=True,
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
        )
        return result[0] if result else body

    def remote_snapshot(self) -> dict | None:
        query = urllib.parse.urlencode({"select": "payload,updated_at,device_id", "owner_id": f"eq.{self.settings['cloud_user_id']}"})
        rows = self._request(f"/rest/v1/inventory_snapshots?{query}", authenticated=True)
        return rows[0] if rows else None

    def download(self, connection: sqlite3.Connection) -> str:
        snapshot = self.remote_snapshot()
        if not snapshot:
            raise CloudSyncError("Ainda não existe uma cópia desses dados no Supabase.")
        payload = snapshot["payload"]
        if payload.get("format") != 1:
            raise CloudSyncError("A cópia na nuvem usa um formato incompatível.")
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
                    rows = payload["tables"].get(table, [])
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
        return str(snapshot["updated_at"])
