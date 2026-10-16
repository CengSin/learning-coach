import argparse
import hashlib
import json
from pathlib import Path

from common import project_path, read_json


IGNORED_NAMES = {"import-manifest.json", ".DS_Store", "Thumbs.db"}


def fingerprint(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inventory(root, directory):
    result = {}
    if not directory.exists():
        return result
    if not directory.is_dir():
        raise ValueError("解析目录不是文件夹：" + str(directory))
    for path in sorted(directory.rglob("*")):
        if path.name in IGNORED_NAMES or "__pycache__" in path.parts or path.name.startswith(".writing-"):
            continue
        if path.is_file():
            checked = project_path(root, path.relative_to(root))
            result[path.relative_to(directory).as_posix()] = fingerprint(checked)
    return result


def scan_materials(root):
    root = Path(root).resolve()
    catalogue = read_json(root, "materials/catalogue.json", {"sources": []})
    base = project_path(root, "materials/parsed")
    all_files = inventory(root, base)
    covered = set()
    sources = []
    for source in catalogue.get("sources", []):
        parsed = source.get("parsed", {})
        directory = project_path(root, parsed.get("directory") or "materials/parsed/" + source["id"])
        files = inventory(root, directory)
        covered.update((directory / relative).relative_to(root).as_posix() for relative in files)
        manifest_path = project_path(root, parsed.get("manifest") or (directory / "import-manifest.json").relative_to(root))
        issues = []
        expected = {}
        manifest_exists = manifest_path.is_file()
        if manifest_exists:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
                    raise ValueError("导入清单缺少文件列表")
                if manifest.get("source_id") != source["id"]:
                    issues.append("导入清单的资料ID不匹配")
                if manifest.get("source_sha256") != source.get("sha256"):
                    issues.append("导入清单与已登记原PDF指纹不匹配，需检查版本")
                for item in manifest["files"]:
                    relative = Path(item["path"])
                    if relative.is_absolute() or ".." in relative.parts or not item.get("sha256"):
                        raise ValueError("导入文件路径或指纹无效")
                    expected[relative.as_posix()] = item["sha256"]
            except (OSError, ValueError, TypeError, KeyError) as error:
                issues.append("导入清单需重新检查：" + str(error))
        added = sorted(files.keys() - expected.keys())
        modified = sorted(name for name in files.keys() & expected.keys() if files[name] != expected[name])
        missing = sorted(expected.keys() - files.keys())
        needs_import = bool(added or modified or missing or issues)
        state = "needs_import" if needs_import else "unchanged" if files else "awaiting_files"
        sources.append({"source_id": source["id"], "directory": directory.relative_to(root).as_posix(),
                        "state": state, "registered_quality_status": parsed.get("status", "未登记"),
                        "file_count": len(files), "added": added, "modified": modified,
                        "missing": missing, "issues": issues})
    unassigned = sorted(name for name in all_files if (base / name).relative_to(root).as_posix() not in covered)
    return {"needs_import": bool(unassigned or any(s["state"] == "needs_import" for s in sources)),
            "sources": sources, "unassigned_files": unassigned, "watching": False}


def main():
    parser = argparse.ArgumentParser(description="只读检查解析目录与导入清单，发现新增、修改、缺失或未归属文件；不后台监听")
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    try:
        print(json.dumps(scan_materials(args.root), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
