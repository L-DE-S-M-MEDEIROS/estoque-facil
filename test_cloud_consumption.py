from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

from cloud_sync import CloudSync, CloudSyncError, TABLES


class FirebaseResponse:
    def __init__(self, body: bytes, headers: dict, status: int = 200):
        self.body = body
        self.headers = headers
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class FirebaseTransport:
    """Model the REST response bodies, including silent GET and PUT replies."""

    def __init__(self, snapshot):
        self.snapshot = copy.deepcopy(snapshot)
        self.calls = []
        self.downloaded_bytes = 0
        self.etags_enabled = True
        self.fail_next = None

    def open(self, request, **_kwargs):
        parsed = urllib.parse.urlsplit(request.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        silent = query.get("print") == ["silent"]
        self.calls.append((request.method, parsed.path, silent))
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        if request.method == "PUT":
            self.snapshot = json.loads(request.data)
        value = self.snapshot
        if parsed.path.endswith("/revision.json"):
            value = self.snapshot.get("revision") if self.snapshot else None
        serialized = json.dumps(value, sort_keys=True).encode("utf-8")
        headers = {}
        if self.etags_enabled and dict(
            (key.lower(), val) for key, val in request.header_items()
        ).get("x-firebase-etag") == "true":
            headers["ETag"] = '"' + hashlib.sha256(serialized).hexdigest() + '"'
        body = b"" if silent else serialized
        self.downloaded_bytes += len(body)
        return FirebaseResponse(body, headers, 204 if silent else 200)


class CloudConsumptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = {
            "cloud_provider": "firebase", "cloud_access_token": "test-token",
            "cloud_user_id": "test-user", "cloud_refresh_token": "test-refresh",
        }
        self.cloud = CloudSync(Path(self.temporary.name), self.settings)
        tables = {table: [] for table in TABLES}
        tables["products"] = [{"id": 1, "name": "MARINHO"}]
        self.payload = {
            "format": 1, "app": "Estoque Bolsas Baby", "exported_at": "initial",
            "tables": tables,
            "photos": [{"name": "photo.png", "data": "A" * 128_000}],
        }
        self.snapshot = {
            "payload": self.payload, "revision": 4,
            "device_id": "remote-device", "updated_by": "test-user",
            "updated_at": "2026-10-01T12:00:00+00:00",
        }
        self.transport = FirebaseTransport(self.snapshot)
        urlopen = patch("cloud_sync.urllib.request.urlopen", side_effect=self.transport.open)
        urlopen.start()
        self.addCleanup(urlopen.stop)

    def prime_cache(self):
        self.assertEqual(self.cloud.remote_snapshot(), self.snapshot)
        self.assertIsNotNone(self.cloud._remote_cache_etag)

    def session(self, user="test-user"):
        return {"idToken": "new-token", "refreshToken": "new-refresh", "localId": user}

    def test_thirty_unchanged_polls_download_only_the_initial_snapshot_body(self):
        self.prime_cache()
        initial_bytes = self.transport.downloaded_bytes
        for _ in range(30):
            self.assertEqual(self.cloud.remote_snapshot(), self.snapshot)
        self.assertGreater(initial_bytes, 128_000)
        self.assertEqual(self.transport.downloaded_bytes, initial_bytes)
        self.assertEqual(len(self.transport.calls), 31)
        self.assertEqual(sum(not call[2] for call in self.transport.calls), 1)

    def test_changed_payload_with_same_revision_and_timestamp_is_fetched(self):
        self.prime_cache()
        self.transport.snapshot["payload"]["tables"]["products"][0]["name"] = "VERDE"
        result = self.cloud.remote_snapshot()
        self.assertEqual(result["payload"]["tables"]["products"][0]["name"], "VERDE")
        self.assertEqual(result["revision"], self.snapshot["revision"])
        self.assertEqual([call[2] for call in self.transport.calls], [False, True, False])

    def test_missing_etag_uses_full_reads_instead_of_trusting_cached_data(self):
        self.transport.etags_enabled = False
        self.cloud.remote_snapshot()
        self.cloud.remote_snapshot()
        self.assertEqual([call[2] for call in self.transport.calls], [False, False])
        self.assertIsNone(self.cloud._remote_cache_snapshot)

    def test_missing_etag_on_probe_falls_back_to_full_read(self):
        self.prime_cache()
        self.transport.etags_enabled = False
        self.assertEqual(self.cloud.remote_snapshot(), self.snapshot)
        self.assertEqual([call[2] for call in self.transport.calls], [False, True, False])
        self.assertIsNone(self.cloud._remote_cache_snapshot)

    def test_deleted_remote_workspace_is_not_returned_from_cache(self):
        self.prime_cache()
        self.transport.snapshot = None
        self.assertIsNone(self.cloud.remote_snapshot())
        self.assertIsNone(self.cloud._remote_cache_snapshot)

    def test_returned_snapshot_cannot_mutate_the_cache(self):
        returned = self.cloud.remote_snapshot()
        returned["payload"]["tables"]["products"][0]["name"] = "caller mutation"
        cached = self.cloud.remote_snapshot()
        self.assertEqual(cached["payload"]["tables"]["products"][0]["name"], "MARINHO")
        cached["payload"]["tables"]["products"][0]["name"] = "another mutation"
        self.assertEqual(self.cloud.remote_snapshot(), self.snapshot)

    def test_network_failure_invalidates_cache_and_next_attempt_reads_full_data(self):
        self.prime_cache()
        self.transport.fail_next = urllib.error.URLError("offline")
        with self.assertRaises(CloudSyncError):
            self.cloud.remote_snapshot()
        self.assertIsNone(self.cloud._remote_cache_snapshot)
        self.assertEqual(self.cloud.remote_snapshot(), self.snapshot)
        self.assertFalse(self.transport.calls[-1][2])

    def test_invalid_json_invalidates_cache(self):
        self.prime_cache()
        with patch("cloud_sync.urllib.request.urlopen", return_value=FirebaseResponse(b"not-json", {})):
            with self.assertRaises(CloudSyncError):
                self.cloud.remote_snapshot()
        self.assertIsNone(self.cloud._remote_cache_snapshot)

    def test_account_change_without_signout_requires_new_full_snapshot(self):
        self.prime_cache()
        self.settings["cloud_user_id"] = "other-user"
        self.cloud.remote_snapshot()
        self.assertEqual([call[2] for call in self.transport.calls], [False, False])

    def test_workspace_change_requires_new_full_snapshot(self):
        self.prime_cache()
        with patch("cloud_sync.SHARED_WORKSPACE_KEY", "another-workspace"):
            self.cloud.remote_snapshot()
        self.assertEqual([call[2] for call in self.transport.calls], [False, False])
        self.assertIn("another-workspace", self.transport.calls[-1][1])

    def test_signout_invalidates_cache(self):
        self.prime_cache()
        self.cloud.sign_out()
        self.assertIsNone(self.cloud._remote_cache_snapshot)
        with self.assertRaises(CloudSyncError):
            self.cloud.remote_snapshot()
        self.assertEqual(len(self.transport.calls), 1)

    def test_signin_and_signup_invalidate_cache_even_for_same_account(self):
        for method in (self.cloud.sign_in, self.cloud.sign_up):
            with self.subTest(method=method.__name__):
                self.cloud.remote_snapshot()
                with patch.object(self.cloud, "_auth_request", return_value=self.session()):
                    method("test@example.invalid", "test-password")
                self.assertIsNone(self.cloud._remote_cache_snapshot)
                self.cloud.remote_snapshot()
                self.assertFalse(self.transport.calls[-1][2])

    def test_legitimate_token_refresh_keeps_same_account_cache(self):
        self.prime_cache()
        self.settings["cloud_access_token"] = "eyJhbGciOiJub25lIn0.eyJleHAiOjF9.signature"
        with patch.object(self.cloud, "_auth_request", return_value=self.session()) as refresh:
            self.assertEqual(self.cloud.remote_snapshot(), self.snapshot)
        refresh.assert_called_once()
        self.assertEqual([call[2] for call in self.transport.calls], [False, True])

    def test_expired_session_failure_clears_cache_and_does_not_return_old_data(self):
        self.prime_cache()
        self.settings["cloud_access_token"] = "eyJhbGciOiJub25lIn0.eyJleHAiOjF9.signature"
        with patch.object(self.cloud, "_auth_request", side_effect=CloudSyncError("expired")):
            with self.assertRaises(CloudSyncError):
                self.cloud.remote_snapshot()
        self.assertFalse(self.cloud.signed_in)
        self.assertIsNone(self.cloud._remote_cache_snapshot)

    def test_transient_retry_propagates_response_headers(self):
        self.transport.fail_next = urllib.error.HTTPError(
            "https://example.invalid", 503, "unavailable", {}, io.BytesIO(b"{}")
        )
        with patch("cloud_sync.time.sleep"):
            self.prime_cache()
        self.assertEqual(len(self.transport.calls), 2)
        self.cloud.remote_snapshot()
        self.assertTrue(self.transport.calls[-1][2])

    def test_silent_upload_has_no_echo_and_populates_cache_when_etag_is_returned(self):
        result = self.cloud._upload_payload(self.payload, 5)
        self.assertEqual(result["payload"], self.payload)
        self.assertEqual(self.transport.downloaded_bytes, 0)
        self.assertEqual(self.transport.calls[0], ("PUT", "/workspaces/bolsas-baby.json", True))
        self.assertEqual(self.cloud.remote_snapshot(), result)
        self.assertEqual(self.transport.downloaded_bytes, 0)
        self.assertTrue(self.transport.calls[-1][2])

    def test_upload_without_etag_requires_full_read_next_time(self):
        self.transport.etags_enabled = False
        result = self.cloud._upload_payload(self.payload, 5)
        self.assertIsNone(self.cloud._remote_cache_snapshot)
        self.assertEqual(self.cloud.remote_snapshot(), result)
        self.assertFalse(self.transport.calls[-1][2])

    def test_manual_upload_reads_only_remote_revision_before_silent_write(self):
        with patch.object(self.cloud, "_local_snapshot", return_value=(self.payload, 8, True)), \
                patch.object(self.cloud, "_finish_sync", return_value=False):
            result = self.cloud.upload(object())
        self.assertEqual(result["revision"], 5)
        self.assertEqual(self.transport.calls, [
            ("GET", "/workspaces/bolsas-baby/revision.json", False),
            ("PUT", "/workspaces/bolsas-baby.json", True),
        ])
        self.assertEqual(self.transport.downloaded_bytes, len(b"4"))

    def test_upload_error_invalidates_existing_cache(self):
        self.prime_cache()
        self.transport.fail_next = urllib.error.URLError("offline")
        with self.assertRaises(CloudSyncError):
            self.cloud._upload_payload(self.payload, 5)
        self.assertIsNone(self.cloud._remote_cache_snapshot)

    def test_rejected_import_invalidates_cache(self):
        self.prime_cache()
        with self.assertRaises(CloudSyncError):
            self.cloud._download_snapshot(object(), {"payload": {"format": 999}})
        self.assertIsNone(self.cloud._remote_cache_snapshot)


if __name__ == "__main__":
    unittest.main()
