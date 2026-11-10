"""课中轻量记账：每轮一条命令追加课堂日志并核时，收尾时统一写回 progress/questions。

日志 records/class-log/YYYY-MM-DD.jsonl 只追加、不改写，本身就是含用户原答的真实课堂记录；
commit 按日志条目 ID 幂等合并，重复执行不会重复写入。
"""
import argparse
import json
from pathlib import Path

from common import current_time, parse_time, project_path, read_json, write_json, write_text
from knowledge import DIMENSIONS, REVIEW_STATUSES

LOG_DIRECTORY = "records/class-log"
ANSWER_STATUSES = DIMENSIONS["作答"] | DIMENSIONS["延迟保持"] | DIMENSIONS["迁移"]
MISTAKE_STATUSES = {"当场订正", "未通过"}


def log_path(day):
    return f"{LOG_DIRECTORY}/{day}.jsonl"


def read_log(root, day):
    path = project_path(root, log_path(day))
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def class_day(session, timezone, at):
    if session.get("status") in ("running", "paused") and session.get("started_at"):
        return parse_time(session["started_at"]).date().isoformat()
    return at.date().isoformat()


def split_check(value, label):
    if value is None:
        return None
    method, separator, result = value.partition("|")
    if not separator or not method.strip() or not result.strip():
        raise ValueError(label + " 须写成“方法|结果”")
    return {"method": method.strip(), "result": result.strip()}


def build_entry(args, entry_id, at, session_id):
    entry = {"id": entry_id, "at": at.isoformat(), "session_id": session_id, "kind": args.kind}
    if args.kind == "question":
        check = split_check(args.check, "--check")
        if args.provenance in ("自编", None) and check is None:
            raise ValueError("自编题须用 --check 记录独立复算，例如 \"工具复算：枚举两种情形|一致\"")
        entry.update(question_id=args.id, title=args.title, stem=args.stem, answer=args.answer,
                     answer_source=args.answer_source, knowledge_ids=args.knowledge or [],
                     provenance=args.provenance or "自编", independent_check=check)
    elif args.kind == "teach":
        entry.update(knowledge_id=args.knowledge, title=args.title, summary=args.summary)
    elif args.kind == "answer":
        status = args.status
        dimension = args.dimension
        if status not in DIMENSIONS[dimension]:
            raise ValueError(f"{dimension} 的状态只能是：" + "、".join(sorted(DIMENSIONS[dimension])))
        if dimension == "延迟保持" and not args.baseline:
            raise ValueError("延迟保持须用 --baseline 引用更早学习日的事件ID")
        mode = args.mode or ("提示后作答" if status == "提示后正确" else "独立作答")
        grading = split_check(args.grading_check, "--grading-check")
        if status in MISTAKE_STATUSES and args.original and grading is None:
            raise ValueError("写入错题须用 --grading-check \"复核方法|复核结果\" 记录判错前的二次核对")
        entry.update(knowledge_id=args.knowledge, question_ids=args.question or [], dimension=dimension,
                     status=status, summary=args.summary, mode=mode,
                     without_explanation=not args.saw_explanation, baseline_id=args.baseline,
                     original=args.original, correction=args.correction, grading_check=grading,
                     review_status=args.review_status)
    elif args.kind == "recall":
        gaps = [gap.strip() for gap in (args.gap or []) if gap.strip()]
        entry.update(knowledge_id=args.knowledge, gaps=gaps, review_status=args.review_status)
    return {key: value for key, value in entry.items() if value is not None}


def tick(root, course, session, at):
    from session_clock import snapshot, timer_markdown, transition
    if session.get("status") in ("running", "paused"):
        session = transition(session, "tick", course["time_policy"], at)
        write_json(root, "data/session.json", session)
        write_text(root / "学习计时.md", timer_markdown(session, course["time_policy"]))
    info = snapshot(session, course["time_policy"], at)
    return {"phase": info["phase"], "elapsed_minutes": round(info["elapsed_seconds"] / 60, 1),
            "remaining_minutes": round(info["remaining_seconds"] / 60, 1),
            "next_action": info["next_action"]}


def find_knowledge(progress, knowledge_id, title=None, *, create=False):
    entries = progress.setdefault("knowledge", [])
    entry = next((item for item in entries if item["id"] == knowledge_id), None)
    if entry is None:
        if not create or not title:
            raise ValueError("知识点不存在：" + knowledge_id + "；新知识点先用 teach --title 建立")
        entry = {"id": knowledge_id, "title": title, "evidence_history": []}
        entries.append(entry)
    if entry.get("status") == "未学":
        entry.pop("status")
    entry.setdefault("evidence_history", [])
    return entry


def append_event(entry, event):
    if any(item.get("id") == event["id"] for item in entry["evidence_history"]):
        return False
    entry["evidence_history"].append(event)
    return True


def apply_entry(progress, questions, entry, source, day, period):
    changed = 0
    kind = entry["kind"]
    items = questions.setdefault("items", [])
    if kind == "question":
        if any(item["id"] == entry["question_id"] for item in items):
            return 0
        item = {"id": entry["question_id"], "title": entry["title"], "knowledge_ids": entry["knowledge_ids"],
                "stem": entry["stem"], "verification": entry["answer"], "answer_source": entry.get("answer_source"),
                "provenance": entry["provenance"], "source": source,
                "exposure": {"shown_at": entry["at"], "answered": False, "answer_available": False,
                             "unseen_for_testing": False}}
        if entry.get("independent_check"):
            item["independent_check"] = entry["independent_check"]
        items.append(item)
        return 1
    if kind == "teach":
        knowledge = find_knowledge(progress, entry["knowledge_id"], entry.get("title"), create=True)
        return int(append_event(knowledge, {"id": entry["id"], "date": day, "dimension": "讲解", "status": "已讲解",
                                            "summary": entry["summary"], "source": source}))
    knowledge = find_knowledge(progress, entry["knowledge_id"])
    if kind == "recall":
        gaps = entry["gaps"]
        event = {"id": entry["id"], "date": day, "dimension": "作答", "status": "未通过" if gaps else "独立正确",
                 "summary": "闭卷复述遗漏或错误：" + "；".join(gaps) if gaps else "闭卷复述无遗漏或错误",
                 "source": source,
                 "assessment": {"mode": "闭卷复述", "without_explanation": True, "recall_gaps": gaps}}
    else:
        assessment = {"mode": entry["mode"], "without_explanation": entry["without_explanation"],
                      "question_ids": entry["question_ids"]}
        event = {"id": entry["id"], "date": day, "dimension": entry["dimension"], "status": entry["status"],
                 "summary": entry["summary"], "source": source, "assessment": assessment}
        if entry.get("baseline_id"):
            event["baseline_id"] = entry["baseline_id"]
        for question_id in entry["question_ids"]:
            question = next((item for item in items if item["id"] == question_id), None)
            if question is None:
                raise ValueError("题目不存在：" + question_id + "；先用 question 登记")
            exposure = question.setdefault("exposure", {})
            if not (exposure.get("answered") and exposure.get("answer_available")):
                exposure.update(answered=True, answer_available=True)
                changed += 1
        mistakes = progress.setdefault("mistakes", [])
        if entry.get("original") and entry["status"] in MISTAKE_STATUSES and not any(item.get("id") == entry["id"] for item in mistakes):
            mistake = {"id": entry["id"], "date": day, "knowledge_id": entry["knowledge_id"],
                       "original": entry["original"], "correction": entry.get("correction") or entry["summary"],
                       "review_status": "待复查", "evidence": source,
                       "assessment": {**assessment, "grading_check": entry["grading_check"]}}
            if period:
                mistake["period"] = period
            mistakes.append(mistake)
            changed += 1
    changed += int(append_event(knowledge, event))
    review_status = entry.get("review_status")
    if review_status and knowledge.get("review_status") != review_status:
        knowledge["review_status"] = review_status
        changed += 1
    return changed


def period_for(progress, day):
    for period in progress.get("periods", []):
        if period.get("start", "") <= day <= period.get("end", ""):
            return period.get("id")
    return None


def commit(root, day, at):
    entries = read_log(root, day)
    progress = read_json(root, "data/progress.json")
    questions = read_json(root, "data/questions.json", {"schema_version": 1, "items": []})
    source = log_path(day)
    period = period_for(progress, day)
    applied = 0
    for entry in entries:
        applied += apply_entry(progress, questions, entry, source, day, period)
    if applied:
        progress["updated_at"] = at.isoformat()
        write_json(root, "data/questions.json", questions)
        write_json(root, "data/progress.json", progress)
    return {"date": day, "log": source, "entries": len(entries), "changes": applied}


def main():
    parser = argparse.ArgumentParser(description="课中一行记账（自带核时）；收尾 commit 合并进 progress/questions")
    parser.add_argument("--root", default=".")
    actions = parser.add_subparsers(dest="action", required=True)
    log = actions.add_parser("log", help="追加一条课堂日志并核时")
    kinds = log.add_subparsers(dest="kind", required=True)

    question = kinds.add_parser("question", help="登记一道出给用户的题")
    question.add_argument("--id", required=True)
    question.add_argument("--title", required=True)
    question.add_argument("--stem", required=True)
    question.add_argument("--answer", required=True, help="参考答案")
    question.add_argument("--answer-source", help="答案依据：资料名及章节／页码")
    question.add_argument("--knowledge", action="append", help="关联知识点ID，可重复")
    question.add_argument("--provenance", default="自编")
    question.add_argument("--check", help="独立复算：\"工具复算：说明|一致\"")

    teach = kinds.add_parser("teach", help="记录一段讲解（不计掌握）")
    teach.add_argument("--knowledge", required=True)
    teach.add_argument("--title", help="新知识点标题；已有知识点可省略")
    teach.add_argument("--summary", required=True)

    answer = kinds.add_parser("answer", help="记录一次用户作答")
    answer.add_argument("--knowledge", required=True)
    answer.add_argument("--question", action="append", help="题目ID，可重复")
    answer.add_argument("--status", required=True, choices=sorted(ANSWER_STATUSES))
    answer.add_argument("--dimension", default="作答", choices=["作答", "延迟保持", "迁移"])
    answer.add_argument("--summary", required=True, help="一句话结果")
    answer.add_argument("--mode", choices=["独立作答", "提示后作答", "练习"])
    answer.add_argument("--saw-explanation", action="store_true", help="作答时看过讲解或答案")
    answer.add_argument("--baseline", help="延迟保持引用的更早事件ID")
    answer.add_argument("--original", help="用户原答（写错题时必填）")
    answer.add_argument("--correction", help="订正要点")
    answer.add_argument("--grading-check", help="判错复核：\"方法|结果\"")
    answer.add_argument("--review-status", choices=sorted(REVIEW_STATUSES))

    recall = kinds.add_parser("recall", help="记录闭卷复述，只写遗漏或错误")
    recall.add_argument("--knowledge", required=True)
    recall.add_argument("--gap", action="append", help="遗漏或错误的关键事实，可重复；全对不填")
    recall.add_argument("--review-status", choices=sorted(REVIEW_STATUSES))

    done = actions.add_parser("commit", help="把课堂日志幂等合并进 progress/questions")
    done.add_argument("--date", help="YYYY-MM-DD，默认当前场次或今天")
    actions.add_parser("show", help="查看今天的课堂日志")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        course = read_json(root, "data/course.json")
        session = read_json(root, "data/session.json", {"status": "idle"})
        at = current_time(course["timezone"])
        day = class_day(session, course["timezone"], at)
        if args.action == "log":
            entries = read_log(root, day)
            entry_id = "L" + day.replace("-", "") + f"-{len(entries) + 1:03d}"
            entry = build_entry(args, entry_id, at, session.get("id") if session.get("status") in ("running", "paused") else None)
            path = project_path(root, log_path(day))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            print(json.dumps({"logged": entry_id, **tick(root, course, session, at)}, ensure_ascii=False))
        elif args.action == "commit":
            print(json.dumps(commit(root, args.date or day, at), ensure_ascii=False))
        else:
            print(json.dumps(read_log(root, day), ensure_ascii=False, indent=1))
    except (ValueError, KeyError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
