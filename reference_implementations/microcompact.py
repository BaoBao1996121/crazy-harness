from __future__ import annotations


def microcompact(items, *, artifact_store, offload_chars=8000):
    inline = []
    refs = []
    discarded = 0
    for item in items:
        if item.importance == "low" and item.kind in {"transient", "nudge"}:
            discarded += 1
            continue
        if item.importance not in {"high", "critical"} and len(item.content) > offload_chars:
            refs.append(artifact_store.write_text(item.kind, item.content, summary=f"offloaded {item.kind}"))
            inline.append(item.model_copy(update={"content": f"[offloaded {item.kind}]"}))
            continue
        inline.append(item)
    return MicrocompactResult(inline_items=inline, offloaded_refs=refs, discarded_count=discarded)
