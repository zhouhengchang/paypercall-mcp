#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
billing.py — 计费内核

职责：
  1. 租户/钱包/套餐余量/授权凭证的持久化（sqlite，单文件可备份）
  2. 价目表读取与热重载（改 config/pricing.json 立刻生效，不重启）
  3. 调用台账（ledger）：每笔调用记录 售价 / 上游成本 / 毛利 / 状态
  4. 对账汇总：按工具、按渠道、按租户、按天

金额一律是整数 micro-USD（1 USD = 1_000_000），全程不用浮点，避免对账误差。
"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid

MICRO = 1_000_000


def usd(micro):
    return round(micro / MICRO, 6)


def to_micro(value):
    """把美元数字转成 micro，四舍五入到整数。"""
    return int(round(float(value) * MICRO))


def now():
    return int(time.time())


def hash_key(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_api_key():
    return "ppc_live_" + secrets.token_hex(20)


class Pricing(object):
    """价目表。文件变更后按 mtime 自动重载，便于调价/上新品。"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._mtime = 0
        self._data = {"tools": {}, "defaults": {}}
        self.reload()

    def reload(self):
        with self._lock:
            st = os.stat(self.path)
            self._mtime = st.st_mtime
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            self._data.setdefault("tools", {})
            self._data.setdefault("defaults", {})
        return self._data

    def _ensure_fresh(self):
        try:
            if os.stat(self.path).st_mtime != self._mtime:
                self.reload()
        except OSError:
            pass

    @property
    def defaults(self):
        self._ensure_fresh()
        d = self._data.get("defaults") or {}
        d.setdefault("min_charge_micro", 1000)
        d.setdefault("markup", 2.0)
        d.setdefault("free_tools", [])
        return d

    def tools(self):
        self._ensure_fresh()
        return self._data.get("tools") or {}

    def spec(self, tool):
        self._ensure_fresh()
        return (self._data.get("tools") or {}).get(tool)

    def is_free(self, tool):
        return tool in (self.defaults.get("free_tools") or [])

    def quote(self, tool, args=None, sku_price_micro=None):
        """
        返回 (price_micro, cost_micro)。
        sku_price_micro：SKU 级定价（成品交付类），覆盖目录基准价。
        """
        spec = self.spec(tool)
        if spec is None:
            return None, None
        if self.is_free(tool):
            return 0, 0
        price = int(spec.get("price_micro") or 0)
        cost = int(spec.get("cost_micro") or 0)
        if sku_price_micro is not None:
            price = int(sku_price_micro)
        floor = int(self.defaults.get("min_charge_micro") or 0)
        if price and price < floor:
            price = floor
        return max(price, 0), max(cost, 0)

    def set_price(self, tool, price_micro):
        with self._lock:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            tools = data.setdefault("tools", {})
            if tool not in tools:
                raise KeyError(tool)
            tools[tool]["price_micro"] = int(price_micro)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        self.reload()


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  channel       TEXT NOT NULL DEFAULT 'direct',
  api_key_hash  TEXT UNIQUE,
  api_key_hint  TEXT,
  plan          TEXT NOT NULL DEFAULT 'wallet',
  quota_micro   INTEGER NOT NULL DEFAULT 0,
  quota_used_micro INTEGER NOT NULL DEFAULT 0,
  balance_micro INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'active',
  created_at    INTEGER NOT NULL,
  meta          TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,
  tenant_id   TEXT,
  tool        TEXT NOT NULL,
  channel     TEXT,
  rail        TEXT,
  price_micro INTEGER NOT NULL DEFAULT 0,
  cost_micro  INTEGER NOT NULL DEFAULT 0,
  status      TEXT NOT NULL,
  ref         TEXT,
  req_id      TEXT,
  ms          INTEGER DEFAULT 0,
  detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger(ts);
CREATE INDEX IF NOT EXISTS idx_ledger_tool ON ledger(tool);
CREATE INDEX IF NOT EXISTS idx_ledger_tenant ON ledger(tenant_id);
CREATE TABLE IF NOT EXISTS payments (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           INTEGER NOT NULL,
  tenant_id    TEXT,
  rail         TEXT NOT NULL,
  amount_micro INTEGER NOT NULL,
  asset        TEXT,
  network      TEXT,
  payer        TEXT,
  tx           TEXT,
  nonce        TEXT UNIQUE,
  status       TEXT NOT NULL,
  raw          TEXT
);
CREATE INDEX IF NOT EXISTS idx_payments_ts ON payments(ts);
CREATE TABLE IF NOT EXISTS licenses (
  id         TEXT PRIMARY KEY,
  ts         INTEGER NOT NULL,
  tenant_id  TEXT,
  sku        TEXT,
  kind       TEXT,
  machine    TEXT,
  token      TEXT,
  status     TEXT NOT NULL DEFAULT 'active',
  meta       TEXT
);
CREATE TABLE IF NOT EXISTS leads (
  id         TEXT PRIMARY KEY,
  ts         INTEGER NOT NULL,
  tenant_id  TEXT,
  kind       TEXT,
  contact    TEXT,
  payload    TEXT,
  status     TEXT NOT NULL DEFAULT 'new'
);
CREATE TABLE IF NOT EXISTS topups (
  id           TEXT PRIMARY KEY,
  ts           INTEGER NOT NULL,
  plan_id      TEXT,
  cny          REAL NOT NULL DEFAULT 0,
  credit_micro INTEGER NOT NULL DEFAULT 0,
  channel      TEXT NOT NULL,
  payer_ref    TEXT NOT NULL,
  contact      TEXT NOT NULL,
  note         TEXT,
  status       TEXT NOT NULL DEFAULT 'pending',
  tenant_id    TEXT,
  reviewed_at  INTEGER,
  reviewer     TEXT
);
CREATE INDEX IF NOT EXISTS idx_topups_ts ON topups(ts);
CREATE INDEX IF NOT EXISTS idx_topups_status ON topups(status);
"""


class Store(object):
    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._local = threading.local()
        self.init_schema()

    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, timeout=20, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=20000")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return c

    def init_schema(self):
        c = self.conn()
        c.executescript(SCHEMA)
        c.commit()

    # ---------- 租户 ----------

    def create_tenant(self, name, channel="direct", plan="wallet",
                      balance_micro=0, quota_micro=0, meta=None):
        tid = "t_" + uuid.uuid4().hex[:12]
        key = new_api_key()
        hint = key[:14] + "..." + key[-4:]
        self.conn().execute(
            "INSERT INTO tenants(id,name,channel,api_key_hash,api_key_hint,plan,"
            "quota_micro,quota_used_micro,balance_micro,status,created_at,meta) "
            "VALUES(?,?,?,?,?,?,?,0,?, 'active', ?, ?)",
            (tid, name, channel, hash_key(key), hint, plan,
             int(quota_micro), int(balance_micro), now(),
             json.dumps(meta or {}, ensure_ascii=False)),
        )
        self.conn().commit()
        return tid, key

    def tenant_by_key(self, raw_key):
        if not raw_key:
            return None
        row = self.conn().execute(
            "SELECT * FROM tenants WHERE api_key_hash=?", (hash_key(raw_key),)
        ).fetchone()
        return dict(row) if row else None

    def tenant(self, tid):
        row = self.conn().execute("SELECT * FROM tenants WHERE id=?", (tid,)).fetchone()
        return dict(row) if row else None

    def tenants(self):
        rows = self.conn().execute(
            "SELECT * FROM tenants ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def recharge(self, tid, amount_micro, rail="manual", ref=None):
        c = self.conn()
        c.execute("UPDATE tenants SET balance_micro = balance_micro + ? WHERE id=?",
                  (int(amount_micro), tid))
        c.execute(
            "INSERT INTO payments(ts,tenant_id,rail,amount_micro,status,raw) "
            "VALUES(?,?,?,?, 'credited', ?)",
            (now(), tid, rail, int(amount_micro), json.dumps({"ref": ref})),
        )
        c.commit()

    # ---------- 扣费 ----------

    def charge_wallet(self, tid, price_micro, cost_micro, tool, channel,
                      rail="wallet", ref=None, req_id=None, ms=0, detail=None):
        """钱包扣费 + 记台账，同一事务，避免出现扣了钱没账或反之。"""
        c = self.conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT balance_micro FROM tenants WHERE id=?", (tid,)).fetchone()
            if row is None:
                c.rollback()
                return False, "tenant_not_found"
            if price_micro > 0 and row["balance_micro"] < price_micro:
                c.rollback()
                return False, "insufficient_balance"
            if price_micro > 0:
                c.execute("UPDATE tenants SET balance_micro = balance_micro - ? WHERE id=?",
                          (int(price_micro), tid))
            c.execute(
                "INSERT INTO ledger(ts,tenant_id,tool,channel,rail,price_micro,cost_micro,"
                "status,ref,req_id,ms,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (now(), tid, tool, channel, rail, int(price_micro), int(cost_micro),
                 "ok", ref, req_id, int(ms),
                 json.dumps(detail or {}, ensure_ascii=False)[:2000]),
            )
            c.commit()
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            try:
                c.rollback()
            except Exception:  # noqa: BLE001
                pass
            return False, "charge_error:%s" % exc

    def charge_quota(self, tid, price_micro, cost_micro, tool, channel,
                     req_id=None, ms=0, detail=None):
        """订阅包次：从套餐余量里扣，不动钱包。"""
        c = self.conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT quota_micro, quota_used_micro FROM tenants WHERE id=?", (tid,)
            ).fetchone()
            if row is None:
                c.rollback()
                return False, "tenant_not_found"
            remain = row["quota_micro"] - row["quota_used_micro"]
            if remain < price_micro:
                c.rollback()
                return False, "quota_exhausted"
            c.execute("UPDATE tenants SET quota_used_micro = quota_used_micro + ? WHERE id=?",
                      (int(price_micro), tid))
            c.execute(
                "INSERT INTO ledger(ts,tenant_id,tool,channel,rail,price_micro,cost_micro,"
                "status,ref,req_id,ms,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (now(), tid, tool, channel, "subscription", int(price_micro), int(cost_micro),
                 "ok", None, req_id, int(ms),
                 json.dumps(detail or {}, ensure_ascii=False)[:2000]),
            )
            c.commit()
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            try:
                c.rollback()
            except Exception:  # noqa: BLE001
                pass
            return False, "charge_error:%s" % exc

    def record_free(self, tid, tool, channel, req_id=None, ms=0, detail=None):
        c = self.conn()
        c.execute(
            "INSERT INTO ledger(ts,tenant_id,tool,channel,rail,price_micro,cost_micro,"
            "status,ref,req_id,ms,detail) VALUES(?,?,?,?,?,0,0,?,?,?,?,?)",
            (now(), tid, tool, channel, "free", "free", None, req_id, int(ms),
             json.dumps(detail or {}, ensure_ascii=False)[:2000]),
        )
        c.commit()

    def record_failed(self, tid, tool, channel, rail, reason, req_id=None, ms=0):
        c = self.conn()
        c.execute(
            "INSERT INTO ledger(ts,tenant_id,tool,channel,rail,price_micro,cost_micro,"
            "status,ref,req_id,ms,detail) VALUES(?,?,?,?,?,0,0,?,?,?,?,?)",
            (now(), tid, tool, channel, rail, "failed", None, req_id, int(ms),
             json.dumps({"reason": reason}, ensure_ascii=False)[:500]),
        )
        c.commit()

    # ---------- 支付流水 ----------

    def nonce_used(self, nonce):
        if not nonce:
            return False
        row = self.conn().execute("SELECT 1 FROM payments WHERE nonce=?", (nonce,)).fetchone()
        return row is not None

    def record_payment(self, tenant_id, rail, amount_micro, asset=None, network=None,
                       payer=None, tx=None, nonce=None, status="settled", raw=None):
        c = self.conn()
        try:
            c.execute(
                "INSERT INTO payments(ts,tenant_id,rail,amount_micro,asset,network,payer,"
                "tx,nonce,status,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (now(), tenant_id, rail, int(amount_micro), asset, network, payer, tx,
                 nonce, status, json.dumps(raw or {}, ensure_ascii=False)[:4000]),
            )
            c.commit()
            return True
        except sqlite3.IntegrityError:
            c.rollback()
            return False

    # ---------- 授权凭证 ----------

    def save_license(self, lid, tenant_id, sku, kind, machine, token, meta=None):
        self.conn().execute(
            "INSERT INTO licenses(id,ts,tenant_id,sku,kind,machine,token,status,meta) "
            "VALUES(?,?,?,?,?,?,?, 'active', ?)",
            (lid, now(), tenant_id, sku, kind, machine, token,
             json.dumps(meta or {}, ensure_ascii=False)),
        )
        self.conn().commit()

    def license(self, lid):
        row = self.conn().execute("SELECT * FROM licenses WHERE id=?", (lid,)).fetchone()
        return dict(row) if row else None

    # ---------- 线索 ----------

    def save_lead(self, tenant_id, kind, contact, payload):
        lid = "L" + uuid.uuid4().hex[:10]
        self.conn().execute(
            "INSERT INTO leads(id,ts,tenant_id,kind,contact,payload) VALUES(?,?,?,?,?,?)",
            (lid, now(), tenant_id, kind, contact,
             json.dumps(payload or {}, ensure_ascii=False)),
        )
        self.conn().commit()
        return lid

    def leads(self, limit=100):
        rows = self.conn().execute(
            "SELECT * FROM leads ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 扫码充值（人工确认）----------
    #
    # 个人/经营收款码没有回调通知，无法自动入账。这里的流程刻意做成
    # 用户提交 -> 后台人工核对 -> 一键入账并下发 API Key，
    # 而不是假装能自动到账。伪造"自动确认"会让真实付款人拿不到额度。

    def create_topup(self, plan_id, cny, credit_micro, channel, payer_ref,
                     contact, note=None):
        tid = "TP" + uuid.uuid4().hex[:10].upper()
        self.conn().execute(
            "INSERT INTO topups(id,ts,plan_id,cny,credit_micro,channel,payer_ref,"
            "contact,note,status) VALUES(?,?,?,?,?,?,?,?,?, 'pending')",
            (tid, now(), plan_id, float(cny), int(credit_micro), channel,
             payer_ref, contact, note),
        )
        self.conn().commit()
        return tid

    def topup(self, tid):
        row = self.conn().execute("SELECT * FROM topups WHERE id=?", (tid,)).fetchone()
        return dict(row) if row else None

    def topups(self, limit=100, status=None):
        if status:
            rows = self.conn().execute(
                "SELECT * FROM topups WHERE status=? ORDER BY ts DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = self.conn().execute(
                "SELECT * FROM topups ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def approve_topup(self, tid, reviewer="admin", name=None):
        """确认到账：不存在租户则新建并返回一次性 api_key，否则直接加余额。"""
        t = self.topup(tid)
        if not t:
            raise KeyError("topup_not_found")
        if t["status"] != "pending":
            raise ValueError("already_%s" % t["status"])
        api_key = None
        tenant_id = t["tenant_id"]
        if tenant_id and self.tenant(tenant_id):
            self.recharge(tenant_id, t["credit_micro"], rail="qr_" + t["channel"],
                          ref=tid)
        else:
            tenant_id, api_key = self.create_tenant(
                name or ("qr-" + t["contact"][:24]),
                channel="qr_" + t["channel"], plan="wallet",
                balance_micro=t["credit_micro"],
                meta={"topup": tid, "contact": t["contact"]},
            )
            self.conn().execute(
                "INSERT INTO payments(ts,tenant_id,rail,amount_micro,status,raw) "
                "VALUES(?,?,?,?, 'credited', ?)",
                (now(), tenant_id, "qr_" + t["channel"], t["credit_micro"],
                 json.dumps({"ref": tid, "cny": t["cny"]}, ensure_ascii=False)),
            )
        self.conn().execute(
            "UPDATE topups SET status='approved', tenant_id=?, reviewed_at=?, "
            "reviewer=? WHERE id=?",
            (tenant_id, now(), reviewer, tid),
        )
        self.conn().commit()
        return {"tenant_id": tenant_id, "api_key": api_key,
                "credit_usd": round(t["credit_micro"] / 1e6, 4)}

    def reject_topup(self, tid, reviewer="admin", reason=None):
        t = self.topup(tid)
        if not t:
            raise KeyError("topup_not_found")
        if t["status"] != "pending":
            raise ValueError("already_%s" % t["status"])
        self.conn().execute(
            "UPDATE topups SET status='rejected', reviewed_at=?, reviewer=?, "
            "note=COALESCE(note,'')||? WHERE id=?",
            (now(), reviewer, (" | reject: " + (reason or "")) if reason else "",
             tid),
        )
        self.conn().commit()
        return {"ok": True, "status": "rejected"}

    # ---------- 报表 ----------

    def summary(self, days=7):
        since = now() - days * 86400
        c = self.conn()
        tot = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(price_micro),0) rev, COALESCE(SUM(cost_micro),0) cost "
            "FROM ledger WHERE ts>=? AND status='ok'", (since,)
        ).fetchone()
        by_tool = c.execute(
            "SELECT tool, COUNT(*) n, SUM(price_micro) rev, SUM(cost_micro) cost "
            "FROM ledger WHERE ts>=? AND status='ok' GROUP BY tool ORDER BY rev DESC",
            (since,),
        ).fetchall()
        by_channel = c.execute(
            "SELECT channel, COUNT(*) n, SUM(price_micro) rev "
            "FROM ledger WHERE ts>=? AND status='ok' GROUP BY channel ORDER BY rev DESC",
            (since,),
        ).fetchall()
        by_rail = c.execute(
            "SELECT rail, COUNT(*) n, SUM(price_micro) rev "
            "FROM ledger WHERE ts>=? AND status='ok' GROUP BY rail ORDER BY rev DESC",
            (since,),
        ).fetchall()
        by_day = c.execute(
            "SELECT date(ts,'unixepoch','localtime') d, COUNT(*) n, "
            "SUM(price_micro) rev, SUM(cost_micro) cost "
            "FROM ledger WHERE ts>=? AND status='ok' GROUP BY d ORDER BY d",
            (since,),
        ).fetchall()
        fails = c.execute(
            "SELECT COUNT(*) n FROM ledger WHERE ts>=? AND status='failed'", (since,)
        ).fetchone()
        paid = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(amount_micro),0) amt FROM payments "
            "WHERE ts>=? AND status IN ('settled','credited')", (since,)
        ).fetchone()
        return {
            "window_days": days,
            "calls": tot["n"],
            "revenue_micro": tot["rev"],
            "cost_micro": tot["cost"],
            "gross_micro": tot["rev"] - tot["cost"],
            "revenue_usd": usd(tot["rev"]),
            "cost_usd": usd(tot["cost"]),
            "gross_usd": usd(tot["rev"] - tot["cost"]),
            "margin": round((tot["rev"] - tot["cost"]) / tot["rev"], 4) if tot["rev"] else 0,
            "failed_calls": fails["n"],
            "payments": paid["n"],
            "payments_usd": usd(paid["amt"]),
            "by_tool": [dict(r, revenue_usd=usd(r["rev"] or 0),
                             cost_usd=usd(r["cost"] or 0)) for r in by_tool],
            "by_channel": [dict(r) for r in by_channel],
            "by_rail": [dict(r) for r in by_rail],
            "by_day": [dict(r) for r in by_day],
        }

    def ledger(self, limit=100, tenant_id=None):
        sql = "SELECT * FROM ledger"
        params = []
        if tenant_id:
            sql += " WHERE tenant_id=?"
            params.append(tenant_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self.conn().execute(sql, params).fetchall()]

    def tenant_usage(self, tid, days=30):
        since = now() - days * 86400
        c = self.conn()
        rows = c.execute(
            "SELECT tool, COUNT(*) n, SUM(price_micro) rev FROM ledger "
            "WHERE tenant_id=? AND ts>=? AND status='ok' GROUP BY tool",
            (tid, since),
        ).fetchall()
        tot = c.execute(
            "SELECT COALESCE(SUM(price_micro),0) rev FROM ledger "
            "WHERE tenant_id=? AND ts>=? AND status='ok'", (tid, since)
        ).fetchone()
        return {"window_days": days, "spent_micro": tot["rev"],
                "spent_usd": usd(tot["rev"]),
                "by_tool": [dict(r, revenue_usd=usd(r["rev"] or 0)) for r in rows]}


class Signer(object):
    """HMAC 签名器：授权凭证/许可令牌。买方可以离线验签，减少售后争议。"""

    def __init__(self, secret_path):
        os.makedirs(os.path.dirname(os.path.abspath(secret_path)), exist_ok=True)
        if os.path.exists(secret_path):
            with open(secret_path, "r", encoding="utf-8") as f:
                self.secret = f.read().strip()
        else:
            self.secret = secrets.token_hex(32)
            with open(secret_path, "w", encoding="utf-8") as f:
                f.write(self.secret)
        self.secret = self.secret.encode("utf-8")

    def sign(self, payload: dict) -> str:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
        sig = hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        return "%s.%s" % (sig, _b64(body))

    @staticmethod
    def verify(token: str) -> bool:
        # 公开验签走 verify_token（需要 secret），这里仅保留接口一致性
        raise NotImplementedError("use verify_token(secret, token)")

    def verify_token(self, secret_hex, token):
        try:
            sig, body_b64 = token.split(".", 1)
        except ValueError:
            return None
        body = _unb64(body_b64)
        want = hmac.new(bytes.fromhex(secret_hex), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            return None
        return json.loads(body.decode("utf-8"))


def _b64(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    import base64
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)
