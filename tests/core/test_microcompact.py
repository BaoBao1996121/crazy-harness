from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.context import ContextItem, microcompact


def test_microcompact_offloads_large_tool_output(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    items = [
        ContextItem(role="tool", kind="tool_result", content="x" * 100, importance="normal"),
        ContextItem(role="system", kind="policy", content="keep me", importance="critical"),
    ]

    result = microcompact(items, artifact_store=store, offload_chars=50)

    assert len(result.offloaded_refs) == 1
    assert "keep me" in [item.content for item in result.inline_items]
