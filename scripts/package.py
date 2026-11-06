"""将构建产物打成 npm/DSH 可识别的本地 tarball；只打包清单内文件。"""
import json
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
if not (root / "dist" / "host.js").is_file():
    raise SystemExit("Run node scripts/build.mjs first")
output = root / "data" / f'{manifest["name"]}-{manifest["version"]}.tgz'
output.parent.mkdir(exist_ok=True)
files = {root / "package.json"}
for name in manifest["files"]:
    path = root / name
    if not path.exists():
        raise SystemExit(f"Missing package entry: {name}")
    files.update(path.rglob("*") if path.is_dir() else [path])
with tarfile.open(output, "w:gz") as archive:
    for path in sorted(files):
        relative = path.relative_to(root)
        if not path.is_file() or "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        archive.add(path, arcname="package/" + relative.as_posix(), recursive=False)
with tarfile.open(output) as archive:
    names = archive.getnames()
    assert "package/dist/host.js" in names and "package/src/bili_agent/worker.py" in names
    assert "package/dist/panel/index.html" in names and "package/src/bili_agent/social.py" in names
    assert "package/THIRD-PARTY-NOTICES.md" in names
    assert "package/src/bili_agent/prompts/persona.md" in names
    assert "package/src/bili_agent/automation.py" in names
    assert not any("credentials.json" in name or "node_modules" in name for name in names)
print(f"Packaged {len(names)} files: {output}")
