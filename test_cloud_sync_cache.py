from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloud_sync import CloudSync


def snapshot(name: str = "MARINHO") -> dict:
    return {
        "payload": {"format": 1, "tables": {"products": [{"id": 1, "name": name}]}},
        "revision": 3,
        "updated_at": "2026-10-01T12:00:00+00:00",
    }


class CloudSyncRemoteCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = {
            "cloud_provider": "firebase",
            "cloud_user_id": "user-a",
            "cloud_access_token": "test-token",
        }
        self.cloud = CloudSync(Path(self.temp.name), self.settings)

    def test_same_etag_reuses_full_snapshot_without_downloading_it_again(self):
        calls = []
        remote = snapshot()

        def request(path, **kwargs):
            calls.append(path)
            headers = kwargs["response_headers"]
            headers["etag"] = '"same"'
            return None if "print=silent" in path else remote

        with patch.object(self.cloud, "_request", side_effect=request):
            first = self.cloud.remote_snapshot()
            second = self.cloud.remote_snapshot()

        self.assertEqual(first, remote)
        self.assertEqual(second, remote)
        self.assertEqual(calls, ["/workspaces/bolsas-baby.json", "/workspaces/bolsas-baby.json?print=silent"])

    def test_changed_etag_forces_one_full_download(self):
        calls = []
        current_etag = ['"one"']
        remote = snapshot()

        def request(path, **kwargs):
            calls.append(path)
            headers = kwargs["response_headers"]
            if "print=silent" in path:
                headers["etag"] = current_etag[0]
                return None
            headers["etag"] = current_etag[0]
            return remote

        with patch.object(self.cloud, "_request", side_effect=request):
            self.cloud.remote_snapshot()
            remote = snapshot("VERDE")
            current_etag[0] = '"two"'
            result = self.cloud.remote_snapshot()

        self.assertEqual(result["payload"]["tables"]["products"][0]["name"], "VERDE")
        self.assertEqual(calls.count("/workspaces/bolsas-baby.json"), 2)

    def test_missing_etag_disables_cache_safely(self):
        calls = []

        def request(path, **kwargs):
            calls.append(path)
            return snapshot()

        with patch.object(self.cloud, "_request", side_effect=request):
            self.cloud.remote_snapshot()
            self.cloud.remote_snapshot()

        self.assertEqual(calls, ["/workspaces/bolsas-baby.json", "/workspaces/bolsas-baby.json"])

    def test_user_scope_change_cannot_reuse_previous_account_cache(self):
        calls = []

        def request(path, **kwargs):
            calls.append(path)
            kwargs["response_headers"]["etag"] = '"same"'
            return None if "print=silent" in path else snapshot()

        with patch.object(self.cloud, "_request", side_effect=request):
            self.cloud.remote_snapshot()
            self.settings["cloud_user_id"] = "user-b"
            self.cloud.remote_snapshot()

        self.assertEqual(calls, [
            "/workspaces/bolsas-baby.json",
            "/workspaces/bolsas-baby.json",
        ])

    def test_upload_uses_silent_response_and_can_seed_cache(self):
        body = snapshot()
        body["payload"] = {"format": 1, "tables": {}}
        calls = []

        def request(path, **kwargs):
            calls.append((path, kwargs))
            if path.endswith("/revision.json"):
                return 8
            kwargs["response_headers"]["etag"] = '"new"'
            return None

        with patch.object(self.cloud, "_request", side_effect=request):
            self.cloud._upload_payload(body["payload"], 9)

        self.assertEqual(calls[0][0], "/workspaces/bolsas-baby.json?print=silent")
        self.assertEqual(calls[0][1]["method"], "PUT")
        self.assertEqual(calls[0][1]["headers"]["X-Firebase-ETag"], "true")


if __name__ == "__main__":
    unittest.main()
