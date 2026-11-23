"""Kiểm tra module updater: phân tích semver, chọn asset, so khớp phiên bản."""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from updater import _match_asset, get_current_version, parse_semver


class TestUpdater(unittest.TestCase):
    def test_parse_semver(self):
        self.assertEqual(parse_semver("v1.7.0"), (1, 7, 0))
        self.assertEqual(parse_semver("1.6.9"), (1, 6, 9))
        self.assertEqual(parse_semver("v2.0.0-beta"), (2, 0, 0))
        self.assertEqual(parse_semver("v10.2"), (10, 2))
        self.assertTrue(parse_semver("v1.7.0") > parse_semver("v1.6.9"))
        self.assertTrue(parse_semver("v2.0.0") > parse_semver("v1.9.9"))
        self.assertFalse(parse_semver("v1.6.9") > parse_semver("v1.7.0"))

    def test_current_version(self):
        ver = get_current_version(str(ROOT))
        self.assertEqual(ver, "1.7.0")

    def test_match_asset(self):
        assets = [
            {"name": "MuseDesktop-Windows-x86_64-v1.7.0.zip", "browser_download_url": "http://example.com/win.zip"},
            {"name": "MuseDesktop-macOS-arm64-v1.7.0.zip", "browser_download_url": "http://example.com/mac.zip"},
            {"name": "MuseDesktop-Linux-x86_64-v1.7.0.zip", "browser_download_url": "http://example.com/linux.zip"},
        ]
        asset = _match_asset(assets)
        self.assertIsNotNone(asset)
        # Should return valid asset dict
        self.assertIn("name", asset)
        self.assertIn("browser_download_url", asset)


if __name__ == "__main__":
    unittest.main()
