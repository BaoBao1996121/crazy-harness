from __future__ import annotations


def microcompact(self, events):
    kept = []
    discarded = []
    offloaded = []
    for event in events:
        if event.payload.get("transient") is True:
            discarded.append(event.event_id)
            continue
        if len(str(event.payload.get("output", ""))) > 2000:
            offloaded.append(event.event_id)
            compacted = event.model_copy(
                update={"payload": {"summary": "large output offloaded", "output_ref": event.event_id}}
            )
            kept.append(compacted)
            continue
        kept.append(event)
    return MicrocompactResult(
        kept_events=kept,
        discarded_event_ids=discarded,
        offloaded_event_ids=offloaded,
    )


def build_messages(self, events):
    compacted = self.microcompact(events)
    lines = []
    for event in compacted.kept_events:
        lines.append(f"{event.type} from {event.source}: {event.payload}")
    return [{"role": "user", "content": "\n".join(lines)}]
