"""医学教学数据沙箱的基础运行入口。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from domain import AuthorizationError, NotFound, SandboxError, VersionConflict

SERVICE_ID = "medical-sandbox"
SERVICE_NAME = "医学教学数据沙箱"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def error_payload(exc):
    """把领域异常渲染为稳定的 (状态码, JSON 载荷) 错误响应。

    版本冲突返回 409，并说明实际保留（即后续读取）的版本内容摘要、
    新提交摘要、差异字段与处置结果，与溯源、复现报告中的冲突审计互相印证。
    """
    if isinstance(exc, VersionConflict):
        return 409, {
            "service": SERVICE_ID,
            "error": "版本冲突",
            "message": str(exc),
            "conflict": exc.detail,
        }
    if isinstance(exc, NotFound):
        return 404, {"service": SERVICE_ID, "error": "记录不存在", "message": str(exc)}
    if isinstance(exc, AuthorizationError):
        return 403, {"service": SERVICE_ID, "error": "授权失败", "message": str(exc)}
    if isinstance(exc, SandboxError):
        return 400, {"service": SERVICE_ID, "error": "规则违例", "message": str(exc)}
    return 500, {"service": SERVICE_ID, "error": "内部错误", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """提供健康检查，供本地联调和运维巡检使用。"""

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps(health_payload(), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
