import copy
import hashlib
import json
import os
import shutil
import tempfile
import unittest
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch

from common import read_json, write_json, write_text
from pdf_pages import selected_pages
from scan_materials import scan_materials
from knowledge import knowledge_view, migrate_knowledge, validate_knowledge
from render import heatmap, initialize, render_outputs, review_schedule, snapshot_period
from session_clock import archive_finished, check_daily_budget, elapsed, snapshot, transition
from validate import validate


POLICY = {"stop_concepts_before_end_minutes": 10, "stop_questions_before_end_minutes": 5, "notice_elapsed_minutes": [20, 40]}
START = datetime(2026, 10, 16, 14, 35, 31, tzinfo=ZoneInfo("Asia/Shanghai"))


class WorkflowTests(unittest.TestCase):
    def started(self, minutes=60):
        return transition({"status": "idle"}, "start", POLICY, START, minutes=minutes)

    def test_continue_keeps_original_start(self):
        session = self.started()
        updated = transition(session, "tick", POLICY, START + timedelta(minutes=23))
        self.assertEqual(updated["started_at"], session["started_at"])
        self.assertEqual(snapshot(updated, POLICY, START + timedelta(minutes=23))["remaining_seconds"], 37 * 60)
        with self.assertRaises(ValueError):
            transition(updated, "start", POLICY, START + timedelta(minutes=23), minutes=60)

    def test_deadline_boundaries(self):
        session = self.started()
        for minutes, expected in [(49, "learn"), (50, "consolidate"), (52, "consolidate"), (55, "wrap_up"), (60, "close_now"), (63, "close_now")]:
            self.assertEqual(snapshot(session, POLICY, START + timedelta(minutes=minutes))["phase"], expected)
        self.assertEqual(snapshot(session, POLICY, START + timedelta(minutes=63))["overtime_seconds"], 180)

    def test_short_session_uses_own_deadline(self):
        session = self.started(25)
        self.assertEqual(snapshot(session, POLICY, START + timedelta(minutes=17))["phase"], "consolidate")
        self.assertEqual(snapshot(session, POLICY, START + timedelta(minutes=20))["phase"], "wrap_up")

    def test_pause_requires_reason_and_adjusts_deadline(self):
        session = self.started()
        with self.assertRaises(ValueError):
            transition(session, "pause", POLICY, START + timedelta(minutes=20))
        paused = transition(session, "pause", POLICY, START + timedelta(minutes=20), reason="用户明确休息")
        resumed = transition(paused, "resume", POLICY, START + timedelta(minutes=30))
        self.assertEqual(elapsed(resumed, START + timedelta(minutes=35)), 25 * 60)
        self.assertEqual(resumed["deadline_at"], (START + timedelta(minutes=70)).isoformat())

    def test_real_1016_duration_and_closed_tick(self):
        ended = transition(self.started(), "finish", POLICY, START + timedelta(minutes=56, seconds=26), reason="用户在收尾窗口要求结束")
        self.assertEqual(elapsed(ended, START + timedelta(hours=2)), 3386)
        self.assertEqual(transition(ended, "tick", POLICY, START + timedelta(hours=2)), ended)
        self.assertEqual(snapshot(ended, POLICY, START + timedelta(hours=2))["remaining_seconds"], 214)

    def test_extension_needs_explicit_reason(self):
        session = self.started()
        with self.assertRaises(ValueError):
            transition(session, "extend", POLICY, START + timedelta(minutes=50), minutes=10)
        extended = transition(session, "extend", POLICY, START + timedelta(minutes=50), minutes=10, reason="用户明确再加10分钟")
        self.assertEqual(extended["budget_seconds"], 4200)

    def test_selected_pdf_pages_and_limits(self):
        self.assertEqual(selected_pages("6-8,8,10", 52), [6, 7, 8, 10])
        for value in ("0-3", "51-54", "8-6", "1-52"):
            with self.assertRaises(ValueError):
                selected_pages(value, 52)

    def test_new_session_does_not_reset_daily_budget(self):
        course = {"time_policy": {"weekday_minutes": 60, "weekend_minutes": 180}}
        progress = {"days": [{"date": "2026-10-16", "duration_seconds": 3386}]}
        with self.assertRaises(ValueError):
            check_daily_budget(course, progress, START, 60, None)
        check_daily_budget(course, progress, START, 3, None)
        with self.assertRaises(ValueError):
            check_daily_budget(course, progress, START, 10, "普通原因备注")
        check_daily_budget(course, progress, START, 10, "用户明确另加10分钟", override=True)

    def test_first_session_checks_weekday_weekend_and_zero_budget(self):
        course = {"time_policy": {"weekday_minutes": 60, "weekend_minutes": 180}}
        for at, requested in ((START, 61), (START + timedelta(days=1), 181)):
            with self.assertRaises(ValueError):
                check_daily_budget(course, {"days": []}, at, requested, None)
        self.assertEqual(check_daily_budget(course, {"days": []}, START, 60, None)["over_budget_seconds"], 0)
        course["time_policy"]["weekday_minutes"] = 0
        with self.assertRaises(ValueError):
            check_daily_budget(course, {"days": []}, START, 1, None)

    def test_extension_counts_completed_and_current_session_once(self):
        course = {"time_policy": {"weekday_minutes": 60}}
        progress = {"days": [{"date": START.date().isoformat(), "duration_seconds": 600}]}
        active = self.started(40)
        with self.assertRaises(ValueError):
            check_daily_budget(course, progress, START + timedelta(minutes=30), 15, "延长15分钟", session=active)
        report = check_daily_budget(course, progress, START + timedelta(minutes=30), 15, "用户明确另加15分钟", session=active, override=True)
        self.assertEqual(report["projected_seconds"], 65 * 60)
        self.assertEqual(report["over_budget_seconds"], 300)
        report = check_daily_budget(course, progress, START + timedelta(minutes=50), 5, "用户明确加5分钟", session=active)
        self.assertEqual(report["planned_session_seconds"], 50 * 60)

    def test_unknown_time_keeps_known_lower_bound_in_budget_check(self):
        course = {"time_policy": {"weekday_minutes": 60}}
        progress = {"days": [{"date": START.date().isoformat(), "duration_seconds": 7200, "has_unknown_duration": True}]}
        with self.assertRaisesRegex(ValueError, "120分"):
            check_daily_budget(course, progress, START, 30, "普通说明")
        report = check_daily_budget(course, progress, START, 30, "用户明确本次另学30分钟", override=True)
        self.assertTrue(report["has_unknown_duration"])
        self.assertEqual(report["projected_seconds"], 150 * 60)
        self.assertEqual(report["over_budget_seconds"], 90 * 60)

    def test_short_course_requires_usable_windows_or_review_mode(self):
        for minutes in (10, 8, 5, 3):
            with self.assertRaises(ValueError):
                self.started(minutes)
        session = transition({"status": "idle"}, "start", POLICY, START, minutes=8, concept_window=3, question_window=1)
        self.assertEqual(snapshot(session, POLICY, START)["phase"], "learn")
        self.assertEqual(snapshot(session, POLICY, START + timedelta(minutes=5))["phase"], "consolidate")
        self.assertEqual(snapshot(session, POLICY, START + timedelta(minutes=7))["phase"], "wrap_up")
        review = transition({"status": "idle"}, "start", POLICY, START, minutes=8, review_only=True)
        self.assertEqual(snapshot(review, POLICY, START)["phase"], "consolidate")
        for concepts, questions in ((1, 2), (-1, 0), (float("nan"), 1)):
            with self.assertRaises(ValueError):
                transition({"status": "idle"}, "start", POLICY, START, minutes=8, concept_window=concepts, question_window=questions)

    def test_cli_rejects_budget_overrun_without_mutating_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root, "测试课程", "预算边界", "Asia/Shanghai")
            course = read_json(root, "data/course.json")
            course["time_policy"].update(weekday_minutes=30, weekend_minutes=30)
            write_json(root, "data/course.json", course)
            command = [sys.executable, "-B", str(Path(__file__).with_name("session_clock.py")), "--root", str(root)]
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            failed = subprocess.run(command + ["start", "--minutes", "40", "--reason", "普通说明"], capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(before, {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()})
            subprocess.run(command + ["start", "--minutes", "20"], check=True, capture_output=True, text=True)
            before = (root / "data/session.json").read_bytes()
            failed = subprocess.run(command + ["extend", "--minutes", "20", "--reason", "普通说明"], capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(before, (root / "data/session.json").read_bytes())
            allowed = subprocess.run(command + ["extend", "--minutes", "20", "--reason", "用户明确本次再加20分钟", "--override-daily-budget"], check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(allowed.stdout)["daily_budget_check"]["over_budget_seconds"], 600)

    def test_overlapping_pauses_are_rejected(self):
        session = self.started()
        session["pauses"] = [{"start": (START + timedelta(minutes=10)).isoformat(), "end": (START + timedelta(minutes=20)).isoformat()}, {"start": (START + timedelta(minutes=15)).isoformat(), "end": (START + timedelta(minutes=25)).isoformat()}]
        with self.assertRaises(ValueError):
            elapsed(session, START + timedelta(minutes=40))

    def test_unknown_time_is_not_budget_or_zero(self):
        data = {"days": [{"date": "2026-10-15", "duration_seconds": None, "title": "历史学习", "record": None}]}
        output = heatmap(data, START.date())
        self.assertIn("已学习，用时未知", output)
        self.assertIn('class="day unknown"', output)
        self.assertNotIn("已确认用时 60分", output)

    def test_build_is_repeatable_and_checker_catches_stale_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root, "测试课程", "测试目标", "Asia/Shanghai")
            outputs = render_outputs(root)
            for path, content in outputs.items():
                write_text(root / path, content)
            self.assertEqual(outputs, render_outputs(root))
            report = validate(root)
            self.assertTrue(report["ok"], report)
            write_text(root / "index.html", outputs["index.html"] + '<a href="missing.html">错误链接</a>')
            errors = validate(root)["errors"]
            self.assertTrue(any("链接目标不存在" in error for error in errors))
            self.assertTrue(any("生成视图过期" in error for error in errors))

    def test_finished_session_archive_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root, "测试课程", "测试目标", "Asia/Shanghai")
            ended = transition(self.started(), "finish", POLICY, START + timedelta(minutes=56, seconds=26), reason="用户结束")
            archive_finished(root, ended, POLICY)
            archive_finished(root, copy.deepcopy(ended), POLICY)
            progress = read_json(root, "data/progress.json")
            self.assertEqual(len(progress["sessions"]), 1)
            self.assertEqual(len(progress["days"]), 1)
            self.assertEqual(progress["days"][0]["duration_seconds"], 3386)

    def test_archiving_known_part_preserves_unknown_day(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root, "测试课程", "未知时长", "Asia/Shanghai")
            progress = read_json(root, "data/progress.json")
            progress["days"] = [{"date": START.date().isoformat(), "title": "原历史", "duration_seconds": None, "record": None}]
            write_json(root, "data/progress.json", progress)
            ended = transition(self.started(), "finish", POLICY, START + timedelta(minutes=8), reason="结束")
            archive_finished(root, ended, POLICY)
            progress = read_json(root, "data/progress.json")
            self.assertEqual(progress["days"][0]["duration_seconds"], 480)
            self.assertTrue(progress["days"][0]["has_unknown_duration"])
            self.assertIn("另有未可靠计时部分", render_outputs(root)["index.html"])

    def test_archive_preserves_known_time_without_old_session_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root, "测试课程", "历史时长", "Asia/Shanghai")
            progress = read_json(root, "data/progress.json")
            progress["days"] = [{"date": START.date().isoformat(), "title": "可靠旧时长", "duration_seconds": 600, "record": None}]
            write_json(root, "data/progress.json", progress)
            ended = transition(self.started(), "finish", POLICY, START + timedelta(minutes=8), reason="结束")
            archive_finished(root, ended, POLICY)
            self.assertEqual(read_json(root, "data/progress.json")["days"][0]["duration_seconds"], 1080)
            archive_finished(root, ended, POLICY)
            self.assertEqual(read_json(root, "data/progress.json")["days"][0]["duration_seconds"], 1080)

    def test_knowledge_keeps_dimensions_and_validates_retention_dates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root, "测试课程", "证据校验", "Asia/Shanghai")
            write_text(root / "record.md", "真实证据的测试夹具")
            first = {"id": "first", "dimension": "作答", "status": "当场订正", "date": "2026-10-16", "summary": "局部订正", "source": "record.md"}
            transfer = {"id": "transfer", "dimension": "迁移", "status": "迁移正确", "date": "2026-10-16", "summary": "改变输入后独立正确", "source": "record.md"}
            retention = {"id": "retention", "dimension": "延迟保持", "status": "隔日正确", "date": "2026-10-16", "summary": "复查正确", "source": "record.md", "baseline_id": "first"}
            entry = {"id": "point", "title": "知识点", "evidence_history": [first, transfer], "review_status": "待隔日检查"}
            self.assertEqual(validate_knowledge(root, [entry]), [])
            entry["evidence_history"].append(retention)
            self.assertTrue(validate_knowledge(root, [entry]))
            retention["date"] = "2026-10-17"
            self.assertEqual(validate_knowledge(root, [entry]), [])
            view = knowledge_view(entry)
            self.assertEqual(len(view["evidence"]), 3)
            self.assertEqual(view["history"][0], first)
            retention["baseline_id"] = "missing"
            self.assertTrue(validate_knowledge(root, [entry]))
            transfer["status"] = "不存在的状态"
            self.assertTrue(validate_knowledge(root, [entry]))
            progress = read_json(root, "data/progress.json")
            progress["knowledge"] = [entry]
            write_json(root, "data/progress.json", progress)
            self.assertFalse(validate(root, check_generated=False)["ok"])

    def test_knowledge_migration_preserves_summary_without_new_evidence(self):
        old = {"status": "当场订正，隔日待查", "evidence": "原文订正摘要", "last_verified": "2026-10-16", "source": "原记录.md"}
        entry = dict(old, id="point", title="知识点")
        progress = {"knowledge": [entry]}
        self.assertEqual(migrate_knowledge(progress), 1)
        self.assertEqual(entry["legacy_summary"], old)
        self.assertEqual(entry["evidence_history"], [])
        self.assertEqual(entry["review_status"], "待隔日检查")
        self.assertEqual(migrate_knowledge(progress), 0)

    def test_review_date_uses_last_planned_learning_day_and_carries_over(self):
        course = {"timezone": "Asia/Shanghai", "cycle": {"review_weekday": 6}}
        period = {"id": "week", "start": "2026-10-12", "end": "2026-10-18", "planned_learning_dates": ["2026-10-12", "2026-10-16"]}
        progress = {"periods": [period]}
        with patch("render.current_time", return_value=START + timedelta(days=3)):
            row = review_schedule(course, progress)[0]
        self.assertEqual(row["date"], "2026-10-16")
        self.assertIn("待补复盘", row["status"])
        del period["planned_learning_dates"]
        self.assertEqual(review_schedule(course, progress)[0]["date"], "2026-10-18")
        course["cycle"]["planned_weekdays"] = [0, 2, 4]
        self.assertEqual(review_schedule(course, progress)[0]["date"], "2026-10-16")

    def test_imported_links_are_graded_and_missing_images_still_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root, "测试课程", "导入边界", "Asia/Shanghai")
            body = root / "materials/parsed/book/正文.md"
            write_text(body, "[辅助目录](missing.md)")
            report = validate(root, check_generated=False)
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["material_issues"][0]["severity"], "warning")
            self.assertFalse(validate(root, check_generated=False, strict_materials=True)["ok"])
            write_text(body, "![必要示意图](missing.png)")
            self.assertFalse(validate(root, check_generated=False)["ok"])
            write_text(body, '<img src="missing.png">')
            write_text(root / "materials/parsed/book/正文.html", body.read_text())
            self.assertFalse(validate(root, check_generated=False)["ok"])
            quick = validate(root, check_generated=False, quick=True)
            self.assertTrue(quick["ok"])
            self.assertFalse(quick["source_fingerprints_checked"])
            self.assertFalse(quick["imported_documents_checked"])
            write_text(root / "index.html", '<a href="materials/parsed/book/missing.md">教材正文</a>')
            self.assertFalse(validate(root, check_generated=False)["ok"])

    def test_quick_validation_skips_full_source_fingerprints(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root, "测试课程", "轻量检查", "Asia/Shanghai")
            (root / "materials/book.pdf").write_bytes(b"source fixture")
            write_json(root, "materials/catalogue.json", {"sources": [{"id": "book", "path": "materials/book.pdf", "sha256": "wrong", "page_count": 1}]})
            self.assertFalse(validate(root, check_generated=False)["ok"])
            with patch("validate.hashlib.sha256", side_effect=AssertionError("quick 不应读取全文指纹")):
                self.assertTrue(validate(root, check_generated=False, quick=True)["ok"])

    def test_clock_command_updates_reading_views(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root, "测试课程", "测试目标", "Asia/Shanghai")
            command = [sys.executable, "-B", str(Path(__file__).with_name("session_clock.py")), "--root", str(root), "start", "--minutes", "25"]
            subprocess.run(command, check=True, capture_output=True, text=True)
            self.assertIn("本场进行中", (root / "index.html").read_text())
            self.assertTrue(validate(root)["ok"])

    def test_period_snapshot_requires_actual_review_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root, "测试课程", "测试目标", "Asia/Shanghai")
            progress = read_json(root, "data/progress.json")
            period = {"id": "2026-W42", "start": "2026-10-12", "end": "2026-10-18", "status": "待复盘", "review_plan": []}
            progress["periods"] = [period]
            write_json(root, "data/progress.json", progress)
            with self.assertRaises(ValueError):
                snapshot_period(root, period["id"])
            write_text(root / "records/review-evidence.md", "测试夹具中的实际复盘记录")
            period.update({"status": "已复盘", "review_completed_at": "2026-10-18T15:00:00+08:00", "review_evidence": "records/review-evidence.md"})
            progress["days"] = [{"date": "2026-10-05", "duration_seconds": None}, {"date": "2026-10-16", "duration_seconds": 600}]
            progress["mistakes"] = [{"id": "old", "date": "2026-10-05", "original": "原答", "correction": "订正", "latest_note": "周期后复查正确", "latest_date": "2026-10-20"},
                                    {"id": "new", "date": "2026-10-16", "original": "本周期原答", "correction": "订正"},
                                    {"id": "later", "date": "2026-10-19", "original": "后续周期原答", "correction": "订正"}]
            write_json(root, "data/progress.json", progress)
            generated_at = START + timedelta(days=7)
            with patch("render.current_time", return_value=generated_at):
                snapshot_period(root, period["id"])
            frozen = read_json(root, "records/periods/2026-W42.json")
            self.assertEqual(frozen["snapshot"]["generated_at"], generated_at.isoformat())
            self.assertEqual(frozen["snapshot"]["state_basis"], "generation_time")
            self.assertEqual([day["date"] for day in frozen["days"]], ["2026-10-16"])
            self.assertEqual([mistake["id"] for mistake in frozen["mistakes"]], ["old", "new"])
            self.assertEqual(frozen["mistakes"][0]["latest_date"], "2026-10-20")
            before = (root / "records/periods/2026-W42.json").read_bytes()
            with self.assertRaises(ValueError):
                snapshot_period(root, period["id"])
            self.assertEqual((root / "records/periods/2026-W42.json").read_bytes(), before)


    def parsed_fixture(self, root):
        initialize(root, "测试课程", "测试目标", "Asia/Shanghai")
        directory = root / "materials/parsed/book"
        directory.mkdir(parents=True)
        body = directory / "正文.md"
        body.write_text("旧正文", encoding="utf-8")
        manifest = {"source_id": "book", "source_sha256": "original-pdf", "files": [
            {"path": body.name, "sha256": hashlib.sha256(body.read_bytes()).hexdigest()}]}
        write_json(root, "materials/parsed/book/import-manifest.json", manifest)
        write_json(root, "materials/catalogue.json", {"sources": [{"id": "book", "sha256": "original-pdf", "parsed": {
            "directory": "materials/parsed/book", "status": "partial", "manifest": "materials/parsed/book/import-manifest.json"}}]})
        return directory, body

    def test_parsed_scan_is_read_only_and_reuses_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.parsed_fixture(root)
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            report = scan_materials(root)
            self.assertFalse(report["needs_import"])
            self.assertEqual(report["sources"][0]["state"], "unchanged")
            self.assertEqual(report["sources"][0]["registered_quality_status"], "partial")
            self.assertEqual(before, {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_parsed_scan_detects_content_change_with_same_size_and_mtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory, body = self.parsed_fixture(root)
            original = body.stat()
            body.write_text("新正文", encoding="utf-8")
            os.utime(body, ns=(original.st_atime_ns, original.st_mtime_ns))
            self.assertEqual(body.stat().st_size, original.st_size)
            report = scan_materials(root)
            self.assertEqual(report["sources"][0]["modified"], [body.name])
            self.assertTrue(report["needs_import"])

    def test_parsed_scan_detects_assets_missing_and_unassigned_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory, body = self.parsed_fixture(root)
            body.unlink()
            (directory / "图.png").write_bytes(b"fixture-image")
            (root / "materials/parsed/新资料.md").write_text("新资料", encoding="utf-8")
            report = scan_materials(root)
            self.assertEqual(report["sources"][0]["missing"], [body.name])
            self.assertEqual(report["sources"][0]["added"], ["图.png"])
            self.assertEqual(report["unassigned_files"], ["新资料.md"])
            self.assertTrue(report["needs_import"])

    def test_parsed_scan_reports_invalid_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory, body = self.parsed_fixture(root)
            (directory / "import-manifest.json").write_text("{bad", encoding="utf-8")
            report = scan_materials(root)
            self.assertTrue(report["needs_import"])
            self.assertTrue(report["sources"][0]["issues"])

    def test_relocated_skill_builds_and_checks_paths_with_spaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "学习项目 with spaces"
            relocated = root / ".agents/skills/自定义位置"
            shutil.copytree(Path(__file__).resolve().parents[1], relocated, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            script = relocated / "scripts/render.py"
            subprocess.run([sys.executable, "-B", str(script), "--root", str(root), "init", "--title", "测试", "--goal", "迁移验证"], check=True, capture_output=True, text=True)
            course = read_json(root, "data/course.json")
            self.assertEqual(course["skill_path"], ".agents/skills/自定义位置/SKILL.md")
            self.assertIn(".agents/skills/自定义位置/scripts/render.py", (root / "学习上下文.md").read_text())
            subprocess.run([sys.executable, "-B", str(relocated / "scripts/validate.py"), "--root", str(root)], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
