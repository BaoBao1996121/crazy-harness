from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from crazy_harness.taskpacks import RepoMaintainerTaskPack

def digest(root: Path) -> str:
    data = b"".join(p.relative_to(root).as_posix().encode() + p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())
    return sha256(data).hexdigest()


with TemporaryDirectory() as tmp:
    pack = RepoMaintainerTaskPack(Path(tmp))
    left, right = pack.prepare("single"), pack.prepare("team")
    assert left.workspace != right.workspace
    assert digest(left.workspace) == digest(right.workspace)
    (left.workspace / "calculator.py").write_text("changed", encoding="utf-8")
    assert digest(left.workspace) != digest(right.workspace)
print("eval fixture isolation: ok")
