import os
import unittest
from datetime import datetime
from unittest import mock

from app.build_info import browse_repo_url, format_built_at, panel_build_info, repo_name_from_url


class BuildInfoTests(unittest.TestCase):
    def tearDown(self):
        panel_build_info.cache_clear()

    def test_format_built_at_uses_local_time(self):
        raw = "2026-10-16T07:41:00Z"
        expected = datetime.fromisoformat("2026-10-16T07:41:00+00:00").astimezone().strftime("%d.%m.%Y %H:%M")
        self.assertEqual(format_built_at(raw), expected)

    def test_browse_repo_url(self):
        self.assertEqual(
            browse_repo_url("https://github.com/mavotronik/NTA-test-stand.git"),
            "https://github.com/mavotronik/NTA-test-stand",
        )
        self.assertEqual(
            browse_repo_url("git@github.com:mavotronik/NTA-test-stand.git"),
            "https://github.com/mavotronik/NTA-test-stand",
        )
        self.assertEqual(browse_repo_url("/home/mavotronik/work/GrapesNTA"), "")
        self.assertEqual(browse_repo_url(""), "")
        self.assertEqual(repo_name_from_url("https://github.com/mavotronik/NTA-test-stand"), "NTA-test-stand")

    def test_repo_url_from_env(self):
        panel_build_info.cache_clear()
        with mock.patch.dict(os.environ, {"PANEL_REPO_URL": "git@github.com:org/project.git"}):
            info = panel_build_info()
        self.assertEqual(info.repo_url, "https://github.com/org/project")
        self.assertEqual(info.repo_name, "project")

    def test_format_built_at_blank(self):
        self.assertEqual(format_built_at(""), "—")
        self.assertEqual(format_built_at("unknown"), "—")

    def test_env_overrides_checkout(self):
        panel_build_info.cache_clear()
        with mock.patch.dict(
            os.environ,
            {
                "PANEL_GIT_COMMIT": "abc123def4567890aaaa",
                "PANEL_BUILD_TIME": "2026-01-02T03:04:05Z",
            },
        ):
            info = panel_build_info()
        self.assertEqual(info.commit, "abc123def4567890aaaa")
        self.assertEqual(info.commit_short, "abc123def456")
        self.assertEqual(
            info.built_at,
            datetime.fromisoformat("2026-01-02T03:04:05+00:00").astimezone().strftime("%d.%m.%Y %H:%M"),
        )
