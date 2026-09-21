"""Unit, boundary, and adversarial tests for AGYM Council Content-Addressed Storage.

Covers:
- SHA-256 calculation and strict format validation
- Two-level prefix sharding and atomic storage
- Content deduplication
- Streaming read/write
- File permissions (POSIX private)
- Multi-layer directory traversal defense (.., backslashes, null bytes, drive letters, symlinks)
- Concurrent writes
- Workspace staging helper
"""

from __future__ import annotations

import concurrent.futures
import os
import stat
import tempfile
import unittest
from pathlib import Path

from agym.council.artifacts import (
    ArtifactNotFoundError,
    ArtifactStore,
    InvalidArtifactHashError,
    PathTraversalError,
    stage_artifact_to_workspace,
    store_artifact,
    validate_safe_relative_path,
)


class TestArtifactStoreBasics(unittest.TestCase):
    """Test standard CAS operations: hash computation, store, get, streaming, dedup."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name) / "artifacts"
        self.store = ArtifactStore(self.root)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_sha256_computation(self) -> None:
        self.assertEqual(
            self.store.compute_sha256(b"hello"),
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        )
        self.assertEqual(
            self.store.compute_sha256(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )

    def test_store_and_retrieve_artifact(self) -> None:
        payload = b"AGYM Council artifact payload 12345"
        result = self.store.store(payload)

        # Check path layout: root / hash[:2] / hash
        expected_shard = result.content_hash[:2]
        expected_path = self.root / expected_shard / result.content_hash
        self.assertEqual(result.path, expected_path)
        self.assertTrue(result.path.is_file())
        self.assertEqual(result.byte_size, len(payload))

        # Retrieve
        retrieved = self.store.get(result.content_hash)
        self.assertEqual(retrieved, payload)
        self.assertTrue(self.store.exists(result.content_hash))

    def test_deduplication(self) -> None:
        payload = b"Deduplicated payload content"
        res1 = self.store.store(payload)
        mtime1 = res1.path.stat().st_mtime_ns

        # Store identical payload again
        res2 = self.store.store(payload)
        mtime2 = res2.path.stat().st_mtime_ns

        self.assertEqual(res1.content_hash, res2.content_hash)
        self.assertEqual(mtime1, mtime2)

    def test_streaming_retrieval(self) -> None:
        large_payload = b"ChunkDataABC123!" * 10000  # 160,000 bytes
        res = self.store.store(large_payload)

        streamed = b"".join(self.store.get_stream(res.content_hash, chunk_size=8192))
        self.assertEqual(streamed, large_payload)

    def test_file_permissions(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX file permissions not applicable on Windows")

        res = self.store.store(b"Secret private data")
        file_mode = stat.S_IMODE(res.path.stat().st_mode)
        self.assertEqual(file_mode, 0o600)

        dir_mode = stat.S_IMODE(self.root.stat().st_mode)
        self.assertEqual(dir_mode, 0o700)

    def test_delete_artifact(self) -> None:
        res = self.store.store(b"Temporary artifact")
        self.assertTrue(self.store.exists(res.content_hash))
        self.assertTrue(self.store.delete(res.content_hash))
        self.assertFalse(self.store.exists(res.content_hash))
        self.assertFalse(self.store.delete(res.content_hash))

    def test_get_nonexistent_raises_artifact_not_found(self) -> None:
        fake_hash = "f" * 64
        with self.assertRaises(ArtifactNotFoundError):
            self.store.get(fake_hash)
        with self.assertRaises(ArtifactNotFoundError):
            list(self.store.get_stream(fake_hash))


class TestPathTraversalAndSecurityDefenses(unittest.TestCase):
    """Test defense-in-depth protections against path traversal and hash manipulation."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name) / "workspace"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.store = ArtifactStore(Path(self.tmp_dir.name) / "artifacts")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_dot_dot_rejection(self) -> None:
        traversal_attempts = [
            "../../etc/passwd",
            "../secret.txt",
            "sub/../../outside.txt",
            "foo/bar/../../../etc/hosts",
            "normal/path/..",
            "..",
        ]
        for subpath in traversal_attempts:
            with self.subTest(subpath=subpath):
                with self.assertRaises(PathTraversalError):
                    validate_safe_relative_path(self.base_dir, subpath)

    def test_backslash_traversal_rejection(self) -> None:
        backslash_attempts = [
            r"..\..\secret.txt",
            r"foo\..\..\bar",
            r"sub\..\outside",
        ]
        for subpath in backslash_attempts:
            with self.subTest(subpath=subpath):
                with self.assertRaises(PathTraversalError):
                    validate_safe_relative_path(self.base_dir, subpath)

    def test_null_byte_rejection(self) -> None:
        with self.assertRaises(PathTraversalError):
            validate_safe_relative_path(self.base_dir, "safe.txt\0/etc/passwd")

    def test_absolute_path_and_drive_rejection(self) -> None:
        with self.assertRaises(PathTraversalError):
            validate_safe_relative_path(self.base_dir, "/etc/shadow")
        with self.assertRaises(PathTraversalError):
            validate_safe_relative_path(self.base_dir, "C:\\Windows\\System32")
        with self.assertRaises(PathTraversalError):
            validate_safe_relative_path(self.base_dir, "D:data.txt")

    def test_symlink_breakout_prevention(self) -> None:
        if os.name == "nt":
            self.skipTest("Symlink permissions restricted on Windows non-admin")

        external_dir = Path(self.tmp_dir.name) / "external"
        external_dir.mkdir()
        external_file = external_dir / "secret.txt"
        external_file.write_text("classified", encoding="utf-8")

        symlink_path = self.base_dir / "external_link"
        try:
            symlink_path.symlink_to(external_dir)
        except OSError:
            self.skipTest("Creating symlinks not supported in environment")

        with self.assertRaises(PathTraversalError):
            validate_safe_relative_path(self.base_dir, "external_link/secret.txt")

    def test_malformed_hash_rejection(self) -> None:
        malformed_hashes = [
            "../etc/passwd",
            "abc",
            "A" * 64,  # Uppercase not permitted
            "g" * 64,  # Non-hex character
            "12345",
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855extra",  # 69 chars
            "",
        ]
        for bad_hash in malformed_hashes:
            with self.subTest(bad_hash=bad_hash):
                with self.assertRaises(InvalidArtifactHashError):
                    self.store.get_artifact_path(bad_hash)


class TestConcurrencyAndWorkspaceStaging(unittest.TestCase):
    """Test concurrent CAS access and safe workspace projection."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name) / "artifacts"
        self.store = ArtifactStore(self.root)
        self.workspace = Path(self.tmp_dir.name) / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_concurrent_atomic_writes(self) -> None:
        payload = b"ConcurrentWritePayload" * 1000

        def write_task() -> str:
            res = self.store.store(payload)
            return res.content_hash

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(write_task) for _ in range(20)]
            hashes = [f.result() for f in futures]

        # All threads must produce the identical hash without file corruption
        self.assertTrue(all(h == hashes[0] for h in hashes))
        self.assertEqual(self.store.get(hashes[0]), payload)

    def test_stage_artifact_to_workspace_safe(self) -> None:
        payload = b"{\"stage\": \"findings\", \"value\": 42}"
        res = self.store.store(payload)

        dest = stage_artifact_to_workspace(
            self.store, res.content_hash, self.workspace, "inputs/input.json"
        )
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_bytes(), payload)
        self.assertEqual(dest, self.workspace / "inputs" / "input.json")

    def test_stage_artifact_to_workspace_traversal_blocked(self) -> None:
        payload = b"Escaping payload"
        res = self.store.store(payload)

        with self.assertRaises(PathTraversalError):
            stage_artifact_to_workspace(
                self.store, res.content_hash, self.workspace, "../../escaped.txt"
            )
        escaped_file = Path(self.tmp_dir.name) / "escaped.txt"
        self.assertFalse(escaped_file.exists())

    def test_stage_nonexistent_artifact_raises(self) -> None:
        with self.assertRaises(ArtifactNotFoundError):
            stage_artifact_to_workspace(
                self.store, "0" * 64, self.workspace, "file.txt"
            )


class TestStoreArtifactDatabaseIntegration(unittest.TestCase):
    """Test store_artifact behavior with SQLite persistence and CAS deduplication."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.store = ArtifactStore(self.root / "cas")
        self.db_path = self.root / "test.db"
        from agym.council.storage import create_run, init_db
        self.conn = init_db(self.db_path)
        run_data = {
            "name": "Artifact Run",
            "goal": "Test artifacts",
            "inputs": [{"id": "i1", "description": "d", "required": True, "value": "v"}],
            "workers": [
                {"id": "w1", "name": "W1", "account_ref": "a1", "model": "m1", "role": "r", "instructions": "i", "task": "t"},
                {"id": "w2", "name": "W2", "account_ref": "a2", "model": "m2", "role": "r", "instructions": "i", "task": "t"},
            ],
            "stages": [{"id": "s1", "kind": "independent", "workers": ["w1", "w2"], "instruction": "i"}],
            "final_stage": "s1",
        }
        self.run_id = create_run(self.conn, run_data)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_duplicate_content_across_workers_generates_distinct_artifact_records(self) -> None:
        """Two workers outputting identical bytes must produce distinct artifact records."""
        identical_payload = b"Exact duplicate report content from both workers"
        art1 = store_artifact(
            run_id=self.run_id,
            name="output_s1_w1.json",
            data=identical_payload,
            stage_id="s1",
            worker_id="w1",
            artifact_store=self.store,
            conn=self.conn,
        )
        art2 = store_artifact(
            run_id=self.run_id,
            name="output_s1_w2.json",
            data=identical_payload,
            stage_id="s1",
            worker_id="w2",
            artifact_store=self.store,
            conn=self.conn,
        )

        self.assertNotEqual(art1.id, art2.id, "Artifact records must have unique IDs")
        self.assertEqual(art1.sha256, art2.sha256, "CAS SHA-256 hashes must match for identical data")

        rows = self.conn.execute(
            "SELECT artifact_id, name, content_hash FROM artifacts WHERE run_id = ?",
            (self.run_id,),
        ).fetchall()
        self.assertEqual(len(rows), 2, "Both artifact records must be preserved in SQLite")
        row_ids = {r["artifact_id"] for r in rows}
        self.assertIn(art1.id, row_ids)
        self.assertIn(art2.id, row_ids)


if __name__ == "__main__":
    unittest.main()
