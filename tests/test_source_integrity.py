"""Self-tests for scripts/check_source_integrity.py.

Every test builds a small temporary repository and asserts on the audit
findings, so the CI guard is exercised with both positive and negative
cases without touching the real repository.
"""

from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "check_source_integrity",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check_source_integrity.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _write(root: pathlib.Path, relative: str, content: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


class SourceIntegrityAuditTests(unittest.TestCase):
    def audit(self, files: dict[str, str], requirements: str | None = None):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            for relative, content in files.items():
                _write(root, relative, content)
            if requirements is not None:
                _write(root, "requirements.txt", requirements)
            return MODULE.audit(root)

    def test_clean_stdlib_only_repo_passes(self):
        findings = self.audit(
            {
                "main.py": "import os\nimport json\n\ndef main():\n    return os.name\n",
                "tests/test_main.py": "import unittest\n",
            }
        )
        self.assertEqual([], findings)

    def test_missing_requirements_file_is_tolerated(self):
        findings = self.audit(
            {"main.py": "import sys\nprint(sys.version)\n"},
            requirements=None,
        )
        self.assertEqual([], findings)

    def test_every_alias_in_multi_alias_import_is_validated(self):
        findings = self.audit(
            {
                "main.py": (
                    "import os, nonexistent_module, sys\n"
                    "import json, also_missing_pkg\n"
                )
            }
        )
        self.assertEqual(2, len(findings))
        self.assertIn("'nonexistent_module'", findings[0])
        self.assertIn("'also_missing_pkg'", findings[1])

    def test_all_stdlib_aliases_in_multi_alias_import_pass(self):
        findings = self.audit({"main.py": "import os, sys, json, pathlib\n"})
        self.assertEqual([], findings)

    def test_local_package_with_init_py_is_resolved(self):
        findings = self.audit(
            {
                "main.py": "import pkg\nfrom pkg import helper\nfrom pkg.sub import thing\n",
                "pkg/__init__.py": "",
                "pkg/helper.py": "VALUE = 1\n",
                "pkg/sub/__init__.py": "",
                "pkg/sub/thing.py": "VALUE = 2\n",
            }
        )
        self.assertEqual([], findings)

    def test_package_without_init_py_is_reported(self):
        findings = self.audit(
            {
                "main.py": "import broken_pkg\n",
                "broken_pkg/mod.py": "VALUE = 1\n",
            }
        )
        self.assertEqual(1, len(findings))
        self.assertIn("'broken_pkg'", findings[0])

    def test_import_from_local_package_roots_are_validated(self):
        findings = self.audit(
            {"main.py": "from pkg import thing\n"},
        )
        self.assertEqual(1, len(findings))
        self.assertIn("'pkg'", findings[0])

    def test_syntax_error_is_reported(self):
        findings = self.audit({"main.py": "def broken(:\n"})
        self.assertEqual(1, len(findings))
        self.assertIn("syntax error", findings[0])

    def test_duplicate_top_level_definition_is_reported(self):
        findings = self.audit(
            {"main.py": "def helper():\n    return 1\n\ndef helper():\n    return 2\n"}
        )
        self.assertEqual(1, len(findings))
        self.assertIn("defined 2 times", findings[0])


class WebdriverConstructionDetectionTests(unittest.TestCase):
    def findings(self, test_file_content: str):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            _write(root, "requirements.txt", "selenium==4.28.1\n")
            _write(root, "tests/test_driver.py", test_file_content)
            _write(
                root,
                "tests/__init__.py",
                "",
            )
            return MODULE.audit(root)

    def test_plain_webdriver_chrome_call_is_detected(self):
        findings = self.findings(
            "import unittest\n"
            "from selenium import webdriver\n"
            "class T(unittest.TestCase):\n"
            "    def test_x(self):\n"
            "        webdriver.Chrome()\n"
        )
        self.assertEqual(1, len(findings))
        self.assertIn("Chrome", findings[0])

    def test_aliased_webdriver_receiver_is_detected(self):
        findings = self.findings(
            "import unittest\n"
            "from selenium import webdriver as wd\n"
            "class T(unittest.TestCase):\n"
            "    def test_x(self):\n"
            "        wd.Chrome()\n"
        )
        self.assertEqual(1, len(findings))
        self.assertIn("Chrome", findings[0])

    def test_module_style_import_with_alias_is_detected(self):
        findings = self.findings(
            "import unittest\n"
            "import selenium.webdriver as wd2\n"
            "class T(unittest.TestCase):\n"
            "    def test_x(self):\n"
            "        wd2.Firefox()\n"
        )
        self.assertEqual(1, len(findings))
        self.assertIn("Firefox", findings[0])

    def test_plain_module_import_chain_is_detected(self):
        findings = self.findings(
            "import unittest\n"
            "import selenium.webdriver\n"
            "class T(unittest.TestCase):\n"
            "    def test_x(self):\n"
            "        selenium.webdriver.Chrome()\n"
        )
        self.assertEqual(1, len(findings))

    def test_direct_class_import_from_selenium_webdriver_is_detected(self):
        findings = self.findings(
            "import unittest\n"
            "from selenium.webdriver import Chrome\n"
            "class T(unittest.TestCase):\n"
            "    def test_x(self):\n"
            "        Chrome()\n"
        )
        self.assertEqual(1, len(findings))
        self.assertIn("Chrome", findings[0])

    def test_direct_webdriver_class_from_browser_module_is_detected(self):
        findings = self.findings(
            "import unittest\n"
            "from selenium.webdriver.chrome.webdriver import WebDriver\n"
            "class T(unittest.TestCase):\n"
            "    def test_x(self):\n"
            "        WebDriver()\n"
        )
        self.assertEqual(1, len(findings))
        self.assertIn("WebDriver", findings[0])

    def test_mock_patch_string_literals_are_never_flagged(self):
        findings = self.findings(
            "import unittest\n"
            "from unittest.mock import patch\n"
            "from selenium import webdriver\n"
            "class T(unittest.TestCase):\n"
            "    @patch('selenium.webdriver.Chrome')\n"
            "    def test_x(self, _fake):\n"
            "        with patch.object(webdriver, 'Chrome'):\n"
            "            pass\n"
        )
        self.assertEqual([], findings)

    def test_reference_without_call_is_not_construction(self):
        findings = self.findings(
            "import unittest\n"
            "from selenium import webdriver\n"
            "class T(unittest.TestCase):\n"
            "    driver_class = webdriver.Chrome\n"
            "    def test_x(self):\n"
            "        self.assertIsNotNone(self.driver_class)\n"
        )
        self.assertEqual([], findings)

    def test_non_webdriver_attribute_call_is_not_flagged(self):
        findings = self.findings(
            "import unittest\n"
            "from selenium import webdriver\n"
            "class T(unittest.TestCase):\n"
            "    def test_x(self):\n"
            "        self.driver.quit()\n"
        )
        self.assertEqual([], findings)

    def test_by_and_support_imports_are_not_driver_construction(self):
        findings = self.findings(
            "import unittest\n"
            "from selenium.webdriver.common.by import By\n"
            "from selenium.webdriver.support.ui import WebDriverWait\n"
            "class T(unittest.TestCase):\n"
            "    def test_x(self):\n"
            "        self.assertIsNotNone(By.CSS_SELECTOR)\n"
        )
        self.assertEqual([], findings)


class AuditEntryPointTests(unittest.TestCase):
    def test_main_returns_zero_on_clean_repo_and_one_on_findings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            _write(root, "main.py", "import os\nprint(os.name)\n")
            self.assertEqual(0, MODULE.main([str(root)]))
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            _write(root, "main.py", "import totally_missing_pkg\n")
            self.assertEqual(1, MODULE.main([str(root)]))

    def test_main_without_arguments_audits_the_real_repository(self):
        self.assertEqual(0, MODULE.main([]))


if __name__ == "__main__":
    unittest.main()
