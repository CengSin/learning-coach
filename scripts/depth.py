"""检查备课额度与单主题实时配额；不写课程数据，不推断掌握或自动控制课堂。"""
import copy
import math
from collections import defaultdict
from datetime import datetime


DEFAULT_LEVELS = {
    "了解": dict(minutes=15, lectures=1, exercises=1, review_sessions=1, review_exercises=1, review_minutes=10),
    "熟悉": dict(minutes=25, lectures=1, exercises=2, review_sessions=1, review_exercises=1, review_minutes=15),
    "掌握": dict(minutes=35, lectures=2, exercises=3, review_sessions=2, review_exercises=2, review_minutes=15),
    "熟练掌握": dict(minutes=40, lectures=2, exercises=3, review_sessions=3, review_exercises=4, review_minutes=20),
}
FIRST_FIELDS = ("minutes", "lectures", "exercises")
USAGE_FIELDS = (*FIRST_FIELDS, "sessions")


def number(value, *, integer=False, positive=False):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and (value > 0 if positive else value >= 0)
            and (not integer or isinstance(value, int)))


def present(value):
    return isinstance(value, str) and bool(value.strip())


def level_limits(policy):
    levels = copy.deepcopy(DEFAULT_LEVELS)
    for name, overrides in policy.get("levels", {}).items():
        levels[name].update(overrides)
    return levels


def topic_limits(topic, levels):
    limits = {key: levels[topic["level"]][key] for key in FIRST_FIELDS}
    limits.update(topic.get("limits", {}))
    return limits


def phase_limits(topic, phase, levels):
    if phase == "first_pass":
        return topic_limits(topic, levels)
    profile = levels[topic["level"]]
    return dict(minutes=profile["review_sessions"] * profile["review_minutes"],
                lectures=profile["review_sessions"],
                exercises=profile["review_exercises"],
                sessions=profile["review_sessions"])


def usage_key(topic_id, phase):
    return f"{topic_id}::{phase}"


def recorded_usage(progress, topic_id, phase):
    usage = progress.get("depth_usage")
    if not isinstance(usage, dict):
        return None
    record = usage.get(usage_key(topic_id, phase))
    if not isinstance(record, dict):
        return None
    totals = record.get("totals")
    if not isinstance(totals, dict):
        return None
    return record


def topic_first_pass_closed(topic, progress):
    topic_id = topic["id"]
    record = recorded_usage(progress, topic_id, "first_pass")
    if record and record.get("closed") is True:
        return True
    linked_ids = topic.get("knowledge_ids")
    candidate_ids = set(linked_ids) if isinstance(linked_ids, list) and linked_ids else {topic_id}
    for entry in progress.get("knowledge", []):
        if isinstance(entry, dict) and entry.get("id") in candidate_ids and entry.get("first_pass_closed") is True:
            return True
    return False


def summarize_log_usage(entries):
    """从课堂原始日志按 (topic_id, phase) 聚合单日/单场消耗。"""
    grouped = defaultdict(lambda: {"lectures": 0, "exercises": 0, "timestamps": [], "close_first_pass": False})
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        topic_id = entry.get("topic_id")
        if not present(topic_id):
            continue
        phase = entry.get("depth_phase") or "first_pass"
        if phase not in ("first_pass", "practice"):
            continue
        bucket = grouped[(topic_id, phase)]
        kind = entry.get("kind")
        if kind == "teach":
            bucket["lectures"] += 1
        elif kind in ("question", "recall"):
            bucket["exercises"] += 1
        if entry.get("close_first_pass") is True and phase == "first_pass":
            bucket["close_first_pass"] = True
        at = entry.get("at")
        if isinstance(at, str):
            try:
                bucket["timestamps"].append(datetime.fromisoformat(at))
            except ValueError:
                pass
    result = {}
    for key, bucket in grouped.items():
        minutes = 0.0
        if len(bucket["timestamps"]) >= 2:
            span = (max(bucket["timestamps"]) - min(bucket["timestamps"])).total_seconds()
            minutes = round(max(0.0, span) / 60.0, 1)
        result[key] = {
            "minutes": minutes,
            "lectures": bucket["lectures"],
            "exercises": bucket["exercises"],
            "sessions": 1,
            "close_first_pass": bucket["close_first_pass"],
        }
    return result


def topic_quota_status(course, progress, entries, topic_id, phase="first_pass"):
    """计算当前课堂中指定主题的实时配额消耗与是否触达上限。"""
    policy = course.get("depth_policy")
    if not isinstance(policy, dict) or not policy.get("enabled"):
        return None
    topics = {t["id"]: t for t in policy.get("topics", []) if isinstance(t, dict) and present(t.get("id"))}
    if topic_id not in topics or phase not in ("first_pass", "practice"):
        return None
    levels = level_limits(policy)
    topic = topics[topic_id]
    stage_caps = phase_limits(topic, phase, levels)
    session_usage = summarize_log_usage(entries).get((topic_id, phase), dict(minutes=0.0, lectures=0, exercises=0, sessions=0))
    prior_record = recorded_usage(progress, topic_id, phase)
    prior_totals = prior_record["totals"] if prior_record else dict(minutes=0.0, lectures=0, exercises=0, sessions=0)

    # Also inspect planned single-session cap in next_session.depth_plan if present
    plan_items = [
        item for item in progress.get("next_session", {}).get("depth_plan", {}).get("items", [])
        if isinstance(item, dict) and item.get("topic_id") == topic_id and item.get("phase") == phase
    ]
    plan_caps = None
    if plan_items:
        plan_caps = {
            field: sum(item.get(field, 0) for item in plan_items if number(item.get(field)))
            for field in FIRST_FIELDS
        }
        # If prior_record is empty, fall back to max used_before snapshot from plan_items
        if not prior_record:
            for item in plan_items:
                used = item.get("used_before")
                if isinstance(used, dict):
                    for field in FIRST_FIELDS:
                        if number(used.get(field)):
                            prior_totals[field] = max(prior_totals[field], used[field])

    cumulative = {
        "minutes": round(prior_totals.get("minutes", 0) + session_usage["minutes"], 1),
        "lectures": int(prior_totals.get("lectures", 0) + session_usage["lectures"]),
        "exercises": int(prior_totals.get("exercises", 0) + session_usage["exercises"]),
    }
    exceeded = []
    for field in FIRST_FIELDS:
        if cumulative[field] >= stage_caps[field]:
            exceeded.append(f"累计{field}({cumulative[field]}/{stage_caps[field]})")
        elif plan_caps and plan_caps[field] > 0 and session_usage[field] >= plan_caps[field]:
            exceeded.append(f"本场{field}({session_usage[field]}/{plan_caps[field]})")
    return {
        "topic_id": topic_id,
        "phase": phase,
        "session_used": {field: session_usage[field] for field in FIRST_FIELDS},
        "cumulative_used": cumulative,
        "stage_limits": {field: stage_caps[field] for field in FIRST_FIELDS},
        "plan_limits": plan_caps,
        "quota_reached": bool(exceeded),
        "exceeded_fields": exceeded,
    }


def validate_depth_plan(course, progress):
    policy = course.get("depth_policy")
    plan = progress.get("next_session", {}).get("depth_plan")
    if policy is None and plan is None:
        return []
    if not isinstance(policy, dict) or not isinstance(policy.get("enabled"), bool):
        return ["深度策略须明确 enabled 布尔值"]
    if not policy["enabled"]:
        return ["深度小纲存在但策略未启用"] if plan is not None else []
    errors = []
    overrides = policy.get("levels", {})
    if not isinstance(overrides, dict):
        return ["深度档位覆盖须为对象"]
    changed = False
    for name, values in overrides.items():
        if name not in DEFAULT_LEVELS or not isinstance(values, dict):
            errors.append("无效深度档位覆盖：" + str(name))
            continue
        for key, value in values.items():
            if key not in DEFAULT_LEVELS[name] or not number(value, integer=key != "minutes" and key != "review_minutes",
                                                             positive=key in FIRST_FIELDS):
                errors.append("无效深度额度：" + name + "." + str(key))
            elif value != DEFAULT_LEVELS[name][key]:
                changed = True
    if changed and not present(policy.get("override_reason")):
        errors.append("修改深度默认额度须记录 override_reason")
    if errors:
        return errors
    levels = level_limits(policy)
    groups, topics = {}, {}
    for field, lookup in (("groups", groups), ("topics", topics)):
        entries = policy.get(field)
        if not isinstance(entries, list) or not entries:
            errors.append("深度策略缺少非空列表：" + field)
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not present(entry.get("id")):
                errors.append("深度策略条目缺少 id：" + field)
                continue
            if entry["id"] in lookup:
                errors.append("深度策略 id 重复：" + entry["id"])
            lookup[entry["id"]] = entry
            if not present(entry.get("title")):
                errors.append("深度策略条目缺少 title：" + entry["id"])
    module_ids = {item.get("id") for item in course.get("modules", []) if isinstance(item, dict) and present(item.get("id"))}
    module_group_minutes = defaultdict(float)
    for group in groups.values():
        if not number(group.get("minutes"), positive=True):
            errors.append("父级预算无效：" + group["id"])
        mod_id = group.get("module_id")
        if mod_id is not None:
            if not present(mod_id) or (module_ids and mod_id not in module_ids):
                errors.append("父级预算关联模块不存在：" + group["id"])
            elif number(group.get("minutes"), positive=True):
                module_group_minutes[mod_id] += group["minutes"]
    # Reconcile module-linked group budgets against daily_plan module minutes
    planning = course.get("planning", {})
    if module_group_minutes and planning.get("status") in ("active", "provisional") and course.get("daily_plan"):
        daily_by_module = defaultdict(float)
        for day_item in course.get("daily_plan", []):
            if isinstance(day_item, dict) and present(day_item.get("module_id")) and number(day_item.get("minutes")):
                daily_by_module[day_item["module_id"]] += day_item["minutes"]
        acknowledged = bool(planning.get("capacity_issues"))
        for mod_id, needed in module_group_minutes.items():
            if needed > daily_by_module.get(mod_id, 0) and not acknowledged:
                errors.append(f"模块深度父级预算合计（{needed:g}分）超过每日简纲可用分钟（{daily_by_module.get(mod_id, 0):g}分），请重排或在 planning.capacity_issues 说明取舍：{mod_id}")
    group_totals = defaultdict(float)
    for topic in topics.values():
        topic_id = topic["id"]
        for field in ("outline_object", "outline_verb", "outline_source"):
            if not present(topic.get(field)):
                errors.append("主题缺少大纲映射 " + field + "：" + topic_id)
        level = topic.get("level")
        if not isinstance(level, str) or level not in levels:
            errors.append("主题深度档位无效：" + topic_id)
            continue
        if topic.get("outline_verb") != level and not present(topic.get("mapping_reason")):
            errors.append("非同名动词映射须有 mapping_reason：" + topic_id)
        group_id = topic.get("group_id")
        if not isinstance(group_id, str) or group_id not in groups:
            errors.append("主题父级不存在：" + topic_id)
            continue
        limits = topic.get("limits", {})
        if not isinstance(limits, dict):
            errors.append("主题 limits 须为对象：" + topic_id)
            continue
        valid = True
        for key, value in limits.items():
            if key not in FIRST_FIELDS or not number(value, integer=key != "minutes", positive=key == "minutes"):
                errors.append("主题额度无效：" + topic_id + "." + str(key))
                valid = False
            elif value > levels[level][key]:
                errors.append("主题额度超过档位上限：" + topic_id + "." + key)
                valid = False
        if valid:
            group_totals[group_id] += topic_limits(topic, levels)["minutes"]
    for group_id, total in group_totals.items():
        if number(groups[group_id].get("minutes"), positive=True) and total > groups[group_id]["minutes"]:
            errors.append("子主题合计超过父级预算：" + group_id)
    # Validate optional topic_ids on daily_plan entries
    for day_item in course.get("daily_plan", []):
        if isinstance(day_item, dict) and "topic_ids" in day_item:
            day_topics = day_item["topic_ids"]
            if not isinstance(day_topics, list) or any(not isinstance(tid, str) or tid not in topics for tid in day_topics):
                errors.append("每日简纲 topic_ids 包含无效主题：" + str(day_item.get("date", "未知日期")))
    if errors:
        return errors
    if not isinstance(plan, dict):
        return ["已启用深度策略，小纲须有 depth_plan"]
    for field in ("available_minutes", "overhead_minutes"):
        if not number(plan.get(field), positive=field == "available_minutes"):
            errors.append("小纲分钟无效：" + field)
    items = plan.get("items")
    if not isinstance(items, list) or not items:
        return errors + ["深度小纲 items 须为非空列表"]
    totals, baselines = {}, {}
    scheduled_minutes = 0
    for index, item in enumerate(items):
        label = "深度小纲第 " + str(index + 1) + " 项"
        if not isinstance(item, dict):
            errors.append(label + "须为对象")
            continue
        topic_id, phase = item.get("topic_id"), item.get("phase")
        if not isinstance(topic_id, str) or topic_id not in topics or phase not in ("first_pass", "practice"):
            errors.append(label + "的主题或阶段无效")
            continue
        topic = topics[topic_id]
        if phase == "first_pass" and topic_first_pass_closed(topic, progress):
            errors.append("主题首轮主线已封口，不可再排入 first_pass，请改用 practice 复查配额：" + topic_id)
            continue
        for field in ("goal", "stop_when"):
            if not present(item.get(field)):
                errors.append(label + "缺少 " + field)
        if any(not number(item.get(key), integer=key != "minutes", positive=key == "minutes") for key in FIRST_FIELDS):
            errors.append(label + "的分钟或讲练数无效")
            continue
        scheduled_minutes += item["minutes"]
        key = (topic_id, phase)
        used = item.get("used_before", dict(minutes=0, lectures=0, exercises=0, sessions=0))
        if (not isinstance(used, dict) or any(not number(used.get(field), integer=field != "minutes")
                                             for field in USAGE_FIELDS)
                or ("used_before" in item and not present(used.get("source")))):
            errors.append(label + "的已投入快照无效或缺少出处")
            continue
        # Cross-check used_before against committed depth_usage so prior consumption cannot be silently reset
        prior_record = recorded_usage(progress, topic_id, phase)
        if prior_record:
            rec_totals = prior_record["totals"]
            if "used_before" not in item and any(rec_totals.get(f, 0) > 0 for f in USAGE_FIELDS):
                errors.append("主题已有历史投入记录，小纲须填写 used_before 且不得低于已归档消耗：" + topic_id + "." + phase)
                continue
            for field in USAGE_FIELDS:
                if used.get(field, 0) < rec_totals.get(field, 0):
                    errors.append("主题 used_before 低于已归档历史消耗：" + topic_id + "." + phase + "." + field)
        if key in baselines and used != baselines[key]:
            errors.append("同主题同阶段已投入快照不一致：" + topic_id)
            continue
        baselines[key] = used
        total = totals.setdefault(key, dict(minutes=used["minutes"], lectures=used["lectures"],
                                            exercises=used["exercises"], sessions=used["sessions"]))
        for field in FIRST_FIELDS:
            total[field] += item[field]
        total["sessions"] += 1
        profile = levels[topic["level"]]
        if phase == "practice":
            if topic["level"] == "了解" and not present(item.get("review_reason")):
                errors.append("了解级复查须有原错或前置理由：" + topic_id)
            if item["minutes"] > profile["review_minutes"]:
                errors.append("单次复查超过分钟上限：" + topic_id)
    for (topic_id, phase), total in totals.items():
        topic = topics[topic_id]
        limits = phase_limits(topic, phase, levels)
        for field, limit in limits.items():
            if total[field] > limit:
                errors.append("主题累计超过阶段额度：" + topic_id + "." + phase + "." + field)
    if (number(plan.get("available_minutes"), positive=True) and number(plan.get("overhead_minutes"))
            and scheduled_minutes + plan["overhead_minutes"] > plan["available_minutes"]):
        errors.append("主题与衔接合计超过本次可用分钟")
    return errors


def depth_markdown(course, progress):
    policy = course.get("depth_policy", {})
    plan = progress.get("next_session", {}).get("depth_plan")
    if not isinstance(policy, dict) or not policy.get("enabled") or not isinstance(plan, dict):
        return []
    cell = lambda value: str(value).replace("|", "/").replace("\n", " ")
    topics = {topic["id"]: topic for topic in policy.get("topics", []) if isinstance(topic, dict) and "id" in topic}
    levels = level_limits(policy)
    lines = ["", "## 教学深度与额度", "", "题数和分钟是教学上限，不是官方配分或掌握证明；已投入跨场次累计，达到上限转下一项。", "",
             "| 主题/父级 | 大纲动词与对象/出处 | 规划深度/阶段 | 此前分钟 | 本次分钟/讲/练 | 目标与转出条件 |",
             "|---|---|---|---|---|---|"]
    for item in plan.get("items", []):
        if not isinstance(item, dict) or item.get("topic_id") not in topics:
            continue
        topic = topics[item["topic_id"]]
        used = item.get("used_before", {})
        prior = used.get("minutes", 0) if isinstance(used, dict) else "待核"
        profile = levels[topic["level"]]
        limits = topic_limits(topic, levels) if item.get("phase") == "first_pass" else dict(
            minutes=profile["review_sessions"] * profile["review_minutes"],
            lectures=profile["review_sessions"], exercises=profile["review_exercises"])
        cap = "；累计上限 " + str(limits["minutes"]) + "分／" + str(limits["lectures"]) + "讲／" + str(limits["exercises"]) + "题"
        row = (topic.get("title", "") + "/" + str(topic.get("group_id", "")),
               str(topic.get("outline_verb", "")) + " " + str(topic.get("outline_object", "")) + "；" + str(topic.get("outline_source", "")),
               str(topic.get("level", "")) + "/" + str(item.get("phase", "")) + cap, prior,
               "/".join(str(item.get(key, "待定")) for key in FIRST_FIELDS),
               str(item.get("goal", "")) + "；" + str(item.get("stop_when", "")))
        lines.append("| " + " | ".join(cell(value) for value in row) + " |")
    lines += ["", "本次可用 " + cell(plan.get("available_minutes", "待定")) + " 分钟；衔接等 "
              + cell(plan.get("overhead_minutes", "待定")) + " 分钟。"]
    lines += ["父级第一轮预算：" + "；".join(cell(group.get("title", group.get("id", "")))
                                        + "≤" + cell(group.get("minutes", "待定")) + "分钟"
                                        for group in policy.get("groups", []) if isinstance(group, dict)) + "。"]
    return lines
