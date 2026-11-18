#!/usr/bin/env python3
"""Package built assets and Python runtime without development dependencies."""
import argparse
import datetime
import gzip
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import tarfile
import tempfile
import zipfile


ROOT_FILES = {"VERSION", "README.md", "LICENSE", "NOTICE", "THIRD_PARTY.md", ".gitignore", ".gitattributes", ".node-version"}


def source_allowed(relative):
    parts = relative.parts
    if any(p in {"node_modules", "__pycache__", ".git", ".pytest_cache", "state", "data", "dist", ".demo-data"}
           or p.startswith((".qa", "tmp")) for p in parts):
        return False
    if len(parts) == 1:
        return parts[0] in ROOT_FILES
    if any(p.startswith(".") for p in parts):
        return False
    if parts[0] == "server":
        return relative.suffix == ".py"
    if parts[0] == "shared":
        return relative.suffix == ".json"
    if parts[0] == "tests":
        return relative.suffix in {".py", ".json", ".ini", ".bcm", ".md"} or relative.name == "LICENSE"
    if parts[0] == "deploy":
        return relative.suffix in {".py", ".sh", ".service", ".md"} or relative.name.endswith(".example.json")
    if parts[0] == "ui":
        return (parts[1] in {"src", "tests", "public"} and relative.suffix in {".ts", ".tsx", ".css", ".txt", ".svg"}
                or len(parts) == 2 and parts[1] in {"package.json", "package-lock.json", "tsconfig.json", "vite.config.ts", "index.html"})
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output_dir.resolve()
    if output.is_relative_to(root):
        raise SystemExit("Choose an output directory outside the source tree")
    output.mkdir(parents=True, exist_ok=True)
    version = (root / "VERSION").read_text().strip()
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    if not 0 <= epoch <= 4354819199:
        raise SystemExit("SOURCE_DATE_EPOCH must fit the supported archive timestamp range")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version):
        raise SystemExit("Invalid release version")
    if not (root / "ui/dist/index.html").is_file():
        raise SystemExit("Build the frontend before packaging")
    paths = []
    for directory in ["server", "shared", "deploy", "ui/dist"]:
        for path in (root / directory).rglob("*"):
            if path.is_symlink():
                raise SystemExit("Bundle source must not contain symlinks: " + str(path))
            if path.is_file() and (directory == "ui/dist" or source_allowed(path.relative_to(root))):
                paths.append(path)
    paths += [root / name for name in ("VERSION", "README.md", "LICENSE", "NOTICE", "THIRD_PARTY.md")]
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise SystemExit("Unsafe bundle source: " + str(path))
    manifest = {"product": "sonic-web-manager", "version": version, "files": {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }}
    bundle_name = "sonic-web-manager-" + version
    archive = output / (bundle_name + ".tar.gz")
    with tempfile.TemporaryDirectory(prefix="sonic-web-release-") as temporary:
        stage = Path(temporary) / bundle_name
        stage.mkdir()
        for path in paths:
            target = stage / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        with archive.open("wb") as output_stream, gzip.GzipFile(filename="", mode="wb", fileobj=output_stream, mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as tar:
                tar.add(stage, arcname=bundle_name, filter=lambda info: _normalize(info, epoch))
    source_archive = output / (bundle_name + "-source.zip")
    with zipfile.ZipFile(source_archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as source_zip:
        for path in sorted(root.rglob("*")):
            parts = path.relative_to(root).parts
            if not path.is_file() or not source_allowed(path.relative_to(root)):
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise SystemExit("Source archive must not contain symlinks")
            info = zipfile.ZipInfo("/".join(parts), datetime.datetime.fromtimestamp(max(epoch, 315532800), datetime.timezone.utc).timetuple()[:6])
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            source_zip.writestr(info, path.read_bytes(), compresslevel=9)
    result = {"version": version, "bundle_files": len(manifest["files"]), "artifacts": []}
    for artifact in [archive, source_archive]:
        result["artifacts"].append({"path": str(artifact), "bytes": artifact.stat().st_size,
                                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()})
    print(json.dumps(result, indent=2))


def _normalize(info, epoch=0):
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mode = 0o755 if info.isdir() else 0o644
    info.mtime = epoch
    info.pax_headers = {}
    return info


if __name__ == "__main__":
    main()
