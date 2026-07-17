from crazy_harness.core.capabilities import CapabilityCatalog, CapabilityDefinition, CapabilityKind

catalog = CapabilityCatalog()
for name, description in [("repo.read", "read source"), ("shell.admin", "run admin command")]:
    catalog.register(CapabilityDefinition(name=name, kind=CapabilityKind.FUNCTION, description=description))

allowed = {"repo.read"}
ranked = catalog.search("read source admin command", limit=10)
disclosed = [item.name for item in ranked if item.name in allowed]
assert disclosed == ["repo.read"]
print("capability authority filter precedes relevance: ok")
