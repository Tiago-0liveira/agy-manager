"""Release version and source consistency checks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.release_version import compute_next_version, get_local_version, update_source_version


class ReleaseVersionTests(unittest.TestCase):
    def test_skips_existing_tags_with_or_without_prefix(self) -> None:
        self.assertEqual(compute_next_version("0.1.0", {"v0.1.0", "0.1.1"}), "0.1.2")

    def test_versioned_source_files_agree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agym").mkdir()
            (root / "agym" / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
            (root / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")
            update_source_version(root, "0.1.7")
            self.assertEqual(get_local_version(root), "0.1.7")
            self.assertIn('version = "0.1.7"', (root / "pyproject.toml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
