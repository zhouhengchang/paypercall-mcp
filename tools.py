#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools.py — 可售能力实现

每个 handler 对应一条收款线，共同约定：
  - 返回 (mcp_result, cost_micro_override or None, price_micro_override or None)
  - 抛 ToolError 表示"没干成"——调用方不计费，宁可少收一笔也不卖空
  - 不臆造结果：真的调不到上游就报错，不返回假数据骗一次调用费

库存（提示词包/模板/知识库）从 inventory/ 目录实时读取，加文件即上新品。
"""

import json
import os
import re
import time
import uuid

import payments
from billing import usd, to_micro, MICRO

CJK = re.compile(r"[\u4e00-\u9fff]")


class ToolError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _est_tokens(text):
    """粗略 token 估算：中文按字、英文按词，宁多不少（用于成本核算，不用于计费口径）。"""
    if not text:
        return 0
    cjk = len(CJK.findall(text))
    rest = CJK.sub(" ", text)
    return int(cjk * 1.05 + len(rest.split()) * 1.35) + 4


TOOL_SCHEMAS = {
    "llm.chat": {
        "description": "多模型统一对话。一次调用自动路由到已配置的最便宜可用模型，按次固定价，输出上限 2048 token。需要上游模型 key，未配置会明确报错且不扣费。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "用户输入。与 messages 二选一。"},
                "messages": {"type": "array", "description": "OpenAI 格式消息数组",
                             "items": {"type": "object"}},
                "provider": {"type": "string",
                             "description": "指定上游：deepseek/glm/qwen/kimi/minimax/hunyuan/openrouter，不填则自动选"},
                "max_tokens": {"type": "integer", "description": "输出上限，默认 1024，最大 2048"},
                "temperature": {"type": "number", "description": "默认 0.7"},
            },
        },
    },
    "agent.consume": {
        "description": "订阅客户的包次消耗：校验套餐余量并签发可离线校验的一次性权益令牌。余额不足会明确报错。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "智能体标识"},
                "units": {"type": "integer", "description": "本次消耗包次数，默认 1"},
            },
            "required": ["agent"],
        },
    },
    "mcp.skill.invoke": {
        "description": "MCP 技能代理调用。把已登记的上游 MCP 技能统一收口计费，换上游实现不影响调用方。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "已登记的技能 ID"},
                "args": {"type": "object", "description": "透传给上游工具的参数"},
            },
            "required": ["skill"],
        },
    },
    "prompt.pack.get": {
        "description": "按 SKU 交付可商用提示词包（系统提示词、变量说明、规则、示例）。sku 可先调 asset.catalog 查。",
        "inputSchema": {
            "type": "object",
            "properties": {"sku": {"type": "string", "description": "提示词包 SKU"}},
            "required": ["sku"],
        },
    },
    "code.template.get": {
        "description": "按 SKU 交付可运行源码模板，返回全部文件内容与商用许可编号。",
        "inputSchema": {
            "type": "object",
            "properties": {"sku": {"type": "string", "description": "模板 SKU"}},
            "required": ["sku"],
        },
    },
    "asset.license.issue": {
        "description": "签发带 HMAC 签名的商用授权凭证，买方离线可验，可用于平台申诉举证。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "素材/SKU 编号"},
                "buyer": {"type": "string", "description": "被授权方名称"},
                "scope": {"type": "string", "description": "授权范围，如 digital-ads / app-embed / print"},
                "days": {"type": "integer", "description": "有效期天数，默认 365"},
            },
            "required": ["sku", "buyer"],
        },
    },
    "plugin.license.activate": {
        "description": "把授权绑定到机器指纹，返回可离线校验的授权令牌，防一份授权多机复用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "插件 SKU"},
                "machine": {"type": "string", "description": "机器指纹（建议 sha256(主机名+MAC)）"},
                "seats": {"type": "integer", "description": "授权席位，默认 1"},
            },
            "required": ["sku", "machine"],
        },
    },
    "compute.quote": {
        "description": "算力分销报价：按卡型/时长/区域返回分销价与零售价，价差即毛利。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "gpu": {"type": "string", "description": "卡型，如 4090 / A100-80G / H20"},
                "hours": {"type": "number", "description": "使用时长（小时），默认 1"},
                "region": {"type": "string", "description": "区域，默认 cn-north"},
            },
            "required": ["gpu"],
        },
    },
    "kb.answer": {
        "description": "在自有知识库上做检索问答，返回答案与出处段落。不依赖外部大模型，离线可用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "问题"},
                "top_k": {"type": "integer", "description": "返回段落数，默认 3"},
            },
            "required": ["question"],
        },
    },
    "content.brief": {
        "description": "按平台/品类/时长生成结构化口播脚本与分镜要点，交付即可给写手或剪辑用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "description": "douyin/xhs/bilibili/kuaishou/shipinhao"},
                "product": {"type": "string", "description": "产品与核心卖点"},
                "seconds": {"type": "integer", "description": "目标时长（秒），默认 45"},
                "audience": {"type": "string", "description": "目标人群"},
            },
            "required": ["platform", "product"],
        },
    },
    "custom.quote": {
        "description": "定制需求预报价（免费引流）。落线索并返回报价区间与排期，人工跟进转外包订单。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "description": "需求类型：web/app/miniprogram/agent/data/design/other"},
                "scope": {"type": "string", "description": "需求描述"},
                "deadline_days": {"type": "integer", "description": "期望交付天数，默认 30"},
                "contact": {"type": "string", "description": "联系方式（选填）"},
            },
            "required": ["kind", "scope"],
        },
    },
    "policy.check": {
        "description": "上架合规预检（免费）：扫描侵权、极限词、平台风控词，返回命中项与替换建议。",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "待检文案"}},
            "required": ["text"],
        },
    },
    "asset.catalog": {
        "description": "在售 SKU 目录（免费）：列出全部提示词包/模板/素材及其价格，供 Agent 自主选购。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "pricing.list": {
        "description": "完整价目表与计费口径（免费）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "usage.report": {
        "description": "自助对账（免费）：查询本 key 的用量、扣费与付款记录，减少售后。",
        "inputSchema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "统计天数，默认 30"}},
        },
    },
}

# 极限词/风控词（广告法 + 常见平台规则），用于 policy.check
BANNED = [
    "国家级", "最高级", "最佳", "第一品牌", "首个", "独家", "唯一", "顶级", "极品",
    "绝无仅有", "史无前例", "万能", "100%", "永久", "根治", "特效", "包治",
    "秒杀全网", "史上最低", "国家级认证", "无副作用", "稳赚", "保本", "高回报",
]
INFRINGE_HINTS = [
    ("破解", "涉嫌侵犯软件著作权"),
    ("绿色版", "涉嫌侵犯软件著作权"),
    ("去水印", "可能侵犯他人作品权利，需确认授权来源"),
    ("搬运", "搬运他人内容易触发平台侵权判定"),
    ("代下载", "注意素材版权来源"),
    ("影视资源", "注意信息网络传播权"),
]


class Tools(object):
    def __init__(self, cfg, pricing, store, signer, base_dir):
        self.cfg = cfg
        self.pricing = pricing
        self.store = store
        self.signer = signer
        self.base = base_dir
        self.proxy_mode = cfg.get("http_proxy_mode", "direct")
        with open(os.path.join(base_dir, cfg.get("providers_file", "config/providers.json")),
                  "r", encoding="utf-8") as f:
            self.providers_cfg = json.load(f)
        self.skills = self._load_skills()
        self.inventory = self._load_inventory()

    # ---------- 库存 ----------

    def _load_json_dir(self, sub):
        out = {}
        d = os.path.join(self.base, "inventory", sub)
        if not os.path.isdir(d):
            return out
        for name in sorted(os.listdir(d)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                    item = json.load(f)
                out[item["sku"]] = item
            except Exception:  # noqa: BLE001
                continue
        return out

    def _load_inventory(self):
        inv = {}
        for sub in ("prompts", "templates", "assets"):
            inv.update(self._load_json_dir(sub))
        return inv

    def reload_inventory(self):
        self.inventory = self._load_inventory()
        return len(self.inventory)

    def _load_skills(self):
        path = os.path.join(self.base, "config", "skills.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return (json.load(f) or {}).get("skills", {})
        except Exception:  # noqa: BLE001
            return {}

    # ---------- MCP 工具清单 ----------

    def list_tools(self):
        specs = self.pricing.tools()
        out = []
        for name, schema in TOOL_SCHEMAS.items():
            spec = specs.get(name) or {}
            price, _ = self.pricing.quote(name)
            desc = schema["description"]
            if price:
                desc = "[%s USD/次] %s" % (("%.6f" % usd(price)).rstrip("0").rstrip("."), desc)
            else:
                desc = "[免费] " + desc
            out.append({
                "name": name,
                "description": desc,
                "inputSchema": schema["inputSchema"],
                "annotations": {"revenueLine": spec.get("revenue_line", "")},
            })
        return out

    # ---------- 定价 ----------

    def resolve_quote(self, name, args):
        sku_price = None
        if name in ("prompt.pack.get", "code.template.get", "asset.license.issue"):
            sku = (args or {}).get("sku")
            item = self.inventory.get(sku or "")
            if item:
                sku_price = int(item.get("price_micro") or 0) or None
        return self.pricing.quote(name, args, sku_price)

    # ---------- 分发 ----------

    def run(self, name, args, ctx):
        """返回 (result, cost_micro, price_micro)。"""
        fn = getattr(self, "h_" + name.replace(".", "_"), None)
        if fn is None:
            raise ToolError("unknown_tool", "tool not implemented: %s" % name, 404)
        return fn(args or {}, ctx or {})

    @staticmethod
    def _text(obj, extra=None):
        if isinstance(obj, str):
            body = obj
        else:
            body = json.dumps(obj, ensure_ascii=False, indent=2)
        if extra:
            body += "\n\n" + extra
        return {"content": [{"type": "text", "text": body}], "isError": False}

    # ---------- 各条收款线 ----------

    def h_llm_chat(self, args, ctx):
        cfg = self.providers_cfg
        order = cfg.get("default_order") or []
        provs = cfg.get("providers") or {}
        want = (args.get("provider") or "").strip()
        if want and want not in provs:
            raise ToolError("unknown_provider", "provider 不在路由表里：%s" % want)
        candidates = [want] if want else order
        max_tokens = min(int(args.get("max_tokens") or 1024), 2048)
        messages = args.get("messages")
        if not messages:
            prompt = (args.get("prompt") or "").strip()
            if not prompt:
                raise ToolError("bad_request", "prompt 与 messages 至少要给一个")
            messages = [{"role": "user", "content": prompt}]

        errors = []
        for name in candidates:
            spec = provs.get(name) or {}
            key = os.environ.get(spec.get("key_env") or "", "").strip()
            if not key:
                errors.append("%s: no key (%s)" % (name, spec.get("key_env")))
                continue
            body = {
                "model": spec.get("model"),
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": float(args.get("temperature") or 0.7),
                "stream": False,
            }
            status, text = payments.http_json(
                spec.get("base_url"), body, timeout=90,
                headers={"Authorization": "Bearer " + key},
                proxy_mode=self.proxy_mode,
            )
            if status != 200:
                errors.append("%s: HTTP %s %s" % (name, status, str(text)[:160]))
                continue
            try:
                data = json.loads(text)
                content = data["choices"][0]["message"]["content"]
            except Exception:  # noqa: BLE001
                errors.append("%s: bad response" % name)
                continue
            usage = data.get("usage") or {}
            pin = int(usage.get("prompt_tokens") or 0)
            pout = int(usage.get("completion_tokens") or 0)
            cost = int(round(
                pin / 1000.0 * float(spec.get("cost_per_1k_input") or 0) * MICRO +
                pout / 1000.0 * float(spec.get("cost_per_1k_output") or 0) * MICRO))
            return self._text({
                "provider": name,
                "model": spec.get("model"),
                "content": content,
                "usage": {"prompt_tokens": pin, "completion_tokens": pout},
            }), cost, None
        raise ToolError(
            "upstream_unavailable",
            "没有可用上游模型（本次未扣费）。详情：" + " | ".join(errors[:4]),
            503,
        )

    def h_agent_consume(self, args, ctx):
        units = max(int(args.get("units") or 1), 1)
        agent = args.get("agent") or "default"
        tenant_id = ctx.get("tenant_id")
        tid = str(uuid.uuid4())
        token = self.signer.sign({
            "typ": "entitlement", "agent": agent, "units": units,
            "tenant": tenant_id, "iss": int(time.time()), "jti": tid,
        })
        self.store.save_license(tid, tenant_id, agent, "entitlement", None, token,
                                {"units": units})
        return self._text({
            "agent": agent, "units": units, "entitlement_id": tid,
            "token": token,
            "verify": "HMAC-SHA256，密钥见服务端 data/license_secret.txt（对外发布时用公钥方案）",
            "expires_hint": "令牌本身不含过期，业务侧按 entitlement_id 记录消费",
        }), None, None

    def h_mcp_skill_invoke(self, args, ctx):
        skill = args.get("skill")
        spec = self.skills.get(skill or "")
        if not spec:
            raise ToolError("unknown_skill", "未登记的技能：%s（可用：%s）"
                            % (skill, ", ".join(sorted(self.skills)[:10]) or "无"), 404)
        url = spec.get("url")
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": spec.get("tool", skill), "arguments": args.get("args") or {}}}
        headers = spec.get("headers") or {}
        status, text = payments.http_json(url, payload, timeout=60, headers=headers,
                                         proxy_mode=self.proxy_mode)
        if status != 200:
            raise ToolError("upstream_failed", "上游技能返回 %s：%s" % (status, str(text)[:200]), 502)
        return self._text({"skill": skill, "upstream": json.loads(text)}), None, None

    def h_prompt_pack_get(self, args, ctx):
        sku = args.get("sku")
        item = self.inventory.get(sku or "")
        if not item or item.get("kind") != "prompt_pack":
            raise ToolError("sku_not_found", "没有这个提示词包：%s" % sku, 404)
        lic = "LIC-" + uuid.uuid4().hex[:10].upper()
        token = self.signer.sign({"typ": "license", "sku": sku, "lic": lic,
                                  "tenant": ctx.get("tenant_id"), "ts": int(time.time())})
        self.store.save_license(lic, ctx.get("tenant_id"), sku, "prompt_pack", None, token)
        return self._text({
            "sku": sku, "title": item.get("title"), "license": item.get("license"),
            "license_id": lic, "license_token": token,
            "pack": item.get("body"), "compliance_note": item.get("compliance_note"),
        }), None, int(item.get("price_micro") or 0) or None

    def h_code_template_get(self, args, ctx):
        sku = args.get("sku")
        item = self.inventory.get(sku or "")
        if not item or item.get("kind") != "code_template":
            raise ToolError("sku_not_found", "没有这个模板：%s" % sku, 404)
        lic = "LIC-" + uuid.uuid4().hex[:10].upper()
        token = self.signer.sign({"typ": "license", "sku": sku, "lic": lic,
                                  "tenant": ctx.get("tenant_id"), "ts": int(time.time())})
        self.store.save_license(lic, ctx.get("tenant_id"), sku, "code_template", None, token)
        return self._text({
            "sku": sku, "title": item.get("title"), "license_id": lic,
            "license_token": token, "files": item.get("files"),
            "usage_note": "文件内容即完整源码，按 files 的 key 落盘即可运行。",
        }), None, int(item.get("price_micro") or 0) or None

    def h_asset_license_issue(self, args, ctx):
        sku = args.get("sku")
        buyer = args.get("buyer")
        if not sku or not buyer:
            raise ToolError("bad_request", "sku 与 buyer 必填")
        days = int(args.get("days") or 365)
        lic = "LIC-" + uuid.uuid4().hex[:10].upper()
        payload = {
            "typ": "commercial_license", "lic": lic, "sku": sku, "buyer": buyer,
            "scope": args.get("scope") or "digital-ads", "days": days,
            "issued_at": int(time.time()), "expire_at": int(time.time()) + days * 86400,
            "issuer": "pay-per-call-gateway",
        }
        token = self.signer.sign(payload)
        self.store.save_license(lic, ctx.get("tenant_id"), sku, "commercial", None, token, payload)
        return self._text({
            "license_id": lic, "certificate": payload, "signature_token": token,
            "how_to_verify": "HMAC-SHA256(sort_keys JSON) 十六进制签名 + '.' + base64url(JSON)。"
                             "对外商用建议改为非对称签名，把公钥交给买方离线验。",
        }), None, None

    def h_plugin_license_activate(self, args, ctx):
        sku = args.get("sku")
        machine = args.get("machine")
        seats = max(int(args.get("seats") or 1), 1)
        if not sku or not machine:
            raise ToolError("bad_request", "sku 与 machine 必填")
        lid = "ACT-" + uuid.uuid4().hex[:10].upper()
        token = self.signer.sign({
            "typ": "plugin_activation", "sku": sku, "machine": machine,
            "seats": seats, "tid": ctx.get("tenant_id"), "jti": lid, "ts": int(time.time()),
        })
        self.store.save_license(lid, ctx.get("tenant_id"), sku, "plugin", machine, token,
                                {"seats": seats})
        return self._text({
            "activation_id": lid, "sku": sku, "machine": machine, "seats": seats,
            "activation_token": token,
            "runtime_check": "插件启动时校验 token 内的 machine 与本地指纹一致，不一致拒绝运行。",
        }), None, None

    def h_compute_quote(self, args, ctx):
        gpu = (args.get("gpu") or "").lower()
        hours = float(args.get("hours") or 1)
        region = args.get("region") or "cn-north"
        table = {
            "4090": {"cost": 1.6, "sell": 2.6},
            "a100-80g": {"cost": 8.5, "sell": 12.8},
            "h20": {"cost": 6.2, "sell": 9.4},
            "3090": {"cost": 0.9, "sell": 1.6},
            "l40s": {"cost": 4.8, "sell": 7.2},
        }
        key = None
        for k in table:
            if k in gpu:
                key = k
                break
        if key is None:
            raise ToolError("gpu_not_found", "不支持的卡型：%s（可选：%s）"
                            % (gpu, ", ".join(table)), 404)
        t = table[key]
        cost = t["cost"] * hours
        sell = t["sell"] * hours
        return self._text({
            "gpu": key, "region": region, "hours": hours,
            "list_price_usd": round(sell, 4),
            "volume_tiers": [
                {"hours": ">=100", "discount": "5%"},
                {"hours": ">=500", "discount": "10%"},
                {"hours": ">=2000", "discount": "15%"},
            ],
            "delivery": "容器实例，按小时计费，支持镜像预热",
        }), to_micro(cost), None

    def h_kb_answer(self, args, ctx):
        q = (args.get("question") or "").strip()
        if not q:
            raise ToolError("bad_request", "question 必填")
        top_k = max(1, min(int(args.get("top_k") or 3), 5))
        d = os.path.join(self.base, "inventory", "kb")
        docs = []
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                if not name.endswith(".md"):
                    continue
                with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                    docs.append((name, f.read()))
        if not docs:
            raise ToolError("kb_empty", "知识库为空，未扣费", 503)
        terms = [t for t in re.split(r"[\s,，。、?？!！;；:：]+", q) if len(t) >= 2]
        scored = []
        for name, text in docs:
            paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
            for p in paras:
                score = sum(p.count(t) for t in terms)
                if score:
                    scored.append((score, name, p))
        scored.sort(key=lambda x: -x[0])
        picked = scored[:top_k]
        if not picked:
            return self._text({
                "question": q,
                "answer": "知识库中没有检索到相关段落。可换个说法，或把问题发到社群由人工跟进。",
                "citations": [],
            }), None, None
        bullets = "\n\n".join("- 【%s】%s" % (n, p.replace("\n", " ")[:400]) for _, n, p in picked)
        return self._text({
            "question": q,
            "answer": "命中的知识库段落如下，按相关度排序：\n\n" + bullets,
            "citations": [{"source": n, "score": s} for s, n, _ in picked],
            "note": "答案由检索拼接，未经大模型改写，可直接引用原文。",
        }), None, None

    def h_content_brief(self, args, ctx):
        platform = (args.get("platform") or "douyin").lower()
        product = args.get("product") or ""
        seconds = max(15, min(int(args.get("seconds") or 45), 600))
        if not product:
            raise ToolError("bad_request", "product 必填")
        hooks = {
            "douyin": ["前三秒抛冲突：'别再花冤枉钱了'", "痛点直给 + 结果前置"],
            "xhs": ["第一句就交代身份与场景", "用具体数字替代形容词"],
            "bilibili": ["开头给结论，中间给推导", "允许 15 秒铺垫但必须有信息增量"],
            "kuaishou": ["真人出镜，直接讲价", "口语化，不用书面词"],
            "shipinhao": ["开头点明受益人群", "结尾给一个可执行动作"],
        }.get(platform, ["问题-方案-证据-行动"])
        script = [
            {"t": "0-3s", "purpose": "钩子",
             "line": "%s 我用它解决了最烦的那一步。" % product[:40],
             "shot": "特写使用瞬间，无字幕堆叠"},
            {"t": "3-%ds" % max(9, seconds // 3), "purpose": "问题具象化",
             "line": "具体说三个场景：早上赶时间、临时改需求、复盘找不到记录。",
             "shot": "分屏对比，左失败右成功"},
            {"t": "%d-%ds" % (max(9, seconds // 3), int(seconds * 0.7)), "purpose": "方案演示",
             "line": "操作只讲两步，别念说明书。",
             "shot": "屏幕录制 + 手部特写"},
            {"t": "%d-%ds" % (int(seconds * 0.7), seconds), "purpose": "行动指令",
             "line": "想要同款的，看评论区置顶。",
             "shot": "定格产品 + 价格信息卡"},
        ]
        return self._text({
            "platform": platform, "product": product, "seconds": seconds,
            "audience": args.get("audience") or "未指定",
            "hook_options": hooks,
            "shotlist": script,
            "checklist": [
                "不出现极限词（最/第一/国家级）",
                "功效类不做疗效承诺",
                "背景音乐与素材已获商用授权",
                "AI 生成画面按规定标识",
            ],
            "delivery": "把 shotlist 直接发给拍摄/剪辑即可执行。",
        }), None, None

    def h_custom_quote(self, args, ctx):
        kind = (args.get("kind") or "other").lower()
        scope = args.get("scope") or ""
        days = int(args.get("deadline_days") or 30)
        if not scope:
            raise ToolError("bad_request", "scope 必填")
        base = {"web": (3000, 12000), "app": (8000, 40000), "miniprogram": (4000, 20000),
                "agent": (2000, 15000), "data": (1500, 8000), "design": (800, 6000),
                "other": (1000, 8000)}
        lo, hi = base.get(kind, base["other"])
        size = len(scope)
        mult = 1.0 + min(size / 600.0, 1.2)
        rush = 1.25 if days < 14 else 1.0
        lead = self.store.save_lead(ctx.get("tenant_id"), kind, args.get("contact"), args)
        return self._text({
            "lead_id": lead,
            "kind": kind,
            "quote_range_usd": [int(lo * mult * rush), int(hi * mult * rush)],
            "eta_days": max(days, 7),
            "next_step": "已落线索库，商务会在 1 个工作日内联系确认范围后给固定报价。",
            "note": "此为区间预报价，最终以书面 SOW 为准；未签约前不产生任何费用。",
        }), None, None

    def h_policy_check(self, args, ctx):
        text = args.get("text") or ""
        if not text:
            raise ToolError("bad_request", "text 必填")
        hits = [w for w in BANNED if w in text]
        infringe = [{"word": w, "risk": r} for w, r in INFRINGE_HINTS if w in text]
        fixes = []
        for w in hits:
            fixes.append({"word": w, "suggest": "改为客观描述，如'销量领先'→'销量较好'"})
        risk = "high" if hits or infringe else ("medium" if "免费" in text and "领取" in text else "low")
        return self._text({
            "risk_level": risk,
            "banned_words": hits,
            "infringement_hints": infringe,
            "suggestions": fixes,
            "note": "规则库覆盖广告法极限词与常见平台风控词，不构成法律意见。",
        }), None, None

    def h_asset_catalog(self, args, ctx):
        items = []
        for sku, item in sorted(self.inventory.items()):
            items.append({
                "sku": sku, "kind": item.get("kind"), "title": item.get("title"),
                "price_usd": usd(int(item.get("price_micro") or 0)),
                "tags": item.get("tags") or [],
                "buy_with": "prompt.pack.get" if item.get("kind") == "prompt_pack"
                            else "code.template.get",
            })
        return self._text({"count": len(items), "items": items,
                           "note": "用 buy_with 指定的工具 + sku 即可购买并当场交付。"}), None, None

    def h_pricing_list(self, args, ctx):
        out = []
        for name, spec in sorted(self.pricing.tools().items()):
            price, cost = self.pricing.quote(name)
            out.append({
                "tool": name, "revenue_line": spec.get("revenue_line"),
                "title": spec.get("title"), "price_usd": usd(price),
                "free": price == 0, "desc": spec.get("desc"),
            })
        return self._text({
            "currency": "USD", "billing": "按次计费，失败不扣费",
            "rails": ["预充值钱包(API Key)", "x402 链上按次", "订阅包次"],
            "tools": out,
        }), None, None

    def h_usage_report(self, args, ctx):
        tid = ctx.get("tenant_id")
        days = max(1, min(int(args.get("days") or 30), 365))
        data = self.store.tenant_usage(tid, days)
        t = self.store.tenant(tid) or {}
        data["balance_usd"] = usd(t.get("balance_micro") or 0)
        data["plan"] = t.get("plan")
        if t.get("plan") == "subscription":
            remain = (t.get("quota_micro") or 0) - (t.get("quota_used_micro") or 0)
            data["quota_remain_usd"] = usd(max(remain, 0))
        return self._text(data), None, None
