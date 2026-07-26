from __future__ import annotations

import json
import shutil
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import mkdtemp
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from crazy_harness.core.checkpoints.models import WorkspaceFileEntry, WorkspaceSnapshot


class CheckpointError(RuntimeError):
    pass


class CheckpointPolicyError(CheckpointError):
    pass


class CheckpointIntegrityError(CheckpointError):
    pass


class WorkspaceSnapshotStore:
    """Content-addressed, immutable snapshots for disposable workspaces."""

    def __init__(
        self,
        root: Path,
        *,
        max_files: int = 2_000,
        max_total_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        if max_files < 1:
            raise ValueError("max_files must be positive")
        if max_total_bytes < 1:
            raise ValueError("max_total_bytes must be positive")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes

    def create(self, workspace: Path) -> WorkspaceSnapshot:
        entries = self._scan(Path(workspace))
        object_id = self._workspace_digest(entries)
        object_dir = self.object_path(object_id)
        if object_dir.exists():
            return self.verify(object_id)

        object_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(mkdtemp(prefix=f".{object_id}.", dir=object_dir.parent))
        try:
            archive_path = staging / "workspace.zip"
            self._write_archive(archive_path, entries)
            snapshot = self._snapshot(
                object_id=object_id,
                archive_sha256=self._file_sha256(archive_path),
                entries=entries,
            )
            (staging / "manifest.json").write_text(
                snapshot.model_dump_json(indent=2),
                encoding="utf-8",
            )
            try:
                staging.replace(object_dir)
            except OSError:
                if not object_dir.exists():
                    raise
            return self.verify(snapshot)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def verify(self, snapshot: WorkspaceSnapshot | str) -> WorkspaceSnapshot:
        expected = snapshot if isinstance(snapshot, WorkspaceSnapshot) else None
        object_id = snapshot.object_id if expected is not None else str(snapshot)
        object_dir = self.object_path(object_id)
        try:
            stored = WorkspaceSnapshot.model_validate_json(
                (object_dir / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise CheckpointIntegrityError("snapshot manifest is missing or invalid") from exc
        if stored.object_id != object_id or (expected is not None and stored != expected):
            raise CheckpointIntegrityError("snapshot manifest identity mismatch")
        if stored.manifest_sha256 != self._manifest_digest(stored):
            raise CheckpointIntegrityError("snapshot manifest hash mismatch")
        archive = object_dir / "workspace.zip"
        if self._file_sha256(archive) != stored.archive_sha256:
            raise CheckpointIntegrityError("snapshot archive hash mismatch")
        entries = self._read_archive(archive, stored)
        if self._workspace_digest(entries) != stored.object_id:
            raise CheckpointIntegrityError("snapshot workspace hash mismatch")
        return stored

    def restore(self, snapshot: WorkspaceSnapshot | str, target: Path) -> WorkspaceSnapshot:
        stored = self.verify(snapshot)
        target = Path(target)
        if target.exists():
            raise CheckpointPolicyError(f"restore target already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(mkdtemp(prefix=f".{target.name}.restore.", dir=target.parent))
        try:
            entries = self._read_archive(self.archive_path(stored.object_id), stored)
            for entry, content in entries:
                destination = staging / Path(*PurePosixPath(entry.path).parts)
                if not destination.resolve().is_relative_to(staging.resolve()):
                    raise CheckpointIntegrityError("snapshot entry escapes restore target")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            if self._workspace_digest(self._scan(staging)) != stored.object_id:
                raise CheckpointIntegrityError("restored workspace hash mismatch")
            staging.replace(target)
            return stored
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def object_path(self, object_id: str) -> Path:
        self._validate_object_id(object_id)
        return self.root / object_id[:2] / object_id

    def archive_path(self, object_id: str) -> Path:
        return self.object_path(object_id) / "workspace.zip"

    def _scan(self, workspace: Path) -> list[tuple[WorkspaceFileEntry, bytes]]:
        if workspace.is_symlink() or not workspace.is_dir():
            raise CheckpointPolicyError(f"workspace is not a real directory: {workspace}")
        entries: list[tuple[WorkspaceFileEntry, bytes]] = []
        casefolded: set[str] = set()
        total_bytes = 0
        for path in sorted(workspace.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink():
                raise CheckpointPolicyError(f"symbolic link is not allowed: {path}")
            if path.is_dir():
                continue
            if not path.is_file():
                raise CheckpointPolicyError(f"special file is not allowed: {path}")
            relative = path.relative_to(workspace).as_posix()
            self._validate_relative_path(relative)
            folded = relative.casefold()
            if folded in casefolded:
                raise CheckpointPolicyError(f"case-insensitive path collision: {relative}")
            casefolded.add(folded)
            content = path.read_bytes()
            total_bytes += len(content)
            if len(entries) + 1 > self.max_files:
                raise CheckpointPolicyError("workspace exceeds file count budget")
            if total_bytes > self.max_total_bytes:
                raise CheckpointPolicyError("workspace exceeds byte budget")
            entries.append(
                (
                    WorkspaceFileEntry(
                        path=relative,
                        size=len(content),
                        sha256=sha256(content).hexdigest(),
                    ),
                    content,
                )
            )
        return entries

    def _read_archive(
        self,
        archive: Path,
        snapshot: WorkspaceSnapshot,
    ) -> list[tuple[WorkspaceFileEntry, bytes]]:
        expected = {entry.path: entry for entry in snapshot.files}
        entries: list[tuple[WorkspaceFileEntry, bytes]] = []
        try:
            with ZipFile(archive) as bundle:
                names = bundle.namelist()
                if len(names) != len(set(names)) or set(names) != set(expected):
                    raise CheckpointIntegrityError("snapshot archive entries mismatch manifest")
                for name in sorted(names):
                    self._validate_relative_path(name)
                    content = bundle.read(name)
                    entry = expected[name]
                    if len(content) != entry.size or sha256(content).hexdigest() != entry.sha256:
                        raise CheckpointIntegrityError(f"snapshot file hash mismatch: {name}")
                    entries.append((entry, content))
        except (BadZipFile, KeyError, OSError) as exc:
            raise CheckpointIntegrityError("snapshot archive is invalid") from exc
        if len(entries) != snapshot.file_count or sum(item.size for item, _ in entries) != snapshot.total_bytes:
            raise CheckpointIntegrityError("snapshot aggregate metadata mismatch")
        return entries

    @staticmethod
    def _write_archive(
        archive: Path,
        entries: list[tuple[WorkspaceFileEntry, bytes]],
    ) -> None:
        with ZipFile(archive, "w", ZIP_DEFLATED) as bundle:
            for entry, content in entries:
                info = ZipInfo(entry.path, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                bundle.writestr(info, content)

    @classmethod
    def _snapshot(
        cls,
        *,
        object_id: str,
        archive_sha256: str,
        entries: list[tuple[WorkspaceFileEntry, bytes]],
    ) -> WorkspaceSnapshot:
        files = tuple(entry for entry, _ in entries)
        provisional = WorkspaceSnapshot(
            object_id=object_id,
            archive_sha256=archive_sha256,
            manifest_sha256="0" * 64,
            file_count=len(files),
            total_bytes=sum(entry.size for entry in files),
            files=files,
        )
        return provisional.model_copy(
            update={"manifest_sha256": cls._manifest_digest(provisional)}
        )

    @staticmethod
    def _manifest_digest(snapshot: WorkspaceSnapshot) -> str:
        payload = snapshot.model_dump(mode="json", exclude={"manifest_sha256"})
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _workspace_digest(entries: list[tuple[WorkspaceFileEntry, bytes]]) -> str:
        digest = sha256()
        for entry, content in entries:
            relative = entry.path.encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        return digest.hexdigest()

    @staticmethod
    def _file_sha256(path: Path) -> str:
        try:
            return sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise CheckpointIntegrityError(f"snapshot object is unreadable: {path}") from exc

    @staticmethod
    def _validate_object_id(object_id: str) -> None:
        if len(object_id) != 64 or any(char not in "0123456789abcdef" for char in object_id):
            raise CheckpointPolicyError("snapshot object id must be lowercase SHA-256")

    @staticmethod
    def _validate_relative_path(value: str) -> None:
        pure = PurePosixPath(value)
        if (
            not value
            or "\\" in value
            or "\x00" in value
            or pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
            or ":" in pure.parts[0]
        ):
            raise CheckpointIntegrityError(f"unsafe snapshot path: {value}")
