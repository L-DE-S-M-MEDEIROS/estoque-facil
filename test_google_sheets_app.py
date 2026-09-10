from __future__ import annotations

import unittest

import app
from cloud_sync import CloudSync


class GoogleSheetsAppIntegrationTests(unittest.TestCase):
    def test_app_exposes_only_google_sheets_online_link(self):
        self.assertEqual(
            app.GOOGLE_SHEETS_URL,
            "https://docs.google.com/spreadsheets/d/1eXMlyvFpO_-MkD8oaux1NrlupqR-ECNyEZS1XSgJIiY/edit?usp=sharing",
        )
        self.assertFalse(hasattr(CloudSync, "excel_online_status"))
        self.assertFalse(hasattr(CloudSync, "excel_online_download"))
        self.assertFalse(hasattr(CloudSync, "excel_online_upload"))


if __name__ == "__main__":
    unittest.main()
