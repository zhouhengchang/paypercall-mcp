#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
payments.py — 收款通道适配

三条腿，覆盖"所有平台调一次就收一次钱"的全部现实路径：

  A. x402 链上按次支付（exact-EVM, v1）
     面向 Claude / Cursor / OpenRouter / 各类 agent 市场：调用方没有你的账号，
     直接带 X-PAYMENT 头付款。先 verify 再执行业务，业务成功才 settle ——
     拿不到 txHash 不算收入，也绝不"本地验个格式就放行"。

  B. API Key + 预充值钱包
     面向扣子/Coze、钉钉、微信小程序、支付宝小程序这类平台插件：
     平台侧只能传 header，不能签链上交易，走余额扣费。

  C. 平台订单 webhook
     微信支付 / 支付宝 / Stripe 的回调，HMAC 验签通过才入账。

安全底线（缺一即拒，不做宽松跳过）：
  - 金额、收款地址、网络、有效期、nonce 全部严格校验
  - nonce 落库唯一索引，防重放
  - facilitator 不可达 → 503 拒绝服务，绝不白送
"""

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request

ZERO_ADDR = "0x0000000000000000000000000000000000000000"


def _opener(proxy_mode="direct"):
    """本机常挂 HTTP_PROXY，直连模式必须显式清空，否则 127.0.0.1 或 API 会被劫持。"""
    if proxy_mode == "env":
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url, payload=None, timeout=15, headers=None, method=None, proxy_mode="direct"):
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "pay-per-call-gateway/1.0")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    opener = _opener(proxy_mode)
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return exc.code, body
    except Exception as exc:  # noqa: BLE001
        return 0, "transport_error:%s" % exc


class X402Error(Exception):
    pass


class X402(object):
    def __init__(self, cfg, proxy_mode="direct"):
        self.cfg = cfg or {}
        self.proxy_mode = proxy_mode

    # ---------- 报价 ----------

    @property
    def enabled(self):
        return bool(self.cfg.get("enabled")) and \
            self.cfg.get("pay_to", ZERO_ADDR).lower() != ZERO_ADDR

    def accepts(self, resource, amount_micro, description=""):
        """生成 402 响应里的 accepts 列表（v1 exact-EVM）。"""
        return [{
            "x402Version": 1,
            "scheme": "exact",
            "network": self.cfg.get("network", "base-sepolia"),
            "maxAmountRequired": str(int(amount_micro)),
            "resource": resource,
            "description": description[:400],
            "mimeType": "application/json",
            "payTo": self.cfg.get("pay_to"),
            "maxTimeoutSeconds": int(self.cfg.get("max_timeout_seconds", 120)),
            "asset": self.cfg.get("asset"),
            "extra": {
                "name": self.cfg.get("asset_name", "USDC"),
                "version": self.cfg.get("asset_version", "2"),
            },
        }]

    # ---------- 解析 ----------

    @staticmethod
    def decode_header(header_value):
        if not header_value:
            raise X402Error("missing X-PAYMENT header")
        raw = header_value.strip()
        pad = "=" * (-len(raw) % 4)
        try:
            body = base64.urlsafe_b64decode(raw + pad)
        except Exception:  # noqa: BLE001
            try:
                body = base64.b64decode(raw + pad)
            except Exception as exc:  # noqa: BLE001
                raise X402Error("X-PAYMENT is not base64: %s" % exc)
        try:
            return json.loads(body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise X402Error("X-PAYMENT is not JSON: %s" % exc)

    # ---------- 本地字段校验（白名单式，缺一即拒）----------

    def check_shape(self, payload, requirements):
        """
        返回 (ok, reason, info)。只做"结构完整性 + 参数一致性"，
        签名真伪/余额/nonce 未消费一律交给 facilitator，本地永不判定通过。
        """
        if not isinstance(payload, dict):
            return False, "payload_not_object", {}
        if int(payload.get("x402Version") or 0) != 1:
            return False, "unsupported_version", {}
        if payload.get("scheme") != "exact":
            return False, "unsupported_scheme", {}
        network = payload.get("network")
        if network not in (self.cfg.get("allow_networks") or [self.cfg.get("network")]):
            return False, "network_not_allowed", {"network": network}

        inner = payload.get("payload")
        if not isinstance(inner, dict):
            return False, "missing_payload", {}
        auth = inner.get("authorization")
        if not isinstance(auth, dict):
            return False, "missing_authorization", {}
        for field in ("from", "to", "value", "validAfter", "validBefore", "nonce"):
            if auth.get(field) in (None, ""):
                return False, "missing_authorization.%s" % field, {}
        sig = inner.get("signature") or auth.get("signature")
        if not sig:
            return False, "missing_signature", {}

        pay_to = (requirements.get("payTo") or "").lower()
        to = str(auth.get("to", "")).lower()
        if pay_to != ZERO_ADDR and to != pay_to:
            return False, "pay_to_mismatch", {"expected": pay_to, "got": to}

        try:
            value = int(auth.get("value"))
        except (TypeError, ValueError):
            return False, "value_not_integer", {}
        need = int(requirements.get("maxAmountRequired") or 0)
        if value < need:
            return False, "amount_too_low", {"need": need, "got": value}

        try:
            valid_before = int(auth.get("validBefore"))
            valid_after = int(auth.get("validAfter") or 0)
        except (TypeError, ValueError):
            return False, "validity_not_integer", {}
        ts = int(time.time())
        if valid_before and valid_before < ts:
            return False, "payment_expired", {"validBefore": valid_before, "now": ts}
        if valid_after and valid_after > ts + 60:
            return False, "not_yet_valid", {"validAfter": valid_after}

        return True, "ok", {
            "payer": auth.get("from"),
            "value": value,
            "nonce": str(auth.get("nonce")),
        }

    # ---------- facilitator ----------

    def _call(self, action, payload, requirements):
        url = self.cfg.get("facilitator", "").rstrip("/") + "/" + action
        status, body = http_json(
            url,
            {"x402Version": 1, "paymentPayload": payload, "paymentRequirements": requirements},
            timeout=20,
            proxy_mode=self.proxy_mode,
        )
        if status == 0:
            return None, {"error": "facilitator_unreachable", "detail": body[:300]}
        try:
            return status, json.loads(body)
        except Exception:  # noqa: BLE001
            return status, {"error": "facilitator_bad_json", "detail": body[:300]}

    def verify(self, payload, requirements):
        status, data = self._call("verify", payload, requirements)
        if status is None:
            return False, data
        if status >= 500:
            return False, {"error": "facilitator_error", "status": status, "detail": data}
        if not isinstance(data, dict):
            return False, {"error": "facilitator_bad_response"}
        if data.get("isValid") is True:
            return True, data
        return False, {"error": "invalid_payment",
                       "reason": data.get("invalidReason") or data.get("error"),
                       "detail": data}

    def settle(self, payload, requirements):
        status, data = self._call("settle", payload, requirements)
        if status is None:
            return False, data
        if not isinstance(data, dict):
            return False, {"error": "facilitator_bad_response", "status": status}
        if data.get("success") is True and data.get("transaction"):
            return True, data
        return False, {"error": "settle_failed", "detail": data, "status": status}

    # ---------- 响应头 ----------

    @staticmethod
    def encode_receipt(info):
        raw = json.dumps(info, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def verify_webhook(secret, body: bytes, signature: str):
    """平台订单回调验签：HMAC-SHA256(secret, raw_body) hex。"""
    if not secret or not signature:
        return False
    want = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature.strip().lower())
