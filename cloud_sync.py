from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from database_utils import (
    INVENTORY_TABLES, acknowledge_sync, database_integrity_errors,
    get_sync_state, has_pending_sync, register_database_functions,
)


FIREBASE_API_KEY = "AIzaSyBzAJgXOVYuduVdd3DNdlURAtNK-ZPAP_E"
FIREBASE_DATABASE_URL = "https://estoque-bolsas-baby-default-rtdb.firebaseio.com"
FIREBASE_IDENTITY_URL = "https://identitytoolkit.googleapis.com/v1"
FIREBASE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token"
TABLES = INVENTORY_TABLES
SHARED_WORKSPACE_KEY = "bolsas-baby"
TOKEN_REFRESH_SKEW_SECONDS = 90


class CloudSyncError(RuntimeError):
    pass


class LocalChangesPending(CloudSyncError):
    """A local commit made the remote download decision obsolete."""


def _response_error_message(detail: object, *, explicit_only: bool = False) -> str | None:
    """Extract a useful API error without assuming a single response shape."""
    if not isinstance(detail, dict):
        return None
    keys = ("error",) if explicit_only else ("error", "msg", "message", "error_description")
    for key in keys:
        value = detail.get(key)
        if isinstance(value, dict):
            value = (
                value.get("message")
                or value.get("description")
                or value.get("code")
            )
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


class CloudSync:
    def __init__(self, folder: Path, settings: dict):
        self.folder = folder
        self.settings = settings
        self.device_id = settings.setdefault("cloud_device_id", str(uuid.uuid4()))
        self._remote_cache_scope: tuple[str, ...] | None = None
        self._remote_cache_etag: str | None = None
        self._remote_cache_snapshot: dict | None = None

    def _remote_scope(self) -> tuple[str, ...]:
        return (
            FIREBASE_DATABASE_URL, SHARED_WORKSPACE_KEY,
            str(self.settings.get("cloud_provider") or ""),
            str(self.settings.get("cloud_user_id") or ""),
        )

    def _invalidate_remote_cache(self) -> None:
        self._remote_cache_scope = None
        self._remote_cache_etag = None
        self._remote_cache_snapshot = None

    def _cache_remote_snapshot(self, snapshot: dict, response_headers: dict) -> None:
        self._invalidate_remote_cache()
        etag = response_headers.get("etag")
        if isinstance(etag, str) and etag.strip() and snapshot.get("payload"):
            self._remote_cache_scope = self._remote_scope()
            self._remote_cache_etag = etag
            self._remote_cache_snapshot = copy.deepcopy(snapshot)

    @property
    def signed_in(self) -> bool:
        return bool(
            self.settings.get("cloud_provider") == "firebase"
            and self.settings.get("cloud_access_token")
            and self.settings.get("cloud_user_id")
        )

    @property
    def email(self) -> str:
        return str(self.settings.get("cloud_email", ""))

    @staticmethod
    def _read_response(response):
        raw = response.read()
        try:
            return json.loads(raw) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CloudSyncError("O Firebase retornou uma resposta inválida.") from error

    @staticmethod
    def _token_expiry(token: str) -> int | None:
        """Read only the JWT expiry claim; signature validation stays in Firebase."""
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None
            encoded = parts[1].replace("-", "+").replace("_", "/")
            encoded += "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
            expiry = int(payload.get("exp"))
            return expiry if expiry > 0 else None
        except (ValueError, TypeError, KeyError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError):
            return None

    def _token_needs_refresh(self) -> bool:
        token = str(self.settings.get("cloud_access_token") or "")
        expiry = self._token_expiry(token)
        return expiry is not None and expiry <= int(time.time()) + TOKEN_REFRESH_SKEW_SECONDS

    def _refresh_or_reauthenticate(self) -> None:
        try:
            self.refresh_session()
        except CloudSyncError as error:
            # Do not keep a dead session that causes an error on every timer tick.
            self.sign_out()
            raise CloudSyncError(
                "A sessão do Firebase expirou. Entre novamente em Configurações → Conta."
            ) from error

    def _request(self, path: str, *, method="GET", body=None, authenticated=False, headers=None, retry=True, response_headers=None):
        if response_headers is not None:
            response_headers.clear()
        request_headers = {"Content-Type": "application/json"}
        request_url = FIREBASE_DATABASE_URL + path
        if authenticated:
            if not self.signed_in:
                raise CloudSyncError("Entre na sua conta para sincronizar.")
            # Firebase ID tokens must be sent in the Realtime Database REST
            # `auth` query parameter.  The Authorization: Bearer header is
            # reserved for Google OAuth2 access tokens and makes Firebase
            # answer "Unauthorized request." when used with an ID token.
            separator = "&" if "?" in request_url else "?"
            request_url = (
                f"{request_url}{separator}auth="
                f"{urllib.parse.quote(self.settings['cloud_access_token'], safe='')}"
            )
            if self._token_needs_refresh() and self.settings.get("cloud_refresh_token"):
                self._refresh_or_reauthenticate()
                request_url = FIREBASE_DATABASE_URL + path
                separator = "&" if "?" in request_url else "?"
                request_url = (
                    f"{request_url}{separator}auth="
                    f"{urllib.parse.quote(self.settings['cloud_access_token'], safe='')}"
                )
        if headers:
            request_headers.update(headers)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(request_url, data=data, method=method, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response_headers is not None:
                    response_headers.update({
                        str(key).lower(): value
                        for key, value in getattr(response, "headers", {}).items()
                    })
                return self._read_response(response)
        except urllib.error.HTTPError as error:
            self._invalidate_remote_cache()
            if error.code in (408, 425, 429, 500, 502, 503, 504) and retry:
                time.sleep(0.75)
                return self._request(path, method=method, body=body, authenticated=authenticated, headers=headers, retry=False, response_headers=response_headers)
            try:
                detail = json.loads(error.read().decode("utf-8"))
                message = _response_error_message(detail)
            except (ValueError, UnicodeDecodeError):
                message = None
            normalized_message = (message or "").casefold().strip().rstrip(".")
            token_rejected = error.code == 401 and normalized_message in {
                "unauthorized request",
                "invalid token",
                "invalid id token",
                "auth token is expired",
                "auth token is invalid",
            }
            if authenticated and error.code == 401 and retry and self.settings.get("cloud_refresh_token"):
                # Refresh only an expired/invalid Firebase ID token. A
                # "Permission denied" response means the account reached the
                # database but is not allowed by its rules; retrying with a
                # fresh token would only hide that configuration problem.
                if token_rejected or not message:
                    self._refresh_or_reauthenticate()
                    return self._request(path, method=method, body=body, authenticated=True, headers=headers, retry=False, response_headers=response_headers)
            if authenticated and token_rejected:
                self.sign_out()
                raise CloudSyncError(
                    "A sessão do Firebase expirou. Entre novamente em Configurações → Conta."
                ) from error
            if error.code == 401:
                message = message or (
                    "O Firebase recusou esta conta. Verifique se ela está autorizada nas regras "
                    "do Realtime Database."
                )
            raise CloudSyncError(message or f"O Firebase respondeu com erro {error.code}.") from error
        except (urllib.error.URLError, TimeoutError) as error:
            self._invalidate_remote_cache()
            raise CloudSyncError("Não foi possível conectar ao Firebase. Verifique a internet.") from error
        except CloudSyncError:
            self._invalidate_remote_cache()
            raise

    def _auth_request(self, endpoint: str, body: dict, *, form=False) -> dict:
        headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
        data = (
            urllib.parse.urlencode(body).encode("utf-8")
            if form
            else json.dumps(body, ensure_ascii=False).encode("utf-8")
        )
        request = urllib.request.Request(endpoint, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = self._read_response(response)
                return result if isinstance(result, dict) else {}
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8"))
                nested = detail.get("error") if isinstance(detail, dict) else None
                code = nested.get("message") if isinstance(nested, dict) else None
            except (ValueError, UnicodeDecodeError):
                code = None
            messages = {
                "EMAIL_EXISTS": "Este e-mail já possui uma conta. Use o botão Entrar.",
                "EMAIL_NOT_FOUND": "Conta não encontrada.",
                "INVALID_LOGIN_CREDENTIALS": "E-mail ou senha incorretos.",
                "INVALID_PASSWORD": "E-mail ou senha incorretos.",
                "USER_DISABLED": "Esta conta foi desativada.",
                "TOO_MANY_ATTEMPTS_TRY_LATER": "Muitas tentativas. Aguarde um pouco e tente novamente.",
                "WEAK_PASSWORD : Password should be at least 6 characters": "Use uma senha com pelo menos 6 caracteres.",
            }
            raise CloudSyncError(messages.get(str(code), "Não foi possível autenticar no Firebase.")) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise CloudSyncError("Não foi possível conectar ao Firebase. Verifique a internet.") from error

    def sign_in(self, email: str, password: str) -> None:
        self._invalidate_remote_cache()
        result = self._auth_request(
            f"{FIREBASE_IDENTITY_URL}/accounts:signInWithPassword?key={FIREBASE_API_KEY}",
            {"email": email, "password": password, "returnSecureToken": True},
        )
        self._store_session(result, email)

    def sign_up(self, email: str, password: str) -> bool:
        self._invalidate_remote_cache()
        result = self._auth_request(
            f"{FIREBASE_IDENTITY_URL}/accounts:signUp?key={FIREBASE_API_KEY}",
            {"email": email, "password": password, "returnSecureToken": True},
        )
        self._store_session(result, email)
        return True

    def refresh_session(self) -> None:
        result = self._auth_request(
            f"{FIREBASE_TOKEN_URL}?key={FIREBASE_API_KEY}",
            {"grant_type": "refresh_token", "refresh_token": self.settings.get("cloud_refresh_token", "")},
            form=True,
        )
        self._store_session(result, self.email)

    def _store_session(self, result: dict, email: str) -> None:
        previous_scope = self._remote_scope()
        access_token = result.get("idToken") or result.get("id_token")
        refresh_token = result.get("refreshToken") or result.get("refresh_token")
        user_id = result.get("localId") or result.get("user_id")
        if not access_token or not user_id:
            raise CloudSyncError("O Firebase não retornou uma sessão válida.")
        self.settings.update({
            "cloud_provider": "firebase",
            "cloud_access_token": access_token,
            "cloud_refresh_token": refresh_token or "",
            "cloud_user_id": user_id,
            "cloud_email": email.strip().lower(),
        })
        if self._remote_scope() != previous_scope:
            self._invalidate_remote_cache()

    def sign_out(self) -> None:
        self._invalidate_remote_cache()
        for key in ("cloud_provider", "cloud_access_token", "cloud_refresh_token", "cloud_user_id", "cloud_email"):
            self.settings.pop(key, None)

    def export_payload(self, connection: sqlite3.Connection) -> dict:
        # Read all tables from one SQLite snapshot. A concurrent movement must
        # never produce half of a batch in the outgoing payload.
        own_transaction = not connection.in_transaction
        if own_transaction:
            connection.execute("BEGIN")
        try:
            return self._export_payload(connection)
        finally:
            if own_transaction:
                connection.rollback()  # End this read-only snapshot.

    def _export_payload(self, connection: sqlite3.Connection) -> dict:
        tables = {}
        for table in TABLES:
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            tables[table] = [dict(zip(columns, row, strict=True)) for row in connection.execute(f"SELECT {','.join(columns)} FROM {table}")]
        photos = []
        for row in tables["products"]:
            source = Path(str(row.get("photo") or ""))
            if source.is_file():
                name = source.name
                photos.append({
                    "name": name,
                    "data": base64.b64encode(source.read_bytes()).decode("ascii"),
                })
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

    def _local_snapshot(self, connection: sqlite3.Connection) -> tuple[dict, int, bool]:
        if connection.in_transaction:
            raise CloudSyncError("Aguarde a gravação local antes de sincronizar.")
        connection.execute("BEGIN")
        try:
            revision, synced_revision = get_sync_state(connection)
            return self.export_payload(connection), revision, revision != synced_revision
        finally:
            connection.rollback()

    def _finish_sync(self, connection: sqlite3.Connection, payload: dict, snapshot: dict, revision: int) -> bool:
        with connection:
            acknowledge_sync(connection, revision)
        self._remember_sync(payload, snapshot)
        pending = has_pending_sync(connection)
        if not pending:
            self.settings.pop("cloud_local_modified_at", None)
        return pending

    def _upload_payload(self, payload: dict, revision: int) -> dict:
        snapshot = {
            "payload": payload,
            "revision": max(1, revision),
            "device_id": self.device_id,
            "updated_by": self.settings["cloud_user_id"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        response_headers: dict[str, str] = {}
        try:
            result = self._request(
                f"/workspaces/{SHARED_WORKSPACE_KEY}.json?print=silent",
                method="PUT",
                body=snapshot,
                authenticated=True,
                headers={"X-Firebase-ETag": "true"},
                response_headers=response_headers,
            )
        except Exception:
            self._invalidate_remote_cache()
            raise
        snapshot = result if isinstance(result, dict) else snapshot
        self._cache_remote_snapshot(snapshot, response_headers)
        self._remember_sync(payload, snapshot)
        return snapshot

    def upload(self, connection: sqlite3.Connection) -> dict:
        payload, revision, _pending = self._local_snapshot(connection)
        remote_revision = self._request(
            f"/workspaces/{SHARED_WORKSPACE_KEY}/revision.json", authenticated=True
        )
        snapshot = self._upload_payload(payload, int(remote_revision or 0) + 1)
        self._finish_sync(connection, payload, snapshot, revision)
        return snapshot

    def remote_snapshot(self) -> dict | None:
        if self._remote_cache_scope != self._remote_scope():
            self._invalidate_remote_cache()
        path = f"/workspaces/{SHARED_WORKSPACE_KEY}.json"
        response_headers: dict[str, str] = {}
        try:
            if self._remote_cache_snapshot is not None and self._remote_cache_etag:
                self._request(
                    path + "?print=silent", authenticated=True,
                    headers={"X-Firebase-ETag": "true"},
                    response_headers=response_headers,
                )
                if (self._remote_cache_scope == self._remote_scope()
                        and response_headers.get("etag") == self._remote_cache_etag
                        and self._remote_cache_snapshot is not None):
                    return copy.deepcopy(self._remote_cache_snapshot)
                self._invalidate_remote_cache()
            result = self._request(
                path, authenticated=True, headers={"X-Firebase-ETag": "true"},
                response_headers=response_headers,
            )
            if isinstance(result, dict) and result.get("payload"):
                self._cache_remote_snapshot(result, response_headers)
                return result
            self._invalidate_remote_cache()
            return None
        except Exception:
            self._invalidate_remote_cache()
            raise

    def _download_snapshot(self, connection: sqlite3.Connection, snapshot: dict, *, expected_revision: int | None = None) -> str:
        # Imported or rejected data must be read afresh before it can be reused.
        self._invalidate_remote_cache()
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
        photos = payload.get("photos", [])
        if not isinstance(photos, (dict, list)):
            raise CloudSyncError("A cópia na nuvem contém fotos inválidas.")
        decoded_photos: dict[str, bytes] = {}
        try:
            photo_items = (
                photos.items()
                if isinstance(photos, dict)
                else ((item.get("name"), item.get("data")) for item in photos if isinstance(item, dict))
            )
            for name, encoded in photo_items:
                if not isinstance(name, str) or not name.strip() or not isinstance(encoded, str):
                    raise ValueError
                safe_name = Path(name).name
                if safe_name in decoded_photos:
                    raise ValueError
                decoded_photos[safe_name] = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError, binascii.Error) as error:
            raise CloudSyncError("A cópia na nuvem contém fotos inválidas.") from error
        register_database_functions(connection)
        backup = self.folder / f"antes-da-sincronizacao-{datetime.now():%Y%m%d-%H%M%S-%f}.db"
        if connection.in_transaction:
            raise LocalChangesPending("Há uma gravação local em andamento.")
        backup_connection = sqlite3.connect(backup)
        try:
            connection.backup(backup_connection)
        finally:
            backup_connection.close()
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            with connection:
                # Hold SQLite's writer lock only while importing, never during
                # network I/O. Check again *inside* this transaction to close
                # the race between receiving Firebase's reply and replacing rows.
                connection.execute("BEGIN IMMEDIATE")
                if expected_revision is not None and get_sync_state(connection)[0] != expected_revision:
                    raise LocalChangesPending("Uma movimentação foi salva durante a sincronização.")
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
                integrity_errors = database_integrity_errors(connection)
                if integrity_errors:
                    raise CloudSyncError(
                        "A cópia na nuvem falhou na verificação de integridade: "
                        + "; ".join(integrity_errors[:3])
                    )
                photos_folder = self.folder / "fotos"
                photos_folder.mkdir(exist_ok=True)
                for name, contents in decoded_photos.items():
                    temporary_photo = photos_folder / f".{name}.{uuid.uuid4().hex}.tmp"
                    temporary_photo.write_bytes(contents)
                    temporary_photo.replace(photos_folder / name)
                acknowledge_sync(connection, get_sync_state(connection)[0])
        finally:
            connection.execute("PRAGMA foreign_keys=ON")
        self._remember_sync(payload, snapshot)
        return str(snapshot["updated_at"])

    def download(self, connection: sqlite3.Connection) -> str:
        revision = get_sync_state(connection)[0]
        snapshot = self.remote_snapshot()
        if not snapshot:
            raise CloudSyncError("Ainda não existe uma cópia compartilhada no Firebase.")
        try:
            return self._download_snapshot(connection, snapshot, expected_revision=revision)
        except Exception:
            self._invalidate_remote_cache()
            raise

    def synchronize(self, connection: sqlite3.Connection, prefer_local: bool = False) -> dict:
        """Keep this device aligned with the single inventory shared by authenticated users."""
        local_payload, local_revision, local_pending = self._local_snapshot(connection)
        local_fingerprint = self.payload_fingerprint(local_payload)
        remote = self.remote_snapshot()
        # A request can take seconds. The UI remains usable during that time,
        # so the snapshot used to make this decision may already be outdated.
        if get_sync_state(connection)[0] != local_revision:
            return {"action": "pending", "pending": True}

        def upload_payload(revision: int) -> dict:
            snapshot = self._upload_payload(local_payload, revision)
            pending = self._finish_sync(connection, local_payload, snapshot, local_revision)
            return {"action": "uploaded", "snapshot": snapshot, "pending": pending}

        def download_snapshot() -> dict:
            try:
                updated_at = self._download_snapshot(connection, remote, expected_revision=local_revision)
            except LocalChangesPending:
                return {"action": "pending", "pending": True}
            except Exception:
                self._invalidate_remote_cache()
                raise
            return {"action": "downloaded", "updated_at": updated_at, "snapshot": remote,
                    "pending": has_pending_sync(connection)}

        if remote is None:
            return upload_payload(1)

        remote_payload = remote.get("payload") or {}
        if remote_payload.get("format") != 1:
            self._invalidate_remote_cache()
            raise CloudSyncError("A cópia compartilhada usa um formato incompatível.")
        remote_fingerprint = self.payload_fingerprint(remote_payload)
        last_fingerprint = str(self.settings.get("cloud_last_fingerprint") or "")
        remote_revision = int(remote.get("revision") or 1)

        if local_fingerprint == remote_fingerprint:
            pending = self._finish_sync(connection, remote_payload, remote, local_revision)
            return {"action": "unchanged", "snapshot": remote, "pending": pending}
        if local_pending or (prefer_local and self.payload_has_user_data(local_payload)):
            return upload_payload(remote_revision + 1)
        if not self.payload_has_user_data(local_payload) and self.payload_has_user_data(remote_payload):
            return download_snapshot()
        if last_fingerprint:
            if local_fingerprint == last_fingerprint:
                return download_snapshot()
            if remote_fingerprint == last_fingerprint:
                return upload_payload(remote_revision + 1)

        local_modified = str(self.settings.get("cloud_local_modified_at") or "")
        remote_modified = str(remote.get("updated_at") or "")
        if remote_modified and (not local_modified or remote_modified >= local_modified):
            return download_snapshot()
        return upload_payload(remote_revision + 1)
