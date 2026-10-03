from pathlib import Path
import json, re, compileall
root = Path(__file__).resolve().parents[1]
hacs = json.loads((root / "hacs.json").read_text())
assert hacs["name"] and hacs["render_readme"]
version = (root / "VERSION").read_text().strip()
assert re.fullmatch(r"\d+\.\d+\.\d+", version)
if (root / "custom_components").exists():
    components = [p for p in (root / "custom_components").iterdir() if p.is_dir() and p.name != "__pycache__"]
    assert len(components) == 1, "HACS requires one integration per repository"
    p = components[0]
    m = json.loads((p / "manifest.json").read_text())
    for key in ("domain", "name", "version", "documentation", "issue_tracker", "codeowners"):
        assert m[key], key
    assert m["domain"] == p.name and m["version"] == version
    assert compileall.compile_dir(p, quiet=1)
else:
    assert (root / "dist" / hacs["filename"]).is_file()
    assert (root / "dist" / "ha-custom-dashboards.js").stat().st_size > 50000
print("Local package structure checks passed")
