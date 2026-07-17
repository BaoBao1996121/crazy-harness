from crazy_harness.core.artifacts import ArtifactStore


def test_artifact_store_writes_and_reads_text(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")

    ref = store.write_text("log", "hello", summary="test log")

    assert ref.kind == "log"
    assert store.read_text(ref) == "hello"
