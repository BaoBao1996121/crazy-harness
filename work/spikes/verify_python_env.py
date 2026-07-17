import importlib.util
import sys

mods = ["pydantic", "httpx", "pytest"]
print(sys.executable)
print(sys.version)
print({name: bool(importlib.util.find_spec(name)) for name in mods})
