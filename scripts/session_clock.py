import argparse
import copy
import json
import math
from datetime import timedelta
from pathlib import Path

from common import current_time, duration, parse_time, read_json, write_json, write_text


def elapsed(session, at):
    if session.get("status") == "idle" or not session.get("started_at"):
        return 0
    stop = parse_time(session["ended_at"]) if session.get("ended_at") else at
    start = parse_time(session["started_at"])
    if stop < start:
        raise ValueError("当前时钟早于开课时间，请先核对时钟")
    pauses = 0
    previous_end = start
    for pause in session.get("pauses", []):
        begin = parse_time(pause["start"])
        end = parse_time(pause["end"]) if pause.get("end") else stop
        if begin < previous_end or end < begin or end > stop:
            raise ValueError("暂停时段超出场次范围")
        pauses += (end - begin).total_seconds()
        previous_end = end
    return int((stop - start).total_seconds() - pauses)


def snapshot(session, policy, at):
    used = elapsed(session, at)
    budget = session.get("budget_seconds", 0)
    remaining = budget - used
    state = session.get("status", "idle")
    windows = session.get("closing_windows", policy)
    if state in ("idle", "ended", "paused"):
        phase = state
    elif remaining <= 0:
        phase = "close_now"
    elif remaining <= windows.get("stop_questions_before_end_minutes", 5) * 60:
        phase = "wrap_up"
    elif session.get("review_only") or remaining <= windows.get("stop_concepts_before_end_minutes", 10) * 60:
        phase = "consolidate"
    else:
        phase = "learn"
    return {"id": session.get("id"), "status": state, "phase": phase,
            "checked_at": at.isoformat(), "elapsed_seconds": used,
            "remaining_seconds": max(0, remaining), "overtime_seconds": max(0, -remaining),
            "started_at": session.get("started_at"), "deadline_at": session.get("deadline_at"),
            "closing_windows": {key: windows.get(key, default) for key, default in
                                (("stop_concepts_before_end_minutes", 10), ("stop_questions_before_end_minutes", 5))},
            "review_only": session.get("review_only", False)}


def transition(session, action, policy, at, minutes=None, reason=None, *, concept_window=None, question_window=None, review_only=False):
    result = copy.deepcopy(session)
    state = result.get("status", "idle")
    reason = reason.strip() if reason else None
    if action != "start" and (concept_window is not None or question_window is not None or review_only):
        raise ValueError("本场收尾窗口和纯复习模式只在 start 时设置")
    if action == "start":
        if state in ("running", "paused"):
            raise ValueError("已有未结束场次，继续时请核时或恢复，不能重新开始计时")
        if minutes is None or not math.isfinite(minutes) or round(minutes * 60) <= 0:
            raise ValueError("开课必须明确正数分钟预算")
        concepts = policy.get("stop_concepts_before_end_minutes", 10) if concept_window is None else concept_window
        questions = policy.get("stop_questions_before_end_minutes", 5) if question_window is None else question_window
        if not all(math.isfinite(value) and value >= 0 for value in (concepts, questions)) or questions >= concepts:
            raise ValueError("收尾窗口须为有限非负分钟，且新题窗口小于新概念窗口")
        if not review_only and concepts * 60 >= round(minutes * 60):
            raise ValueError("短课需先设置本场窗口：--concept-window-minutes 和 --question-window-minutes，且新概念窗口小于预算；纯复习可用 --review-only")
        result = {"id": at.strftime("%Y%m%dT%H%M%S%z"), "status": "running",
                  "started_at": at.isoformat(), "budget_seconds": round(minutes * 60),
                  "deadline_at": (at + timedelta(minutes=minutes)).isoformat(),
                  "pauses": [], "last_checked_at": at.isoformat(), "ended_at": None,
                  "finish_reason": None, "start_reason": reason, "notice_points_seen": [], "extensions": [],
                  "closing_windows": {"stop_concepts_before_end_minutes": concepts, "stop_questions_before_end_minutes": questions},
                  "review_only": review_only}
    elif action == "tick":
        if state not in ("running", "paused"):
            return result
        result["last_checked_at"] = at.isoformat()
    elif action == "pause":
        if state != "running" or not reason:
            raise ValueError("只有进行中场次可暂停，且必须记录用户明确暂停的原因")
        elapsed(result, at)
        result["pauses"].append({"start": at.isoformat(), "end": None, "reason": reason})
        result["status"] = "paused"
        result["last_checked_at"] = at.isoformat()
    elif action == "resume":
        if state != "paused":
            raise ValueError("当前场次没有暂停")
        pause = result["pauses"][-1]
        offset = at - parse_time(pause["start"])
        if offset.total_seconds() < 0:
            raise ValueError("恢复时间早于暂停时间")
        pause["end"] = at.isoformat()
        result["deadline_at"] = (parse_time(result["deadline_at"]) + offset).isoformat()
        result["status"] = "running"
        result["last_checked_at"] = at.isoformat()
    elif action == "extend":
        if state not in ("running", "paused") or not reason or minutes is None or not math.isfinite(minutes) or round(minutes * 60) <= 0:
            raise ValueError("加课需要未结束场次、用户明确授权的正数分钟和原因")
        result["budget_seconds"] += round(minutes * 60)
        result["deadline_at"] = (parse_time(result["deadline_at"]) + timedelta(minutes=minutes)).isoformat()
        result.setdefault("extensions", []).append({"at": at.isoformat(), "minutes": minutes, "reason": reason})
        result["last_checked_at"] = at.isoformat()
    elif action == "finish":
        if state == "ended":
            return result
        if state not in ("running", "paused") or not reason:
            raise ValueError("结束场次需要已开始的场次和真实收尾原因")
        if state == "paused":
            pause = result["pauses"][-1]
            result["deadline_at"] = (parse_time(result["deadline_at"]) + at - parse_time(pause["start"])).isoformat()
            pause["end"] = at.isoformat()
        result["ended_at"] = at.isoformat()
        result["last_checked_at"] = at.isoformat()
        result["status"] = "ended"
        result["finish_reason"] = reason
    else:
        raise ValueError(f"未知操作：{action}")
    info = snapshot(result, policy, at)
    notices = set(result.get("notice_points_seen", []))
    thresholds = list(policy.get("notice_elapsed_minutes", []))
    for key, before in info["closing_windows"].items():
        if result.get("review_only") and key == "stop_concepts_before_end_minutes":
            continue
        thresholds.append(max(0, result["budget_seconds"] / 60 - before))
    for point in thresholds:
        if info["elapsed_seconds"] >= point * 60:
            notices.add(point)
    result["notice_points_seen"] = sorted(notices)
    return result


def timer_markdown(session, policy):
    at = parse_time(session["last_checked_at"]) if session.get("last_checked_at") else current_time("Asia/Shanghai")
    info = snapshot(session, policy, at)
    names = {"idle": "未开课", "running": "进行中", "paused": "已暂停", "ended": "已结束"}
    rows = [("状态", names[session.get("status", "idle")]),
            ("场次", session.get("id") or "无"),
            ("实际开始", session.get("started_at") or "尚未开课"),
            ("预算", duration(session.get("budget_seconds", 0))),
            ("本场窗口", "新概念结束前 " + str(info["closing_windows"]["stop_concepts_before_end_minutes"]) + " 分钟；新题结束前 " + str(info["closing_windows"]["stop_questions_before_end_minutes"]) + " 分钟"),
            ("纯复习", "是" if session.get("review_only") else "否"),
            ("计划截止", session.get("deadline_at") or "尚未确定"),
            ("最近实际核时", session.get("last_checked_at") or "无"),
            ("已用／剩余", duration(info["elapsed_seconds"]) + "／" + duration(info["remaining_seconds"])),
            ("实际超时", duration(info["overtime_seconds"])),
            ("确认暂停", json.dumps(session.get("pauses", []), ensure_ascii=False)),
            ("实际结束", session.get("ended_at") or "尚未结束"),
            ("收尾原因", session.get("finish_reason") or "尚未收尾")]
    if session.get("daily_budget_check"):
        check = session["daily_budget_check"]
        text = "已归档 " + duration(check["known_completed_seconds"]) + "；含本场计划 " + duration(check["projected_seconds"])
        text += "；原日预算 " + duration(check["limit_seconds"]) if check["limit_seconds"] is not None else "；原日预算未设"
        text += "；超额 " + duration(check["over_budget_seconds"])
        if check["has_unknown_duration"]:
            text += "；另有未可靠计时部分"
        if check["override"]:
            text += "；本次已记录预算覆盖：" + check["reason"]
        rows.append(("最近预算检查", text + "（" + check["checked_at"] + "）"))
    return "# 学习计时\n\n由 data/session.json 生成；核时发生在处理消息时，不是后台闹钟。\n\n| 项目 | 当前值 |\n|---|---|\n" + "\n".join(f"| {k} | {str(v).replace('|', '／')} |" for k, v in rows) + "\n"


def archive_finished(root, session, policy):
    progress = read_json(root, "data/progress.json")
    sessions = progress.setdefault("sessions", [])
    previous = next((item for item in sessions if item["id"] == session["id"]), None)
    if previous == session:
        return
    day = parse_time(session["started_at"]).date().isoformat()
    already_archived = sum(elapsed(item, parse_time(item["ended_at"])) for item in sessions
                           if parse_time(item["started_at"]).date().isoformat() == day)
    sessions[:] = [item for item in sessions if item["id"] != session["id"]]
    sessions.append(session)
    days = progress.setdefault("days", [])
    entry = next((item for item in days if item["date"] == day), None)
    if entry is None:
        entry = {"date": day, "title": "学习场次已结束，内容待据实补记", "record": None, "duration_seconds": 0}
        days.append(entry)
    elif entry.get("duration_seconds") is None:
        entry["has_unknown_duration"] = True
    older_known = max(0, (entry.get("duration_seconds") or 0) - already_archived)
    entry["duration_seconds"] = older_known + sum(elapsed(item, parse_time(item["ended_at"])) for item in sessions if parse_time(item["started_at"]).date().isoformat() == day)
    progress["updated_at"] = session["ended_at"]
    write_json(root, "data/progress.json", progress)


def check_daily_budget(course, progress, at, minutes, reason, *, session=None, override=False):
    if minutes is None or not math.isfinite(minutes) or round(minutes * 60) <= 0:
        raise ValueError("预算必须为正数分钟")
    day_id = parse_time(session["started_at"]).date().isoformat() if session else at.date().isoformat()
    day = next((d for d in progress.get("days", []) if d["date"] == day_id), None)
    archived = sum(elapsed(s, parse_time(s["ended_at"])) for s in progress.get("sessions", [])
                   if s.get("status") == "ended" and parse_time(s["started_at"]).date().isoformat() == day_id
                   and (session is None or s["id"] != session["id"]))
    known = max((day.get("duration_seconds") or 0) if day else 0, archived)
    unknown = bool(day and (day.get("duration_seconds") is None or day.get("has_unknown_duration")))
    planned = round(minutes * 60)
    if session:
        planned = max(session["budget_seconds"] + planned, elapsed(session, at))
    day_at = parse_time(session["started_at"]) if session else at
    key = "weekend_minutes" if day_at.weekday() >= 5 else "weekday_minutes"
    limit = course["time_policy"].get(key)
    limit_seconds = round(limit * 60) if limit is not None else None
    over = max(0, known + planned - limit_seconds) if limit_seconds is not None else 0
    reason = reason.strip() if reason else None
    report = {"date": day_id, "known_completed_seconds": known, "has_unknown_duration": unknown,
              "limit_seconds": limit_seconds, "planned_session_seconds": planned,
              "projected_seconds": known + planned, "over_budget_seconds": over,
              "override": bool(override), "reason": reason, "checked_at": at.isoformat(),
              "requested_minutes": minutes}
    detail = "已确认用时 " + duration(known) + "；含本场计划 " + duration(known + planned)
    if limit_seconds is not None:
        detail += "；原日预算 " + duration(limit_seconds) + "；超出 " + duration(over)
    if override and not reason:
        raise ValueError("预算覆盖必须用 --reason 记录用户明确授权的本次时长。" + detail)
    if (over or unknown) and not (override and reason):
        message = "同日另有未可靠计时部分。" if unknown else "本次将超过当天原预算。"
        raise ValueError(message + detail + "。已有明确的本次时长授权时，用 --override-daily-budget --reason 记录；普通原因备注不覆盖预算")
    return report


def main():
    parser = argparse.ArgumentParser(description="使用实际系统时钟维护学习场次，不接受虚构历史起点")
    parser.add_argument("--root", default=".")
    parser.add_argument("action", choices=["start", "tick", "pause", "resume", "extend", "finish", "status"])
    parser.add_argument("--minutes", type=float)
    parser.add_argument("--reason")
    parser.add_argument("--override-daily-budget", action="store_true", help="已有用户明确时长授权时，覆盖本次超额或未知剩余额度检查")
    parser.add_argument("--concept-window-minutes", type=float)
    parser.add_argument("--question-window-minutes", type=float)
    parser.add_argument("--review-only", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    course = read_json(root, "data/course.json")
    session = read_json(root, "data/session.json", {"status": "idle"})
    at = current_time(course["timezone"])
    try:
        if args.override_daily_budget and args.action not in ("start", "extend"):
            raise ValueError("预算覆盖只适用于 start 和 extend")
        seen_before = set(session.get("notice_points_seen", []))
        previous = session
        if args.action != "status":
            session = transition(session, args.action, course["time_policy"], at, args.minutes, args.reason,
                                 concept_window=args.concept_window_minutes, question_window=args.question_window_minutes,
                                 review_only=args.review_only)
            if args.action in ("start", "extend"):
                budget_check = check_daily_budget(course, read_json(root, "data/progress.json"), at, args.minutes, args.reason,
                                                 session=previous if args.action == "extend" else None,
                                                 override=args.override_daily_budget)
                session["daily_budget_check"] = budget_check
                if args.action == "extend":
                    session["extensions"][-1]["daily_budget_check"] = budget_check
            elif args.override_daily_budget:
                raise ValueError("预算覆盖只适用于 start 和 extend")
            write_json(root, "data/session.json", session)
            if session["status"] == "ended" and args.action == "finish":
                archive_finished(root, session, course["time_policy"])
            write_text(root / "学习计时.md", timer_markdown(session, course["time_policy"]))
            from render import render_outputs
            for relative, content in render_outputs(root).items():
                write_text(root / relative, content)
        info = snapshot(session, course["time_policy"], at)
        if session.get("daily_budget_check"):
            info["daily_budget_check"] = session["daily_budget_check"]
        info["crossed_notice_minutes"] = sorted(set(session.get("notice_points_seen", [])) - seen_before)
        print(json.dumps(info, ensure_ascii=False, indent=2))
    except (ValueError, KeyError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
