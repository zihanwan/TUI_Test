import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from mcinst.cli import main, parser
from mcinst.errors import MetadataError
from mcinst.models import ManifestResult


class CliTests(unittest.TestCase):
    def test_missing_textual_reports_sync_command_without_importing_ui(self):
        out, err = io.StringIO(), io.StringIO()
        with patch("mcinst.cli.importlib.util.find_spec", return_value=None):
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(main(["tui"]), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("uv sync", err.getvalue())

    def test_default_download_limits_and_overrides(self):
        options = parser().parse_args(["install", "1.20.1", "--directory", "instances/test"])
        self.assertEqual((options.concurrency, options.per_source), (16, 8))
        options = parser().parse_args(
            [
                "install",
                "1.20.1",
                "--directory",
                "instances/test",
                "--concurrency",
                "24",
                "--per-source",
                "12",
            ]
        )
        self.assertEqual((options.concurrency, options.per_source), (24, 12))

    def test_no_command_shows_help_without_network(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([]), 0)
        self.assertIn("versions", output.getvalue())

    def test_invalid_limits_fail_before_network(self):
        for limit in ("0", "-1", "no"):
            with self.subTest(limit=limit), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    main(["versions", "--limit", limit])
                self.assertEqual(exc.exception.code, 2)

    def test_errors_use_stderr_and_nonzero_exit(self):
        output, error = io.StringIO(), io.StringIO()
        with patch("mcinst.catalog.CatalogService.manifest", side_effect=MetadataError("模拟失败")):
            with redirect_stdout(output), redirect_stderr(error):
                self.assertEqual(main(["versions"]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("模拟失败", error.getvalue())

    def test_json_filtering_preserves_manifest_order(self):
        result = ManifestResult.model_validate(
            {
                "source": "official",
                "url": "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json",
                "manifest": {
                    "latest": {"release": "26.1", "snapshot": "sample-snapshot"},
                    "versions": [
                        {
                            "id": version_id,
                            "type": kind,
                            "url": "https://piston-meta.mojang.com/test.json",
                            "sha1": "0" * 40,
                            "releaseTime": "2026-01-01T00:00:00Z",
                            "time": "2026-01-01T00:00:00Z",
                        }
                        for version_id, kind in (
                            ("sample-snapshot", "snapshot"),
                            ("26.1", "release"),
                            ("1.21.1", "release"),
                            ("1.9", "release"),
                        )
                    ],
                },
            }
        )
        with patch("mcinst.catalog.CatalogService.manifest", return_value=result):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["versions", "--json", "--limit", "2"]), 0)
            data = json.loads(output.getvalue())
            self.assertEqual([v["id"] for v in data["versions"]], ["26.1", "1.21.1"])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["versions", "--json", "--all", "--search", "snapshot"]), 0)
            data = json.loads(output.getvalue())
            self.assertEqual([v["id"] for v in data["versions"]], ["sample-snapshot"])
