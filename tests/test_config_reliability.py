from __future__ import annotations

import importlib
import os
import unittest
from unittest.mock import patch

import config


class ConfigSortSettingsTests(unittest.TestCase):
    """The primary-feed sort parameter/value are configurable and validated."""

    def test_custom_sort_param_and_value_are_validated_together(self) -> None:
        with patch.object(config.Config, "FEED_QUERY_SORT_PARAM", "order"), \
             patch.object(config.Config, "PRIMARY_FEED_NEWEST_SORT_VALUE", "newest"), \
             patch.object(
                 config.Config,
                 "PRIMARY_SEARCH_URL",
                 "https://www.freelancermap.com/projects?order=newest",
             ), \
             patch.object(config.Config, "PERSONALIZED_SEARCH_URL", ""):
            errors = config.Config.validate_runtime()
        self.assertFalse(any("must be sorted" in e for e in errors))
        self.assertEqual([], [e for e in errors if "FREELANCERMAP_PRIMARY_FEED" in e])

    def test_custom_sort_param_mismatch_is_rejected(self) -> None:
        with patch.object(config.Config, "FEED_QUERY_SORT_PARAM", "order"), \
             patch.object(config.Config, "PRIMARY_FEED_NEWEST_SORT_VALUE", "newest"), \
             patch.object(
                 config.Config,
                 "PRIMARY_SEARCH_URL",
                 "https://www.freelancermap.com/projects?sort=1",
             ), \
             patch.object(config.Config, "PERSONALIZED_SEARCH_URL", ""):
            errors = config.Config.validate_runtime()
        self.assertTrue(any("must be sorted" in e for e in errors))

    def test_invalid_sort_param_name_is_rejected(self) -> None:
        for bad_name in ("", "sort key", "so&rt"):
            with self.subTest(name=bad_name), \
                 patch.object(config.Config, "FEED_QUERY_SORT_PARAM", bad_name), \
                 patch.object(config.Config, "PERSONALIZED_SEARCH_URL", ""):
                errors = config.Config.validate_runtime()
            self.assertTrue(
                any("valid query-parameter name" in e for e in errors),
                msg=f"expected rejection for {bad_name!r}",
            )

    def test_invalid_sort_value_is_rejected(self) -> None:
        for bad_value in ("", "1=2", "new&est", "new est"):
            with self.subTest(value=bad_value), \
                 patch.object(config.Config, "PRIMARY_FEED_NEWEST_SORT_VALUE", bad_value), \
                 patch.object(config.Config, "PERSONALIZED_SEARCH_URL", ""):
                errors = config.Config.validate_runtime()
            self.assertTrue(
                any("non-empty value" in e for e in errors),
                msg=f"expected rejection for {bad_value!r}",
            )

    def test_secondary_feed_sort_values_stay_checked(self) -> None:
        with patch.object(config.Config, "FEED_QUERY_SORT_PARAM", "order"), \
             patch.object(
                 config.Config,
                 "PERSONALIZED_SEARCH_URL",
                 "https://www.freelancermap.com/projects?order=2",
             ):
            errors = config.Config.validate_runtime()
        self.assertFalse(any("supported sort" in e for e in errors))
        with patch.object(config.Config, "FEED_QUERY_SORT_PARAM", "order"), \
             patch.object(
                 config.Config,
                 "PERSONALIZED_SEARCH_URL",
                 "https://www.freelancermap.com/projects?order=9",
             ):
            errors = config.Config.validate_runtime()
        self.assertTrue(any("supported sort" in e for e in errors))


class ConfigPersonalizedFeedRequirementTests(unittest.TestCase):
    def test_personalized_required_without_url_is_rejected(self) -> None:
        with patch.object(config.Config, "PERSONALIZED_FEED_REQUIRED", True), \
             patch.object(config.Config, "PERSONALIZED_SEARCH_URL", ""):
            errors = config.Config.validate_runtime()
        self.assertTrue(any("PERSONALIZED_FEED_REQUIRED" in e for e in errors))

    def test_personalized_required_with_url_is_accepted(self) -> None:
        with patch.object(config.Config, "PERSONALIZED_FEED_REQUIRED", True), \
             patch.object(
                 config.Config,
                 "PERSONALIZED_SEARCH_URL",
                 "https://www.freelancermap.com/projects?sort=2",
             ):
            errors = config.Config.validate_runtime()
        self.assertFalse(any("PERSONALIZED_FEED_REQUIRED" in e for e in errors))

    def test_personalized_optional_without_url_is_accepted(self) -> None:
        with patch.object(config.Config, "PERSONALIZED_FEED_REQUIRED", False), \
             patch.object(config.Config, "PERSONALIZED_SEARCH_URL", ""):
            errors = config.Config.validate_runtime()
        self.assertFalse(any("PERSONALIZED_FEED_REQUIRED" in e for e in errors))


class ConfigPrivacyDefaultsTests(unittest.TestCase):
    """Raw HTML and diagnostic captures are privacy-safe off by default."""

    def _reload(self) -> config:
        return importlib.reload(config)

    def test_privacy_sensitive_capture_defaults_are_off(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DIAGNOSTIC_CAPTURE_HTML": "",
                "DIAGNOSTIC_CAPTURE_SCREENSHOT": "",
                "STORE_RAW_HTML": "",
                "CHROME_DISABLE_DEV_SHM_USAGE": "",
            },
            clear=False,
        ):
            cfg = self._reload()
        self.assertFalse(cfg.Config.DIAGNOSTIC_CAPTURE_HTML)
        self.assertFalse(cfg.Config.DIAGNOSTIC_CAPTURE_SCREENSHOT)
        self.assertFalse(cfg.Config.STORE_RAW_HTML)
        self.assertFalse(cfg.Config.CHROME_DISABLE_DEV_SHM_USAGE)

    def test_unsafe_chrome_flags_are_opt_in(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CHROME_NO_SANDBOX": "",
                "CHROME_DISABLE_DEV_SHM_USAGE": "",
            },
            clear=False,
        ):
            cfg = self._reload()
        self.assertFalse(cfg.Config.CHROME_NO_SANDBOX)
        self.assertFalse(cfg.Config.CHROME_DISABLE_DEV_SHM_USAGE)
        with patch.dict(os.environ, {"CHROME_DISABLE_DEV_SHM_USAGE": "true"}, clear=False):
            cfg = self._reload()
        self.assertTrue(cfg.Config.CHROME_DISABLE_DEV_SHM_USAGE)


if __name__ == "__main__":
    unittest.main()
