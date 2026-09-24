"""验证基础服务在领域模块开发前保持可运行。"""

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from domain import (
    AuthorizationError,
    NotFound,
    Sandbox,
    SandboxError,
    VersionConflict,
)
from service import Handler, SERVICE_ID, SERVICE_NAME, error_payload, health_payload


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_health_payload_has_stable_identity(self):
        self.assertEqual(health_payload(), {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME})

    def test_health_endpoint_returns_json(self):
        with urlopen(f"{self.base_url}/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            self.assertEqual(json.load(response), health_payload())

    def test_unknown_route_is_not_exposed(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()


class ErrorPayloadTest(unittest.TestCase):
    """服务错误响应：说明实际保留的版本、冻结指纹与冲突来源。"""

    SEED = "fixtures/seed.json"

    def test_version_conflict_payload_explains_source(self):
        box = Sandbox.from_seed(self.SEED)
        v1 = box.snapshots[("CASE-AML", 1)]
        rows = [dict(r) for r in v1["rows"]]
        rows[0]["age"] = 99
        with self.assertRaises(VersionConflict) as ctx:
            box.add_snapshot("CASE-AML", 1, rows,
                             identity_fields=list(v1["identity_fields"]),
                             released_at=v1["released_at"])
        status, payload = error_payload(ctx.exception)
        self.assertEqual(status, 409)
        self.assertEqual(payload["service"], SERVICE_ID)
        self.assertEqual(payload["error"], "版本冲突")
        conflict = payload["conflict"]
        self.assertEqual(conflict["key"], {"case_id": "CASE-AML", "version": 1})
        self.assertEqual(conflict["differing_fields"], ["rows"])
        # 实际保留（即后续读取）的版本摘要与冻结指纹一致，新提交摘要不一致
        self.assertEqual(conflict["existing"]["content_hash"], v1["content_hash"])
        self.assertNotEqual(conflict["incoming"]["content_hash"], v1["content_hash"])
        self.assertEqual(conflict["resolution"], "拒绝覆盖，保留原版本")
        # 载荷必须是可 JSON 序列化的 HTTP 响应
        json.dumps(payload, ensure_ascii=False)

    def test_common_domain_errors_have_stable_status(self):
        status, payload = error_payload(NotFound("未知病例：X"))
        self.assertEqual((status, payload["error"]), (404, "记录不存在"))
        status, payload = error_payload(AuthorizationError("无权"))
        self.assertEqual((status, payload["error"]), (403, "授权失败"))
        status, payload = error_payload(SandboxError("规则"))
        self.assertEqual((status, payload["error"]), (400, "规则违例"))
        status, payload = error_payload(RuntimeError("意外"))
        self.assertEqual((status, payload["error"]), (500, "内部错误"))


if __name__ == "__main__":
    unittest.main()
