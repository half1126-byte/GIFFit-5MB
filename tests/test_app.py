from __future__ import annotations

import argparse
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import app  # noqa: E402


class CliValidationTests(unittest.TestCase):
    def test_rejects_limit_above_five_megabytes_before_tool_discovery(self) -> None:
        args = argparse.Namespace(
            inputs=["missing.mp4"],
            output_dir=Path("unused-output"),
            limit_bytes=5_000_001,
            quality="quality",
        )
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = app.run_cli(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertFalse(payload["ok"])
        self.assertIn("5,000,000", payload["errors"][0]["error"])


if __name__ == "__main__":
    unittest.main()
