import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def read_json(root, relative, default=None):
    path = project_path(root, relative)
    if not path.exists() and default is not None:
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def project_path(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"路径超出项目目录：{relative}")
    return path


def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(root, relative, value):
    write_text(project_path(root, relative), json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def parse_time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return result


def current_time(timezone):
    return datetime.now(ZoneInfo(timezone)).replace(microsecond=0)


def duration(seconds):
    if seconds is None:
        return "未可靠记录"
    seconds = max(0, int(seconds))
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}分{remainder:02d}秒"
