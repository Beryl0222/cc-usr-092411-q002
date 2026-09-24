"""医学教学数据沙箱的领域契约测试。

覆盖需求中的全部治理承诺：
限时切片与脱敏、披露检查与小样本阻断、跨校到期收权、
勘误/升级前向影响、同意撤回即时效力、已评分作业指纹冻结、
教师复现与身份隔离、结论四要素溯源。
"""

import copy
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from domain import (
    EXPORT_APPROVED,
    EXPORT_BLOCKED,
    STATUS_GRADED,
    STATUS_REVOKED,
    STATUS_SANDBOX,
    AuthorizationError,
    NotFound,
    Sandbox,
    SandboxError,
    VersionConflict,
)

SEED = "fixtures/seed.json"
T = lambda s: datetime.fromisoformat(s)  # noqa: E731


class SeedTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def test_shared_case_enters_two_courses(self):
        case = self.box.cases["CASE-AML"]
        self.assertEqual(set(case["courses"]), {"C-LOCAL", "C-CROSS"})

    def test_six_ledgers_are_separate(self):
        # 六类分账各自独立存放，不混入一个总表
        for ledger in (self.box.cases, self.box.snapshots, self.box.consents,
                       self.box.policies, self.box.environment_versions,
                       self.box.assignments):
            self.assertGreater(len(ledger), 0)
        # 病例账不含数据行，也不含作业结论——分账不混存
        self.assertNotIn("rows", self.box.cases["CASE-AML"])
        self.assertNotIn("conclusion", self.box.cases["CASE-AML"])


class TimeLimitedSliceTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox.from_seed(SEED)
        self.now = T("2026-04-01T09:00:00")

    def test_slice_is_deidentified_and_objective_scoped(self):
        session = self.box.issue_slice("S-LIN", "TASK-LOCAL-Q1", self.now)
        self.assertEqual(session["status"], STATUS_SANDBOX)
        row = session["rows"][0]
        # 只含教学目标字段，且身份字段永不下发、城市被丢弃
        self.assertEqual(set(row), {"age", "diagnosis", "cell_type", "marker"})
        self.assertNotIn("patient_id", row)
        self.assertNotIn("city", row)
        # 年龄按十岁段泛化
        self.assertEqual(row["age"], "40-49")
        self.assertTrue(session["expires_at"] > self.now)

    def test_slice_expires(self):
        session = self.box.issue_slice("S-LIN", "TASK-LOCAL-Q1", self.now,
                                       ttl_minutes=240)
        after = session["expires_at"]
        dead = self.box._session_live(session, after)
        self.assertEqual(dead, "切片过期")

    def test_student_from_other_course_cannot_get_slice(self):
        # 王学生只注册跨校课程，不能取得本校课程切片
        with self.assertRaises(AuthorizationError):
            self.box.issue_slice("S-WANG", "TASK-LOCAL-Q1", self.now)


class DisclosureTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox.from_seed(SEED)
        self.now = T("2026-04-01T09:00:00")
        self.session = self.box.issue_slice("S-LIN", "TASK-LOCAL-Q1", self.now)

    def _export(self, export_id, groups, columns):
        return self.box.request_export(
            export_id, "S-LIN", self.session["id"], groups, columns, self.now,
            assignment_id="AS-LIN-01")

    def test_small_sample_group_is_blocked(self):
        # AML-M5 只有 3 例，低于 k≥5
        record = self._export("EXP-1", [{"key": "AML-M5", "count": 3}],
                              ["diagnosis", "cell_type"])
        self.assertEqual(record["decision"], EXPORT_BLOCKED)
        self.assertTrue(any("小样本" in reason for reason in record["reasons"]))

    def test_compliant_export_is_approved(self):
        record = self._export("EXP-2", [{"key": "AML-M2", "count": 5}],
                              ["diagnosis", "cell_type", "age"])
        self.assertEqual(record["decision"], EXPORT_APPROVED)
        self.assertEqual(record["reasons"], [])

    def test_identity_column_is_blocked_even_with_enough_rows(self):
        record = self._export("EXP-3", [{"key": "AML-M2", "count": 8}],
                              ["diagnosis", "patient_name"])
        self.assertEqual(record["decision"], EXPORT_BLOCKED)
        self.assertTrue(any("身份字段" in reason for reason in record["reasons"]))

    def test_export_after_slice_expiry_is_blocked(self):
        later = T("2026-04-01T14:00:00")  # 默认 240 分钟后
        record = self.box.request_export(
            "EXP-4", "S-LIN", self.session["id"],
            [{"key": "AML-M2", "count": 5}], ["diagnosis"], later)
        self.assertEqual(record["decision"], EXPORT_BLOCKED)
        self.assertIn("切片过期", record["reasons"])


class CrossCourseWithdrawalTest(unittest.TestCase):
    """同一病例进入两个课程，教学中途撤回同意。"""

    WITHDRAW_AT = T("2026-05-05T12:00:00")

    def setUp(self):
        self.box = Sandbox.from_seed(SEED)
        day_before = T("2026-05-04T09:00:00")
        self.local_session = self.box.issue_slice(
            "S-LIN", "TASK-LOCAL-Q1", day_before, ttl_minutes=2880)
        self.cross_session = self.box.issue_slice(
            "S-WANG", "TASK-CROSS-Q1", day_before, ttl_minutes=2880)
        # 撤回前：合规导出曾获批；小样本导出在披露队列
        self.box.request_export(
            "EXP-OK", "S-LIN", self.local_session["id"],
            [{"key": "AML-M2", "count": 5}], ["diagnosis"], day_before)
        self.box.request_export(
            "EXP-SMALL", "S-WANG", self.cross_session["id"],
            [{"key": "AML-M5", "count": 3}], ["diagnosis"], day_before)

    def test_withdrawal_blocks_small_sample_and_live_exports(self):
        self.assertEqual(self.box.exports["EXP-SMALL"]["decision"], EXPORT_BLOCKED)
        self.assertEqual(self.box.exports["EXP-OK"]["decision"], EXPORT_APPROVED)
        self.box.withdraw_consent("CONS-AML-TEACH", self.WITHDRAW_AT)
        # 小样本导出保持阻断；曾获批的导出因撤回立即失效
        self.assertEqual(self.box.exports["EXP-SMALL"]["decision"], EXPORT_BLOCKED)
        self.assertEqual(self.box.exports["EXP-OK"]["decision"], EXPORT_BLOCKED)
        self.assertIn("同意撤回", self.box.exports["EXP-OK"]["reasons"])

    def test_withdrawal_revokes_sessions_in_both_courses(self):
        self.box.withdraw_consent("CONS-AML-TEACH", self.WITHDRAW_AT)
        self.assertEqual(self.local_session["status"], STATUS_REVOKED)
        self.assertEqual(self.cross_session["status"], STATUS_REVOKED)
        # 撤回后两门课都不能再取切片
        with self.assertRaises(AuthorizationError):
            self.box.issue_slice("S-LIN", "TASK-LOCAL-Q1", self.WITHDRAW_AT)
        with self.assertRaises(AuthorizationError):
            self.box.issue_slice("S-WANG", "TASK-CROSS-Q1", self.WITHDRAW_AT)

    def test_affected_assignments_are_listed_across_both_courses(self):
        report = self.box.withdraw_consent("CONS-AML-TEACH", self.WITHDRAW_AT)
        ids = {row["assignment_id"] for row in report["assignments"]}
        self.assertEqual(ids, {"AS-LIN-01", "AS-WANG-01", "AS-GAO-01"})
        courses = {row["course_id"] for row in report["assignments"]}
        self.assertEqual(courses, {"C-LOCAL", "C-CROSS"})

    def test_graded_work_keeps_fingerprint_and_is_flagged(self):
        self.box.withdraw_consent("CONS-AML-TEACH", self.WITHDRAW_AT)
        lin = self.box.assignments["AS-LIN-01"]
        self.assertEqual(lin["status"], STATUS_GRADED)
        self.assertEqual(lin["grade"], "A")
        self.assertEqual(lin["conclusion"],
                         "AML-M2 组原始粒细胞占比高，CD34 阳性为主")
        self.assertEqual(lin["fingerprint"]["snapshot"]["version"], 1)
        self.assertEqual(lin["fingerprint"]["environment"]["version"], 1)
        types = {flag["type"] for flag in lin["risk_flags"]}
        self.assertIn("同意撤回", types)

    def test_new_small_sample_request_after_withdrawal_stays_blocked(self):
        self.box.withdraw_consent("CONS-AML-TEACH", self.WITHDRAW_AT)
        # 撤回后仍在试图导出小样本组：会话已撤回且小样本，双重阻断
        record = self.box.request_export(
            "EXP-AFTER", "S-WANG", self.cross_session["id"],
            [{"key": "AML-M5", "count": 3}], ["diagnosis"], self.WITHDRAW_AT)
        self.assertEqual(record["decision"], EXPORT_BLOCKED)
        self.assertTrue(any("小样本" in r for r in record["reasons"]))
        self.assertTrue(any("同意撤回" in r or "撤回" in r for r in record["reasons"]))

    def test_double_withdraw_is_rejected(self):
        self.box.withdraw_consent("CONS-AML-TEACH", self.WITHDRAW_AT)
        with self.assertRaises(SandboxError):
            self.box.withdraw_consent("CONS-AML-TEACH", self.WITHDRAW_AT)


class ErratumAndUpgradeTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def test_graded_fingerprint_pins_old_versions(self):
        lin = self.box.assignments["AS-LIN-01"]
        self.assertEqual(lin["fingerprint"]["snapshot"]["version"], 1)
        self.assertEqual(lin["fingerprint"]["environment"]["version"], 1)
        types = {f["type"] for f in lin["risk_flags"]}
        self.assertEqual(types, {"病例勘误", "工具升级"})

    def test_old_snapshot_remains_immutable_after_erratum(self):
        # 勘误以新版本发布，v1 内容绝不被改写
        v1 = self.box.snapshots[("CASE-AML", 1)]
        self.assertEqual(v1["rows"][2]["cell_type"], "早幼粒细胞")
        self.assertEqual(self.box.snapshots[("CASE-AML", 2)]["rows"][2]["cell_type"],
                         "异常早幼粒细胞")

    def test_new_tasks_pin_latest_versions_only_at_creation(self):
        # 5 月新建的任务自动钉到新版本
        may_task = self.box.tasks["TASK-LOCAL-Q2"]
        self.assertEqual((may_task["snapshot_version"], may_task["environment_version"]),
                         (2, 2))
        # 但 4 月时点创建的任务只能钉到当时已发布的版本
        april_task = self.box.create_task(
            "TASK-CHECK", "C-LOCAL", "CASE-AML",
            ["diagnosis"], "POL-K5", "ENV-SCANPY", now=T("2026-04-01T00:00:00"))
        self.assertEqual((april_task["snapshot_version"], april_task["environment_version"]),
                         (1, 1))

    def test_grading_is_append_only(self):
        with self.assertRaises(SandboxError):
            self.box.grade_assignment(
                "T-CHEN", "AS-LIN-01", "C", "改分", T("2026-05-20T00:00:00"))


class TeacherReproduceTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def test_teacher_reproduces_in_frozen_environment_without_identity(self):
        report = self.box.reproduce_report(
            "T-CHEN", "AS-LIN-01", T("2026-05-20T00:00:00"))
        self.assertEqual(report["status"], "复现一致")
        self.assertTrue(report["fingerprint_intact"])
        self.assertTrue(report["rerun"]["matches_graded"])
        self.assertEqual(report["identity_access"], "拒绝")
        self.assertIn("patient_id", report["identity_fields"])

    def test_teacher_cannot_reproduce_other_course(self):
        # 陈教师无权复现跨校课程的作业，即便病例相同
        with self.assertRaises(AuthorizationError):
            self.box.reproduce_report("T-CHEN", "AS-GAO-01",
                                      T("2026-05-20T00:00:00"))
        # 赵教师可以复现自己课程的作业
        report = self.box.reproduce_report(
            "T-ZHAO", "AS-GAO-01", T("2026-05-20T00:00:00"))
        self.assertEqual(report["status"], "复现一致")

    def test_teacher_capability_never_includes_identity_read(self):
        with self.assertRaises(AuthorizationError):
            self.box.read_patient_identity("T-CHEN", "CASE-AML")

    def test_repro_detects_tool_missing_in_frozen_image(self):
        # 作业使用了冻结镜像里不存在的工具 → 复现必须判为不一致
        now = T("2026-04-01T09:00:00")
        session = self.box.issue_slice("S-LIN", "TASK-LOCAL-Q1", now)
        self.box.submit_assignment(
            "AS-TAMPER", "S-LIN", "TASK-LOCAL-Q1", "可疑结论",
            [{"step": "用外部工具重聚类", "tool": "seurat"}], now,
            slice_ids=[session["id"]])
        self.box.grade_assignment(
            "T-CHEN", "AS-TAMPER", "C", "工具来源存疑", T("2026-04-05T00:00:00"))
        report = self.box.reproduce_report(
            "T-CHEN", "AS-TAMPER", T("2026-05-20T00:00:00"))
        self.assertEqual(report["status"], "复现不一致")
        self.assertEqual(report["rerun"]["missing_tools"], ["seurat"])


class ExpiryTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox.from_seed(SEED)
        self.session = self.box.issue_slice(
            "S-WANG", "TASK-CROSS-Q1", T("2026-05-14T09:00:00"),
            ttl_minutes=2880)

    def test_cross_institutional_course_expiry_revokes_access(self):
        revoked = self.box.sweep_expired(T("2026-05-16T00:00:00"))
        self.assertIn("S-WANG@C-CROSS", revoked)
        self.assertIn("S-GAO@C-CROSS", revoked)
        # 进行中的沙箱会话一并收回
        self.assertEqual(self.session["status"], STATUS_REVOKED)
        with self.assertRaises(AuthorizationError):
            self.box.issue_slice("S-WANG", "TASK-CROSS-Q1",
                                 T("2026-05-16T09:00:00"))

    def test_local_course_unaffected_when_cross_course_expires(self):
        self.box.sweep_expired(T("2026-05-16T00:00:00"))
        enrollment = self.box._enrollment("S-LIN", "C-LOCAL")
        self.assertNotEqual(enrollment["status"], STATUS_REVOKED)
        # 本校课程仍开放，切片正常
        session = self.box.issue_slice(
            "S-LIN", "TASK-LOCAL-Q2", T("2026-05-16T09:00:00"))
        self.assertEqual(session["status"], STATUS_SANDBOX)

    def test_sweep_is_idempotent(self):
        first = self.box.sweep_expired(T("2026-07-01T00:00:00"))
        second = self.box.sweep_expired(T("2026-07-02T00:00:00"))
        self.assertIn("S-LIN@C-LOCAL", first)
        self.assertEqual(second, [])


class TraceTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def test_trace_has_four_required_parts(self):
        trace = self.box.trace("AS-LIN-01")
        self.assertIn("数据范围", trace)
        self.assertIn("处理步骤", trace)
        self.assertIn("课程授权", trace)
        self.assertIn("教师复核", trace)

    def test_trace_points_to_exact_data_scope(self):
        scope = self.box.trace("AS-LIN-01")["数据范围"]
        self.assertEqual(scope["case_id"], "CASE-AML")
        self.assertEqual(scope["snapshot_version"], 1)
        self.assertEqual(scope["objective_fields"],
                         ["age", "diagnosis", "cell_type", "marker"])
        self.assertEqual(len(scope["content_hash"]), 16)

    def test_trace_records_authorization_lineage(self):
        auth = self.box.trace("AS-LIN-01")["课程授权"]
        self.assertEqual(auth["course_id"], "C-LOCAL")
        self.assertEqual(auth["student_id"], "S-LIN")
        self.assertEqual(auth["consents"][0]["consent_id"], "CONS-AML-TEACH")
        self.assertEqual(auth["consents"][0]["withdrawn_at"], None)

    def test_trace_records_teacher_review(self):
        reviews = self.box.trace("AS-LIN-01")["教师复核"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["teacher_id"], "T-CHEN")
        self.assertEqual(reviews[0]["grade"], "A")

    def test_trace_shows_withdrawal_after_it_happens(self):
        self.box.withdraw_consent("CONS-AML-TEACH", T("2026-05-05T12:00:00"))
        consent = self.box.trace("AS-LIN-01")["课程授权"]["consents"][0]
        self.assertEqual(consent["withdrawn_at"], "2026-05-05T12:00:00")
        self.assertTrue(
            any(f["type"] == "同意撤回" for f in self.box.trace("AS-LIN-01")["风险标记"]))

    def test_case_listing_covers_both_courses(self):
        listing = self.box.assignments_for_case("CASE-AML")
        self.assertEqual({row["assignment_id"] for row in listing},
                         {"AS-LIN-01", "AS-WANG-01", "AS-GAO-01"})
        self.assertEqual({row["course_id"] for row in listing},
                         {"C-LOCAL", "C-CROSS"})


class VersionImmutabilityTest(unittest.TestCase):
    """事故回归：同版本号被登记异内容时，拒绝覆盖、保留原记录与冲突审计。"""

    def setUp(self):
        self.box = Sandbox.from_seed(SEED)
        self.v1 = self.box.snapshots[("CASE-AML", 1)]

    def _overwrite_v1(self, **changes):
        payload = {
            "rows": [dict(row) for row in self.v1["rows"]],
            "identity_fields": list(self.v1["identity_fields"]),
            "released_at": self.v1["released_at"],
        }
        payload.update(changes)
        return self.box.add_snapshot("CASE-AML", 1, **payload)

    def test_same_version_different_rows_is_rejected_and_original_kept(self):
        rows = [dict(row) for row in self.v1["rows"]]
        rows[2]["cell_type"] = "异常早幼粒细胞"  # 上传者沿用旧版本号塞入新数据
        with self.assertRaises(VersionConflict) as ctx:
            self._overwrite_v1(rows=rows)
        # 原记录原样保留，成绩里冻结的指纹仍指向它
        kept = self.box.snapshots[("CASE-AML", 1)]
        self.assertEqual(kept["rows"][2]["cell_type"], "早幼粒细胞")
        self.assertEqual(kept["content_hash"], self.v1["content_hash"])
        # 冲突审计：说明既有/新提交摘要与差异字段
        conflict = ctx.exception.detail
        self.assertEqual(conflict["ledger"], "snapshots")
        self.assertEqual(conflict["key"], {"case_id": "CASE-AML", "version": 1})
        self.assertEqual(conflict["differing_fields"], ["rows"])
        self.assertEqual(conflict["existing"]["content_hash"], self.v1["content_hash"])
        self.assertNotEqual(conflict["incoming"]["content_hash"],
                            self.v1["content_hash"])
        self.assertEqual(conflict["resolution"], "拒绝覆盖，保留原版本")
        self.assertEqual(self.box.conflicts, [conflict])
        # 已评分作业的冻结指纹不变，教师复现读到的仍是原始数据
        report = self.box.reproduce_report("T-CHEN", "AS-LIN-01",
                                           T("2026-05-20T00:00:00"))
        self.assertEqual(report["status"], "复现一致")
        self.assertTrue(report["snapshot_matches_frozen"])
        self.assertEqual(report["actual_read"]["snapshot"]["content_hash"],
                         report["frozen_fingerprint"]["snapshot"]["content_hash"])
        self.assertEqual([c["seq"] for c in report["conflicts"]],
                         [conflict["seq"]])

    def test_identity_fields_change_is_rejected(self):
        with self.assertRaises(VersionConflict) as ctx:
            self._overwrite_v1(identity_fields=self.v1["identity_fields"] + ["ssn"])
        self.assertEqual(ctx.exception.detail["differing_fields"], ["identity_fields"])
        self.assertEqual(self.box.snapshots[("CASE-AML", 1)]["identity_fields"],
                         ["patient_id", "patient_name", "phone"])

    def test_release_time_change_is_rejected(self):
        with self.assertRaises(VersionConflict) as ctx:
            self._overwrite_v1(released_at=T("2026-02-21T00:00:00"))
        self.assertEqual(ctx.exception.detail["differing_fields"], ["released_at"])
        self.assertEqual(self.box.snapshots[("CASE-AML", 1)]["released_at"],
                         T("2026-02-20T00:00:00"))

    def test_tool_manifest_change_is_rejected(self):
        env_v1 = self.box.environment_versions[("ENV-SCANPY", 1)]
        tools = dict(env_v1["tools"], scanpy="1.10.4")
        with self.assertRaises(VersionConflict) as ctx:
            self.box.add_environment("ENV-SCANPY", 1, tools,
                                     released_at=env_v1["released_at"],
                                     note=env_v1["note"])
        conflict = ctx.exception.detail
        self.assertEqual(conflict["ledger"], "environment_versions")
        self.assertEqual(conflict["differing_fields"], ["tools"])
        self.assertEqual(
            self.box.environment_versions[("ENV-SCANPY", 1)]["tools"]["scanpy"],
            "1.9.8")
        # 冲突审计可按环境筛选
        self.assertEqual(len(self.box.conflicts_for(environment_id="ENV-SCANPY")), 1)
        self.assertEqual(self.box.conflicts_for(case_id="CASE-AML"), [])

    def test_idempotent_replay_is_accepted_without_side_effects(self):
        flags_before = copy.deepcopy(
            {aid: a["risk_flags"] for aid, a in self.box.assignments.items()})
        # 进程恢复/重试场景：完全一致的重复登记是幂等重放
        self.assertIs(self._overwrite_v1(), self.box.snapshots[("CASE-AML", 1)])
        v2 = self.box.snapshots[("CASE-AML", 2)]
        replayed_v2 = self.box.add_snapshot(
            "CASE-AML", 2, [dict(r) for r in v2["rows"]],
            identity_fields=list(v2["identity_fields"]),
            released_at=v2["released_at"], replaces=1,
            erratum_note=v2["erratum_note"])
        self.assertIs(replayed_v2, v2)
        env_v2 = self.box.environment_versions[("ENV-SCANPY", 2)]
        self.assertIs(self.box.add_environment(
            "ENV-SCANPY", 2, dict(env_v2["tools"]),
            released_at=env_v2["released_at"], note=env_v2["note"],
            replaces=1), env_v2)
        # 无冲突审计、无新增版本、无重复风险标记
        self.assertEqual(self.box.conflicts, [])
        self.assertEqual(len(self.box.snapshots), 2)
        self.assertEqual(len(self.box.environment_versions), 2)
        self.assertEqual(flags_before, {aid: a["risk_flags"]
                                        for aid, a in self.box.assignments.items()})

    def test_rejected_overwrite_keeps_trace_on_original(self):
        rows = [dict(row) for row in self.v1["rows"]]
        rows[0]["diagnosis"] = "AML-M5"
        with self.assertRaises(VersionConflict):
            self._overwrite_v1(rows=rows)
        trace = self.box.trace("AS-LIN-01")
        self.assertEqual(trace["数据范围"]["snapshot_version"], 1)
        self.assertEqual(trace["数据范围"]["content_hash"], self.v1["content_hash"])
        self.assertEqual(trace["数据范围"]["registration_hash"],
                         self.v1["registration_hash"])
        self.assertEqual(len(trace["冲突审计"]), 1)
        self.assertEqual(trace["冲突审计"][0]["differing_fields"], ["rows"])


class VersionChainTest(unittest.TestCase):
    """新版本只能显式引用所替代的当前最新版本。"""

    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def _rows(self, marker):
        rows = [dict(r) for r in self.box.snapshots[("CASE-AML", 2)]["rows"]]
        rows[0]["marker"] = marker
        return rows

    def test_erratum_without_replaces_is_rejected(self):
        with self.assertRaises(SandboxError) as ctx:
            self.box.add_snapshot("CASE-AML", 3, self._rows("CD34+"),
                                  identity_fields=["patient_id"],
                                  released_at=T("2026-05-20T00:00:00"))
        self.assertIn("replaces=2", str(ctx.exception))

    def test_replaces_must_point_at_latest_version(self):
        with self.assertRaises(SandboxError) as ctx:
            self.box.add_snapshot("CASE-AML", 3, self._rows("CD34+"),
                                  identity_fields=["patient_id"],
                                  released_at=T("2026-05-20T00:00:00"),
                                  replaces=1)  # 当前最新是 v2
        self.assertIn("v2", str(ctx.exception))

    def test_first_version_cannot_reference_replaces(self):
        with self.assertRaises(NotFound):
            self.box.add_snapshot("CASE-T2D", 1, [], identity_fields=[],
                                  released_at=T("2026-05-20T00:00:00"),
                                  replaces=9)

    def test_version_must_advance_beyond_replaced(self):
        self.box.add_snapshot("CASE-T2D", 5, [], identity_fields=[],
                              released_at=T("2026-05-20T00:00:00"))
        with self.assertRaises(SandboxError):
            self.box.add_snapshot("CASE-T2D", 4, [], identity_fields=[],
                                  released_at=T("2026-05-21T00:00:00"), replaces=5)

    def test_proper_erratum_chain_preserves_history(self):
        record = self.box.add_snapshot(
            "CASE-AML", 3, self._rows("CD34+CD13+"),
            identity_fields=["patient_id", "patient_name", "phone"],
            released_at=T("2026-05-20T00:00:00"),
            replaces=2, erratum_note="P-001 标记补充")
        self.assertEqual(record["replaces"], 2)
        # 旧版本原样保留
        self.assertEqual(self.box.snapshots[("CASE-AML", 1)]["rows"][2]["cell_type"],
                         "早幼粒细胞")
        self.assertEqual(self.box.snapshots[("CASE-AML", 2)]["rows"][2]["cell_type"],
                         "异常早幼粒细胞")
        # 钉住旧版本的已评分作业被追加勘误标记（含钉 v1 的作业）
        lin_versions = {f["current_version"]
                        for f in self.box.assignments["AS-LIN-01"]["risk_flags"]
                        if f["type"] == "病例勘误"}
        self.assertEqual(lin_versions, {2, 3})
        # 既有任务仍钉原版本；新任务创建时才取 v3
        self.assertEqual(self.box.tasks["TASK-LOCAL-Q2"]["snapshot_version"], 2)
        task = self.box.create_task("TASK-NEW", "C-LOCAL", "CASE-AML",
                                    ["diagnosis"], "POL-K5", "ENV-SCANPY",
                                    now=T("2026-05-21T09:00:00"))
        self.assertEqual(task["snapshot_version"], 3)

    def test_environment_upgrade_requires_explicit_replaces(self):
        with self.assertRaises(SandboxError):
            self.box.add_environment("ENV-SCANPY", 3, {"python": "3.12"},
                                     released_at=T("2026-05-20T00:00:00"))
        record = self.box.add_environment(
            "ENV-SCANPY", 3, {"python": "3.12"},
            released_at=T("2026-05-20T00:00:00"), replaces=2, note="解释器升级")
        self.assertEqual(record["replaces"], 2)
        # 旧镜像原样保留
        self.assertEqual(
            self.box.environment_versions[("ENV-SCANPY", 2)]["tools"]["scanpy"],
            "1.10.4")
        gao_currents = {f["current"]
                        for f in self.box.assignments["AS-GAO-01"]["risk_flags"]
                        if f["type"] == "工具升级"}
        self.assertIn("ENV-SCANPY:v3", gao_currents)


class BatchRegistrationTest(unittest.TestCase):
    """批量登记：任一冲突整体拒绝，不留部分版本或错误风险标记。"""

    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def _snapshot_entry(self, version, replaces, marker, note=""):
        rows = [dict(r, marker=marker)
                for r in self.box.snapshots[("CASE-AML", 2)]["rows"]]
        return {"kind": "snapshot", "case_id": "CASE-AML", "version": version,
                "rows": rows, "identity_fields": ["patient_id"],
                "released_at": T("2026-05-20T00:00:00"), "replaces": replaces,
                "erratum_note": note}

    def test_conflicting_batch_leaves_no_partial_state(self):
        flags_before = copy.deepcopy(
            {aid: a["risk_flags"] for aid, a in self.box.assignments.items()})
        bad_rows = [dict(r) for r in self.box.snapshots[("CASE-AML", 1)]["rows"]]
        bad_rows[0]["age"] = 99
        entries = [
            self._snapshot_entry(3, 2, "CD34+"),
            {"kind": "environment", "id": "ENV-SCANPY", "version": 3,
             "tools": {"python": "3.12"}, "released_at": "2026-05-20T00:00:00",
             "replaces": 2},
            {"kind": "snapshot", "case_id": "CASE-AML", "version": 1,
             "rows": bad_rows, "identity_fields": ["patient_id", "patient_name", "phone"],
             "released_at": "2026-02-20T00:00:00"},  # 同版本异内容 → 冲突
        ]
        with self.assertRaises(VersionConflict):
            self.box.register_versions(entries)
        # 不留部分版本
        self.assertNotIn(("CASE-AML", 3), self.box.snapshots)
        self.assertNotIn(("ENV-SCANPY", 3), self.box.environment_versions)
        # 不留错误风险标记
        self.assertEqual(flags_before, {aid: a["risk_flags"]
                                        for aid, a in self.box.assignments.items()})
        # 冲突审计保留，说明冲突来源
        self.assertEqual(len(self.box.conflicts), 1)
        self.assertEqual(self.box.conflicts[0]["key"],
                         {"case_id": "CASE-AML", "version": 1})
        self.assertEqual(self.box.conflicts[0]["differing_fields"], ["rows"])

    def test_valid_batch_commits_chain_atomically(self):
        entries = [
            self._snapshot_entry(3, 2, "CD34+", note="v3 勘误"),
            {"kind": "snapshot", "case_id": "CASE-AML", "version": 4,
             "rows": [dict(r) for r in self.box.snapshots[("CASE-AML", 2)]["rows"]],
             "identity_fields": ["patient_id"],
             "released_at": T("2026-05-21T00:00:00"),
             "replaces": 3, "erratum_note": "v4 勘误"},
            {"kind": "environment", "id": "ENV-SCANPY", "version": 3,
             "tools": {"python": "3.12"}, "released_at": "2026-05-20T00:00:00",
             "replaces": 2},
        ]
        results = self.box.register_versions(entries)
        self.assertEqual([r["outcome"] for r in results],
                         ["created", "created", "created"])
        self.assertEqual(self.box.snapshots[("CASE-AML", 4)]["replaces"], 3)
        self.assertIn(("ENV-SCANPY", 3), self.box.environment_versions)
        self.assertEqual(self.box.conflicts, [])

    def test_batch_replay_after_recovery_is_idempotent(self):
        entries = [self._snapshot_entry(3, 2, "CD34+", note="v3 勘误")]
        first = self.box.register_versions(entries)
        flags_before = copy.deepcopy(
            {aid: a["risk_flags"] for aid, a in self.box.assignments.items()})
        # 进程恢复后重放同一批次：全部幂等，无重复版本、无新增标记、无冲突
        second = self.box.register_versions(entries)
        self.assertEqual([r["outcome"] for r in first], ["created"])
        self.assertEqual([r["outcome"] for r in second], ["replayed"])
        self.assertEqual(len(self.box.snapshots), 3)
        self.assertEqual(self.box.conflicts, [])
        self.assertEqual(flags_before, {aid: a["risk_flags"]
                                        for aid, a in self.box.assignments.items()})

    def test_unknown_entry_kind_is_rejected(self):
        with self.assertRaises(SandboxError):
            self.box.register_versions([{"kind": "consent"}])


class ConcurrencyTest(unittest.TestCase):
    """并发上传同一版本：只有一个胜出，结果唯一。"""

    def test_concurrent_conflicting_uploads_have_single_winner(self):
        box = Sandbox.from_seed(SEED)
        base_rows = [dict(r) for r in box.snapshots[("CASE-AML", 2)]["rows"]]
        winners, losers = [], []

        def upload(i):
            rows = [dict(r, marker=f"M{i}") for r in base_rows]
            try:
                box.add_snapshot("CASE-AML", 3, rows,
                                 identity_fields=["patient_id"],
                                 released_at=T("2026-05-20T00:00:00"),
                                 replaces=2, erratum_note=f"并发-{i}")
                winners.append(i)
            except VersionConflict:
                losers.append(i)

        threads = [threading.Thread(target=upload, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # 唯一结果：一个胜出，其余全部冲突留痕
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 7)
        self.assertEqual(len(box.conflicts), 7)
        stored = box.snapshots[("CASE-AML", 3)]
        self.assertEqual(stored["erratum_note"], f"并发-{winners[0]}")
        self.assertEqual(stored["rows"][0]["marker"], f"M{winners[0]}")

    def test_concurrent_identical_uploads_all_replay(self):
        box = Sandbox.from_seed(SEED)
        base_rows = [dict(r) for r in box.snapshots[("CASE-AML", 2)]["rows"]]
        errors = []

        def upload():
            try:
                box.add_snapshot("CASE-AML", 3, [dict(r) for r in base_rows],
                                 identity_fields=["patient_id"],
                                 released_at=T("2026-05-20T00:00:00"),
                                 replaces=2, erratum_note="同一批上传")
            except SandboxError as exc:  # noqa: F841
                errors.append(exc)

        threads = [threading.Thread(target=upload) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(box.snapshots), 3)
        self.assertEqual(box.conflicts, [])
        # 勘误标记只追加一次：AS-LIN-01 与 AS-GAO-01 各一条
        v3_flags = [f for a in box.assignments.values() for f in a["risk_flags"]
                    if f["type"] == "病例勘误" and f.get("current_version") == 3]
        self.assertEqual(len(v3_flags), 2)


class ProcessRecoveryTest(unittest.TestCase):
    """进程恢复：从夹具重建后，风险标记与登记指纹与运行期一致。"""

    def _flag_multiset(self, assignment):
        return sorted(json.dumps(f, ensure_ascii=False, sort_keys=True)
                      for f in assignment["risk_flags"])

    def test_reload_reproduces_live_flags_and_registration_hashes(self):
        live = Sandbox.from_seed(SEED)
        rows_v3 = [dict(r, marker="CD34+")
                   for r in live.snapshots[("CASE-AML", 2)]["rows"]]
        tools_v3 = {"python": "3.12.0", "scanpy": "1.10.4", "pandas": "2.2.2"}
        live.add_snapshot("CASE-AML", 3, rows_v3,
                          identity_fields=["patient_id", "patient_name", "phone"],
                          released_at=T("2026-05-20T00:00:00"),
                          replaces=2, erratum_note="v3 勘误")
        live.add_environment("ENV-SCANPY", 3, tools_v3,
                             released_at=T("2026-05-21T00:00:00"),
                             replaces=2, note="v3 镜像")
        # 把同样的版本写回夹具，模拟崩溃后从持久层恢复
        seed = json.loads(Path(SEED).read_text(encoding="utf-8"))
        seed["snapshots"].append({
            "case_id": "CASE-AML", "version": 3,
            "released_at": "2026-05-20T00:00:00", "replaces": 2,
            "erratum_note": "v3 勘误",
            "identity_fields": ["patient_id", "patient_name", "phone"],
            "rows": rows_v3,
        })
        seed["environments"].append({
            "id": "ENV-SCANPY", "version": 3, "tools": tools_v3,
            "released_at": "2026-05-21T00:00:00", "replaces": 2, "note": "v3 镜像",
        })
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8") as fh:
            json.dump(seed, fh, ensure_ascii=False)
            tmp_path = fh.name
        try:
            recovered = Sandbox.from_seed(tmp_path)
        finally:
            os.unlink(tmp_path)
        for aid in ("AS-LIN-01", "AS-GAO-01"):
            self.assertEqual(self._flag_multiset(live.assignments[aid]),
                             self._flag_multiset(recovered.assignments[aid]))
        self.assertEqual(live.snapshots[("CASE-AML", 3)]["registration_hash"],
                         recovered.snapshots[("CASE-AML", 3)]["registration_hash"])
        self.assertEqual(
            live.environment_versions[("ENV-SCANPY", 3)]["registration_hash"],
            recovered.environment_versions[("ENV-SCANPY", 3)]["registration_hash"])
        self.assertEqual(live.conflicts, [])
        self.assertEqual(recovered.conflicts, [])

    def test_reconcile_is_idempotent_after_live_events(self):
        box = Sandbox.from_seed(SEED)
        box.add_snapshot("CASE-AML", 3,
                         [dict(r) for r in box.snapshots[("CASE-AML", 2)]["rows"]],
                         identity_fields=["patient_id"],
                         released_at=T("2026-05-20T00:00:00"),
                         replaces=2, erratum_note="v3 勘误")
        before = {aid: self._flag_multiset(a)
                  for aid, a in box.assignments.items()}
        box._reconcile_flags()
        after = {aid: self._flag_multiset(a)
                 for aid, a in box.assignments.items()}
        self.assertEqual(before, after)


class CrossCourseConflictTest(unittest.TestCase):
    """跨课程共用病例：覆盖被拒后，两门课程读到的仍是一致的原始版本。"""

    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def test_both_courses_keep_reading_original_after_rejected_overwrite(self):
        v1 = self.box.snapshots[("CASE-AML", 1)]
        rows = [dict(r) for r in v1["rows"]]
        rows[2]["cell_type"] = "异常早幼粒细胞"
        with self.assertRaises(VersionConflict):
            self.box.add_snapshot("CASE-AML", 1, rows,
                                  identity_fields=list(v1["identity_fields"]),
                                  released_at=v1["released_at"])
        # 两门课程的切片仍是原始投影
        now = T("2026-04-01T09:00:00")
        local = self.box.issue_slice("S-LIN", "TASK-LOCAL-Q1", now)
        cross = self.box.issue_slice("S-WANG", "TASK-CROSS-Q1", now)
        self.assertEqual(local["rows"], cross["rows"])
        self.assertEqual(local["rows"][2]["cell_type"], "早幼粒细胞")
        # 两门课程的已评分作业：复现一致，报告与溯源都说明实际读取与冲突来源
        for teacher, aid in (("T-CHEN", "AS-LIN-01"), ("T-ZHAO", "AS-GAO-01")):
            report = self.box.reproduce_report(teacher, aid, T("2026-05-20T00:00:00"))
            self.assertEqual(report["status"], "复现一致")
            self.assertEqual(report["actual_read"]["snapshot"]["version"], 1)
            self.assertTrue(report["snapshot_matches_frozen"])
            self.assertEqual(len(report["conflicts"]), 1)
            trace = self.box.trace(aid)
            self.assertEqual(trace["数据范围"]["snapshot_version"], 1)
            self.assertEqual(len(trace["冲突审计"]), 1)


if __name__ == "__main__":
    unittest.main()
