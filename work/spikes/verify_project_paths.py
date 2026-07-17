from pathlib import Path

root = Path("E:/LLM/AI/Crazy")
assert root.exists() and root.is_dir()
assert (root / "docs").exists()
probe = root / "work" / ".write_probe"
probe.write_text("ok", encoding="utf-8")
assert probe.read_text(encoding="utf-8") == "ok"
probe.unlink()
print("paths ok")
