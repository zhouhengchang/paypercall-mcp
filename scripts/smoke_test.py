#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_test.py — 端到端自测

起一个假的 facilitator + 一个独立网关实例（独立 db，独立端口），
把三条收款通道和"失败不扣费"全部跑一遍。

    python scripts/smoke_test.py

退出码 0 = 全过。任何一条失败都会打印实际返回。
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8301
FAC_PORT = 8399
BASE_URL = "http://127.0.0.1:%d" % PORT
PAYER = "0x9a1b2c3d4e5f60718293a4b5c6d7e8f901234567"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

RESULTS = []


def req(method, path, body=None, headers=None, raw=False):
    url = BASE_URL + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with OPENER.open(r, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
            hdrs = dict(resp.headers)
            status = resp.status
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        hdrs = dict(exc.headers)
        status = exc.code
    if raw:
        return status, text, hdrs
    try:
        return status, json.loads(text), hdrs
    except Exception:  # noqa: BLE001
        return status, text, hdrs


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else "   << " + str(detail)[:300]))


def mcp(method, params=None, headers=None, mid=1):
    return req("POST", "/mcp", {"jsonrpc": "2.0", "id": mid, "method": method,
                                "params": params or {}}, headers)


def call(name, args, headers=None):
    return mcp("tools/call", {"name": name, "arguments": args}, headers)


# ---------------- 假 facilitator ----------------

class FakeFac(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path.endswith("/verify"):
            auth = ((body.get("paymentPayload") or {}).get("payload") or {}).get("authorization") or {}
            out = {"isValid": True, "payer": auth.get("from") or PAYER}
        elif self.path.endswith("/settle"):
            out = {"success": True, "transaction": "0x" + "ab" * 32,
                   "network": "base-sepolia", "payer": PAYER}
        elif self.path.endswith("/supported"):
            out = {"kinds": [{"x402Version": 1, "scheme": "exact", "network": "base-sepolia"}]}
        else:
            out = {"error": "unknown"}
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start_facilitator():
    srv = ThreadingHTTPServer(("127.0.0.1", FAC_PORT), FakeFac)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------- 构造 x402 载荷 ----------------

PAY_TO = "0x1111111111111111111111111111111111111111"


def payment_header(amount_micro, nonce, to=None, value=None):
    now = int(time.time())
    auth = {
        "from": PAYER,
        "to": to or PAY_TO,
        "value": str(value if value is not None else amount_micro),
        "validAfter": str(now - 10),
        "validBefore": str(now + 300),
        "nonce": nonce,
    }
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "base-sepolia",
        "payload": {"signature": "0x" + "00" * 65, "authorization": auth},
    }
    raw = json.dumps(payload).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def main():
    db = os.path.join(BASE, "data", "smoke.db")
    tokfile = os.path.join(BASE, "data", "smoke_admin.txt")
    # 用独立的价目表副本，避免跑自测把正式价格改掉
    pfile = os.path.join(BASE, "data", "smoke-pricing.json")
    shutil.copyfile(os.path.join(BASE, "config", "pricing.json"), pfile)
    for p in (db, db + "-wal", db + "-shm", tokfile,
              os.path.join(BASE, "data", "smoke-bootstrap.txt")):
        if os.path.exists(p):
            os.remove(p)

    start_facilitator()
    env = dict(os.environ)
    env.update({
        "PPC_PORT": str(PORT),
        "PPC_DB_PATH": "data/smoke.db",
        "PPC_PRICING_FILE": "data/smoke-pricing.json",
        "PPC_PUBLIC_BASE_URL": BASE_URL,
        "PPC_ADMIN_TOKEN_FILE": "data/smoke_admin.txt",
        "PPC_X402_PAY_TO": PAY_TO,
        "PPC_X402_FACILITATOR": "http://127.0.0.1:%d" % FAC_PORT,
        "PPC_X402_ENABLED": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    proc = subprocess.Popen([sys.executable, os.path.join(BASE, "app.py")],
                            cwd=BASE, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        ok = False
        for _ in range(60):
            time.sleep(0.3)
            try:
                s, d, _ = req("GET", "/healthz")
                if s == 200:
                    ok = True
                    break
            except Exception:  # noqa: BLE001
                pass
        if not ok:
            print("网关没起来，输出如下：")
            proc.kill()
            print((proc.stdout.read() or b"").decode("utf-8", "replace")[:3000])
            return 1

        print("\n[1] 基础可用性")
        s, d, _ = req("GET", "/healthz")
        check("healthz 200", s == 200 and d.get("ok") is True, d)
        check("x402 已启用", d.get("x402") is True, d)
        s, d, _ = req("GET", "/pricing")
        check("价目表可读", s == 200 and len(d.get("tools", [])) >= 15, len(d.get("tools", [])))
        s, d, _ = req("GET", "/openapi.json")
        check("openapi 可读", s == 200 and "paths" in d, list(d)[:5] if isinstance(d, dict) else d)
        s, txt, _ = req("GET", "/llms.txt", raw=True)
        check("llms.txt 可读", s == 200 and "MCP" in txt)
        s, d, _ = req("GET", "/.well-known/x402")
        check("x402 发现端点", s == 200 and d.get("x402Version") == 1, d)

        print("\n[2] MCP 协议")
        s, d, _ = mcp("initialize")
        check("initialize", s == 200 and d["result"]["serverInfo"]["name"] == "pay-per-call-gateway",
              d)
        s, d, _ = mcp("tools/list")
        names = [t["name"] for t in d.get("result", {}).get("tools", [])]
        check("tools/list >= 15 个工具", len(names) >= 15, len(names))
        check("付费工具描述带单价", any("[0.00" in t["description"]
                                        for t in d["result"]["tools"]), names[:3])

        print("\n[3] 免费工具不计费")
        s, d, _ = call("pricing.list", {})
        bill = d["result"].get("_billing", {})
        check("无 key 也能调免费工具", s == 200 and not d["result"]["isError"], d)
        check("免费工具金额为 0", bill.get("reason") == "free_tool", bill)

        print("\n[4] 无凭证调付费工具必须 402")
        s, d, hdrs = call("llm.chat", {"prompt": "hi"})
        check("HTTP 402", s == 402, s)
        check("返回 x402 accepts", bool(d.get("accepts")), d.get("accepts"))
        check("标了单价", d.get("payment_required", {}).get("price_usd", 0) > 0, d.get("payment_required"))

        print("\n[5] 钱包扣费")
        tok = open(tokfile, "r", encoding="utf-8").read().strip()
        s, d, _ = req("POST", "/admin/api/tenants",
                      {"name": "smoke-wallet", "plan": "wallet", "channel": "coze",
                       "balance_usd": 1}, {"X-Admin-Token": tok})
        key = d.get("api_key")
        tid = d.get("id")
        check("创建租户并拿到 key", bool(key) and key.startswith("ppc_live_"), d)
        H = {"X-API-Key": key}

        s, d, _ = call("usage.report", {"days": 30}, H)
        bal0 = d["result"]["content"][0]["text"]
        bal0 = json.loads(bal0)["balance_usd"]
        check("初始余额 $1", abs(bal0 - 1.0) < 1e-6, bal0)

        s, d, _ = call("kb.answer", {"question": "x402 的 nonce 为什么要落库"}, H)
        bill = d["result"].get("_billing", {})
        check("付费工具调用成功", s == 200 and not d["result"]["isError"], d["result"].get("content"))
        check("已扣费", bill.get("charged") is True, bill)
        check("余额减少", bill.get("balance_usd", 99) < bal0, bill)

        s, d, _ = call("usage.report", {"days": 30}, H)
        rep = json.loads(d["result"]["content"][0]["text"])
        check("对账里能看到这笔", rep.get("spent_micro", 0) > 0, rep)

        print("\n[6] 业务失败绝不扣费")
        s, d, _ = call("llm.chat", {"prompt": "hi"}, H)
        bill = d["result"].get("_billing", {})
        check("上游未配置时返回错误", d["result"]["isError"] is True, d["result"]["content"])
        check("失败不扣费", bill.get("charged") is False, bill)
        s, d, _ = call("usage.report", {"days": 30}, H)
        rep2 = json.loads(d["result"]["content"][0]["text"])
        check("余额未变", rep2["spent_micro"] == rep["spent_micro"],
              (rep["spent_micro"], rep2["spent_micro"]))

        s, d, _ = call("prompt.pack.get", {"sku": "不存在的SKU"}, H)
        check("SKU 不存在时失败", d["result"]["isError"] is True, d["result"]["content"])
        check("失败仍不扣费", d["result"]["_billing"].get("charged") is False, d["result"]["_billing"])

        print("\n[7] x402 按次收款")
        nonce = "0x" + os.urandom(16).hex()
        hdr = {"X-PAYMENT": payment_header(20000, nonce)}
        s, d, hdrs = call("prompt.pack.get", {"sku": "PROMPT-JD-INTERVIEW"}, hdr)
        bill = d["result"].get("_billing", {})
        check("链上付款被接受", s == 200 and not d["result"]["isError"], d["result"].get("content"))
        check("已结算有 txHash", bill.get("settled") is True and bool(bill.get("tx")), bill)
        check("回执头 X-PAYMENT-RESPONSE", bool(hdrs.get("X-PAYMENT-RESPONSE")), list(hdrs)[:6])
        check("返回了提示词包正文", "system" in d["result"]["content"][0]["text"],
              d["result"]["content"][0]["text"][:120])

        s, d, _ = call("prompt.pack.get", {"sku": "PROMPT-JD-INTERVIEW"}, hdr)
        check("同一 nonce 重放被拒 402", s == 402, s)
        check("拒绝原因 nonce_replayed", d.get("x402Error") == "nonce_replayed", d.get("x402Error"))

        s, d, _ = call("prompt.pack.get", {"sku": "PROMPT-JD-INTERVIEW"},
                       {"X-PAYMENT": payment_header(20000, "0x" + os.urandom(16).hex(),
                                                    to="0x9999999999999999999999999999999999999999")})
        check("收款地址不符被拒 402", s == 402 and d.get("x402Error") == "pay_to_mismatch",
              d.get("x402Error"))

        s, d, _ = call("prompt.pack.get", {"sku": "PROMPT-JD-INTERVIEW"},
                       {"X-PAYMENT": payment_header(20000, "0x" + os.urandom(16).hex(),
                                                    value=1)})
        check("金额不足被拒 402", s == 402 and d.get("x402Error") == "amount_too_low",
              d.get("x402Error"))

        s, d, _ = call("prompt.pack.get", {"sku": "PROMPT-JD-INTERVIEW"},
                       {"X-PAYMENT": "not-base64-!!!"})
        check("乱码付款头不放行", s == 402, s)

        print("\n[8] REST 包装（平台插件用）")
        s, d, _ = req("POST", "/x/kb.answer", {"question": "极限词有哪些"}, H)
        check("REST 200 且 ok", s == 200 and d.get("ok") is True, d)
        check("REST 返回 billing", "charged" in (d.get("billing") or {}), d.get("billing"))
        s, d, _ = req("POST", "/x/llm.chat", {"prompt": "hi"})
        check("REST 无凭证 402", s == 402 and d.get("ok") is False, (s, d.get("error")))

        print("\n[9] 平台订单回调")
        s, d, _ = req("POST", "/webhook/wechatpay", {"tenant_id": tid, "amount": 0.5})
        check("未配置密钥时拒绝 503", s == 503, s)
        os.environ["WECHATPAY_WEBHOOK_SECRET"] = "test"
        # 环境变量在子进程里，这里改主进程无效，需重启才生效，故只校验拒绝路径

        print("\n[10] 盗刷与越权")
        s, d, _ = req("GET", "/admin/api/summary?days=7")
        check("无令牌访问后台 401", s == 401, s)
        s, d, _ = req("GET", "/admin/api/summary?days=7", None, {"X-Admin-Token": "wrong"})
        check("错令牌 401", s == 401, s)
        s, d, _ = call("nuke.everything", {}, H)
        check("未知工具被拒", "error" in d, d)

        print("\n[11] 台账与毛利")
        s, d, _ = req("GET", "/admin/api/summary?days=7", None, {"X-Admin-Token": tok})
        check("收入 > 0", d.get("revenue_micro", 0) > 0, d.get("revenue_micro"))
        rails = {r["rail"]: r["n"] for r in d.get("by_rail", [])}
        check("钱包与 x402 两条腿都有流水",
              "x402" in rails and ("wallet" in rails or "coze" in rails), rails)
        check("毛利率有值", 0 < d.get("margin", 0) <= 1, d.get("margin"))
        check("失败调用被记录", d.get("failed_calls", 0) > 0, d.get("failed_calls"))
        s, d, _ = req("GET", "/admin/api/ledger?limit=50", None, {"X-Admin-Token": tok})
        x402rows = [r for r in d["items"] if r["rail"] == "x402"]
        check("台账里 x402 记录带自 tx", any(r.get("ref") for r in x402rows), x402rows[:1])

        print("\n[12] 调价即时生效")
        s, d, _ = req("POST", "/admin/api/price",
                      {"tool": "kb.answer", "price_usd": 0.05}, {"X-Admin-Token": tok})
        check("调价成功", s == 200 and d.get("price_usd") == 0.05, d)
        s, d, _ = req("GET", "/pricing")
        kb = [t for t in d["tools"] if t["tool"] == "kb.answer"][0]
        check("价目表同步", kb["price_usd"] == 0.05, kb)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("结果：%d/%d 通过" % (passed, total))
    if passed != total:
        print("\n失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print("  - %s  ->  %s" % (name, str(detail)[:200]))
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
