import hashlib
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic_core import to_jsonable_python


def canonical_json(value: object) -> bytes:
    return json.dumps(
        to_jsonable_python(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


class Artifacts:
    def __init__(self, root: Path):
        self.directory = root / "objects"
        self.directory.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def temporary(self) -> Iterator[Path]:
        with NamedTemporaryFile(dir=self.directory, delete=False) as temporary:
            path = Path(temporary.name)
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)

    def publish(self, path: Path) -> str:
        digest = file_digest(path)
        with path.open("rb") as source:
            os.fsync(source.fileno())
        try:
            os.link(path, self.directory / digest)
        except FileExistsError:
            self.read(digest)
        return digest

    def write_json(self, value: object) -> str:
        with self.temporary() as path:
            path.write_bytes(canonical_json(value))
            return self.publish(path)

    def read(self, digest: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid artifact digest")
        path = self.directory / digest
        if file_digest(path) != digest:
            raise ValueError("Artifact checksum mismatch")
        return path
