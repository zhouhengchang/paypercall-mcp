#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""扫码充值全链路自测：提交 -> 防重 -> 后台列表 -> 审核开通 -> 用新 Key 真扣费。"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8300"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 绕过本机代理，否则 127.0.0.1 会被劫持
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ok = 0
bad = 0


def check(name, cond, detail=""):
    global ok, bad
    if cond:
        ok += 1
        print("  PASS  %s" % name)
    else:
        bad += 1
        print("  FAIL  %s  -> %s" % (name, detail))


def req(path, body=None, headers=None, method=None):
    url = BASE + path
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=hdrs,
                              method=method or ("POST" if data else "GET"))
    try:
        with OPENER.open(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


def main():
    tok_path = os.path.join(ROOT, "data", "admin_token.txt")
    if not os.path.exists(tok_path):
        print("缺少 data/admin_token.txt，先启动一次服务")
        return 2
    admin = {"X-Admin-Token": open(tok_path, encoding="utf-8").read().strip()}

    print("\n[1] 提交充值单")
    ref = "TEST%d" % int(time.time())
    s, d = req("/topup/claim", {
        "plan_id": "p75", "channel": "wechat",
        "payer_ref": ref, "contact": "selftest@example.com",
        "note": "自动化自测",
    })
    check("HTTP 201", s == 201, s)
    check("返回单号", bool(d.get("topup_id")), d)
    check("状态 pending", d.get("status") == "pending", d.get("status"))
    check("额度 $10", d.get("credit_usd") == 10, d.get("credit_usd"))
    tid = d.get("topup_id")

    print("\n[2] 同一交易号重复提交必须拒绝")
    s2, d2 = req("/topup/claim", {
        "plan_id": "p75", "channel": "wechat",
        "payer_ref": ref, "contact": "selftest@example.com",
    })
    check("HTTP 409", s2 == 409, s2)
    check("错误码 duplicate", d2.get("error") == "duplicate_payer_ref", d2)

    print("\n[3] 参数校验")
    s3, d3 = req("/topup/claim", {"plan_id": "nope", "channel": "wechat",
                                  "payer_ref": "ABCD", "contact": "a@b.c"})
    check("未知套餐 400", s3 == 400 and d3.get("error") == "unknown_plan", d3)
    s4, d4 = req("/topup/claim", {"plan_id": "p15", "channel": "wechat",
                                  "payer_ref": "AB", "contact": "a@b.c"})
    check("交易号过短 400", s4 == 400 and d4.get("error") == "payer_ref_too_short", d4)
    s5, d5 = req("/topup/claim", {"plan_id": "p15", "channel": "paypal",
                                  "payer_ref": "ABCD", "contact": "a@b.c"})
    check("未知渠道 400", s5 == 400 and d5.get("error") == "unknown_channel", d5)

    print("\n[4] 后台列表与鉴权")
    s6, d6 = req("/admin/api/topups?status=pending")
    check("无令牌 401", s6 == 401, s6)
    s7, d7 = req("/admin/api/topups?status=pending", headers=admin)
    ids = [x["id"] for x in d7.get("items", [])]
    check("待审列表含该单", tid in ids, ids)
    check("列表不泄露 api_key", "api_key" not in json.dumps(d7), "leak")

    print("\n[5] 审核开通")
    s8, d8 = req("/admin/api/topups/approve", {"topup_id": tid}, admin)
    check("开通成功", s8 == 200 and bool(d8.get("api_key")), d8)
    key = d8.get("api_key") or ""
    check("api_key 格式", key.startswith("ppc_live_"), key[:12])
    check("额度 $10", d8.get("credit_usd") == 10, d8.get("credit_usd"))

    print("\n[6] 重复开通必须拒绝（防双倍给额度）")
    s9, d9 = req("/admin/api/topups/approve", {"topup_id": tid}, admin)
    check("HTTP 409", s9 == 409, s9)
    check("错误 already_approved", d9.get("error") == "already_approved", d9)

    print("\n[7] 新 Key 真实扣费")
    s10, d10 = req("/x/kb.answer", {"question": "非ce 为什么要落库"},
                   {"X-API-Key": key})
    check("调用成功", s10 == 200 and d10.get("ok") is True, d10)
    billed = d10.get("billing") or {}
    check("已扣费", billed.get("charged") is True, billed)

    s11, d11 = req("/admin/api/tenants", headers=admin)
    mine = [t for t in d11.get("items", []) if t["id"] == d8.get("tenant_id")]
    check("租户存在", len(mine) == 1, d11.get("items")[:1])
    if mine:
        t = mine[0]
        check("余额 从 $10 扣掉一次调用", t["balance_micro"] < 10_000_000,
              t["balance_micro"])
        check("渠道标记为扫码", t["channel"].startswith("qr_wechat"), t["channel"])

    print("\n[8] 驳回流程")
    s12, d12 = req("/topup/claim", {"plan_id": "p15", "channel": "alipay",
                                    "payer_ref": ref + "B",
                                    "contact": "reject@example.com"})
    rtid = d12.get("topup_id")
    s13, d13 = req("/admin/api/topups/reject",
                   {"topup_id": rtid, "reason": "自测驳回"}, admin)
    check("驳回成功", s13 == 200 and d13.get("status") == "rejected", d13)
    s14, d14 = req("/admin/api/topups/approve", {"topup_id": rtid}, admin)
    check("已驳回不能再开通", s14 == 409 and d14.get("error") == "already_rejected", d14)

    print("\n[9] 未提交的单不能凭空开通")
    s15, d15 = req("/admin/api/topups/approve", {"topup_id": "TP_NOT_EXIST"}, admin)
    check("HTTP 404", s15 == 404 and d15.get("error") == "topup_not_found", d15)

    print("\n==== 结果：%d 通过 / %d 失败 ====" % (ok, bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
