"""Tests for agym-council CLI commands and loopback security."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agym.council import cli
from agym.council.models import StageConfig, StageKind, WorkerConfig, WorkflowConfig, WorkflowInput
from agym.council.storage import create_run, get_connection, init_db


class TestCouncilCli(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name)
        self.db_path = self.root_path / "council.db"
        init_db(self.db_path)
        self.conn = get_connection(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def test_cli_version(self) -> None:
        """agym-council version outputs council version and dependency status."""
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = cli.main(["version"])
        self.assertEqual(code, 0)
        output = out.getvalue()
        self.assertIn("agym-council", output)
        self.assertIn("fastapi", output)

    def test_cli_validate_preset(self) -> None:
        """agym-council validate quick_council validates bundled preset successfully."""
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = cli.main(["validate", "quick_council"])
        self.assertEqual(code, 0)
        self.assertIn("PASS", out.getvalue())

    def test_cli_validate_nonexistent(self) -> None:
        """agym-council validate nonexistent prints error and returns non-zero."""
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = cli.main(["validate", "nonexistent_preset_xyz"])
        self.assertNotEqual(code, 0)
        self.assertIn("not found", err.getvalue().lower())

    def test_cli_web_security_rejects_non_loopback(self) -> None:
        """agym-council web --host 0.0.0.0 must be rejected immediately."""
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = cli.main(["web", "--host", "0.0.0.0"])
        self.assertEqual(code, 1)
        self.assertIn("security violation", err.getvalue().lower())

        err2 = io.StringIO()
        with mock.patch("sys.stderr", err2):
            code2 = cli.main(["web", "--host", "192.168.1.100"])
        self.assertEqual(code2, 1)
        self.assertIn("security violation", err2.getvalue().lower())

    def test_cli_accounts_list(self) -> None:
        """agym-council accounts list executes without error."""
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = cli.main(["accounts", "list"])
        self.assertEqual(code, 0)

    def test_cli_reconcile(self) -> None:
        """agym-council reconcile runs recovery against SQLite database."""
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = cli.main(["reconcile", "--db", str(self.db_path)])
        self.assertEqual(code, 0)
        self.assertIn("Reconciled", out.getvalue())

    def test_cli_export(self) -> None:
        """agym-council export exports run dossier ZIP archive."""
        wf = WorkflowConfig(
            name="Export Run",
            goal="Export test",
            inputs=[WorkflowInput(id="brief", description="Brief", required=True)],
            workers=[
                WorkerConfig(
                    id="w1",
                    name="Worker",
                    account_ref="p1",
                    model="m1",
                    role="analyst",
                    instructions="Do work",
                    task="Task",
                )
            ],
            stages=[
                StageConfig(
                    id="s1",
                    kind=StageKind.INDEPENDENT,
                    workers=["w1"],
                    instruction="Analyze",
                )
            ],
            final_stage="s1",
        )
        run_id = create_run(self.conn, wf, stages=wf.stages, workers=wf.workers)

        out_zip = self.root_path / f"export_{run_id}.zip"
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf):
            code = cli.main([
                "export",
                run_id,
                "-o",
                str(out_zip),
                "--db",
                str(self.db_path),
            ])
        self.assertEqual(code, 0)
        self.assertTrue(out_zip.is_file())
        self.assertIn(f"Exported run '{run_id}' dossier", out_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
