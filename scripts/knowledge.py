from datetime import date

from common import project_path


DIMENSIONS = {
    "讲解": {"已讲解"},
    "作答": {"提示后正确", "独立正确", "当场订正", "未通过"},
    "延迟保持": {"隔日正确", "未通过"},
    "迁移": {"迁移正确", "未通过"},
}
REVIEW_STATUSES = {"待复查", "待隔日检查", "已安排", "已完成"}


def knowledge_view(entry):
    history = entry.get("evidence_history", [])
    latest = {}
    for event in sorted(history, key=lambda event: event["date"]):
        latest[event["dimension"]] = event
    evidence = [latest[key] for key in DIMENSIONS if key in latest]
    legacy = entry.get("legacy_summary")
    if not history and not legacy and entry.get("status") not in (None, "未学"):
        legacy = {key: entry.get(key) for key in ("status", "evidence", "last_verified", "source")}
    status = "；".join(event["dimension"] + "：" + event["status"] for event in evidence)
    if not status:
        status = "历史摘要：" + str(legacy.get("status", "待拆分")) if legacy else "未学"
    return {"status": status, "evidence": evidence, "history": history,
            "legacy": legacy, "review_status": entry.get("review_status") or "未登记待查事项"}


def validate_knowledge(root, entries):
    errors = []
    for entry in entries:
        label = "知识点 " + str(entry.get("id", "缺少ID")) + "："
        if "status" in entry and entry["status"] != "未学":
            errors.append(label + "请将旧状态和出处保留为 legacy_summary，新增验证写入 evidence_history")
        if entry.get("review_status") is not None and entry["review_status"] not in REVIEW_STATUSES:
            errors.append(label + "无效复查状态")
        history = entry.get("evidence_history", [])
        if entry.get("status") == "未学" and (history or entry.get("legacy_summary")):
            errors.append(label + "未学不能与学习证据或历史摘要并存")
        if not isinstance(history, list):
            errors.append(label + "evidence_history 应为列表")
            continue
        events, dates = {}, {}
        for event in history:
            if not isinstance(event, dict) or not event.get("id"):
                errors.append(label + "验证事件必须有唯一ID")
                continue
            event_id = event["id"]
            if event_id in events:
                errors.append(label + "验证事件ID重复：" + event_id)
            events[event_id] = event
            dimension = event.get("dimension")
            if dimension not in DIMENSIONS or event.get("status") not in DIMENSIONS.get(dimension, set()):
                errors.append(label + "验证维度或状态无效：" + event_id)
            try:
                day = date.fromisoformat(event["date"])
                if day.isoformat() != event["date"]:
                    raise ValueError("日期格式")
                dates[event_id] = day
            except (ValueError, TypeError, KeyError):
                errors.append(label + "验证日期应为项目时区的 YYYY-MM-DD：" + event_id)
            if not isinstance(event.get("summary"), str) or not event["summary"].strip():
                errors.append(label + "验证缺少实际作答摘要：" + event_id)
            if not event.get("source") or not project_path(root, event["source"]).is_file():
                errors.append(label + "验证缺少存在的证据文件：" + event_id)
        for event_id, event in events.items():
            if event.get("dimension") != "延迟保持":
                continue
            baseline = event.get("baseline_id")
            if baseline not in events or baseline not in dates or event_id not in dates:
                errors.append(label + "延迟保持需要同知识点的基线事件：" + event_id)
            elif dates[event_id] <= dates[baseline]:
                errors.append(label + "延迟保持验证必须晚于基线的学习日期：" + event_id)
        legacy = entry.get("legacy_summary")
        if legacy is not None:
            if not isinstance(legacy, dict) or not isinstance(legacy.get("status"), str):
                errors.append(label + "legacy_summary 应保留原状态、原摘要及出处")
            elif legacy.get("source") and not project_path(root, legacy["source"]).is_file():
                errors.append(label + "历史摘要证据文件不存在")
    return errors


def migrate_knowledge(progress):
    count = 0
    for entry in progress.get("knowledge", []):
        if "status" not in entry:
            continue
        if entry["status"] == "未学":
            entry.setdefault("evidence_history", [])
            continue
        if "legacy_summary" in entry:
            raise ValueError("知识点同时有旧状态和 legacy_summary，请先人工核对：" + entry["id"])
        old_status = entry["status"]
        entry["legacy_summary"] = {key: entry.pop(key, None) for key in ("status", "evidence", "last_verified", "source")}
        entry.setdefault("evidence_history", [])
        if entry.get("review_status") is None:
            if "隔日待查" in old_status or "待隔日" in old_status:
                entry["review_status"] = "待隔日检查"
            elif "待" in old_status:
                entry["review_status"] = "待复查"
        count += 1
    return count
