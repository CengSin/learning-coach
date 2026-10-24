from datetime import date
import re

from common import project_path


DIMENSIONS = {
    "讲解": {"已讲解"},
    "作答": {"提示后正确", "独立正确", "当场订正", "未通过"},
    "延迟保持": {"隔日正确", "未通过"},
    "迁移": {"迁移正确", "未通过"},
}
REVIEW_STATUSES = {"待复查", "待隔日检查", "已安排", "已完成"}
HISTORY_NOTICE = "历史记录，未按新规则核验"
FACT_BASES = {"已对照教材原页", "来自官方题目答案", "模型自己的知识，未核对", "推断"}
CONFIRMED_BASES = {"已对照教材原页", "来自官方题目答案"}
ASSESSMENT_MODES = {"独立作答", "提示后作答", "闭卷复述", "练习"}
REFERENCE_CHECK_METHOD = "对照教材原页／官方答案"


def question_kind(question):
    provenance = question.get("provenance")
    if provenance == "自编" or question.get("adapted") is True:
        return "self_authored"
    if provenance in ("官方公开", "官方", "真题", "官方真题"):
        return "official_or_past"
    return "unknown" if provenance in (None, "", "未知") else "other"


def reference_check_located(method):
    parts = re.split("[：:]", method, maxsplit=1)
    if len(parts) != 2 or parts[0].strip() != REFERENCE_CHECK_METHOD:
        return False
    details = parts[1].strip()
    position = (r"第\s*[\d一二三四五六七八九十百千零〇两IVXivx.]+\s*[章节页题]"
                r"|(?:章节?|页码?|题目?|pages?|pp?\.?)\s*\d+(?:\.\d+)*|#page=\d+")
    source = re.sub(position, "", details, flags=re.IGNORECASE)
    return bool(re.search(position, details, re.IGNORECASE) and re.search(r"[^\W\d_]", source))


def independently_checked(question):
    check = question.get("independent_check")
    if not isinstance(check, dict) or not isinstance(check.get("method"), str):
        return False
    method = re.split("[：:]", check["method"], maxsplit=1)[0].strip()
    valid_method = method in ("工具复算", "另一种解法")
    if check["method"].strip().startswith(REFERENCE_CHECK_METHOD):
        valid_method = reference_check_located(check["method"])
    return valid_method and check.get("result") == "一致"


def grading_checked(assessment):
    check = assessment.get("grading_check")
    return isinstance(check, dict) and all(isinstance(check.get(key), str) and check[key].strip()
                                           for key in ("method", "result"))


def assessment_state(event, questions=None):
    if event.get("dimension") == "讲解":
        return "teaching"
    if "assessment" not in event:
        return "historical"
    assessment = event["assessment"]
    if not isinstance(assessment, dict):
        return "practice"
    mode = assessment.get("mode")
    if not isinstance(mode, str) or mode not in ASSESSMENT_MODES or mode == "练习" or assessment.get("without_explanation") is not True:
        return "practice"
    if mode == "提示后作答" and event.get("status") in ("独立正确", "隔日正确", "迁移正确"):
        return "practice"
    if assessment.get("reasonable_disagreement") is True:
        if not grading_checked(assessment) or assessment["grading_check"]["result"].strip() == "待核对":
            return "practice"
    ids = assessment.get("question_ids", [])
    if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
        return "practice"
    if mode == "闭卷复述":
        gaps = assessment.get("recall_gaps")
        if not isinstance(gaps, list) or any(not isinstance(value, str) or not value.strip() for value in gaps):
            return "practice"
    elif not ids:
        return "practice"
    lookup = questions or {}
    for question_id in ids:
        question = lookup.get(question_id)
        if question is None or question.get("exposure", {}).get("answered") is False or (
                question_kind(question) == "self_authored" and not independently_checked(question)):
            return "practice"
    return "eligible"


def evidence_label(event, questions=None):
    label = event["dimension"] + "：" + event["status"]
    if "assessment" not in event:
        return label + "（" + HISTORY_NOTICE + "）"
    state = assessment_state(event, questions)
    if state == "practice":
        return label + "（练习记录，不计掌握依据）"
    if state == "teaching":
        return label + "（教学活动，不计掌握依据）"
    return label


def knowledge_view(entry, questions=None):
    history = entry.get("evidence_history", [])
    latest, practice = {}, []
    for event in sorted(history, key=lambda event: event["date"]):
        if assessment_state(event, questions) == "practice":
            practice.append(event)
            continue
        latest[event["dimension"]] = event
    evidence = [latest[key] for key in DIMENSIONS if key in latest]
    legacy = entry.get("legacy_summary")
    if not history and not legacy and entry.get("status") not in (None, "未学"):
        legacy = {key: entry.get(key) for key in ("status", "evidence", "last_verified", "source")}
    status = "；".join(evidence_label(event, questions) for event in evidence)
    if not status:
        if legacy:
            status = HISTORY_NOTICE + "；历史摘要：" + str(legacy.get("status", "待拆分"))
        else:
            status = "尚无可用作答依据" if practice else "未学"
    if practice:
        status += "；另有练习记录，不计掌握依据"
    return {"status": status, "evidence": evidence, "history": history,
            "legacy": legacy, "practice": practice,
            "review_status": entry.get("review_status") or "未登记待查事项"}


def mastery_basis(progress, questions):
    lookup = {question["id"]: question for question in questions}
    included = set()
    for entry in progress.get("knowledge", []):
        for event in entry.get("evidence_history", []):
            if assessment_state(event, lookup) == "eligible":
                included.update(event["assessment"].get("question_ids", []))
    counts = dict.fromkeys(("self_authored", "official_or_past", "other", "unknown"), 0)
    for question_id in included:
        counts[question_kind(lookup[question_id])] += 1
    denominator = len(included)
    ratio = counts["self_authored"] / denominator if denominator else None
    summary = (f"自编题 {counts['self_authored']}/{denominator}（{ratio:.1%}）；"
               f"官方／真题 {counts['official_or_past']}，其他 {counts['other']}，未知 {counts['unknown']}"
               if denominator else "无可统计依据")
    return {"denominator": denominator, **counts, "self_authored_ratio": ratio, "summary": summary}


def pending_facts(progress):
    result, seen = [], set()
    stored = progress.get("pending_checks", [])
    planned = progress.get("next_session", {}).get("key_facts", [])
    candidates = list(stored) if isinstance(stored, list) else []
    candidates += [fact for fact in (planned if isinstance(planned, list) else [])
                   if isinstance(fact, dict) and (not isinstance(fact.get("basis"), str) or fact["basis"] not in CONFIRMED_BASES)]
    for fact in candidates:
        if not isinstance(fact, dict):
            continue
        key = tuple(str(fact.get(field) or "") for field in ("content", "basis", "location"))
        if key not in seen:
            seen.add(key)
            result.append(fact)
    return result


def accuracy_report(progress, questions):
    warnings = []

    def warn(code, location, message):
        warnings.append({"code": code, "location": location, "message": message})

    plan = progress.get("next_session", {})
    facts = plan.get("key_facts", [])
    if not isinstance(facts, list) or (not facts and (plan.get("objectives") or plan.get("segments"))):
        warn("KEY_FACTS_MISSING", "next_session.key_facts", "小纲缺少本次关键事实清单")
        facts = []
    for index, fact in enumerate(facts):
        location = f"next_session.key_facts[{index}]"
        if not isinstance(fact, dict) or not isinstance(fact.get("basis"), str) or fact["basis"] not in FACT_BASES:
            warn("KEY_FACT_BASIS", location, "关键事实缺少有效依据等级")
        if not isinstance(fact, dict):
            continue
        if not isinstance(fact.get("content"), str) or not fact["content"].strip():
            warn("KEY_FACT_CONTENT", location, "关键事实缺少内容")
        if isinstance(fact.get("basis"), str) and fact["basis"] in CONFIRMED_BASES and (not isinstance(fact.get("location"), str)
                or fact["location"].strip() in ("", "未知", "待定位", "待核对", "未确认")):
            warn("KEY_FACT_LOCATION", location, "已核验事实缺少教材章节／页码或官方答案位置")
    connections = plan.get("connections", [])
    if ("key_facts" in plan and "connections" not in plan) or not isinstance(connections, list):
        warn("CONNECTION_FIELDS", "next_session.connections", "请明确知识关联；没有可靠关联可用空列表")
        connections = []
    for index, connection in enumerate(connections):
        if not isinstance(connection, dict) or any(not isinstance(connection.get(key), str) or not connection[key].strip()
                for key in ("prior_knowledge", "similarities", "limits")):
            warn("CONNECTION_FIELDS", f"next_session.connections[{index}]", "知识关联须有既有知识、相同点和类比不成立之处")
    lookup = {question["id"]: question for question in questions}
    historical_events = historical_mistakes = historical_summaries = 0
    for question in questions:
        check = question.get("independent_check")
        method = check.get("method") if isinstance(check, dict) else None
        if isinstance(method, str) and method.strip().startswith(REFERENCE_CHECK_METHOD) and not reference_check_located(method):
            warn("REFERENCE_CHECK_LOCATION", f"questions[{question['id']}].independent_check.method",
                 "对照资料的方法须在冒号后写明资料名及章节／页码，或题目和答案出处")
    for entry in progress.get("knowledge", []):
        historical_summaries += bool(entry.get("legacy_summary"))
        historical_events += sum("assessment" not in event and event.get("dimension") != "讲解"
                                 for event in entry.get("evidence_history", []))
    records = [(event, False, f"knowledge[{entry['id']}].evidence_history[{event['id']}]")
               for entry in progress.get("knowledge", []) for event in entry.get("evidence_history", [])]
    records += [(mistake, True, f"mistakes[{mistake['id']}]") for mistake in progress.get("mistakes", [])]
    for record, is_mistake, location in records:
        if "assessment" not in record:
            historical_mistakes += is_mistake
            continue
        assessment = record["assessment"]
        if not isinstance(assessment, dict):
            warn("ASSESSMENT_FIELDS", location, "assessment 应为作答条件记录")
            if is_mistake:
                warn("GRADING_RECHECK_MISSING", location, "新错题缺少复核方法、结果")
            continue
        if (is_mistake or assessment.get("reasonable_disagreement") is True) and not grading_checked(assessment):
            warn("GRADING_RECHECK_MISSING", location, "新错题或合理分歧缺少复核方法、结果")
        if record.get("dimension") == "讲解":
            continue
        mode = assessment.get("mode")
        if not isinstance(mode, str) or mode not in ASSESSMENT_MODES or not isinstance(assessment.get("without_explanation"), bool):
            warn("ASSESSMENT_FIELDS", location, "请记录作答方式及是否不看讲解／答案")
        if mode == "提示后作答" and record.get("status") in ("独立正确", "隔日正确", "迁移正确"):
            warn("ASSESSMENT_FIELDS", location, "提示后作答不能标为独立正确或独立保持／迁移")
        if mode == "闭卷复述" and (not isinstance(assessment.get("recall_gaps"), list)
                or any(not isinstance(gap, str) or not gap.strip() for gap in assessment.get("recall_gaps", []))):
            warn("RECALL_GAPS", location, "闭卷复述仅记录遗漏／错误的关键事实，全对用空列表")
        ids = assessment.get("question_ids", [])
        if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
            warn("QUESTION_LINK", location, "question_ids 应为题目ID列表")
            continue
        if not ids and mode not in ("闭卷复述", "练习"):
            warn("QUESTION_LINK", location, "题目作答缺少题目关联；纯复述不必建题")
        for question_id in set(ids):
            question = lookup.get(question_id)
            if question is None:
                warn("QUESTION_LINK", location, "题目不存在：" + question_id)
                continue
            if question.get("exposure", {}).get("answered") is False:
                warn("QUESTION_LINK", location, "作答事件与题目未作答标记矛盾：" + question_id)
            if question_kind(question) == "self_authored" and not independently_checked(question) and (
                    is_mistake or assessment.get("reasonable_disagreement") is True or mode != "练习"):
                warn("UNCHECKED_SELF_AUTHORED", location, "自编或改编题无有效独立复算，仅可练习：" + question_id)
    if historical_events or historical_mistakes or historical_summaries:
        warn("HISTORICAL_UNVERIFIED", "progress", HISTORY_NOTICE +
             f"：{historical_events} 条作答事件、{historical_summaries} 份摘要、{historical_mistakes} 条错题；原复习安排保留")
    return {"warnings": warnings, "mastery_basis": mastery_basis(progress, questions)}


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
