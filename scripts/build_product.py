#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 inventory/ 里的 SKU 打成一份"买家拿到就能用"的商品包。

用法：
    python scripts/build_product.py [输出目录名]

产出：
    products/<name>/           目录（可直接上传到知识星球/小报童/Gumroad）
    products/<name>.zip        压缩包（买家下载的就是它）

设计原则：
- 提示词包输出成人类可读的 Markdown（买家要能直接复制去用），
  同时保留原始 JSON 给会用 API 的人。
- 源码模板按真实目录结构还原，不是塞在一个大 JSON 里。
- 每个文件都带 License 边界说明，避免买家误以为可以转售源码。
"""
import json
import os
import shutil
import sys
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INV = os.path.join(BASE, "inventory")
OUT_ROOT = os.path.join(BASE, "products")

PROMPT_SECTIONS = [
    ("system", "System 提示词"),
    ("user_template", "User 模板（把 {{变量}} 换成你的内容）"),
    ("rules", "输出规则"),
    ("example_output", "示例输出"),
]


def load_all():
    items = []
    for sub in ("prompts", "templates"):
        d = os.path.join(INV, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(d, fn)
            with open(p, encoding="utf-8") as f:
                items.append(json.load(f))
    return items


def render_prompt_md(item):
    b = item.get("body") or {}
    lines = ["# %s" % item["title"], "", item.get("desc", ""), ""]
    lines.append("> SKU：`%s` · 授权：%s · 标签：%s"
                 % (item.get("sku"), item.get("license"),
                    "、".join(item.get("tags") or [])))
    lines.append("")
    for key, label in PROMPT_SECTIONS:
        v = b.get(key)
        if not v:
            continue
        lines.append("## %s" % label)
        lines.append("")
        if isinstance(v, list):
            for x in v:
                lines.append("- %s" % x)
        else:
            lines.append(str(v))
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 怎么用")
    lines.append("")
    lines.append("1. 把「System 提示词」贴到对话的系统指令 / 自定义指令里。")
    lines.append("2. 每次使用时，把「User 模板」复制出来，替换 `{{...}}` 占位符。")
    lines.append("3. 模型输出不符合预期时，对照「输出规则」逐条补充约束。")
    lines.append("")
    return "\n".join(lines)


def build(name):
    out = os.path.join(OUT_ROOT, name)
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)

    items = load_all()
    if not items:
        raise SystemExit("inventory 里没有可打包的 SKU")

    prompt_dir = os.path.join(out, "prompts")
    tpl_dir = os.path.join(out, "templates")
    os.makedirs(prompt_dir)
    os.makedirs(tpl_dir)

    index = []
    for it in items:
        sku = it["sku"]
        kind = it.get("kind")
        if kind == "prompt_pack":
            md = render_prompt_md(it)
            with open(os.path.join(prompt_dir, sku + ".md"), "w",
                      encoding="utf-8") as f:
                f.write(md)
            with open(os.path.join(prompt_dir, sku + ".json"), "w",
                      encoding="utf-8") as f:
                json.dump(it, f, ensure_ascii=False, indent=2)
            index.append((sku, "提示词包", it["title"],
                          "%s.md / %s.json" % (sku, sku)))
        elif kind == "code_template":
            sub = os.path.join(tpl_dir, sku)
            os.makedirs(sub, exist_ok=True)
            files = it.get("files") or {}
            for fname, content in files.items():
                fp = os.path.join(sub, fname.replace("/", os.sep))
                os.makedirs(os.path.dirname(fp) or sub, exist_ok=True)
                with open(fp, "w", encoding="utf-8", newline="\n") as f:
                    f.write(content)
            with open(os.path.join(sub, "SKU.json"), "w", encoding="utf-8") as f:
                json.dump({k: v for k, v in it.items() if k != "files"}, f,
                          ensure_ascii=False, indent=2)
            index.append((sku, "源码模板", it["title"],
                          "templates/%s/" % sku))

    # ---- 主说明 ----
    rows = "\n".join("| `%s` | %s | %s | `%s` |" % r for r in index)
    readme = """# MeterPay 起步套件 v1

给独立开发者/AI 应用方的一份可直接用的资产包：**{n} 个提示词包 + 源码模板**，
全部零第三方依赖，拿到即可用。

## 包含什么

| SKU | 类型 | 内容 | 位置 |
|---|---|---|---|
{rows}

## 怎么用

**提示词包**：打开 `prompts/<SKU>.md`，里面有 System 提示词、User 模板、输出规则、
示例输出四段。把 System 贴进系统指令，把 User 模板里的 `{{变量}}` 换成你的内容即可。

**源码模板**：`templates/<SKU>/` 下是完整可运行的文件。

- `TPL-MCP-PY`：`python mcp_server.py` 起 HTTP 模式；`python mcp_server.py --stdio`
  给 Claude Desktop / Cursor 用。加一个工具只要改 `TOOLS` 和 `call_tool` 两处。
- `TPL-X402-PAID`：按次收费接口骨架，含 verify → 执行 → settle 流程与对账。

## 授权

{license_note}

## 不支持退款的情形

- 已下载全部文件后以"不想要了"为由申请退款（数字商品已被复制，无法回收）
- 因自身环境（Python 版本、网络、平台账号）导致跑不通，且拒绝按 README 排查

**支持的情形**：文件损坏、内容与说明不符、代码在干净的 Python 3.9+ 环境跑不起来
（附报错信息），一律补发或全额退款。

---
生成时间：{ts}
""".format(n=len(index), rows=rows, license_note=LICENSE_NOTE,
           ts=__import__("time").strftime("%Y-%m-%d %H:%M"))

    with open(os.path.join(out, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme)
    with open(os.path.join(out, "LICENSE.md"), "w", encoding="utf-8") as f:
        f.write("# 授权条款\n\n" + LICENSE_NOTE + "\n")

    # ---- zip ----
    zip_path = os.path.join(OUT_ROOT, name + ".zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(out):
            for fn in files:
                fp = os.path.join(root, fn)
                arc = os.path.join(name, os.path.relpath(fp, out))
                z.write(fp, arc)

    total = sum(len(fs) for _, _, fs in os.walk(out))
    size = os.path.getsize(zip_path)
    print("商品包: %s" % out)
    print("压缩包: %s (%.1f KB)" % (zip_path, size / 1024.0))
    print("文件数: %d" % total)
    for sku, kind, title, loc in index:
        print("  - [%s] %s  %s" % (kind, sku, loc))
    return zip_path


LICENSE_NOTE = """**允许**：
- 用于你自己的项目、你客户的项目（单席位 = 一个开发者使用）
- 修改后用于商业产品，无需额外付费
- 交付给客户时作为你解决方案的一部分

**不允许**：
- 原样或改头换面后作为"模板包/提示词包"再次出售或免费分发
- 上传到任何公开代码仓库（含 GitHub 公开库、Gitee、npm、PyPI）
- 用于训练或微调模型

**边界**：买的是"使用权"，不是"版权"。版权归作者所有。
每个席位对应一位开发者；团队使用请按人数购买。
"""


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "meterpay-starter-kit-v1"
    build(name)
