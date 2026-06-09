from __future__ import annotations

import contextlib
import io
import unittest

from archive_scan_qc import __version__
from archive_scan_qc.cli import main


class CliSmokeTests(unittest.TestCase):
    def test_version_output_matches_package_version(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue().strip(), f"archive-scan-qc {__version__}")


if __name__ == "__main__":
    unittest.main()
