#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — Pay-Per-Call Gateway 主服务

一个进程同时对外提供：
  POST /mcp              MCP JSON-RPC（Claude / Cursor / 各类 Agent 直接连）
  GET  /openapi.json     OpenAPI 3.1（扣子/钉钉/抖音等平台"导入插件"用）
  GET  /llms.txt         给 Agent 读的纯文本说明书
  GET  /                 落地页（价目 + 接入方式，用于引流转化）
  GET  /admin            运营后台（收入/成本/毛利/台账/租户/调价）
  POST /webhook/<rail>   微信支付/支付宝/Stripe 回调入账

计费口径：按次计费，**业务失败不扣费**；先验付款再干活，干活成功才结算。
运行： python app.py            （零第三方依赖）
"""

import json
import os
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)


def load_dotenv():
    """读取项目根目录 .env（已在 .gitignore 里）。已存在的环境变量优先，不覆盖。"""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return 0
    n = 0
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ and v:
                os.environ[k] = v
                n += 1
    return n


load_dotenv()

import billing  # noqa: E402
import payments  # noqa: E402
import tools as tools_mod  # noqa: E402
from billing import MICRO, Store, Pricing, Signer, usd  # noqa: E402


def load_config():
    path = os.path.join(BASE_DIR, "config", "gateway.json")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("port", 8300)
    cfg.setdefault("host", "0.0.0.0")
    cfg.setdefault("public_base_url", "http://127.0.0.1:%d" % cfg["port"])
    cfg.setdefault("x402", {})
    cfg.setdefault("webhooks", {})
    # 环境变量覆盖：部署时不用改文件，自测时也能起独立实例
    if os.environ.get("PPC_PORT"):
        cfg["port"] = int(os.environ["PPC_PORT"])
    if os.environ.get("PPC_PUBLIC_BASE_URL"):
        cfg["public_base_url"] = os.environ["PPC_PUBLIC_BASE_URL"]
    if os.environ.get("PPC_DB_PATH"):
        cfg["db_path"] = os.environ["PPC_DB_PATH"]
    if os.environ.get("PPC_PRICING_FILE"):
        cfg["pricing_file"] = os.environ["PPC_PRICING_FILE"]
    if os.environ.get("PPC_ADMIN_TOKEN_FILE"):
        cfg["admin_token_file"] = os.environ["PPC_ADMIN_TOKEN_FILE"]
    if os.environ.get("PPC_X402_PAY_TO"):
        cfg["x402"]["pay_to"] = os.environ["PPC_X402_PAY_TO"]
    if os.environ.get("PPC_X402_FACILITATOR"):
        cfg["x402"]["facilitator"] = os.environ["PPC_X402_FACILITATOR"]
    if os.environ.get("PPC_X402_NETWORK"):
        cfg["x402"]["network"] = os.environ["PPC_X402_NETWORK"]
    if os.environ.get("PPC_X402_ENABLED"):
        cfg["x402"]["enabled"] = os.environ["PPC_X402_ENABLED"] == "1"
    return cfg


CFG = load_config()
STORE = Store(os.path.join(BASE_DIR, CFG["db_path"]))
PRICING = Pricing(os.path.join(BASE_DIR, CFG["pricing_file"]))
SIGNER = Signer(os.path.join(BASE_DIR, CFG["license_secret_file"]))
TOOLS = tools_mod.Tools(CFG, PRICING, STORE, SIGNER, BASE_DIR)
X402 = payments.X402(CFG.get("x402") or {}, CFG.get("http_proxy_mode", "direct"))


def admin_token():
    path = os.path.join(BASE_DIR, CFG["admin_token_file"])
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            tok = f.read().strip()
        if tok:
            return tok
    tok = "adm_" + uuid.uuid4().hex
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(tok)
    return tok


ADMIN_TOKEN = admin_token()


class RateLimiter(object):
    def __init__(self, per_min):
        self.per_min = int(per_min or 120)
        self.lock = threading.Lock()
        self.hits = {}

    def allow(self, key):
        if self.per_min <= 0:
            return True, 0
        now = int(time.time())
        with self.lock:
            bucket = [t for t in self.hits.get(key, []) if t > now - 60]
            if len(bucket) >= self.per_min:
                self.hits[key] = bucket
                return False, self.per_min - len(bucket)
            bucket.append(now)
            self.hits[key] = bucket
            return True, self.per_min - len(bucket)


LIMITER = RateLimiter(CFG.get("rate_limit_per_min", 120))


class Gateway(BaseHTTPRequestHandler):
    server_version = "PayPerCall/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------- 基础设施 ----------------

    def log_message(self, fmt, *args):
        if os.environ.get("PPC_VERBOSE"):
            sys.stderr.write("[ppc] %s %s\n" % (self.address_string(), fmt % args))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _json(self, obj, code=200, headers=None):
        raw = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers",
                         "X-PAYMENT-RESPONSE, X-BILLING, X-RateLimit-Remaining")
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _html(self, path, code=200):
        full = os.path.join(BASE_DIR, "web", path)
        if not os.path.exists(full):
            return self._json({"error": "not_found"}, 404)
        with open(full, "rb") as f:
            raw = f.read()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _static(self, relpath):
        """只允许 web/ 下的白名单后缀，防止路径穿越。"""
        relpath = relpath.replace("\\", "/").lstrip("/")
        if ".." in relpath.split("/"):
            return self._json({"error": "bad_path"}, 400)
        ext = os.path.splitext(relpath)[1].lower()
        ctypes = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".png": "image/png", ".webp": "image/webp",
                  ".svg": "image/svg+xml", ".css": "text/css; charset=utf-8",
                  ".js": "application/javascript; charset=utf-8",
                  ".ico": "image/x-icon"}
        if ext not in ctypes:
            return self._json({"error": "unsupported_type"}, 404)
        full = os.path.join(BASE_DIR, "web", relpath)
        if not os.path.exists(full):
            return self._json({"error": "not_found"}, 404)
        with open(full, "rb") as f:
            raw = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctypes[ext])
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _text(self, text, content_type="text/plain; charset=utf-8", code=200):
        raw = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _cors_preflight(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, X-API-Key, X-PAYMENT, X-Admin-Token")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _tenant(self):
        key = self.headers.get("X-API-Key")
        if not key:
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                key = auth[7:].strip()
        return STORE.tenant_by_key(key)

    # ---------------- 路由 ----------------

    def do_OPTIONS(self):
        self._cors_preflight()

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path.rstrip("/") or "/"
        q = parse_qs(u.query)
        try:
            if p == "/":
                return self._html("index.html")
            if p in ("/admin", "/admin/index.html"):
                return self._html("admin.html")
            if p == "/healthz":
                return self._json({"ok": True, "ts": int(time.time()),
                                   "tools": len(PRICING.tools()),
                                   "x402": X402.enabled})
            if p == "/pricing":
                return self._json(self.pricing_payload())
            if p == "/topup":
                return self._json(self.topup_payload())
            if p.startswith("/assets/"):
                return self._static(p[1:])
            if p == "/openapi.json":
                return self._json(self.openapi_payload())
            if p == "/llms.txt":
                return self._text(self.llms_txt())
            if p == "/.well-known/x402":
                return self._json(self.x402_discovery())
            if p == "/mcp":
                return self._json({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32000,
                              "message": "MCP over HTTP 请用 POST /mcp 发送 JSON-RPC"},
                }, 405)
            if p.startswith("/admin/api/"):
                return self.admin_api(p, q)
            return self._json({"error": "not_found", "path": p}, 404)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"error": "internal_error"}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path.rstrip("/") or "/"
        try:
            if p == "/mcp":
                return self.handle_mcp()
            if p == "/topup/claim":
                return self.handle_topup_claim()
            if p.startswith("/x/"):
                return self.handle_rest_tool(p[3:])
            if p.startswith("/webhook/"):
                return self.handle_webhook(p.split("/")[-1])
            if p.startswith("/admin/api/"):
                return self.admin_api(p, parse_qs(u.query), body=self._body())
            return self._json({"error": "not_found", "path": p}, 404)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"error": "internal_error"}, 500)

    # ---------------- MCP ----------------

    def handle_mcp(self):
        raw = self._body()
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return self._json({"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32700, "message": "Parse error"}}, 400)

        if isinstance(msg, list):
            return self._json([self.dispatch_mcp(m) for m in msg])

        out = self.dispatch_mcp(msg)
        if out is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        code = out.pop("__http_status__", 200)
        headers = out.pop("__headers__", {})
        return self._json(out, code, headers)

    def dispatch_mcp(self, msg):
        if not isinstance(msg, dict):
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "Invalid Request"}}
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": True},
                                 "resources": {}, "prompts": {}},
                "serverInfo": {"name": "pay-per-call-gateway", "version": "1.0.0"},
                "instructions": "所有工具按次计费，失败不扣费。调用前可先调 pricing.list "
                                "查看价格，用 usage.report 对账。"
            }}
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"tools": TOOLS.list_tools()}}
        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"resources": []}}
        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}}
        if method == "tools/call":
            return self.handle_tool_call(mid, params)

        if mid is None:
            return None
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": "Method not found: %s" % method}}

    def handle_tool_call(self, mid, params):
        name = params.get("name")
        args = params.get("arguments") or {}
        started = time.time()
        tenant = self._tenant()

        if not PRICING.spec(name):
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": "Unknown tool: %s" % name}}

        rk = (tenant or {}).get("id") or self.client_address[0]
        ok, remain = LIMITER.allow(rk)
        if not ok:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32005, "message": "Rate limit exceeded, retry in 60s"},
                    "__http_status__": 429,
                    "__headers__": {"X-RateLimit-Remaining": 0}}

        price, cost = TOOLS.resolve_quote(name, args)
        eligible, rail = self.check_entitlement(tenant, price)
        pay_ctx = {}

        # ---- 未满足预付/订阅 → 走 x402 链上按次 ----
        if not eligible and price > 0:
            x = self.x402_flow(name, args, price)
            if x.get("need_payment"):
                return self.payment_required(mid, name, price, x)
            pay_ctx = x
            eligible = True
            rail = "x402"

        if not eligible:
            return self.payment_required(mid, name, price, {})

        # ---- 执行 ----
        ctx = {"tenant_id": (tenant or {}).get("id") or rail, "channel": self.channel_of(tenant)}
        status_override = None
        try:
            result, cost_override, price_override = TOOLS.run(name, args, ctx)
        except tools_mod.ToolError as exc:
            STORE.record_failed(ctx["tenant_id"], name, ctx["channel"], rail,
                                exc.code, ms=int((time.time() - started) * 1000))
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "[%s] %s（本次未扣费）"
                             % (exc.code, exc.message)}],
                "isError": True,
                "_billing": {"charged": False, "reason": exc.code},
            }}
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            STORE.record_failed(ctx["tenant_id"], name, ctx["channel"], rail,
                                "internal_error", ms=int((time.time() - started) * 1000))
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "[internal_error] 服务异常：%s（本次未扣费）" % exc}],
                "isError": True,
                "_billing": {"charged": False, "reason": "internal_error"},
            }}

        if price_override is not None:
            price = price_override
        if cost_override is not None:
            cost = cost_override
        ms = int((time.time() - started) * 1000)

        # ---- 计费落账 ----
        billing_info = {"charged": False, "price_usd": usd(price), "rail": rail}
        headers = {}
        if price == 0:
            STORE.record_free(ctx["tenant_id"], name, ctx["channel"], ms=ms)
            billing_info["reason"] = "free_tool"
        elif rail == "x402":
            settled, info = X402.settle(pay_ctx["payload"], pay_ctx["requirements"])
            if settled:
                STORE.record_payment(ctx["tenant_id"], "x402", price,
                                     asset=(CFG["x402"].get("asset")),
                                     network=(CFG["x402"].get("network")),
                                     payer=pay_ctx.get("payer"),
                                     tx=info.get("transaction"),
                                     nonce=pay_ctx.get("nonce"), raw=info)
                self.ledger_write(ctx, name, rail, price, cost, ms,
                                  ref=info.get("transaction"), detail={"x402": True})
                billing_info.update({"charged": True, "tx": info.get("transaction"),
                                     "payer": pay_ctx.get("payer"), "settled": True})
                headers["X-PAYMENT-RESPONSE"] = X402.encode_receipt(info)
            else:
                # 已交付但没结算成功：如实记为 0 收入 + 真实成本（亏的那笔要看得见）
                STORE.record_payment(ctx["tenant_id"], "x402", price,
                                     payer=pay_ctx.get("payer"),
                                     nonce=pay_ctx.get("nonce"), status="settle_failed",
                                     raw=info)
                self.ledger_write(ctx, name, rail, 0, cost, ms, ref=None,
                                  detail={"settle_failed": info})
                billing_info.update({"charged": False, "settled": False,
                                     "error": info.get("error")})
        elif rail == "subscription":
            ok2, why = STORE.charge_quota(ctx["tenant_id"], price, cost, name,
                                          ctx["channel"], ms=ms)
            billing_info.update({"charged": ok2, "quota": True, "reason": why})
        else:
            ok2, why = STORE.charge_wallet(ctx["tenant_id"], price, cost, name,
                                           ctx["channel"], ms=ms)
            if not ok2:
                billing_info.update({"charged": False, "error": why})
            else:
                billing_info["charged"] = True
                t = STORE.tenant(ctx["tenant_id"]) or {}
                billing_info["balance_usd"] = usd(t.get("balance_micro") or 0)

        result["_billing"] = billing_info
        headers["X-BILLING"] = "charged=%s;rail=%s;price_usd=%s" % (
            billing_info.get("charged"), rail, usd(price))
        headers["X-RateLimit-Remaining"] = remain
        return {"jsonrpc": "2.0", "id": mid, "result": result,
                "__headers__": headers}

    def ledger_write(self, ctx, name, rail, price, cost, ms, ref=None, detail=None):
        STORE.conn().execute(
            "INSERT INTO ledger(ts,tenant_id,tool,channel,rail,price_micro,cost_micro,"
            "status,ref,req_id,ms,detail) VALUES(?,?,?,?,?,?,?, 'ok', ?, NULL, ?, ?)",
            (billing.now(), ctx["tenant_id"], name, ctx["channel"], rail,
             int(price), int(cost), ref, int(ms),
             json.dumps(detail or {}, ensure_ascii=False)[:2000]),
        )
        STORE.conn().commit()

    def channel_of(self, tenant):
        if tenant:
            return tenant.get("channel") or "direct"
        return "anonymous"

    def check_entitlement(self, tenant, price):
        """返回 (是否可放行, 走哪条腿)。价格 0 的免费工具直接放行。"""
        if price <= 0:
            return True, "free"
        if not tenant:
            return False, None
        if tenant.get("status") != "active":
            return False, "blocked"
        if tenant.get("plan") == "subscription":
            remain = (tenant.get("quota_micro") or 0) - (tenant.get("quota_used_micro") or 0)
            if remain >= price:
                return True, "subscription"
        if (tenant.get("balance_micro") or 0) >= price:
            return True, "wallet"
        return False, None

    # ---------------- x402 ----------------

    def x402_flow(self, tool_name, args, price):
        header = self.headers.get("X-PAYMENT") or self.headers.get("x-payment")
        if not header:
            return {"need_payment": True}
        if not X402.enabled:
            return {"need_payment": True, "x402_disabled": True}
        resource = (CFG.get("public_base_url") or "").rstrip("/") + "/mcp"
        requirements = X402.accepts(resource, price, "%s (per call)" % tool_name)[0]
        try:
            payload = X402.decode_header(header)
        except payments.X402Error as exc:
            return {"need_payment": True, "error": str(exc)}
        ok, reason, info = X402.check_shape(payload, requirements)
        if not ok:
            return {"need_payment": True, "error": reason}
        nonce = info.get("nonce")
        if STORE.nonce_used(nonce):
            return {"need_payment": True, "error": "nonce_replayed"}
        verified, vinfo = X402.verify(payload, requirements)
        if not verified:
            return {"need_payment": True,
                    "error": vinfo.get("reason") or vinfo.get("error"),
                    "detail": vinfo}
        return {"payload": payload, "requirements": requirements,
                "payer": vinfo.get("payer") or info.get("payer"), "nonce": nonce}

    def payment_required(self, mid, tool_name, price, extra):
        resource = (CFG.get("public_base_url") or "").rstrip("/") + "/mcp"
        accepts = X402.accepts(resource, price, "%s (per call)" % tool_name) if X402.enabled else []
        content = [{
            "type": "text",
            "text": "调用 %s 需付费 %s USD。当前未检测到可用余额或有效 X-PAYMENT。%s"
                    % (tool_name, usd(price),
                       ("x402 错误：" + str(extra.get("error"))) if extra.get("error") else ""),
        }]
        # 同一个响应体同时满足两种客户端：
        #   x402 客户端读顶层 accepts / x402Version
        #   MCP 客户端读 result.content
        body = {
            "jsonrpc": "2.0",
            "id": mid,
            "x402Version": 1,
            "accepts": accepts,
            "error": "payment_required",
            "x402Error": extra.get("error"),
            "payment_required": {
                "tool": tool_name,
                "price_usd": usd(price),
                "price_micro": int(price),
                "rails": {
                    "x402": "带 X-PAYMENT 头重试，链上按次支付，无需注册",
                    "wallet": "用 X-API-Key，后台充值后按次扣费",
                    "subscription": "购买包次套餐，扣套餐余量",
                },
                "topup_url": (CFG.get("public_base_url") or "") + "/#pricing",
            },
            "result": {
                "content": content,
                "isError": True,
                "_billing": {"charged": False, "reason": "payment_required",
                             "price_usd": usd(price)},
            },
            "__http_status__": 402,
            "__headers__": {"X-Price-USD": usd(price),
                            "WWW-Authenticate": 'x402 realm="pay-per-call"'},
        }
        if extra.get("detail"):
            body["x402Detail"] = extra["detail"]
        return body

    # ---------------- REST 包装（给只认 OpenAPI 的平台插件用）----------------

    def handle_rest_tool(self, name):
        raw = self._body()
        try:
            args = json.loads(raw.decode("utf-8") or "{}") if raw else {}
        except Exception:  # noqa: BLE001
            return self._json({"ok": False, "error": "bad_json"}, 400)
        out = self.handle_tool_call(1, {"name": name, "arguments": args})
        status = out.pop("__http_status__", 200)
        headers = out.pop("__headers__", {})

        if "error" in out:
            payload = {"ok": False, "tool": name, "error": out["error"]}
            for k in ("payment_required", "accepts", "x402Version", "x402Error"):
                if k in out:
                    payload[k] = out[k]
            return self._json(payload, status or 400, headers)

        result = out.get("result") or {}
        billing_info = result.get("_billing") or {}
        text = ""
        for block in result.get("content") or []:
            if block.get("type") == "text":
                text += block.get("text") or ""
        data = None
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001
            data = text

        body = {"ok": not result.get("isError"), "tool": name,
                "billing": billing_info, "data": data}
        if result.get("isError"):
            body["error"] = data if isinstance(data, str) else "tool_failed"
            if status == 200:
                status = 402 if billing_info.get("reason") == "payment_required" else 502
        return self._json(body, status, headers)

    # ---------------- 平台订单回调 ----------------

    def handle_webhook(self, rail):
        rail = rail.strip().lower()
        spec = (CFG.get("webhooks") or {}).get(rail)
        if not spec:
            return self._json({"error": "unknown_rail"}, 404)
        secret = os.environ.get(spec.get("secret_env") or "", "").strip()
        if not secret:
            return self._json({"error": "rail_not_configured",
                               "hint": "设置环境变量 %s 后启用" % spec.get("secret_env")}, 503)
        raw = self._body()
        sig = self.headers.get("X-Signature") or self.headers.get("X-Sign")
        if not payments.verify_webhook(secret, raw, sig or ""):
            return self._json({"error": "bad_signature"}, 401)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return self._json({"error": "bad_json"}, 400)
        tenant_id = data.get("tenant_id") or (data.get("metadata") or {}).get("tenant_id")
        amount = data.get("amount_micro")
        if amount is None and data.get("amount") is not None:
            amount = billing.to_micro(data["amount"])
        if not tenant_id or amount is None:
            return self._json({"error": "missing tenant_id or amount"}, 400)
        if not STORE.tenant(tenant_id):
            return self._json({"error": "tenant_not_found"}, 404)
        STORE.recharge(tenant_id, int(amount), rail=rail, ref=data.get("order_id"))
        t = STORE.tenant(tenant_id)
        return self._json({"ok": True, "tenant_id": tenant_id,
                           "credited_usd": usd(int(amount)),
                           "balance_usd": usd(t["balance_micro"])})

    # ---------------- 管理 API ----------------

    def _admin_ok(self, q):
        tok = self.headers.get("X-Admin-Token") or (q.get("token", [None])[0] if q else None)
        return tok == ADMIN_TOKEN

    def admin_api(self, path, q, body=None):
        if not self._admin_ok(q):
            return self._json({"error": "unauthorized",
                               "hint": "请求头加 X-Admin-Token，令牌见 data/admin_token.txt"}, 401)
        if path == "/admin/api/summary":
            days = int((q.get("days", ["7"])[0]) or 7)
            return self._json(STORE.summary(days))
        if path == "/admin/api/ledger":
            limit = int((q.get("limit", ["100"])[0]) or 100)
            tid = q.get("tenant_id", [None])[0]
            return self._json({"items": STORE.ledger(limit, tid)})
        if path == "/admin/api/tenants":
            if body:
                data = json.loads(body.decode("utf-8") or "{}")
                tid, key = STORE.create_tenant(
                    data.get("name") or "unnamed",
                    channel=data.get("channel") or "direct",
                    plan=data.get("plan") or "wallet",
                    balance_micro=billing.to_micro(data.get("balance_usd") or 0),
                    quota_micro=billing.to_micro(data.get("quota_usd") or 0),
                )
                return self._json({"id": tid, "api_key": key,
                                   "note": "api_key 只显示这一次，请立即保存"})
            return self._json({"items": STORE.tenants()})
        if path == "/admin/api/recharge":
            data = json.loads((body or b"{}").decode("utf-8") or "{}")
            tid = data.get("tenant_id")
            amount = data.get("amount_micro")
            if amount is None:
                amount = billing.to_micro(data.get("amount_usd") or 0)
            if not tid or not STORE.tenant(tid):
                return self._json({"error": "tenant_not_found"}, 404)
            STORE.recharge(tid, int(amount), rail="manual", ref="admin")
            return self._json({"ok": True, "tenant": STORE.tenant(tid)})
        if path == "/admin/api/price":
            data = json.loads((body or b"{}").decode("utf-8") or "{}")
            tool = data.get("tool")
            price = data.get("price_micro")
            if price is None:
                price = billing.to_micro(data.get("price_usd") or 0)
            try:
                PRICING.set_price(tool, int(price))
            except KeyError:
                return self._json({"error": "unknown_tool"}, 404)
            return self._json({"ok": True, "tool": tool,
                               "price_usd": usd(int(price)),
                               "effective": "立即生效，无需重启"})
        if path == "/admin/api/leads":
            return self._json({"items": STORE.leads(100)})
        if path == "/admin/api/topups":
            status = q.get("status", [None])[0]
            return self._json({"items": STORE.topups(200, status)})
        if path == "/admin/api/topups/approve":
            data = json.loads((body or b"{}").decode("utf-8") or "{}")
            tid = data.get("topup_id")
            if not tid:
                return self._json({"error": "topup_id_required"}, 400)
            try:
                out = STORE.approve_topup(tid, reviewer="admin",
                                          name=data.get("name"))
            except KeyError:
                return self._json({"error": "topup_not_found"}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 409)
            out["note"] = ("api_key 只显示这一次，请立即发给客户并保存"
                           if out.get("api_key") else "已叠加到原有余额")
            return self._json(out)
        if path == "/admin/api/topups/reject":
            data = json.loads((body or b"{}").decode("utf-8") or "{}")
            tid = data.get("topup_id")
            if not tid:
                return self._json({"error": "topup_id_required"}, 400)
            try:
                out = STORE.reject_topup(tid, reviewer="admin",
                                         reason=data.get("reason"))
            except KeyError:
                return self._json({"error": "topup_not_found"}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 409)
            return self._json(out)
        if path == "/admin/api/reload":
            n = TOOLS.reload_inventory()
            PRICING.reload()
            return self._json({"ok": True, "skus": n, "tools": len(PRICING.tools())})
        if path == "/admin/api/payments":
            rows = STORE.conn().execute(
                "SELECT * FROM payments ORDER BY id DESC LIMIT 100").fetchall()
            return self._json({"items": [dict(r) for r in rows]})
        return self._json({"error": "not_found"}, 404)

    # ---------------- 扫码充值 ----------------

    def topup_payload(self):
        t = CFG.get("topup") or {}
        chans = []
        for c in t.get("channels") or []:
            chans.append({
                "id": c.get("id"), "name": c.get("name"),
                "qr": (c.get("qr") or ""), "kind": c.get("kind"),
                "note": c.get("note"),
                "url": (CFG.get("public_base_url") or "").rstrip("/") + (c.get("qr") or ""),
            })
        return {
            "enabled": bool(t.get("enabled")),
            "fx_cny_per_usd": t.get("fx_cny_per_usd"),
            "min_cny": t.get("min_cny"),
            "auto_approve": bool(t.get("auto_approve")),
            "plans": t.get("plans") or [],
            "channels": chans,
            "flow": [
                "选择套餐，扫码付款（金额必须与套餐一致）",
                "在下方回填付款交易号后四位/整串 + 联系方式",
                "后台核对到账后自动开通 API Key 并发放等值额度",
                "额度按次扣费，失败不扣，可随时在后台对账",
            ],
            "note": "个人/经营收款码没有支付回调，无法自动确认到账，"
                    "因此采用人工核对（通常 10 分钟内）。这不是收不到钱，"
                    "而是避免把未到账的订单当已付款。",
        }

    def handle_topup_claim(self):
        raw = self._body()
        try:
            d = json.loads(raw.decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return self._json({"ok": False, "error": "bad_json"}, 400)

        t = CFG.get("topup") or {}
        if not t.get("enabled"):
            return self._json({"ok": False, "error": "topup_disabled"}, 503)

        plan_id = (d.get("plan_id") or "").strip()
        channel = (d.get("channel") or "").strip()
        payer_ref = (d.get("payer_ref") or "").strip()
        contact = (d.get("contact") or "").strip()

        plans = {p.get("id"): p for p in (t.get("plans") or [])}
        chan_ids = {c.get("id") for c in (t.get("channels") or [])}

        if plan_id not in plans:
            return self._json({"ok": False, "error": "unknown_plan",
                               "plans": list(plans.keys())}, 400)
        if channel not in chan_ids:
            return self._json({"ok": False, "error": "unknown_channel",
                               "channels": list(chan_ids)}, 400)
        if len(payer_ref) < 4:
            return self._json({"ok": False, "error": "payer_ref_too_short",
                               "hint": "请填付款交易号，至少 4 位"}, 400)
        if len(contact) < 4:
            return self._json({"ok": False, "error": "contact_required",
                               "hint": "留一个能联系到你的邮箱或微信号"}, 400)

        # 同一交易号不能重复提交（防一码多报）
        dup = STORE.conn().execute(
            "SELECT id,status FROM topups WHERE payer_ref=? AND channel=?",
            (payer_ref, channel)).fetchone()
        if dup:
            return self._json({"ok": False, "error": "duplicate_payer_ref",
                               "existing": dict(dup),
                               "hint": "该交易号已提交过，请勿重复提交"}, 409)

        plan = plans[plan_id]
        credit = billing.to_micro(plan.get("credit_usd") or 0)
        tid = STORE.create_topup(plan_id, plan.get("cny") or 0, credit, channel,
                                 payer_ref, contact, d.get("note"))
        # 记线索，便于后续跟进转化
        STORE.save_lead(None, "topup", contact,
                        {"topup_id": tid, "plan_id": plan_id,
                         "channel": channel, "cny": plan.get("cny")})
        return self._json({
            "ok": True,
            "topup_id": tid,
            "status": "pending",
            "credit_usd": plan.get("credit_usd"),
            "cny": plan.get("cny"),
            "next": "等后台核对到账（通常 10 分钟内），完成后会用你留的联系方式发 API Key",
        }, 201)

    # ---------------- 对外元数据 ----------------

    def pricing_payload(self):
        out = []
        for name, spec in sorted(PRICING.tools().items()):
            price, _ = PRICING.quote(name)
            out.append({"tool": name, "revenue_line": spec.get("revenue_line"),
                        "title": spec.get("title"), "price_usd": usd(price),
                        "desc": spec.get("desc")})
        return {"currency": "USD", "billing_mode": "per_call",
                "failed_calls": "免费，不扣费", "tools": out}

    def x402_discovery(self):
        base = (CFG.get("public_base_url") or "").rstrip("/")
        return {
            "x402Version": 1,
            "resources": [{
                "resource": base + "/mcp",
                "mimeType": "application/json",
                "accepts": X402.accepts(base + "/mcp", 1000,
                                        "MCP per-call access") if X402.enabled else [],
            }],
            "facilitator": CFG["x402"].get("facilitator"),
            "note": "每个 tools/call 单独结算，价格见 /pricing",
        }

    def openapi_payload(self):
        base = (CFG.get("public_base_url") or "").rstrip("/")
        paths = {
            "/mcp": {
                "post": {
                    "operationId": "mcp_call",
                    "summary": "MCP JSON-RPC 统一入口（tools/call 按次计费）",
                    "requestBody": {"required": True, "content": {"application/json": {
                        "schema": {"type": "object", "properties": {
                            "jsonrpc": {"type": "string", "default": "2.0"},
                            "id": {"type": "integer"},
                            "method": {"type": "string",
                                       "enum": ["initialize", "tools/list", "tools/call"]},
                            "params": {"type": "object"},
                        }}}}},
                    "responses": {"200": {"description": "OK"},
                                  "402": {"description": "需要付费"}},
                }
            },
            "/pricing": {"get": {"operationId": "get_pricing", "summary": "价目表",
                                 "responses": {"200": {"description": "OK"}}}},
        }
        for name, spec in PRICING.tools().items():
            price, _ = PRICING.quote(name)
            op = name.replace(".", "_")
            paths["/x/" + name] = {
                "post": {
                    "operationId": op,
                    "summary": "%s（%s USD/次）" % (spec.get("title"), usd(price)),
                    "description": spec.get("desc"),
                    "security": [{"ApiKeyAuth": []}],
                    "requestBody": {"required": False, "content": {"application/json": {
                        "schema": tools_mod.TOOL_SCHEMAS[name]["inputSchema"]}}},
                    "responses": {"200": {"description": "交付结果"},
                                  "402": {"description": "余额不足或需付费"}},
                }
            }
        return {
            "openapi": "3.1.0",
            "info": {"title": "Pay-Per-Call AI Marketplace",
                     "version": "1.0.0",
                     "description": "按次计费的 AI 能力市场。每个工具独立计价，调用失败不扣费。"},
            "servers": [{"url": base}],
            "components": {"securitySchemes": {"ApiKeyAuth": {
                "type": "apiKey", "in": "header", "name": "X-API-Key"}}},
            "paths": paths,
        }

    def llms_txt(self):
        base = (CFG.get("public_base_url") or "").rstrip("/")
        lines = [
            "# Pay-Per-Call AI Marketplace",
            "",
            "> 按次计费的 AI 能力接口。MCP 端点: %s/mcp" % base,
            "",
            "## 计费",
            "- 每个工具独立定价，见 %s/pricing" % base,
            "- 业务失败不扣费",
            "- 两种付款方式：X-API-Key（预充值余额）或 X-PAYMENT 头（x402 链上按次）",
            "",
            "## 工具",
        ]
        for name, spec in sorted(PRICING.tools().items()):
            price, _ = PRICING.quote(name)
            lines.append("- %s — %s USD/次 — %s" % (name, usd(price), spec.get("title")))
        lines += ["", "## 自助对账", "- 调 usage.report 查自己的用量与余额", ""]
        return "\n".join(lines)


def bootstrap():
    """首次启动：建 demo 租户，把 key 写在与 db 同级的 bootstrap 文件里方便自测。"""
    path = os.path.join(BASE_DIR, CFG["db_path"]).replace(".db", "") + "-bootstrap.txt"
    if os.path.exists(path):
        return
    tid, key = STORE.create_tenant("demo-agent", channel="direct", plan="wallet",
                                   balance_micro=billing.to_micro(5))
    tid2, key2 = STORE.create_tenant("demo-subscriber", channel="coze", plan="subscription",
                                     quota_micro=billing.to_micro(20))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("demo-agent   (wallet, $5)       id=%s\n  X-API-Key: %s\n\n"
                "demo-subscriber (subscription, $20 quota) id=%s\n  X-API-Key: %s\n"
                % (tid, key, tid2, key2))
    print("[init] demo tenants created -> %s" % path)


def main():
    bootstrap()
    port = int(os.environ.get("PPC_PORT") or CFG["port"])
    srv = ThreadingHTTPServer((CFG["host"], port), Gateway)
    srv.daemon_threads = True
    base = CFG.get("public_base_url") or "http://127.0.0.1:%d" % port
    print("=" * 62)
    print(" Pay-Per-Call Gateway 已启动")
    print("   对外地址   %s" % base)
    print("   MCP        POST %s/mcp" % base)
    print("   OpenAPI    %s/openapi.json" % base)
    print("   落地页     %s/" % base)
    print("   运营后台   %s/admin  (token: %s)" % (base, ADMIN_TOKEN))
    print("   x402       %s (%s)" % ("开启" if X402.enabled else "关闭（pay_to 未配置）",
                                    CFG["x402"].get("network")))
    print("   工具数     %d，SKU %d" % (len(PRICING.tools()), len(TOOLS.inventory)))
    print("=" * 62)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[stop] bye")


if __name__ == "__main__":
    main()
