from crazy_harness.core.capabilities import (
    CapabilityCatalog,
    CapabilityCompileRequest,
    CapabilityCompiler,
    CapabilityDefinition,
    CapabilityKind,
    DisclosureStrategy,
)


def _capability(name: str, description: str, *, instructions: str = "") -> CapabilityDefinition:
    return CapabilityDefinition(
        name=name,
        kind=CapabilityKind.FUNCTION,
        description=description,
        instructions=instructions,
        input_schema={"type": "object", "title": name},
    )


def test_small_catalog_discloses_every_authorized_capability_and_nothing_else():
    catalog = CapabilityCatalog()
    catalog.register(_capability("repo.read", "read repository source"))
    catalog.register(_capability("test.run", "run repository tests"))
    catalog.register(_capability("shell.admin", "administer the host"))
    compiler = CapabilityCompiler(catalog, inline_limit=4, search_limit=2)

    compiled = compiler.compile(
        CapabilityCompileRequest(
            agent_id="generalist",
            assignment_id="task-1",
            mode="scripted",
            query="repair and test the repository",
            allowed_names={"repo.read", "test.run"},
        )
    )

    assert compiled.manifest.strategy is DisclosureStrategy.INLINE_ALL
    assert compiled.manifest.disclosed_names == ("repo.read", "test.run")
    assert compiled.manifest.withheld_names == ()
    assert compiled.manifest.excluded_names == ("shell.admin",)
    assert [definition.name for definition in compiled.definitions] == ["repo.read", "test.run"]
    assert compiled.manifest.definition_hashes.keys() == {"repo.read", "test.run"}


def test_large_catalog_filters_authority_before_search_and_honors_explicit_recall():
    catalog = CapabilityCatalog()
    for definition in (
        _capability("shell.admin", "admin deploy command"),
        _capability("mail.send", "send a status email"),
        _capability("repo.read", "read repository source"),
        _capability("test.run", "run repository tests"),
        _capability("deploy.plan", "plan a safe deploy", instructions="use for deployment planning"),
    ):
        catalog.register(definition)
    compiler = CapabilityCompiler(catalog, inline_limit=2, search_limit=1)

    compiled = compiler.compile(
        CapabilityCompileRequest(
            agent_id="generalist",
            assignment_id="task-2",
            mode="deepseek",
            query="admin deploy planning",
            allowed_names={"mail.send", "repo.read", "test.run", "deploy.plan"},
            always_include=("repo.read",),
            explicit_names=("test.run", "shell.admin"),
        )
    )

    assert compiled.manifest.strategy is DisclosureStrategy.SEARCH_RANKED
    assert compiled.manifest.disclosed_names == ("deploy.plan", "repo.read", "test.run")
    assert compiled.manifest.withheld_names == ("mail.send",)
    assert compiled.manifest.excluded_names == ("shell.admin",)
    assert compiled.manifest.reasons["shell.admin"] == "policy_denied"
    assert compiled.manifest.reasons["test.run"] == "explicit_recall"
    assert compiled.manifest.reasons["deploy.plan"] == "query_match"


def test_manifest_hash_is_stable_across_catalog_registration_order():
    definitions = [
        _capability("repo.read", "read repository source"),
        _capability("test.run", "run repository tests"),
        _capability("repo.diff", "show repository changes"),
    ]
    request = CapabilityCompileRequest(
        agent_id="generalist",
        assignment_id="task-3",
        mode="scripted",
        query="read test diff",
        allowed_names={definition.name for definition in definitions},
    )
    catalogs = []
    for ordered in (definitions, list(reversed(definitions))):
        catalog = CapabilityCatalog()
        for definition in ordered:
            catalog.register(definition)
        catalogs.append(catalog)

    first = CapabilityCompiler(catalogs[0]).compile(request).manifest
    second = CapabilityCompiler(catalogs[1]).compile(request).manifest

    assert first == second
    assert len(first.manifest_hash) == 64
