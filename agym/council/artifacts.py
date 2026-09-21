"""Content-addressed artifact storage for AGYM Council.

Provides SHA-256 hashed file storage with two-level prefix sharding,
atomic writes, deduplication, and defense-in-depth path traversal prevention.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from agym.profiles import _default_data_root

HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ArtifactError(Exception):
    """Base exception for artifact operations."""


class ArtifactNotFoundError(ArtifactError):
    """Raised when an artifact hash does not exist in the store."""


class InvalidArtifactHashError(ArtifactError):
    """Raised when a content hash does not match 64 lowercase hex characters."""


class PathTraversalError(ArtifactError):
    """Raised when a path traversal or containment breach is detected."""


# ---------------------------------------------------------------------------
# Result Data Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactStoreResult:
    """Result of storing an artifact in the CAS."""

    content_hash: str
    byte_size: int
    path: Path


# ---------------------------------------------------------------------------
# Path Security Utilities
# ---------------------------------------------------------------------------


def validate_safe_relative_path(base_dir: Path, relative_path: str | Path) -> Path:
    """Validate that relative_path is safe and remains strictly inside base_dir.

    Enforces multi-layer defenses:
    1. Null byte detection
    2. Segment-level '..' and '.' rejection
    3. Absolute path and Windows drive-letter rejection
    4. Canonical containment verification via relative_to()
    5. Symlink destination containment verification

    Args:
        base_dir: The directory that must contain the resulting path.
        relative_path: The untrusted subpath.

    Returns:
        The canonical resolved Path within base_dir.

    Raises:
        PathTraversalError: If any path traversal or containment breach is detected.
    """
    path_str = str(relative_path)
    if "\0" in path_str:
        raise PathTraversalError(f"Null byte detected in path: {path_str!r}")

    # Check for drive letters (e.g. C:foo or C:\foo)
    if re.match(r"^[a-zA-Z]:", path_str):
        raise PathTraversalError(f"Drive letter specification detected: {path_str!r}")

    # Normalise slashes and check path components
    normalized = path_str.replace("\\", "/")
    if normalized.startswith("/"):
        raise PathTraversalError(f"Absolute path detected: {path_str!r}")

    parts = [p for p in normalized.split("/") if p]
    if not parts:
        raise PathTraversalError("Empty relative path is not permitted")

    if any(part in ("..", ".") for part in parts):
        raise PathTraversalError(f"Directory traversal segment detected: {path_str!r}")

    resolved_base = base_dir.resolve()
    candidate = (resolved_base / Path(*parts)).resolve()

    try:
        candidate.relative_to(resolved_base)
    except ValueError:
        raise PathTraversalError(f"Path escaped base directory: {path_str!r}")

    # Symlink breakout check
    if candidate.is_symlink():
        real_dest = candidate.resolve()
        try:
            real_dest.relative_to(resolved_base)
        except ValueError:
            raise PathTraversalError(f"Symlink escapes base directory: {path_str!r}")

    return candidate


# ---------------------------------------------------------------------------
# Artifact Store
# ---------------------------------------------------------------------------


class ArtifactStore:
    """Content-Addressed Storage (CAS) engine backed by the local filesystem."""

    def __init__(self, root_dir: Path | str | None = None) -> None:
        if root_dir is None:
            self.root_dir = _default_data_root() / "council" / "artifacts"
        else:
            self.root_dir = Path(root_dir).expanduser().resolve()

        self.root_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                self.root_dir.chmod(0o700)
            except OSError:
                pass

    @staticmethod
    def compute_sha256(data: bytes | BinaryIO) -> str:
        """Compute SHA-256 hash of bytes or a readable binary stream."""
        hasher = hashlib.sha256()
        if isinstance(data, (bytes, bytearray, memoryview)):
            hasher.update(data)
        elif hasattr(data, "read"):
            while chunk := data.read(65536):
                hasher.update(chunk)
        else:
            raise TypeError(f"Expected bytes or binary stream, got {type(data)}")
        return hasher.hexdigest().lower()

    @staticmethod
    def validate_hash(content_hash: str) -> str:
        """Validate that content_hash is a valid 64-char lowercase hex SHA-256 digest."""
        if not isinstance(content_hash, str) or not HASH_PATTERN.match(content_hash):
            raise InvalidArtifactHashError(f"Invalid artifact content hash: {content_hash!r}")
        return content_hash

    def get_artifact_path(self, content_hash: str) -> Path:
        """Resolve the storage path for a given content hash with two-level sharding.

        Path layout: root_dir / hash[:2] / hash
        """
        valid_hash = self.validate_hash(content_hash)
        shard = valid_hash[:2]
        return self.root_dir / shard / valid_hash

    def store(self, data: bytes) -> ArtifactStoreResult:
        """Store bytes into the CAS atomically with deduplication.

        Args:
            data: Raw content bytes.

        Returns:
            ArtifactStoreResult with content hash, byte size, and resolved path.
        """
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(f"Expected bytes, got {type(data)}")

        content_hash = self.compute_sha256(data)
        dest_path = self.get_artifact_path(content_hash)
        byte_size = len(data)

        # Deduplication check: if file already exists with matching size, return directly
        if dest_path.is_file() and dest_path.stat().st_size == byte_size:
            return ArtifactStoreResult(content_hash=content_hash, byte_size=byte_size, path=dest_path)

        shard_dir = dest_path.parent
        shard_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                shard_dir.chmod(0o700)
            except OSError:
                pass

        # Write atomically via temporary file in the same shard directory
        temp_fd, temp_path_str = tempfile.mkstemp(prefix="cas_", suffix=".tmp", dir=str(shard_dir))
        temp_path = Path(temp_path_str)
        try:
            with os.fdopen(temp_fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())

            if os.name != "nt":
                try:
                    os.chmod(temp_path, 0o600)
                except OSError:
                    pass

            os.replace(temp_path, dest_path)
        except Exception:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            raise

        return ArtifactStoreResult(content_hash=content_hash, byte_size=byte_size, path=dest_path)

    def store_stream(self, stream: BinaryIO) -> ArtifactStoreResult:
        """Store a binary stream into the CAS atomically.

        Reads the stream into memory or temporary file to compute SHA-256 and store.
        """
        data = stream.read()
        return self.store(data)

    def get(self, content_hash: str) -> bytes:
        """Retrieve raw artifact bytes by content hash.

        Args:
            content_hash: SHA-256 lowercase hex digest.

        Returns:
            The raw bytes of the stored artifact.

        Raises:
            ArtifactNotFoundError: If artifact does not exist.
        """
        path = self.get_artifact_path(content_hash)
        if not path.is_file():
            raise ArtifactNotFoundError(f"Artifact {content_hash} not found in store")
        return path.read_bytes()

    def get_stream(self, content_hash: str, chunk_size: int = 65536) -> Iterator[bytes]:
        """Stream raw artifact bytes in fixed-size chunks."""
        path = self.get_artifact_path(content_hash)
        if not path.is_file():
            raise ArtifactNotFoundError(f"Artifact {content_hash} not found in store")

        with open(path, "rb") as f:
            while chunk := f.read(chunk_size):
                yield chunk

    def exists(self, content_hash: str) -> bool:
        """Check if an artifact exists in the CAS."""
        try:
            path = self.get_artifact_path(content_hash)
            return path.is_file()
        except InvalidArtifactHashError:
            return False

    def delete(self, content_hash: str) -> bool:
        """Delete an artifact from the CAS.

        Returns:
            True if deleted, False if did not exist.
        """
        path = self.get_artifact_path(content_hash)
        if path.is_file():
            path.unlink(missing_ok=True)
            return True
        return False


# ---------------------------------------------------------------------------
# Workspace Staging Helper
# ---------------------------------------------------------------------------


def stage_artifact_to_workspace(
    artifact_store: ArtifactStore,
    content_hash: str,
    workspace_dir: Path,
    target_relative_path: str,
) -> Path:
    """Safely project a CAS artifact into a worker scratch workspace directory.

    Guarantees destination remains strictly contained within workspace_dir.

    Args:
        artifact_store: The CAS repository.
        content_hash: SHA-256 hash of the artifact to project.
        workspace_dir: The scratch workspace root.
        target_relative_path: Logical destination path relative to workspace.

    Returns:
        The resolved Path of the projected file in the workspace.

    Raises:
        ArtifactNotFoundError: If content_hash is not present in the CAS.
        PathTraversalError: If target_relative_path attempts directory escape.
    """
    safe_dest = validate_safe_relative_path(workspace_dir, target_relative_path)
    safe_dest.parent.mkdir(parents=True, exist_ok=True)

    src_file = artifact_store.get_artifact_path(content_hash)
    if not src_file.is_file():
        raise ArtifactNotFoundError(f"Artifact {content_hash} not found in store")

    shutil.copy2(src_file, safe_dest)
    return safe_dest


def store_artifact(
    run_id: str,
    name: str,
    data: bytes,
    stage_id: str | None = None,
    worker_id: str | None = None,
    released: bool = False,
    attempt_id: str | None = None,
    artifact_store: ArtifactStore | None = None,
    conn: Any = None,
    mime_type: str = "text/plain",
    artifact_id: str | None = None,
) -> Any:
    """Store data in the CAS and return a populated ArtifactRef model.

    If conn is provided (sqlite3.Connection), also persists the artifact record to SQLite.

    Args:
        run_id: The run identifier.
        name: Logical name or filename of the artifact.
        data: Raw content bytes.
        stage_id: Optional associated stage ID.
        worker_id: Optional associated worker ID.
        released: True if publicly released to downstream stages.
        attempt_id: Optional associated attempt ID.
        artifact_store: Optional ArtifactStore instance.
        conn: Optional SQLite connection to save record.
        mime_type: MIME media type.
        artifact_id: Optional unique artifact UUID. Generated if omitted.

    Returns:
        ArtifactRef domain model.
    """
    from agym.council.models import ArtifactRef

    store = artifact_store or ArtifactStore()
    res = store.store(data)
    art_id = artifact_id or str(uuid.uuid4())
    ref = ArtifactRef(
        id=art_id,
        run_id=run_id,
        stage_id=stage_id,
        worker_id=worker_id,
        attempt_id=attempt_id,
        name=name,
        path=str(res.path),
        size_bytes=res.byte_size,
        sha256=res.content_hash,
        released=released,
        mime_type=mime_type,
    )
    if conn is not None:
        from agym.council.storage import store_artifact_record

        store_artifact_record(conn, ref)
    return ref

