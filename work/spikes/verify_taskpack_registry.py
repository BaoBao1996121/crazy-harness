from dataclasses import dataclass


@dataclass
class Pack:
    task_pack_id: str
    scripted: tuple[str, ...]

    def scripted_responses(self):
        return list(self.scripted)


packs = {
    pack.task_pack_id: pack
    for pack in (Pack("repo", ("fix",)), Pack("research", ("cite",)))
}
persisted_id = "research"
assert packs[persisted_id].scripted_responses() == ["cite"]
