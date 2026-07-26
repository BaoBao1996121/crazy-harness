from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile
with TemporaryDirectory() as root:
    root = Path(root)
    source, restored, archive = root / "source", root / "restored", root / "snapshot.zip"
    (source / "nested").mkdir(parents=True)
    (source / "a.txt").write_bytes(b"alpha\r\n")
    (source / "nested" / "binary.bin").write_bytes(bytes(range(32)))
    files = [path for path in sorted(source.rglob("*")) if path.is_file()]
    with ZipFile(archive, "w", ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(path, path.relative_to(source).as_posix())
    digest = sha256(archive.read_bytes()).hexdigest()
    with ZipFile(archive) as bundle:
        bundle.extractall(restored)
    assert digest == sha256(archive.read_bytes()).hexdigest()
    assert [(p.relative_to(source), p.read_bytes()) for p in files] == [(p.relative_to(restored), p.read_bytes()) for p in sorted(restored.rglob("*")) if p.is_file()]
print("PASS checkpoint snapshot roundtrip")
