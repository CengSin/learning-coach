import argparse
import copy
import json
import math
from datetime import timedelta
from pathlib import Path

from common import current_time, duration, parse_time, read_json, write_json, write_text

NEXT_ACTIONS = {
    "learn": "按本场目标转段；新授课跨主题合计回顾用时，前置已够或回顾预算已到就同轮进入新内容。当日简纲完成后接下一计划日的内容。先判断剩余时间能否容纳最小完整单元，放不下则连接整理已学内容，不塞零散概念、不等用户说继续",
    "consolidate": "不再引入新概念；在原额度内收束当前题并连接已学内容，不为交回话轮追加确认题或复述",
    "wrap_up": "不再发新题；连接总结已学内容，到截止明确结束课堂",
    "close_now": "已到截止，立即收尾：先明确结束课堂，交代知识连接、待验证事项与下次入口，再结束计时、归档和交付文件",
}


def validate_closing_windows(mode, concepts, questions, budget_seconds, review_only=False):
    if mode not in ("windows", "deadline"):
        raise ValueError("end_mode 须为 windows 或 deadline")
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) and value >= 0 for value in (concepts, questions)):
        raise ValueError("收尾窗口须为有限非负分钟")
    if mode == "deadline":
        if concepts != 0 or questions != 0:
            raise ValueError("deadline 模式不设置提前窗口，本场两个窗口须均为0")
    elif questions >= concepts:
        raise ValueError("新题窗口须小于新概念窗口")
    elif not review_only and concepts * 60 >= budget_seconds:
        raise ValueError("短课需先设置本场窗口：--concept-window-minutes 和 --question-window-minutes，且新概念窗口小于预算；或用 --end-mode deadline；纯复习可用 --review-only")


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


def review_used(clock, elapsed_seconds):
    started = clock.get("started_elapsed")
    active = elapsed_seconds - started if started is not None else 0
    return clock["completed_seconds"] + active


def snapshot(session, policy, at):
    used = elapsed(session, at)
    budget = session.get("budget_seconds", 0)
    remaining = budget - used
    state = session.get("status", "idle")
    windows = session.get("closing_windows", policy)
    mode = session.get("end_mode", "windows")
    if state in ("idle", "ended", "paused"):
        phase = state
    elif remaining <= 0:
        phase = "close_now"
    elif mode == "deadline":
        phase = "consolidate" if session.get("review_only") else "learn"
    elif remaining <= windows.get("stop_questions_before_end_minutes", 5) * 60:
        phase = "wrap_up"
    elif session.get("review_only") or remaining <= windows.get("stop_concepts_before_end_minutes", 10) * 60:
        phase = "consolidate"
    else:
        phase = "learn"
    info = {"id": session.get("id"), "status": state, "phase": phase, "end_mode": mode,
            "checked_at": at.isoformat(), "elapsed_seconds": used,
            "remaining_seconds": max(0, remaining), "overtime_seconds": max(0, -remaining),
            "started_at": session.get("started_at"), "deadline_at": session.get("deadline_at"),
            "closing_windows": {key: windows.get(key, default) for key, default in
                                (("stop_concepts_before_end_minutes", 10), ("stop_questions_before_end_minutes", 5))},
            "review_only": session.get("review_only", False),
            "next_action": NEXT_ACTIONS.get(phase, "当前没有进行中的场次")}
    if "review_clock" in session:
        clock = session["review_clock"]
        spent = review_used(clock, used)
        left = max(0, clock["budget_seconds"] - spent)
        info["review"] = {"budget_seconds": clock["budget_seconds"], "elapsed_seconds": spent,
                          "remaining_seconds": left, "active": clock.get("started_elapsed") is not None}
        if phase == "learn" and left == 0:
            info["next_action"] = "本场回顾额度已用完，收束当前题，在本轮进入或继续新课主线；余时放不下新单元时整理已学连接。"
    return info


def transition(session, action, policy, at, minutes=None, reason=None, *, concept_window=None, question_window=None, review_only=False, end_mode=None,
               review_minutes=None, segment=None):
    result = copy.deepcopy(session)
    state = result.get("status", "idle")
    reason = reason.strip() if reason else None
    if action != "start" and (concept_window is not None or question_window is not None or review_only or end_mode is not None):
        raise ValueError("本场收尾窗口、结束模式和纯复习模式只在 start 时设置")
    if review_minutes is not None and action != "start":
        raise ValueError("回顾预算在开课时设置")
    if segment is not None and (segment not in ("review", "mainline") or action not in ("start", "tick")
                                or (action == "tick" and state != "running")):
        raise ValueError("仅在开课或进行中核时时标记下一段")
    if action == "start":
        if state in ("running", "paused"):
            raise ValueError("已有未结束场次，继续时请核时或恢复，不能重新开始计时")
        if minutes is None or not math.isfinite(minutes) or round(minutes * 60) <= 0:
            raise ValueError("开课必须明确正数分钟预算")
        mode = end_mode if end_mode is not None else policy.get("end_mode", "windows")
        if mode == "deadline":
            concepts = 0 if concept_window is None else concept_window
            questions = 0 if question_window is None else question_window
        else:
            concepts = policy.get("stop_concepts_before_end_minutes", 10) if concept_window is None else concept_window
            questions = policy.get("stop_questions_before_end_minutes", 5) if question_window is None else question_window
        validate_closing_windows(mode, concepts, questions, round(minutes * 60), review_only)
        result = {"id": at.strftime("%Y%m%dT%H%M%S%z"), "status": "running",
                  "started_at": at.isoformat(), "budget_seconds": round(minutes * 60),
                  "deadline_at": (at + timedelta(minutes=minutes)).isoformat(),
                  "pauses": [], "last_checked_at": at.isoformat(), "ended_at": None,
                  "finish_reason": None, "start_reason": reason, "notice_points_seen": [], "extensions": [],
                  "closing_windows": {"stop_concepts_before_end_minutes": concepts, "stop_questions_before_end_minutes": questions},
                  "review_only": review_only, "end_mode": mode}
        if review_minutes is not None:
            if review_only or not math.isfinite(review_minutes) or not 0 <= review_minutes <= minutes:
                raise ValueError("常规课的回顾预算须在零与本场分钟数之间")
            result["review_clock"] = {"budget_seconds": round(review_minutes * 60), "completed_seconds": 0,
                                      "started_elapsed": None}
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
            result["deadline_at"] = (parse_time(result["deadline_at"]) + (at - parse_time(pause["start"]))).isoformat()
            pause["end"] = at.isoformat()
        result["ended_at"] = at.isoformat()
        result["last_checked_at"] = at.isoformat()
        result["status"] = "ended"
        result["finish_reason"] = reason
    else:
        raise ValueError(f"未知操作：{action}")
    if segment is not None:
        clock = result.get("review_clock")
        if clock is None:
            raise ValueError("标记课堂分段需在 start 时指定 --review-minutes")
        used = elapsed(result, at)
        clock["completed_seconds"] = review_used(clock, used)
        clock["started_elapsed"] = used if segment == "review" else None
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
    if "review" in info:
        review = info["review"]
        rows.append(("常规课回顾已用／预算", duration(review["elapsed_seconds"]) + "／" + duration(review["budget_seconds"])))
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
    if "end_mode" in session:
        rows.append(("结束模式", "严格到点，转段仍须判断剩余容量" if info["end_mode"] == "deadline" else "按本场窗口收束，到点结束"))
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
    parser.add_argument("--end-mode", choices=("windows", "deadline"), help="仅本场：按窗口收束或严格到点；不修改课程配置")
    parser.add_argument("--review-minutes", type=float, help="start 时按小纲设置常规课全场回顾分钟，可为零")
    parser.add_argument("--segment", choices=("mainline", "review"), help="start 或 tick 时标记从现在起的教学分段")
    parser.add_argument("--review-only", action="store_true")
    parser.add_argument("--user-stop", action="store_true", help="用户明确要求提前结束（休息、有事、疲惫）时才可在预算未用完前收尾")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    course = read_json(root, "data/course.json")
    session = read_json(root, "data/session.json", {"status": "idle"})
    at = current_time(course["timezone"])
    try:
        if args.action == "status" and (args.segment is not None or args.review_minutes is not None):
            raise ValueError("status 只读；设置回顾预算用 start，切换分段用 tick")
        if args.override_daily_budget and args.action not in ("start", "extend"):
            raise ValueError("预算覆盖只适用于 start 和 extend")
        seen_before = set(session.get("notice_points_seen", []))
        previous = session
        if args.action == "finish" and not args.user_stop:
            early = snapshot(session, course["time_policy"], at)
            if early["status"] == "running" and early["remaining_seconds"] > 0 and not session.get("review_only"):
                raise ValueError("预算未用完：剩余 " + duration(early["remaining_seconds"]) + "。按主线继续，当日简纲完成则接下一计划日的内容；"
                                 "单元闭卷复述不是当天收尾。只有用户明确要求停止时加 --user-stop 并在 --reason 写明用户原话")
        if args.action != "status":
            session = transition(session, args.action, course["time_policy"], at, args.minutes, args.reason,
                                 concept_window=args.concept_window_minutes, question_window=args.question_window_minutes,
                                 review_only=args.review_only, end_mode=args.end_mode,
                                 review_minutes=args.review_minutes, segment=args.segment)
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
