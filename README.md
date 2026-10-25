# Pay-Per-Call Gateway · 按次计费的 AI 能力变现中台

一个零依赖的 Python 服务，把「AI 能力」变成「每次调用都收一次钱」的可售卖商品。
同时对外提供 **MCP 端点**、**OpenAPI（给平台插件导入）**、**落地页**、**运营后台**。

核心不是"做个接口"，而是**三条腿的收款结构 + 一套可信台账**：

| 通道 | 面向谁 | 怎么收 |
|---|---|---|
| 预充值钱包 | 扣子/钉钉/抖音/小程序等平台插件（只能传 header） | 充余额，每次调用扣一次 |
| x402 链上按次 | Claude/Cursor 等 Agent 客户端（没有你的账号） | 带 `X-PAYMENT` 头，验签通过才干活 |
| 订阅包次 | 已购套餐的客户 | 扣套餐余量，超额转钱包 |
| 平台订单回调 | 微信支付/支付宝/Stripe | HMAC 验签后入账 |

---

## 三条硬规矩（这决定的不是能不能跑，而是会不会亏钱）

1. **失败不扣费**。上游挂了、SKU 不存在、参数不对，一律 0 收入，只记失败台账。
   卖空的调用比少收一笔更贵——退款和投诉成本远高于单价。
2. **本地永不判定付款有效**。签名真伪、余额、nonce 是否已消费，只有 facilitator 知道。
   本地只校「结构完整 / 收款地址一致 / 金额够 / 没过期」，其余全部交给 facilitator。
3. **先干活，成功才结算**。x402 做不到"先收钱再干活"——链上交易不可逆，
   业务失败就退不回去。顺序必须是 verify → 执行 → settle。

---

## 快速开始

```bash
cd D:/workbuddy-projects/pay-per-call-gateway
python app.py
```

启动后：

| 入口 | 地址 |
|---|---|
| 落地页（引流转化） | http://127.0.0.1:8300/ |
| MCP 端点 | POST http://127.0.0.1:8300/mcp |
| REST 包装 | POST http://127.0.0.1:8300/x/llm.chat |
| OpenAPI（平台导入） | http://127.0.0.1:8300/openapi.json |
| 给 Agent 读的说明 | http://127.0.0.1:8300/llms.txt |
| 运营后台 | http://127.0.0.1:8300/admin |

首次启动会在 `data/` 下生成：

- `admin_token.txt` —— 运营后台令牌
- `gateway-bootstrap.txt` —— 两个演示租户的 API Key（$5 钱包 / $20 套餐）
- `license_secret.txt` —— 授权凭证签名密钥，**务必备份，丢了老凭证就验不过**
- `gateway.db` —— sqlite，全部账在里边，单文件即可备份/迁移

---

## 接入方式

### 1. MCP 客户端（Claude Desktop / Cursor / Cline）

```json
{
  "mcpServers": {
    "pay-per-call": {
      "command": "node",
      "args": ["D:/workbuddy-projects/pay-per-call-gateway/bin/ppc-mcp.js"],
      "env": {
        "PPC_GATEWAY": "http://127.0.0.1:8300",
        "PPC_API_KEY": "ppc_live_xxx"
      }
    }
  }
}
```

也可以直接用 HTTP 传输，不用 node：

```json
{ "mcpServers": { "pay-per-call": {
  "type": "http", "url": "http://127.0.0.1:8300/mcp",
  "headers": { "X-API-Key": "ppc_live_xxx" } } } }
```

### 2. 平台插件（扣子/钉钉/抖音/支付宝）

用 `platforms/openapi-3.0.yaml`（已按 OpenAPI 3.0 写，这些平台不认 3.1），
把 `servers.url` 改成你的域名后导入。每个 `/x/<tool>` 就是一个插件工具。

### 3. x402 按次付款（无需注册）

```bash
curl -s -X POST http://127.0.0.1:8300/mcp \
  -H 'Content-Type: application/json' \
  -H 'X-PAYMENT: <base64 的 x402 payload>' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"prompt.pack.get","arguments":{"sku":"PROMPT-JD-INTERVIEW"}}}'
```

不带 `X-PAYMENT` 会返回 **HTTP 402**，响应体里有 `accepts`（标准 x402 要求）
和 `payment_required`（金额与充值入口）。

---

## 工具清单（= 收款线）

| 工具 | 收入线 | USD/次 |
|---|---|---|
| `llm.chat` | AI 接口按调用计费 | 0.0015 |
| `agent.consume` | 智能体订阅包次 | 0.004 |
| `mcp.skill.invoke` | MCP 技能代理 | 0.004 |
| `prompt.pack.get` | 提示词包 | 0.02 起 |
| `code.template.get` | 源码模板 | 0.09 起 |
| `asset.license.issue` | 商用素材授权 | 0.03 |
| `plugin.license.activate` | 插件授权 | 0.05 |
| `compute.quote` | 算力分销 | 0.002 |
| `kb.answer` | 知识付费/社群 | 0.01 |
| `content.brief` | 内容商单 | 0.005 |
| `custom.quote` | 定制外包（收线索） | 免费 |
| `policy.check` | 合规预检 | 免费 |
| `asset.catalog` `pricing.list` `usage.report` | 货架/查价/对账 | 免费 |

**免费工具不是慈善**：Agent 能自己查价、自己核对账单，你的客服工单会少一大半。

---

## 上新品 / 调价

- **调价**：后台「调价」输入 USD 单价保存，立即生效（`pricing.json` 热重载，不重启）。
- **上新品（交付类）**：往 `inventory/prompts/` 或 `inventory/templates/` 丢一个 JSON
  （结构见现有文件），然后 `POST /admin/api/reload`，`asset.catalog` 立刻能看到。
- **上新品（能力类）**：在 `config/pricing.json` 加一条 → 在 `tools.py` 加一个 `h_xxx` 方法
  → 在 `TOOL_SCHEMAS` 加 schema。三处，其余全自动（MCP 列表、OpenAPI、价目表、计费）。

---

## 自测

```bash
python scripts/smoke_test.py
```

会起一个**假 facilitator** 和独立实例（独立 db、独立价目表副本），把 48 项全跑一遍：
免费工具不计费、无凭证 402、钱包扣费、失败不扣费、x402 结算拿 txHash、
nonce 重放被拒、收款地址不符被拒、金额不足被拒、乱码付款头不放行、
REST 包装、后台越权、台账毛利、调价生效。

不依赖网络，也不污染正式数据。

---

## 上生产前必改

1. `config/gateway.json` → `public_base_url` 改成真实域名（x402 的 resource 要用它）
2. `x402.pay_to` 改成你的钱包地址 —— **不填就自动关闭 x402**（现在的默认值是零地址）
3. 测试网 `base-sepolia` → 主网 `base` 时，`network` 和 `asset` 必须一起改
4. 每个平台一个租户（`channel` 分清 coze/dingtalk/wechat...），否则对账时分不清钱从哪来
5. `data/license_secret.txt` 备份好；对外卖授权建议改非对称签名，把公钥给买方
6. 上游模型的 key 用环境变量注入（`DEEPSEEK_API_KEY` 等），**不要写进配置文件**

## 上架策略

见 `docs/上架与收费清单.md`：哪些平台真能收到钱、哪些只是引流位、
你那 50 多个平台里哪 16 个其实是**成本项而不是渠道**，以及 30 天上架顺序。

一句话结论：**先把 A 类（自有结算）+ C 类（x402）跑起来，
B 类（平台内结算）排队等资质，D 类（开放平台）只做曝光。**

## 目录

```
app.py                HTTP 服务 + MCP 协议 + 路由 + 后台 API
billing.py            计费内核：租户/钱包/套餐/台账/汇总/签名
payments.py           x402 verify+settle、webhook 验签
tools.py              15 个工具实现（每条收款线一个 handler）
config/pricing.json   价目表（热重载）
config/providers.json 上游模型路由与成本表（压毛利的地方）
config/gateway.json   服务配置
inventory/            可售库存（提示词包/源码模板/知识库）
web/                  落地页 + 运营后台
bin/ppc-mcp.js        npm 插件的 stdio 桥
platforms/            平台导入用 OpenAPI 3.0
scripts/smoke_test.py 48 项端到端自测
```
