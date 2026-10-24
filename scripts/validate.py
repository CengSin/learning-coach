import argparse
import hashlib
import html
import json
import math
import re
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from common import parse_time, project_path, read_json
from knowledge import accuracy_report, validate_knowledge
from render import render_outputs
from session_clock import elapsed


class Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = set()
        self.duplicate_ids = []
        self.links = []
        self.required_links = []
        self.capture = None
        self.title = ""
        self.h1 = ""

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if attrs.get("id"):
            if attrs["id"] in self.ids:
                self.duplicate_ids.append(attrs["id"])
            self.ids.add(attrs["id"])
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag in ("img", "source") and attrs.get("src"):
            self.required_links.append(attrs["src"])
        if tag in ("title", "h1"):
            self.capture = tag

    def handle_endtag(self, tag):
        if tag == self.capture:
            self.capture = None

    def handle_data(self, value):
        if self.capture == "title":
            self.title += value
        if self.capture == "h1":
            self.h1 += value


def unique(items, key, label, errors):
    seen = set()
    for item in items:
        if item.get(key) in seen:
            errors.append(f"{label}重复：{item.get(key)}")
        seen.add(item.get(key))


def validate(root, check_generated=True, *, strict_materials=False, quick=False):
    root = Path(root).resolve()
    errors, warnings = [], []
    accuracy = {"warnings": [], "mastery_basis": None}
    try:
        course = read_json(root, "data/course.json")
        progress = read_json(root, "data/progress.json")
        session = read_json(root, "data/session.json")
        catalogue = read_json(root, "materials/catalogue.json")
        questions = read_json(root, "data/questions.json")
        errors.extend(validate_knowledge(root, progress.get("knowledge", [])))
        policy = course["time_policy"]
        for key in ("weekday_minutes", "weekend_minutes"):
            limit = policy.get(key)
            if limit is not None and (isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit < 0):
                errors.append("日预算应为有限非负分钟或 null：" + key)
        cycle = course.get("cycle", {})
        for value in ([cycle["review_weekday"]] if "review_weekday" in cycle else []) + cycle.get("planned_weekdays", []):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 6:
                errors.append("周期星期应为 0～6，周一为0")
        for period in progress.get("periods", []):
            start, end = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
            if end < start:
                errors.append("周期结束日期早于开始：" + period["id"])
            for value in period.get("planned_learning_dates", []):
                if not start <= date.fromisoformat(value) <= end:
                    errors.append("计划学习日超出周期：" + period["id"])
        for values, key, label in [(progress["days"], "date", "学习日"), (progress.get("knowledge", []), "id", "知识点"), (progress.get("mistakes", []), "id", "错题"), (progress.get("sessions", []), "id", "历史场次"), (catalogue["sources"], "id", "资料"), (questions["items"], "id", "题目")]:
            unique(values, key, label, errors)
        for day in progress["days"]:
            value = day.get("duration_seconds")
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                errors.append("学习时长应为非负整数秒或 null：" + day["date"])
            if "has_unknown_duration" in day and not isinstance(day["has_unknown_duration"], bool):
                errors.append("has_unknown_duration 应为布尔值：" + day["date"])
            if day.get("record") and not project_path(root, day["record"]).is_file():
                errors.append("学习记录不存在：" + day["record"])
        for state in progress.get("sessions", []) + [session]:
            if state.get("status") not in ("idle", "running", "paused", "ended"):
                errors.append("无效计时状态")
            if state.get("status") != "idle":
                windows = state.get("closing_windows")
                if windows:
                    concepts, question_window = windows["stop_concepts_before_end_minutes"], windows["stop_questions_before_end_minutes"]
                    if not all(math.isfinite(value) and value >= 0 for value in (concepts, question_window)) or question_window >= concepts:
                        errors.append("无效本场收尾窗口")
                    elif not state.get("review_only") and concepts * 60 >= state["budget_seconds"]:
                        errors.append("本场收尾窗口占满学习预算")
                point = state.get("ended_at") or state.get("last_checked_at")
                if elapsed(state, parse_time(point)) < 0:
                    errors.append("暂停累计超过实际场次")
                if state.get("status") == "ended" and not state.get("finish_reason"):
                    errors.append("已结束场次缺少收尾原因")
        if session.get("status") == "ended" and not any(s["id"] == session["id"] for s in progress.get("sessions", [])):
            errors.append("已结束场次尚未保存到历史")
        for source in catalogue["sources"]:
            path = project_path(root, source["path"])
            if not path.is_file():
                errors.append("资料不存在：" + source["path"])
                continue
            if not quick:
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() != source["sha256"]:
                    errors.append("资料指纹不符：" + source["id"])
            for entry in source.get("toc", []):
                number = entry.get("pdf_start")
                if number is not None and not 1 <= number <= source["page_count"]:
                    errors.append("目录 PDF 页序越界：" + entry["title"])
            if source.get("ocr", {}).get("status") == "not_configured":
                warnings.append(source["id"] + "：Unlimited-OCR 未配置，未声明实测通过")
        for question in questions["items"]:
            for field in ("provenance", "source", "answer_source", "verification", "knowledge_ids", "exposure"):
                if field not in question:
                    errors.append(f"题目 {question['id']} 缺少 {field}")
            if question.get("provenance") == "自编" and question.get("answer_source") == "官方答案":
                errors.append("自编题不能标为官方答案")
        accuracy = accuracy_report(progress, questions["items"])
        warnings.extend(f"[{item['code']}] {item['location']}：{item['message']}" for item in accuracy["warnings"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {"ok": False, "errors": [str(error)], "warnings": warnings}

    can_render = not errors
    imported_roots = [root / "materials/parsed"]
    imported_roots += [project_path(root, s["parsed"]["directory"]) for s in catalogue["sources"] if s.get("parsed", {}).get("directory")]

    def imported(path):
        return any(path == directory or directory in path.parents for directory in imported_roots)

    documents, material_issues = {}, []

    def report_link_issue(path, message, required=False):
        if imported(path):
            severity = "error" if strict_materials or required else "warning"
            material_issues.append({"document": path.relative_to(root).as_posix(), "severity": severity,
                                    "required_asset": required, "message": message})
            (errors if severity == "error" else warnings).append(message)
        else:
            errors.append(message)

    def document_paths(directory):
        for path in directory.iterdir():
            relative = path.relative_to(root)
            if path.is_dir():
                if path.name in ("__pycache__", ".git") or path.is_symlink() or (quick and imported(path)):
                    continue
                if "assets" in relative.parts and not imported(path):
                    continue
                yield from document_paths(path)
            elif path.is_file() and path.suffix.lower() in (".html", ".md") and not (quick and imported(path)):
                yield path

    paths = list(document_paths(root))
    for path in paths:
        if path.suffix.lower() == ".html":
            doc = Document()
            doc.feed(path.read_text(encoding="utf-8"))
            documents[path] = doc
            if doc.duplicate_ids:
                report_link_issue(path, str(path.relative_to(root)) + " 重复锚点：" + ",".join(doc.duplicate_ids))
            if path.parent.name == "records" and re.fullmatch(r"\d{4}-\d{2}-\d{2}学习要点、错题总结以及注意点", path.stem):
                if doc.title.strip() != path.stem or doc.h1.strip() != path.stem:
                    errors.append("每日记录的文件名、title、h1 不一致：" + path.name)
    for path in paths:
        if path in documents:
            links = [(value, False) for value in documents[path].links] + [(value, True) for value in documents[path].required_links]
        else:
            links = [(target.strip("<>"), bool(image)) for image, target in re.findall(r"(!?)\[[^\]]*\]\((<[^>]+>|[^\s)]+)(?:\s+\"[^\"]*\")?\)", path.read_text(encoding="utf-8"))]
        for value, required in links:
            parts = urlsplit(html.unescape(value))
            if parts.scheme or parts.netloc:
                continue
            target = (path.parent / unquote(parts.path)).resolve() if parts.path else path
            if target != root and root not in target.parents:
                report_link_issue(path, f"链接超出项目：{path.relative_to(root)} -> {value}", required)
                continue
            if not target.exists():
                report_link_issue(path, f"链接目标不存在：{path.relative_to(root)} -> {value}", required)
            elif parts.fragment and target.suffix.lower() == ".html" and target in documents and unquote(parts.fragment) not in documents[target].ids:
                report_link_issue(path, f"锚点不存在：{path.relative_to(root)} -> {value}", required)
    if check_generated and can_render:
        for relative, expected in render_outputs(root).items():
            target = root / relative
            if not target.exists() or target.read_text(encoding="utf-8") != expected:
                errors.append("生成视图过期或不存在：" + relative)
    return {"ok": not errors, "errors": errors, "warnings": sorted(set(warnings)), "checked_documents": len(paths),
            "accuracy_warnings": accuracy["warnings"], "mastery_basis": accuracy["mastery_basis"],
            "material_issues": material_issues, "source_fingerprints_checked": not quick,
            "imported_documents_checked": not quick, "strict_materials": strict_materials}


def main():
    parser = argparse.ArgumentParser(description="检查学习数据、真实时间、资料指纹、本地链接和生成视图")
    parser.add_argument("--root", default=".")
    parser.add_argument("--skip-generated", action="store_true")
    parser.add_argument("--strict-materials", action="store_true", help="将原解析稿的辅助链接问题也作为错误")
    parser.add_argument("--quick", action="store_true", help="跳过全文资料指纹与原解析目录，只做课堂所需的轻量检查")
    args = parser.parse_args()
    report = validate(args.root, not args.skip_generated, strict_materials=args.strict_materials, quick=args.quick)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
