import argparse
import calendar
import copy
import html
import json
import re
import shlex
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from common import current_time, duration, parse_time, project_path, read_json, write_json, write_text
from knowledge import knowledge_view, migrate_knowledge
from session_clock import elapsed, timer_markdown


SKILL_DIRECTORY = Path(__file__).resolve().parents[1]
ASSETS = SKILL_DIRECTORY / "assets"
PARSED_STATUS = {"awaiting_files": "待放入文件", "pending_import": "待读取导入", "imported": "已接入，按章节核验", "partial": "已部分接入，有缺失项", "needs_review": "待核对"}


def esc(value):
    return html.escape(str(value), quote=True)


def link(path, label):
    return f'<a href="{esc(quote(str(path), safe="/#:?=&%"))}">{esc(label)}</a>'


def table(headers, rows):
    head = "".join(f"<th>{esc(x)}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def items(values):
    return "<ul>" + "".join(f"<li>{esc(value)}</li>" for value in values) + "</ul>"


def review_schedule(course, progress):
    metadata = progress.get("snapshot", {})
    today = parse_time(metadata["generated_at"]).date() if metadata.get("generated_at") else current_time(course["timezone"]).date()
    policy = course.get("cycle", {})
    result = []
    for period in progress.get("periods", []):
        start, end = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
        if "planned_learning_dates" in period:
            planned = [date.fromisoformat(value) for value in period["planned_learning_dates"]]
            candidates = [value for value in planned if start <= value <= end]
            basis = "本周期最后一个计划学习日"
        else:
            weekdays = policy.get("planned_weekdays")
            weekdays = set(weekdays) if weekdays is not None else {policy.get("review_weekday", 6)}
            candidates = [start + timedelta(days=offset) for offset in range((end - start).days + 1)
                          if (start + timedelta(days=offset)).weekday() in weekdays]
            basis = "计划学习日" if "planned_weekdays" in policy else "配置的复盘星期"
        due = max(candidates) if candidates else None
        if period.get("review_completed_at") and period.get("review_evidence"):
            status = "已记录实际复盘"
        elif due is None:
            status = "无计划学习日，复盘待安排"
        elif today > due:
            status = "待补复盘，下一次实际学习优先安排"
        elif today == due:
            status = "已到计划复盘日，复习计入当天预算"
        else:
            status = "尚未执行；到期未上课则顺延"
        result.append({"period": period["id"], "date": due.isoformat() if due else None, "basis": basis, "status": status})
    return result


def knowledge_text(entry):
    view = knowledge_view(entry)
    parts = [view["status"], "复查：" + view["review_status"]]
    parts += [event["summary"] + "（" + event["date"] + "）" for event in view["evidence"]]
    if view["legacy"]:
        old = view["legacy"]
        parts.append("保留的历史摘要：" + str(old.get("status", "")) + "。" + str(old.get("evidence", "")) + "（" + str(old.get("last_verified", "未记录")) + "）")
    elif not view["history"] and entry.get("evidence"):
        parts.append(entry["evidence"])
    return "。".join(parts)


def knowledge_rows(entries):
    rows = []
    for entry in entries:
        view = knowledge_view(entry)
        evidence = []
        for event in view["evidence"]:
            evidence.append(esc(event["dimension"] + "：" + event["summary"]) + " · " + link(event["source"], event["date"] + " 的记录"))
        if view["history"]:
            history = ''.join('<li>' + esc(event["date"] + " · " + event["dimension"] + " · " + event["status"] + "：" + event["summary"]) + ' · ' + link(event["source"], "证据") + '</li>' for event in view["history"])
            evidence.append('<details><summary>全部验证记录（' + str(len(view["history"])) + '）</summary><ul>' + history + '</ul></details>')
        if view["legacy"]:
            old = view["legacy"]
            text = esc(str(old.get("status", "")) + "。" + str(old.get("evidence", "")))
            source = link(old["source"], str(old.get("last_verified", "历史")) + " 的记录") if old.get("source") else esc(old.get("last_verified", "出处待定位"))
            evidence.append('<details open><summary>历史摘要，尚未拆分为验证事件</summary><p>' + text + ' · ' + source + '</p></details>')
        elif not view["history"] and entry.get("evidence"):
            evidence.append(esc(entry["evidence"]))
        rows.append([esc(entry["title"]), esc(view["status"]), esc(view["review_status"]), '<br>'.join(evidence)])
    return rows


def page(title, subtitle, content, navigation, eyebrow="学习项目", footer=""):
    template = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    values = {"TITLE": esc(title), "SUBTITLE": esc(subtitle), "CONTENT": content,
              "NAV": "".join(link(target, label) for target, label in navigation),
              "EYEBROW": esc(eyebrow), "FOOTER": footer}
    for key, value in values.items():
        template = template.replace("__" + key + "__", value)
    return template


def session_summary(session):
    status = session.get("status", "idle")
    if status == "idle":
        return "尚未正式开课。资料维护与脚本验证不计课堂学习。"
    start = parse_time(session["started_at"])
    if status == "ended":
        end = parse_time(session["ended_at"])
        return f"{start:%m/%d}已结束：{start:%H:%M:%S}—{end:%H:%M:%S}，实际{duration(elapsed(session, end))}。{session.get('finish_reason', '')}"
    label = "进行中" if status == "running" else "已暂停"
    return f"本场{label}：{start:%Y-%m-%d %H:%M:%S}开始，预算{duration(session['budget_seconds'])}，计划截止{session['deadline_at']}；继续时沿用原起点。"


def context_markdown(course, progress, session, root):
    current = progress["current"]
    script = SKILL_DIRECTORY / "scripts/render.py"
    try:
        script = script.relative_to(root)
    except ValueError:
        pass
    command = "python3 " + shlex.quote(str(script)) + " --root . build"
    lines = ["# 学习上下文", "", "由 data/progress.json 与课程配置生成。更新事实后运行 `" + command + "`；本页不保留过期状态快照。", "",
             f"最后更新：{progress['updated_at']}。课堂状态：{session_summary(session)}", "",
             "## 当前目标与位置", "", course["goal"], "", current.get("stage", "尚未开始"), "", current.get("summary", "尚无作答证据"), "", "## 最新证据", ""]
    for entry in progress.get("knowledge", []):
        lines.append("- " + entry["title"] + "：" + knowledge_text(entry))
    lines += ["", "## 待复查与未完成", ""]
    lines += ["- " + value for value in current.get("unfinished", [])]
    lines += ["", "## 下一次入口", "", progress["next_session"].get("connection", "开课时确定"), ""]
    lines += ["- " + value for value in progress["next_session"].get("objectives", [])]
    lines += ["", "完整安排见 [下次学习小纲](records/下次学习小纲.md)；周期复习与历史错题见 [错题本](review.html)。", "",
              "## 教学与资料", "", ("- 通用教学流程、计时、PDF与题源规则：[学习教练](" + course["skill_path"] + ")。") if course.get("skill_path") else "- 通用教学流程、计时、PDF与题源规则：遵循当前调用的 learning-coach skill。",
              "- 本课程目标、时间预算和学习偏好：[课程配置](data/course.json)。",
              "- 最近场次：[学习计时](学习计时.md)；只有实际开课才建立新计时。",
              "- 教材目录与页码核验：[资料索引](materials/catalogue.json)；课程路线：[学习计划](plan.html)。",
              ("- 历史过程与原答案：[历史学习快照](" + current["history_path"] + ")。") if current.get("history_path") else "- 历史过程与原答案：尚无历史归档。", ""]
    if current.get("latest_record"):
        lines.append(f"- 最新独立学习记录：[{Path(current['latest_record']).stem}]({current['latest_record']})。")
    lines += ["", "## 最近维护", ""]
    lines += ["- " + value for value in progress.get("maintenance", [])]
    return "\n".join(lines) + "\n"


def outline_markdown(course, progress):
    session = progress["next_session"]
    lines = ["# 下次学习小纲", "", "由 data/progress.json 的 next_session 生成。以下是计划，尚未上课、未开始计时。", "",
             "## 接续", "", session.get("connection", "开课时确定"), "", "## 目标", ""]
    lines += ["- " + value for value in session.get("objectives", [])]
    lines += ["", "## 预算与起止", "", session.get("budget_note", "按用户当次可用时间确定"), "", "实际开始与截止在正式开课前核时填写。", "", "## 资料与核验", ""]
    lines += ["- " + value for value in session.get("sources", [])]
    lines += ["", "## 分段安排", "", "| 环节 | 计划 | 实际结果 |", "|---|---|---|"]
    lines += [f"| {row['stage']} | {row['plan'].replace('|', '／')} | 尚未执行 |" for row in session.get("segments", [])]
    lines += ["", "## 验收与顺延", ""]
    lines += ["- " + value for value in session.get("acceptance", [])]
    schedule = review_schedule(course, progress)
    if schedule:
        lines += ["", "## 周期复盘安排", ""]
        lines += ["- " + row["period"] + "：" + (row["date"] or "日期待定") + "（" + row["basis"] + "）；" + row["status"] + "。" for row in schedule]
    return "\n".join(lines) + "\n"


def material_index_markdown(catalogue):
    lines = ["# 资料目录索引", "", "由 catalogue.json 生成。目录标题和正文知识的核验范围分别记录；PDF 页序从1开始，未知映射不推填。", ""]
    for source in catalogue.get("sources", []):
        parsed = source.get("parsed", {})
        lines += ["## " + source["title"], "", source.get("toc_status", "待初始化"), "", source.get("page_mapping", ""), "",
                  *(["用户解析目录：`" + parsed["directory"] + "`；最近登记：" + PARSED_STATUS.get(parsed.get("status"), "待检查") + "。", ""] if parsed.get("directory") else []),
                  "| 目录条目 | 书内起页 | PDF起页 | 核验状态 |", "|---|---|---|---|"]
        for item in source.get("toc", []):
            lines.append(f"| {item['title']} | {item.get('printed_start', '未知')} | {item.get('pdf_start') or '待定位'} | {item.get('verification', '待核验')} |")
        lines += ["", "正文待核对项：", ""]
        lines += ["- " + item["location"] + "：" + item["issue"] for item in source.get("pending", [])]
        lines.append("")
    return "\n".join(lines) + "\n"


def heatmap(progress, today):
    year = today.year
    begin = date(year, 1, 1)
    start = begin - timedelta(days=begin.weekday())
    end = date(year, 12, 31)
    end += timedelta(days=6 - end.weekday())
    by_day = {entry["date"]: entry for entry in progress.get("days", [])}
    latest = max((key for key in by_day if key <= today.isoformat()), default=None)
    months = ''.join(f'<span style="grid-column:{((date(year, month, 1) - start).days // 7) + 1}">{month}月</span>' for month in range(1, 13))
    result = ['<div class="calendar-scroll"><div class="calendar-months">' + months + '</div><div class="calendar-body"><div class="weekdays" aria-hidden="true"><span>周一</span><span></span><span>周三</span><span></span><span>周五</span><span></span><span></span></div><div class="calendar" aria-label="全年学习记录，每列一周，从周一到周日">']
    cursor = start
    while cursor <= end:
        day = by_day.get(cursor.isoformat())
        text = f"{cursor.isoformat()} · "
        css = ""
        target = ""
        if cursor > today or cursor.year != year:
            css = "future"
            text += "未来日期" if cursor > today else "相邻年份"
        elif day:
            seconds = day.get("duration_seconds")
            css = "unknown" if seconds is None else "l" + str(1 if seconds <= 900 else 2 if seconds <= 1800 else 3 if seconds <= 3600 else 4)
            text += ("已学习，用时未知" if seconds is None else "已确认用时 " + duration(seconds)) + " · " + day["title"]
            if day.get("has_unknown_duration") and seconds is not None:
                text += "（另有未可靠计时部分）"
            target = day.get("record") or ""
        else:
            text += "无学习记录"
        result.append(f'<button type="button" class="day {css}" tabindex="{0 if day else -1}" data-latest="{str(cursor.isoformat() == latest).lower()}" title="{esc(text)}" aria-label="{esc(text)}" data-text="{esc(text)}" data-link="{esc(quote(target, safe="/#"))}"></button>')
        cursor += timedelta(days=1)
    result += ['</div></div></div><div class="legend"><span class="sample"></span>无记录 <span class="sample known"></span>已知用时，颜色随用时加深 <span class="sample unknown"></span>已学习，用时未知</div>',
               '<div id="day-detail" class="day-detail" aria-live="polite">点击或用键盘选择日期，查看学习内容与原记录。</div>']
    return "".join(result)


def responsibility_rows():
    return [("目标／预算／偏好", "data/course.json", "备课、计划调整", "用户调整或周期复盘"),
            ("进度／错题／小纲", "data/progress.json", "每次开课与收尾", "真实作答与重要订正后"),
            ("当前计时", "data/session.json", "每次教学回复前", "核时、明确暂停与结束"),
            ("目录／页码／解析", "materials/catalogue.json", "资料准备与相关章节备课", "目录与页段核验后"),
            ("题源与曝光", "data/questions.json", "出题与阶段测试前", "收录、展示、提示、作答后"),
            ("每日原文", "records/", "复习与证据回看", "按保存约定"),
            ("历史方案", "archive/", "需要追溯时", "归档时")]


def plan_page(course, progress, catalogue):
    content = '<h2 id="goal">目标与时间</h2><p>' + esc(course["goal"]) + '</p>'
    content += items(course.get("target", {}).get("notes", []))
    policy = course["time_policy"]
    content += table(["项目", "当前约定"], [[esc(k), esc(v)] for k, v in [
        ("工作日", str(policy.get("weekday_minutes") or "开课时确认") + " 分钟"),
        ("周末总预算", str(policy.get("weekend_minutes") or "开课时确认") + " 分钟，分段执行"),
        ("新概念截止", f"本段结束前 {policy['stop_concepts_before_end_minutes']} 分钟"),
        ("新题截止", f"本段结束前 {policy['stop_questions_before_end_minutes']} 分钟"),
        ("截止", "实际核时；未完成顺延，具体加时需用户明确要求")]])
    content += '<h2 id="route">课程路线</h2><p class="meta">模块顺序来自现有备考路线；教材对应关系按目录初始化。具体能力目标由每次小纲落实，正文未读部分保留待核验。</p>'
    content += table(["模块", "内容", "前置与状态", "教材依据"], [[esc(m["id"] + " " + m["title"]), esc("；".join(m.get("topics", []))), esc(m.get("status", "未学") + "；前置：" + m.get("prerequisite", "无")), esc(m.get("source_note", "资料待补"))] for m in course.get("modules", [])])
    content += '<h2 id="stages">阶段目标</h2>' + table(["阶段", "完成标准", "当前安排"], [[esc(s["title"]), esc(s["goal"]), esc(s["status"])] for s in course.get("stages", [])])
    content += '<h2 id="imports">用户解析结果</h2><p>自行解析 PDF 后，把工具导出的完整文件夹放入对应目录，保留正文及图片、表格素材的相对层级，无需统一改名或填写元数据。放好后告诉 Agent“解析结果放好了”，Agent 会读取并建立章节索引，后续按小纲读取。</p>'
    imports = []
    for source in catalogue.get("sources", []):
        parsed = source.get("parsed", {})
        if parsed.get("directory"):
            directory = parsed["directory"].rstrip("/") + "/"
            imports.append([esc(source["title"]), link(directory, directory), esc(PARSED_STATUS.get(parsed.get("status"), "待检查"))])
    content += table(["对应资料", "解析结果存放目录", "最近登记状态"], imports) if imports else '<p>资料登记后，为每份 PDF 创建 materials/parsed/ 下的专属目录，并告知实际绝对路径。</p>'
    content += '<p class="small">Agent 在用户告知或实际备课时检查新文件；导入完成、正文核验和学习进度分别记录。</p>'
    content += '<h2 id="ocr">PDF 处理安排</h2>'
    ocr = course.get("ocr_policy", {})
    if ocr:
        selected = [s for s in catalogue.get("sources", []) if s["id"] in ocr.get("source_ids", [])]
        scope = f"已登记 {len(selected)} 份，共 {sum(s['page_count'] for s in selected)} 页；任务执行前扣除重复与可复用结果" if selected else "执行前列明本课程选定资料，扣除重复与可复用结果"
        limit = ocr.get("budget_limit")
        budget = "未配置，不启动付费任务" if limit is None else str(limit) + " " + str(ocr.get("currency") or "币种待确认") + "；实际任务仍需明确授权范围"
        strategy = "同次部署：样页合格后连续分批提取，再保存本地并释放专用算力" if ocr.get("paid_compute") == "batch_after_sample" else "执行前比较整批与按需处理的总成本"
        content += table(["项目", "当前约定"], [[esc(k), esc(v)] for k, v in [
            ("付费算力", strategy), ("候选范围", scope),
            ("单批页面准备", str(ocr.get("max_batch_pages", 12)) + " 页；任务可包含多批，模型批大小另按样页调整"),
            ("费用上限", budget), ("学习使用", "按小纲读取本地章节并核对原页；已提取、已核验、已学分别记录")]])
        content += items(ocr.get("notes", []))
    else:
        content += '<p>先建立目录与样页，再按实际成本选择整批或按需提取；付费任务需明确范围、费用上限和资源释放安排。</p>'
    content += '<h2 id="materials">资料与本地目录</h2>'
    for source in catalogue.get("sources", []):
        content += '<h3>' + link(source["path"], source["title"]) + '</h3><p class="meta">' + esc(source["version"] + " · " + str(source["page_count"]) + " 页 · " + source.get("toc_status", "目录未初始化")) + '</p>'
        rows = []
        for entry in source.get("toc", []):
            target = source["path"] + ("#page=" + str(entry["pdf_start"]) if entry.get("pdf_start") else "")
            rows.append([link(target, entry["title"]), esc(entry.get("printed_start", "未确认")), esc(entry.get("pdf_start") or "待定位"), esc(entry.get("verification", "目录已读，正文待核验"))])
        content += table(["目录条目", "书内起页", "PDF起页", "核验情况"], rows)
        content += '<p class="small">' + esc(source.get("page_mapping", "")) + '；Unlimited-OCR：' + esc(source.get("ocr", {}).get("status", "未配置")) + '</p>'
    content += '<h2 id="sources">题源与信息时点</h2><p>题目来源、答案依据和曝光情况保存在题目索引。教材目录已初始化不表示全文已核验。考试、报名和政策信息使用前重新查官方通知。</p>'
    content += '<p>' + link("data/questions.json", "题目索引原文件") + ' · ' + link("materials/catalogue.json", "资料目录原文件")
    if course.get("historical_plan"):
        content += ' · ' + link(course["historical_plan"], "原始长期计划（历史）")
    content += '</p>'
    return page("课程计划与资料大纲", course["title"], content, [("index.html", "返回进度首页"), ("#goal", "目标与预算"), ("#route", "课程路线"), ("#stages", "阶段目标"), ("#imports", "自行解析导入"), ("#ocr", "PDF处理安排"), ("#materials", "教材与目录"), ("#sources", "题源状态")], footer="由课程配置和资料索引生成；当前学习位置以最新作答证据为准。")


def review_page(course, progress):
    content = '<h2 id="period">周期安排</h2>'
    metadata = progress.get("snapshot")
    if metadata:
        content += '<p class="notice">快照生成于 ' + esc(metadata["generated_at"]) + '；验证结论按生成时所见状态保存，可能含周期结束后的更新。学习日限本周期，错题含周期结束前发生的历史条目。</p>'
        period = progress["periods"][0]
        new_count = sum(period["start"] <= mistake["date"] <= period["end"] for mistake in progress.get("mistakes", []))
        content += '<p>本周期新增 ' + str(new_count) + ' 条；带入历史错题 ' + str(len(progress.get("mistakes", [])) - new_count) + ' 条。</p>'
    schedule = {row["period"]: row for row in review_schedule(course, progress)}
    for period in progress.get("periods", []):
        days = [d for d in progress.get("days", []) if period["start"] <= d["date"] <= period["end"]]
        known = sum(d.get("duration_seconds") or 0 for d in days)
        unknown = sum(d.get("duration_seconds") is None or d.get("has_unknown_duration", False) for d in days)
        content += f'<h3>{esc(period["id"])} · {esc(period["start"])} ～ {esc(period["end"])}</h3><p>{esc(period["status"])}</p><p>有记录 {len(days)} 天；已确认用时 {esc(duration(known))}；{unknown} 天存在未知时长。</p>'
        row = schedule[period["id"]]
        content += '<p>计划复盘：' + esc(row["date"] or "日期待定") + '（' + esc(row["basis"]) + '）；' + esc(row["status"]) + '。</p>'
        content += items(period.get("review_plan", []))
    content += '<h2 id="queue">待复查队列</h2>'
    content += table(["内容", "最近证据", "下一步"], [[esc(r["title"]), esc(r["evidence"]), esc(r["next_check"])] for r in progress.get("review_queue", [])])
    content += '<h2 id="mistakes">历史错题与最新结果</h2><p class="meta">原答与当日注记保留；当前结论看最新验证。未做后续核验的历史条目只作索引，不统一判为仍不会。</p>'
    dates = sorted({m["date"] for m in progress.get("mistakes", [])}, reverse=True)
    for day in dates:
        entries = [m for m in progress["mistakes"] if m["date"] == day]
        content += f'<details{" open" if day == dates[0] else ""}><summary>{esc(day)} · {len(entries)} 条记录</summary>'
        rows = []
        for entry in entries:
            note = entry.get("latest_note") or ("当日历史注记：" + entry.get("historical_note", "未记录") + "；后续情况按最新知识证据和复查队列判断。")
            evidence = link(entry["evidence"], "原学习记录") if entry.get("evidence") else "原记录待定位"
            latest = ' · 最新验证日期：' + esc(entry["latest_date"]) if entry.get("latest_date") else ''
            rows.append([esc(entry["original"]), '<span class="red">' + esc(entry["correction"]) + '</span>', esc(note) + latest + '<br>' + evidence])
        content += table(["原答案／步骤", "订正与原因", "最新状态及出处"], rows) + '</details>'
    return page("周期复习与错题本", "按最新证据选题，周期末复习计入当天预算", content, [("index.html", "返回进度首页"), ("#period", "当前周期"), ("#queue", "待复查"), ("#mistakes", "历史错题")], footer="此页是复习安排与真实错题索引；未执行的复习不计为完成。")


def home_page(root, course, progress, session, catalogue, context, outline):
    days = progress.get("days", [])
    known = sum(d.get("duration_seconds") or 0 for d in days)
    unknown = sum(d.get("duration_seconds") is None or d.get("has_unknown_duration", False) for d in days)
    current = progress["current"]
    content = '<div class="stats">' + ''.join(f'<div><strong>{esc(value)}</strong><span>{esc(label)}</span></div>' for value, label in [(str(len(days)), "有记录的学习日"), (duration(known), "已确认课堂用时"), (str(unknown) + " 天", "存在未知时长"), (current.get("module", "待确认"), "当前模块")]) + '</div>'
    content += '<section id="current"><h2>现在学到哪里</h2><p>' + esc(current.get("summary", "尚无作答证据")) + '</p><div class="notice">' + esc(session_summary(session)) + '</div></section>'
    today = current_time(course["timezone"]).date()
    content += '<section id="activity"><h2>学习日历 <span class="tag">' + str(today.year) + '</span></h2>' + heatmap(progress, today) + '</section>'
    content += '<section id="next"><h2>下一次学习</h2><p>' + esc(progress["next_session"].get("connection", "开课时确定")) + '</p>' + items(progress["next_session"].get("objectives", [])) + '<p>' + esc(progress["next_session"].get("budget_note", "")) + '</p><p>' + link("records/下次学习小纲.md", "阅读完整小纲") + ' · ' + link("plan.html", "课程计划与教材目录") + ' · ' + link("review.html", "周期复习与错题本") + '</p></section>'
    content += '<section id="progress"><h2>知识进度与作答证据</h2>' + table(["内容", "各维度最新证据", "复查安排", "证据与历史"], knowledge_rows(progress.get("knowledge", []))) + '</section>'
    content += '<section id="records"><h2>学习记录</h2><ul class="records">'
    for day in sorted(days, key=lambda d: d["date"], reverse=True):
        label = link(day["record"], day["title"]) if day.get("record") else esc(day["title"])
        timing = duration(day.get("duration_seconds")) + ("（另有未可靠计时部分）" if day.get("has_unknown_duration") and day.get("duration_seconds") is not None else "")
        content += f'<li><time>{esc(day["date"])}</time><div>{label}<span class="record-note">{esc(timing)}</span></div></li>'
    content += '</ul></section><section id="profile"><h2>学习约定</h2>' + items(course.get("profile", {}).get("teaching_needs", [])) + '<p>' + esc(course.get("save_policy", {}).get("daily_html_note", "按用户约定保存每日记录")) + '</p></section>'
    content += '<section id="documents"><h2>当前正文与原文件</h2>'
    texts = [("学习上下文.md", context), ("学习计时.md", timer_markdown(session, course["time_policy"])), ("records/下次学习小纲.md", outline)]
    for path in ("AGENTS.md", course.get("skill_path")):
        if path and project_path(root, path).is_file():
            texts.append((path, (root / path).read_text(encoding="utf-8")))
    for path, text in texts:
        content += '<details><summary>' + esc(Path(path).name) + '</summary><p>' + link(path, "打开原文件") + '</p><pre>' + esc(text) + '</pre></details>'
    content += '<details><summary>职责与文件来源</summary>' + table(["职责", "权威来源", "读取时机", "更新时机"], [[esc(a), link(b, b), esc(c), esc(d)] for a, b, c, d in responsibility_rows()]) + '</details></section>'
    content += '<section id="materials"><h2>资料原件</h2>' + '<p>' + ' · '.join(link(s["path"], s["title"]) for s in catalogue.get("sources", [])) + '</p><p>' + link("materials/catalogue.json", "目录与解析状态") + ' · ' + link("data/course.json", "课程配置") + ' · ' + link("data/progress.json", "进度原数据") + '</p></section>'
    content += '<p>自行解析 PDF：' + link("plan.html#imports", "查看每份资料的解析结果存放目录与导入说明") + '。</p>'
    content += '<section id="archive"><h2>历史与验证</h2><ul>'
    for path in sorted((root / "archive").glob("*")):
        if path.is_file() and path.suffix in (".md", ".html"):
            content += '<li>' + link(path.relative_to(root), path.stem) + '</li>'
    content += '</ul><p class="meta">历史文件保留当时的说明，当前安排以本页与最新数据为准。</p></section>'
    footer = '<p>本地阅读：在本项目根目录运行</p><pre>python3 -m http.server 8998 --bind 127.0.0.1</pre><p>' + link("http://127.0.0.1:8998/", "打开本地学习进度") + '。停止服务可在终端按 Ctrl+C。</p><p>页面由课程、进度、计时和资料索引生成；资料维护与脚本验证不计为课堂学习。</p>'
    return page(course["title"], course["goal"], content, [("#current", "当前位置"), ("#activity", "学习日历"), ("#next", "下一次学习"), ("#progress", "知识进度"), ("#records", "学习记录"), ("review.html", "周期错题本"), ("plan.html", "课程与大纲"), ("#documents", "正文与职责"), ("#archive", "历史与验证")], eyebrow="学习进度 · " + str(current.get("latest_day") or "尚未开课"), footer=footer)


def render_outputs(root):
    root = Path(root).resolve()
    course = read_json(root, "data/course.json")
    progress = read_json(root, "data/progress.json")
    session = read_json(root, "data/session.json")
    catalogue = read_json(root, "materials/catalogue.json")
    context = context_markdown(course, progress, session, root)
    outline = outline_markdown(course, progress)
    return {"学习上下文.md": context, "学习计时.md": timer_markdown(session, course["time_policy"]),
            "materials/目录索引.md": material_index_markdown(catalogue),
            "records/下次学习小纲.md": outline, "plan.html": plan_page(course, progress, catalogue),
            "review.html": review_page(course, progress),
            "index.html": home_page(root, course, progress, session, catalogue, context, outline)}


def snapshot_period(root, period_id):
    course = read_json(root, "data/course.json")
    progress = read_json(root, "data/progress.json")
    period = next((p for p in progress.get("periods", []) if p["id"] == period_id), None)
    if period is None or not period.get("review_completed_at") or not period.get("review_evidence"):
        raise ValueError("周期快照需要已实际完成复盘的时间与证据，不将复习计划标为完成")
    evidence = project_path(root, period["review_evidence"])
    if not evidence.is_file():
        raise ValueError("周期复盘证据文件不存在")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", period_id):
        raise ValueError("周期ID只能包含字母、数字、下划线和连字符")
    destination = root / "records/periods" / period_id
    if destination.with_suffix(".json").exists():
        raise ValueError("周期快照已存在，保留历史；更正请记录修订原因并使用新的修订ID")
    frozen = copy.deepcopy(progress)
    frozen["periods"] = [period]
    frozen["days"] = [d for d in progress["days"] if period["start"] <= d["date"] <= period["end"]]
    frozen["mistakes"] = [m for m in progress.get("mistakes", []) if m["date"] <= period["end"]]
    frozen["snapshot"] = {"generated_at": current_time(course["timezone"]).isoformat(),
                          "state_basis": "generation_time", "activity_start": period["start"],
                          "activity_end": period["end"], "mistakes_origin_through": period["end"]}
    write_json(root, str(destination.relative_to(root).with_suffix(".json")), frozen)
    output = review_page(course, frozen)
    import os
    output = re.sub(r'href="([^"#]+)(#[^"]*)?"', lambda m: m.group(0) if ":" in m.group(1) else 'href="' + os.path.relpath(root / m.group(1), destination.parent) + (m.group(2) or '') + '"', output)
    write_text(destination.with_suffix(".html"), output)


def initialize(root, title, goal, timezone):
    ZoneInfo(timezone)
    if (root / "data/course.json").exists():
        raise ValueError("项目已存在，不覆盖课程与真实进度")
    for name in ("data", "materials", "materials/parsed", "records", "archive"):
        (root / name).mkdir(parents=True, exist_ok=True)
    write_json(root, "data/course.json", {"schema_version": 1, "id": root.name, "title": title, "goal": goal, "timezone": timezone,
        "target": {"notes": ["目标期限、基础和资料情况按用户信息填写；未确认的内容保留未确认。"]},
        "time_policy": {"weekday_minutes": None, "weekend_minutes": None, "stop_concepts_before_end_minutes": 10, "stop_questions_before_end_minutes": 5, "notice_elapsed_minutes": []},
        "profile": {"teaching_needs": []}, "save_policy": {"daily_html_note": "用户要求保存时生成每日HTML，进度与错题每次更新。"},
        "ocr_policy": {"mode": "cost_aware", "paid_compute": "batch_after_sample", "source_ids": [], "max_batch_pages": 12, "budget_limit": None, "currency": None},
        "cycle": {"kind": "weekly", "review_weekday": 6}, "modules": [], "stages": []})
    try:
        local_skill = (SKILL_DIRECTORY / "SKILL.md").relative_to(root).as_posix()
    except ValueError:
        local_skill = None
    if local_skill:
        course = read_json(root, "data/course.json")
        course["skill_path"] = local_skill
        write_json(root, "data/course.json", course)
    write_json(root, "data/progress.json", {"schema_version": 1, "updated_at": current_time(timezone).isoformat(),
        "current": {"stage": "资料准备", "summary": "尚未正式开课", "session_note": "未开课", "unfinished": [], "latest_day": None},
        "knowledge": [], "review_queue": [], "mistakes": [], "days": [], "sessions": [], "periods": [], "maintenance": [],
        "next_session": {"connection": "明确预算、初始化资料目录后制定本次小纲", "objectives": [], "sources": [], "segments": [], "acceptance": []}})
    write_json(root, "data/session.json", {"status": "idle", "id": None, "started_at": None, "budget_seconds": 0, "deadline_at": None, "pauses": [], "last_checked_at": None, "ended_at": None})
    write_json(root, "materials/catalogue.json", {"schema_version": 1, "sources": []})
    write_json(root, "data/questions.json", {"schema_version": 1, "items": [], "source_entries": []})


def main():
    parser = argparse.ArgumentParser(description="生成可追溯的学习首页、课程计划、上下文和复习视图")
    parser.add_argument("--root", default=".")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("build")
    commands.add_parser("migrate-knowledge")
    period = commands.add_parser("snapshot-period")
    period.add_argument("--period", required=True)
    new = commands.add_parser("init")
    new.add_argument("--title", required=True)
    new.add_argument("--goal", required=True)
    new.add_argument("--timezone", default="UTC")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        if args.action == "migrate-knowledge":
            progress = read_json(root, "data/progress.json")
            migrated = migrate_knowledge(progress)
            write_json(root, "data/progress.json", progress)
            print(json.dumps({"migrated_knowledge_summaries": migrated, "new_learning_evidence": 0}, ensure_ascii=False))
        if args.action == "snapshot-period":
            snapshot_period(root, args.period)
        if args.action == "init":
            initialize(root, args.title, args.goal, args.timezone)
        outputs = render_outputs(root)
        for path, text in outputs.items():
            write_text(root / path, text)
        print(json.dumps({"generated": list(outputs), "materials_directory": str(root / "materials"), "view": "http://127.0.0.1:8998/", "command": "python3 -m http.server 8998 --bind 127.0.0.1"}, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
