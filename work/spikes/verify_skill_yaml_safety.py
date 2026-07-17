from yaml import safe_load
from yaml.constructor import ConstructorError

frontmatter = safe_load("name: repo-review\ndescription: Review a repository\nallowed-tools: repo.read")
assert frontmatter["name"] == "repo-review"
assert frontmatter["allowed-tools"] == "repo.read"
try:
    safe_load("payload: !!python/object/apply:os.system ['echo unsafe']")
except ConstructorError:
    pass
else:
    raise AssertionError("safe_load accepted a Python object constructor")
print("skill_yaml_safety=ok")
