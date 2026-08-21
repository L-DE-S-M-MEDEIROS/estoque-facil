from __future__ import annotations

import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import updater


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, url: str = "https://release-assets.githubusercontent.com/update.exe"):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class UpdaterTests(unittest.TestCase):
    def release(self, version="1.2.0", data=b"MZnova-versao"):
        digest = hashlib.sha256(data).hexdigest()
        return {
            "tag_name": f"v{version}",
            "name": f"Versão {version}",
            "body": "Melhorias.",
            "draft": False,
            "prerelease": False,
            "html_url": "https://github.com/L-DE-S-M-MEDEIROS/estoque-facil/releases/tag/v1.2.0",
            "assets": [
                {
                    "name": "Estoque.Bolsas.Baby.exe",
                    "size": len(data),
                    "digest": f"sha256:{digest}",
                    "browser_download_url": "https://github.com/L-DE-S-M-MEDEIROS/estoque-facil/releases/download/v1.2.0/Estoque.Bolsas.Baby.exe",
                }
            ],
        }

    def test_version_comparison_is_numeric(self):
        self.assertGreater(updater.version_key("1.10.0"), updater.version_key("1.9.9"))

    def test_current_version_returns_no_update(self):
        with patch("updater._request_json", return_value=self.release("1.1.5")):
            self.assertIsNone(updater.check_for_update("1.1.5", "L-DE-S-M-MEDEIROS/estoque-facil"))

    def test_new_release_returns_verified_asset(self):
        with patch("updater._request_json", return_value=self.release("1.2.0")):
            info = updater.check_for_update("1.1.5", "L-DE-S-M-MEDEIROS/estoque-facil")
        self.assertEqual(info.version, "1.2.0")
        self.assertEqual(len(info.sha256), 64)

    def test_release_rejects_untrusted_download(self):
        release = self.release()
        release["assets"][0]["browser_download_url"] = "http://example.com/update.exe"
        with patch("updater._request_json", return_value=release):
            with self.assertRaisesRegex(updater.UpdateError, "não confiável"):
                updater.check_for_update("1.1.5", "L-DE-S-M-MEDEIROS/estoque-facil")

    def test_download_validates_size_header_and_sha256(self):
        data = b"MZnova-versao"
        with patch("updater._request_json", return_value=self.release(data=data)):
            info = updater.check_for_update("1.1.5", "L-DE-S-M-MEDEIROS/estoque-facil")
        with patch("updater.urllib.request.urlopen", return_value=FakeResponse(data)):
            path = updater.download_update(info)
        try:
            self.assertEqual(path.read_bytes(), data)
        finally:
            path.unlink(missing_ok=True)
            path.parent.rmdir()

    def test_tampered_download_is_deleted(self):
        expected = b"MZoriginal"
        tampered = b"MZalterado"
        release = self.release(data=expected)
        release["assets"][0]["size"] = len(tampered)
        with patch("updater._request_json", return_value=release):
            info = updater.check_for_update("1.1.5", "L-DE-S-M-MEDEIROS/estoque-facil")
        with patch("updater.urllib.request.urlopen", return_value=FakeResponse(tampered)):
            with self.assertRaisesRegex(updater.UpdateError, "SHA-256"):
                updater.download_update(info)

    def test_replacement_removes_previous_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "app.exe"
            downloaded = root / "download.exe"
            target.write_bytes(b"MZversao-anterior")
            downloaded.write_bytes(b"MZversao-nova")
            digest = hashlib.sha256(downloaded.read_bytes()).hexdigest()
            updater.replace_installed_executable(target, downloaded, digest)
            self.assertEqual(target.read_bytes(), b"MZversao-nova")
            self.assertFalse(target.with_name("app.exe.nova").exists())


if __name__ == "__main__":
    unittest.main()
