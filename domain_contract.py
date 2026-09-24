"""医学教学数据沙箱的领域契约测试。

覆盖需求中的全部治理承诺：
限时切片与脱敏、披露检查与小样本阻断、跨校到期收权、
勘误/升级前向影响、同意撤回即时效力、已评分作业指纹冻结、
教师复现与身份隔离、结论四要素溯源、版本登记幂等与冲突审计。
"""

import json
import threading
import unittest
from copy import deepcopy
from datetime import datetime

from domain import (
    EXPORT_APPROVED,
    EXPORT_BLOCKED,
    STATUS_GRADED,
    STATUS_REVOKED,
    STATUS_SANDBOX,
    AuthorizationError,
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


class SnapshotRegistrationTest(unittest.TestCase):
    """版本登记：同版本异内容拒绝覆盖，完全一致才幂等重放，版本链显式引用。"""

    IDENTITY = ["patient_id", "patient_name", "phone"]

    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def _v1_payload(self):
        snap = self.box.snapshots[("CASE-AML", 1)]
        return {
            "case_id": "CASE-AML", "version": 1,
            "rows": deepcopy(snap["rows"]),
            "identity_fields": list(snap["identity_fields"]),
            "released_at": snap["released_at"],
            "replaces": snap["replaces"],
            "erratum_note": snap["erratum_note"],
        }

    def test_identical_replay_is_idempotent(self):
        flags_before = {aid: deepcopy(a["risk_flags"])
                        for aid, a in self.box.assignments.items()}
        audit_before = len(self.box.audit)
        record = self.box.add_snapshot(**self._v1_payload())
        # 幂等重放：返回既有记录，账目不增、无冲突、无审计、无新风险标记
        self.assertIs(record, self.box.snapshots[("CASE-AML", 1)])
        self.assertEqual(len(self.box.snapshots), 2)
        self.assertEqual(self.box.conflicts, [])
        self.assertEqual(len(self.box.audit), audit_before)
        for aid, flags in flags_before.items():
            self.assertEqual(self.box.assignments[aid]["risk_flags"], flags)

    def test_same_version_different_rows_rejected(self):
        payload = self._v1_payload()
        payload["rows"][2]["cell_type"] = "篡改细胞"
        with self.assertRaises(VersionConflict) as ctx:
            self.box.add_snapshot(**payload)
        self.assertIn("数据行", ctx.exception.detail["differing_fields"])
        # 原记录未被覆盖
        self.assertEqual(self.box.snapshots[("CASE-AML", 1)]["rows"][2]["cell_type"],
                         "早幼粒细胞")
        # 冲突审计保留，且与异常指向同一条记录
        conflicts = self.box.snapshot_conflicts("CASE-AML", 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["id"], ctx.exception.detail["conflict_id"])
        self.assertTrue(any(entry["action"] == "版本冲突" for entry in self.box.audit))

    def test_same_version_different_identity_fields_rejected(self):
        payload = self._v1_payload()
        payload["identity_fields"] = ["patient_id", "patient_name"]  # 少了 phone
        with self.assertRaises(VersionConflict) as ctx:
            self.box.add_snapshot(**payload)
        self.assertIn("身份字段", ctx.exception.detail["differing_fields"])
        self.assertEqual(self.box.snapshots[("CASE-AML", 1)]["identity_fields"],
                         self.IDENTITY)

    def test_same_version_different_released_at_rejected(self):
        payload = self._v1_payload()
        payload["released_at"] = T("2026-02-21T00:00:00")
        with self.assertRaises(VersionConflict) as ctx:
            self.box.add_snapshot(**payload)
        self.assertIn("发布时间", ctx.exception.detail["differing_fields"])
        self.assertEqual(self.box.snapshots[("CASE-AML", 1)]["released_at"],
                         T("2026-02-20T00:00:00"))

    def test_same_version_different_lineage_or_note_rejected(self):
        # 版本链声明不同也算异内容
        payload = self._v1_payload()
        payload["replaces"] = 0
        with self.assertRaises(VersionConflict) as ctx:
            self.box.add_snapshot(**payload)
        self.assertIn("版本链", ctx.exception.detail["differing_fields"])
        # 勘误说明不同同样拒绝
        snap2 = self.box.snapshots[("CASE-AML", 2)]
        with self.assertRaises(VersionConflict) as ctx2:
            self.box.add_snapshot(
                "CASE-AML", 2, deepcopy(snap2["rows"]), self.IDENTITY,
                snap2["released_at"], replaces=1, erratum_note="改写后的说明")
        self.assertIn("勘误说明", ctx2.exception.detail["differing_fields"])
        self.assertNotEqual(self.box.snapshots[("CASE-AML", 2)]["erratum_note"],
                            "改写后的说明")

    def test_error_response_states_frozen_fingerprint_and_conflict(self):
        payload = self._v1_payload()
        payload["rows"][2]["cell_type"] = "篡改细胞"
        with self.assertRaises(VersionConflict) as ctx:
            self.box.add_snapshot(**payload)
        response = ctx.exception.as_response()
        frozen = self.box.assignments["AS-LIN-01"]["fingerprint"]["snapshot"]
        # 服务错误响应同时说明：冲突来源、既有内容（=成绩冻结指纹）与登记内容
        self.assertEqual(response["error"], "版本冲突")
        self.assertEqual(response["key"], {"case_id": "CASE-AML", "version": 1})
        self.assertEqual(response["existing"]["content_hash"], frozen["content_hash"])
        self.assertNotEqual(response["incoming"]["content_hash"],
                            frozen["content_hash"])
        self.assertIn("数据行", response["differing_fields"])

    def test_rejected_overwrite_keeps_trace_and_reproduce_coherent(self):
        payload = self._v1_payload()
        payload["rows"][0]["age"] = 99
        with self.assertRaises(VersionConflict) as ctx:
            self.box.add_snapshot(**payload)
        conflict_id = ctx.exception.detail["conflict_id"]
        # 教师复现：实际读取仍是被冻结的 v1 内容，复现一致
        report = self.box.reproduce_report("T-CHEN", "AS-LIN-01",
                                           T("2026-05-20T00:00:00"))
        self.assertEqual(report["status"], "复现一致")
        self.assertTrue(report["read_matches_frozen"])
        self.assertEqual(report["actual_read"]["snapshot"]["content_hash"],
                         report["frozen_fingerprint"]["snapshot"]["content_hash"])
        self.assertEqual([c["id"] for c in report["conflicts"]], [conflict_id])
        # 追溯：两门课程的已评分作业共同指向同一实际读取与冲突来源
        for aid in ("AS-LIN-01", "AS-GAO-01"):
            trace = self.box.trace(aid)
            self.assertEqual([c["id"] for c in trace["版本冲突"]], [conflict_id])
            self.assertEqual(trace["数据范围"]["content_hash"],
                             trace["fingerprint"]["snapshot"]["content_hash"])
            self.assertEqual(trace["数据范围"]["released_at"], "2026-02-20T00:00:00")

    def test_new_version_must_explicitly_replace_latest(self):
        rows = deepcopy(self.box.snapshots[("CASE-AML", 2)]["rows"])
        # 缺 replaces：不允许静默沿用版本链之外的方式发布
        with self.assertRaises(SandboxError):
            self.box.add_snapshot("CASE-AML", 3, rows, self.IDENTITY,
                                  T("2026-06-01T00:00:00"))
        # replaces 指向非当前最新版本：拒绝
        with self.assertRaises(SandboxError):
            self.box.add_snapshot("CASE-AML", 3, rows, self.IDENTITY,
                                  T("2026-06-01T00:00:00"), replaces=1)
        # 首个版本不得声明替代关系
        with self.assertRaises(SandboxError):
            self.box.add_snapshot("CASE-T2D", 1, [], [],
                                  T("2026-06-01T00:00:00"), replaces=1)
        # 显式引用当前最新版本的勘误被接受
        record = self.box.add_snapshot("CASE-AML", 3, rows, self.IDENTITY,
                                       T("2026-06-01T00:00:00"), replaces=2,
                                       erratum_note="v3 勘误")
        self.assertEqual(record["replaces"], 2)
        self.assertEqual(self.box.snapshots[("CASE-AML", 3)], record)

    def test_erratum_flags_graded_assignments_in_both_courses(self):
        rows = deepcopy(self.box.snapshots[("CASE-AML", 2)]["rows"])
        rows[6]["marker"] = "CD64+/CD36+"
        self.box.add_snapshot("CASE-AML", 3, rows, self.IDENTITY,
                              T("2026-06-01T00:00:00"), replaces=2,
                              erratum_note="P-007 标记复核")
        # 跨课程：本校与跨校两门课的已评分作业都收到勘误标记
        for aid in ("AS-LIN-01", "AS-GAO-01"):
            flags = self.box.assignments[aid]["risk_flags"]
            self.assertTrue(any(f["type"] == "病例勘误" and f["current_version"] == 3
                                for f in flags))
        # 未评分作业不打标
        self.assertFalse(any(f.get("current_version") == 3
                             for f in self.box.assignments["AS-WANG-01"]["risk_flags"]))
        # 已创建任务与已评分作业继续使用原不可变内容
        self.assertEqual(self.box.tasks["TASK-LOCAL-Q1"]["snapshot_version"], 1)
        self.assertEqual(self.box.tasks["TASK-LOCAL-Q2"]["snapshot_version"], 2)
        self.assertEqual(
            self.box.assignments["AS-LIN-01"]["fingerprint"]["snapshot"]["version"], 1)
        # 新任务创建时才钉到最新版
        task = self.box.create_task("TASK-NEW", "C-LOCAL", "CASE-AML",
                                    ["diagnosis"], "POL-K5", "ENV-SCANPY",
                                    now=T("2026-06-02T09:00:00"))
        self.assertEqual(task["snapshot_version"], 3)


class EnvironmentRegistrationTest(unittest.TestCase):
    """分析环境版本登记：工具清单异内容拒绝、显式版本链、升级前向标记。"""

    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def _v1_env(self):
        return self.box.environment_versions[("ENV-SCANPY", 1)]

    def test_identical_environment_replay_is_idempotent(self):
        env = self._v1_env()
        before = len(self.box.environment_versions)
        record = self.box.add_environment(
            "ENV-SCANPY", 1, deepcopy(env["tools"]),
            released_at=env["released_at"], note=env["note"])
        self.assertIs(record, env)
        self.assertEqual(len(self.box.environment_versions), before)
        self.assertEqual(self.box.conflicts, [])

    def test_same_version_different_tools_rejected(self):
        env = self._v1_env()
        with self.assertRaises(VersionConflict) as ctx:
            self.box.add_environment("ENV-SCANPY", 1,
                                     dict(env["tools"], scanpy="1.9.9"),
                                     released_at=env["released_at"])
        self.assertIn("工具清单", ctx.exception.detail["differing_fields"])
        # 原记录未被覆盖
        self.assertEqual(self._v1_env()["tools"]["scanpy"], "1.9.8")
        self.assertEqual(len(self.box.environment_conflicts("ENV-SCANPY", 1)), 1)
        # 发布时间变化同样拒绝
        with self.assertRaises(VersionConflict):
            self.box.add_environment("ENV-SCANPY", 1, deepcopy(env["tools"]),
                                     released_at=T("2026-02-02T00:00:00"))
        self.assertEqual(len(self.box.environment_conflicts("ENV-SCANPY", 1)), 2)

    def test_tool_upgrade_requires_chain_and_flags_forward(self):
        # 在钉住 env v2 的任务上产出一份已评分作业
        now = T("2026-05-12T09:00:00")
        session = self.box.issue_slice("S-LIN", "TASK-LOCAL-Q2", now)
        self.box.submit_assignment(
            "AS-UPG", "S-LIN", "TASK-LOCAL-Q2", "基于 v2 环境的差异表达结论",
            [{"step": "差异表达检验", "tool": "scanpy"}], now,
            slice_ids=[session["id"]])
        self.box.grade_assignment("T-CHEN", "AS-UPG", "B", "通过",
                                  T("2026-05-13T10:00:00"))
        tools_v3 = {"python": "3.11.9", "scanpy": "1.11.0", "pandas": "2.2.2"}
        # 缺 replaces 的升级被拒绝
        with self.assertRaises(SandboxError):
            self.box.add_environment("ENV-SCANPY", 3, tools_v3,
                                     released_at=T("2026-05-20T00:00:00"))
        self.box.add_environment("ENV-SCANPY", 3, tools_v3,
                                 released_at=T("2026-05-20T00:00:00"),
                                 note="scanpy 1.11", replaces=2)
        # 钉 v2 的作业收到升级标记；钉 v1 的已评分作业同样前向标记
        self.assertTrue(any(
            f["type"] == "工具升级" and f["current"] == "ENV-SCANPY:v3"
            for f in self.box.assignments["AS-UPG"]["risk_flags"]))
        self.assertTrue(any(
            f["type"] == "工具升级" and f["current"] == "ENV-SCANPY:v3"
            for f in self.box.assignments["AS-LIN-01"]["risk_flags"]))
        # 幂等重放 v3：不重复打标、不产生冲突
        counts = {aid: len(a["risk_flags"])
                  for aid, a in self.box.assignments.items()}
        self.box.add_environment("ENV-SCANPY", 3, tools_v3,
                                 released_at=T("2026-05-20T00:00:00"),
                                 note="scanpy 1.11", replaces=2)
        self.assertEqual({aid: len(a["risk_flags"])
                          for aid, a in self.box.assignments.items()}, counts)
        self.assertEqual(self.box.conflicts, [])
        # 既有任务仍钉原版本
        self.assertEqual(self.box.tasks["TASK-LOCAL-Q2"]["environment_version"], 2)


class BatchRegistrationTest(unittest.TestCase):
    """批量登记原子性：冲突则整批回滚，不留部分版本或错误风险标记。"""

    IDENTITY = ["patient_id", "patient_name", "phone"]

    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def _v3_item(self):
        rows = deepcopy(self.box.snapshots[("CASE-AML", 2)]["rows"])
        rows[0]["marker"] = "CD34-"
        return {"case_id": "CASE-AML", "version": 3, "rows": rows,
                "identity_fields": list(self.IDENTITY),
                "released_at": T("2026-06-01T00:00:00"), "replaces": 2,
                "erratum_note": "v3 批量勘误"}

    def test_conflicting_batch_leaves_no_partial_versions_or_flags(self):
        snap2 = self.box.snapshots[("CASE-AML", 2)]
        bad_v2 = {"case_id": "CASE-AML", "version": 2,
                  "rows": deepcopy(snap2["rows"]),
                  "identity_fields": list(self.IDENTITY),
                  "released_at": snap2["released_at"], "replaces": 1,
                  "erratum_note": snap2["erratum_note"]}
        bad_v2["rows"][2]["cell_type"] = "篡改细胞"  # 与既有 v2 异内容
        flags_before = {aid: deepcopy(a["risk_flags"])
                        for aid, a in self.box.assignments.items()}
        with self.assertRaises(VersionConflict):
            self.box.register_snapshots([self._v3_item(), bad_v2])
        # 整批回滚：合法的 v3 也未落账
        self.assertNotIn(("CASE-AML", 3), self.box.snapshots)
        # 不留下错误风险标记
        for aid, flags in flags_before.items():
            self.assertEqual(self.box.assignments[aid]["risk_flags"], flags)
        # 冲突审计保留
        conflicts = self.box.snapshot_conflicts("CASE-AML", 2)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("数据行", conflicts[0]["differing_fields"])

    def test_batch_chain_commits_atomically(self):
        v3 = self._v3_item()
        v4_rows = deepcopy(v3["rows"])
        v4_rows[1]["marker"] = "CD34-"
        v4 = {"case_id": "CASE-AML", "version": 4, "rows": v4_rows,
              "identity_fields": list(self.IDENTITY),
              "released_at": T("2026-06-03T00:00:00"), "replaces": 3,
              "erratum_note": "v4 复核"}
        records = self.box.register_snapshots([v3, v4])
        self.assertEqual([r["version"] for r in records], [3, 4])
        self.assertIn(("CASE-AML", 4), self.box.snapshots)
        # 已评分作业收到 v3、v4 两条前向勘误标记
        versions = {f.get("current_version")
                    for f in self.box.assignments["AS-LIN-01"]["risk_flags"]
                    if f["type"] == "病例勘误"}
        self.assertTrue({2, 3, 4} <= versions)

    def test_environment_batch_rolls_back_on_conflict(self):
        good_v3 = {"id": "ENV-SCANPY", "version": 3,
                   "tools": {"python": "3.11.9", "scanpy": "1.11.0",
                             "pandas": "2.2.2"},
                   "released_at": T("2026-06-01T00:00:00"),
                   "note": "升级", "replaces": 2}
        bad_v1 = {"id": "ENV-SCANPY", "version": 1,
                  "tools": {"python": "3.12.0", "scanpy": "1.9.8",
                            "pandas": "2.2.1"},
                  "released_at": T("2026-02-01T00:00:00"),
                  "note": "春季学期初标准镜像", "replaces": None}
        with self.assertRaises(VersionConflict):
            self.box.register_environments([good_v3, bad_v1])
        self.assertNotIn(("ENV-SCANPY", 3), self.box.environment_versions)
        self.assertEqual(
            self.box.environment_versions[("ENV-SCANPY", 1)]["tools"]["python"],
            "3.11.9")
        self.assertEqual(len(self.box.environment_conflicts("ENV-SCANPY", 1)), 1)


class ConcurrentRegistrationTest(unittest.TestCase):
    """并发上传同一 (标识, 版本)：得到唯一结果。"""

    THREADS = 8
    IDENTITY = ["patient_id", "patient_name", "phone"]

    def setUp(self):
        self.box = Sandbox.from_seed(SEED)

    def _run_concurrently(self, fn):
        barrier = threading.Barrier(self.THREADS)

        def worker(i):
            barrier.wait(timeout=5)
            fn(i)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(self.THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

    def test_concurrent_conflicting_uploads_yield_single_winner(self):
        base_rows = deepcopy(self.box.snapshots[("CASE-AML", 2)]["rows"])
        wins, losses = [], []

        def upload(i):
            rows = deepcopy(base_rows)
            rows[0]["age"] = 40 + i  # 各上传者内容互不相同
            try:
                self.box.add_snapshot("CASE-AML", 3, rows, self.IDENTITY,
                                      T("2026-06-01T00:00:00"), replaces=2,
                                      erratum_note=f"并发上传 {i}")
                wins.append(i)
            except VersionConflict:
                losses.append(i)

        self._run_concurrently(upload)
        # 唯一结果：恰好一个胜出，其余全部冲突拒绝
        self.assertEqual(len(wins), 1)
        self.assertEqual(len(losses), self.THREADS - 1)
        self.assertEqual(len(self.box.snapshot_conflicts("CASE-AML", 3)),
                         self.THREADS - 1)
        self.assertEqual(self.box.snapshots[("CASE-AML", 3)]["rows"][0]["age"],
                         40 + wins[0])
        ids = [c["id"] for c in self.box.conflicts]
        self.assertEqual(len(ids), len(set(ids)))

    def test_concurrent_identical_uploads_all_replay(self):
        rows = deepcopy(self.box.snapshots[("CASE-AML", 2)]["rows"])
        results = []

        def upload(_i):
            results.append(self.box.add_snapshot(
                "CASE-AML", 3, deepcopy(rows), self.IDENTITY,
                T("2026-06-01T00:00:00"), replaces=2, erratum_note="相同内容"))

        self._run_concurrently(upload)
        # 全部按幂等重放成功，账上只有一条记录，无冲突
        self.assertEqual(len(results), self.THREADS)
        self.assertTrue(all(r is self.box.snapshots[("CASE-AML", 3)]
                            for r in results))
        self.assertEqual(self.box.conflicts, [])


class ProcessRecoveryTest(unittest.TestCase):
    """进程恢复：状态导出后再装载，账本、风险标记、指纹与冲突审计一致。"""

    IDENTITY = ["patient_id", "patient_name", "phone"]

    def _exercised_box(self):
        box = Sandbox.from_seed(SEED)
        box.withdraw_consent("CONS-AML-TEACH", T("2026-05-05T12:00:00"))
        rows3 = deepcopy(box.snapshots[("CASE-AML", 2)]["rows"])
        rows3[6]["marker"] = "CD64+/CD36+"
        box.add_snapshot("CASE-AML", 3, rows3, self.IDENTITY,
                         T("2026-06-01T00:00:00"), replaces=2,
                         erratum_note="P-007 标记复核")
        box.add_environment("ENV-SCANPY", 3,
                            {"python": "3.11.9", "scanpy": "1.11.0",
                             "pandas": "2.2.2"},
                            released_at=T("2026-06-02T00:00:00"),
                            note="scanpy 1.11", replaces=2)
        tampered = deepcopy(box.snapshots[("CASE-AML", 1)]["rows"])
        tampered[0]["age"] = 99
        with self.assertRaises(VersionConflict):
            box.add_snapshot("CASE-AML", 1, tampered, self.IDENTITY,
                             T("2026-02-20T00:00:00"), now=T("2026-06-05T09:00:00"))
        return box

    def test_dump_reload_preserves_ledgers_flags_and_conflicts(self):
        box = self._exercised_box()
        state = json.loads(json.dumps(box.dump_state(), ensure_ascii=False))  # 模拟落盘
        revived = Sandbox.from_state(state)
        # 六账与审计一致
        self.assertEqual(revived.snapshots, box.snapshots)
        self.assertEqual(revived.environment_versions, box.environment_versions)
        self.assertEqual(revived.consents, box.consents)
        self.assertEqual(revived.conflicts, box.conflicts)
        self.assertEqual(revived.audit, box.audit)
        for aid, assignment in box.assignments.items():
            restored = revived.assignments[aid]
            self.assertEqual(restored["status"], assignment["status"])
            self.assertEqual(restored["fingerprint"], assignment["fingerprint"])
            self.assertEqual(restored["risk_flags"], assignment["risk_flags"])
        # 恢复后复现仍一致，冲突来源仍指向同一条审计
        report = revived.reproduce_report("T-CHEN", "AS-LIN-01",
                                          T("2026-06-10T00:00:00"))
        self.assertEqual(report["status"], "复现一致")
        self.assertTrue(report["read_matches_frozen"])
        self.assertEqual([c["id"] for c in report["conflicts"]],
                         [c["id"] for c in revived.snapshot_conflicts("CASE-AML", 1)])
        # 恢复后继续拒绝同版本异内容，且冲突编号不与恢复前重复
        tampered = deepcopy(revived.snapshots[("CASE-AML", 1)]["rows"])
        tampered[1]["age"] = 98
        with self.assertRaises(VersionConflict) as ctx:
            revived.add_snapshot("CASE-AML", 1, tampered, self.IDENTITY,
                                 T("2026-02-20T00:00:00"),
                                 now=T("2026-06-11T09:00:00"))
        self.assertNotIn(ctx.exception.detail["conflict_id"],
                         [c["id"] for c in box.conflicts])
        self.assertEqual(revived.snapshots[("CASE-AML", 1)]["rows"][1]["age"], 45)

    def test_dump_is_deterministic(self):
        box = self._exercised_box()
        first = json.dumps(box.dump_state(), ensure_ascii=False, sort_keys=True)
        revived = Sandbox.from_state(json.loads(first))
        second = json.dumps(revived.dump_state(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
