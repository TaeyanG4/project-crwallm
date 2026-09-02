"""Content-addressed archive for fetched bodies.

The highest-return item in Phase 2, and the one whose value is easiest to
underestimate while writing it.

**Re-extraction without re-fetching.** Phases 3 and 4 iterate on selectors
against the same pages, hundreds of times. With an archive that loop is a
local read; without it, every iteration is a network round trip - roughly ten
times slower, and rude to the site being developed against.

It also pays for itself later: drift diagnosis needs the HTML as it was, the
evidence trail needs something to point at, and a new extractor written in
Phase 6 can be applied retroactively to everything already collected.

**Content addressing, not URL addressing.** The key is ``sha256(body)``, so
two URLs serving identical bytes cost one blob, and the hash doubles as the
content-level duplicate signal that Phase 5 builds on. zstd gets roughly 10:1
on HTML.

Layout is sharded two levels by hash prefix. A flat directory of a hundred
thousand files is slow to list on every filesystem and pathological on some.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import zstandard

__all__ = ["ArchiveRef", "BlobStore", "NullBlobStore"]

_SHARD_DEPTH = 2
_SHARD_WIDTH = 2


@dataclass(frozen=True, slots=True)
class ArchiveRef:
    """Where a body went, and what it cost."""

    digest: str
    """``sha256`` hex of the *uncompressed* body. The identity."""

    size: int
    """Uncompressed length, for reporting and for spotting a truncated store."""

    stored: bool
    """``False`` when the digest was already present. Useful for measuring how
    much of a crawl is duplicate content."""

    @property
    def content_hash(self) -> str:
        return self.digest


class BlobStore:
    """zstd-compressed blobs on the local filesystem."""

    def __init__(self, root: Path, *, level: int = 6) -> None:
        self._root = Path(root)
        self._level = level
        self._compressor = zstandard.ZstdCompressor(level=level)
        self._decompressor = zstandard.ZstdDecompressor()

    @staticmethod
    def digest_of(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()

    def _path(self, digest: str) -> Path:
        shards = [digest[i * _SHARD_WIDTH : (i + 1) * _SHARD_WIDTH] for i in range(_SHARD_DEPTH)]
        return self._root.joinpath(*shards, f"{digest}.zst")

    def put(self, body: bytes) -> ArchiveRef:
        """Store ``body``. Idempotent - the same bytes never write twice."""
        digest = self.digest_of(body)
        path = self._path(digest)
        if path.exists():
            return ArchiveRef(digest, len(body), stored=False)

        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary name and rename, so a crash never leaves a
        # partial blob under a digest that claims to be complete.
        tmp = path.with_suffix(".zst.part")
        tmp.write_bytes(self._compressor.compress(body))
        tmp.replace(path)
        return ArchiveRef(digest, len(body), stored=True)

    def get(self, digest: str) -> bytes | None:
        path = self._path(digest)
        if not path.exists():
            return None
        return self._decompressor.decompress(path.read_bytes())

    def has(self, digest: str) -> bool:
        return self._path(digest).exists()

    def delete(self, digest: str) -> bool:
        path = self._path(digest)
        if not path.exists():
            return False
        path.unlink()
        return True

    def stats(self) -> tuple[int, int]:
        """``(blob count, bytes on disk)``. Walks the tree, so call it for a
        report rather than in a loop."""
        count = 0
        size = 0
        for path in self._root.rglob("*.zst"):
            count += 1
            size += path.stat().st_size
        return count, size


class NullBlobStore:
    """Discards everything. For tests, and for runs where the disk cost is not
    wanted - the digest is still computed, so content duplicate detection keeps
    working."""

    @staticmethod
    def digest_of(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()

    def put(self, body: bytes) -> ArchiveRef:
        return ArchiveRef(self.digest_of(body), len(body), stored=False)

    def get(self, digest: str) -> bytes | None:
        return None

    def has(self, digest: str) -> bool:
        return False

    def delete(self, digest: str) -> bool:
        return False

    def stats(self) -> tuple[int, int]:
        return 0, 0
