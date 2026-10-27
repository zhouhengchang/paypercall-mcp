#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""当 github.com:443 不可达但 api.github.com 可用时，用 Git Data API 提交。

用法：python scripts/gh_api_commit.py <token> <commit message> [file ...]
不给 file 则提交 git status 里所有变更文件。
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

OWNER = "zhouhengchang"
REPO = "paypercall-mcp"
API = "https://api.github.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method, path, token, body=None):
    url = API + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "User-Agent": "ppc-commit",
        "Content-Type": "application/json",
    }
    last = None
    for attempt in range(1, 5):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with OPENER.open(req, timeout=90) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            if e.code >= 500 and attempt < 4:
                last = "HTTP %d" % e.code
                time.sleep(2 * attempt)
                continue
            raise SystemExit("HTTP %d on %s %s\n%s" % (e.code, method, path, raw[:600]))
        except Exception as e:  # 网络抖动：重试
            last = repr(e)
            print("  重试 %d/4 (%s %s): %s" % (attempt, method, path, e))
            time.sleep(2 * attempt)
    raise SystemExit("网络失败，已重试 4 次：%s %s -> %s" % (method, path, last))


def changed_files():
    out = subprocess.run(["git", "status", "--porcelain", "-z"], cwd=ROOT,
                         capture_output=True).stdout.decode("utf-8")
    files = []
    for chunk in out.split("\0"):
        if len(chunk) < 4:
            continue
        p = chunk[3:]
        files.append(p)
    return files


def files_since(rev):
    """用 -z 读文件列表，避免中文路径被 shell 转义拆坏。"""
    out = subprocess.run(["git", "diff", "--name-only", "-z", rev, "HEAD"],
                         cwd=ROOT, capture_output=True).stdout.decode("utf-8")
    return [p for p in out.split("\0") if p]


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    token, msg = sys.argv[1], sys.argv[2]
    rest = sys.argv[3:]
    if rest and rest[0] == "--since":
        files = files_since(rest[1])
    else:
        files = rest or changed_files()
    if not files:
        print("没有要提交的文件")
        return 0

    print("提交 %d 个文件" % len(files))
    blobs = {}
    for rel in files:
        full = os.path.join(ROOT, rel)
        if not os.path.exists(full):
            print("  跳过（已删除）:", rel)
            continue
        with open(full, "rb") as f:
            content = base64.b64encode(f.read()).decode("ascii")
        s, b = call("POST", "/repos/%s/%s/git/blobs" % (OWNER, REPO), token,
                    {"content": content, "encoding": "base64"})
        blobs[rel.replace("\\", "/")] = b["sha"]
        print("  blob", rel, b["sha"][:10])

    s, ref = call("GET", "/repos/%s/%s/git/ref/heads/main" % (OWNER, REPO), token)
    base_commit = ref["object"]["sha"]
    s, commit = call("GET", "/repos/%s/%s/git/commits/%s" % (OWNER, REPO, base_commit), token)
    base_tree = commit["tree"]["sha"]

    entries = [{"path": p, "mode": "100644", "type": "blob", "sha": sh}
               for p, sh in blobs.items()]
    s, tree = call("POST", "/repos/%s/%s/git/trees" % (OWNER, REPO), token,
                   {"base_tree": base_tree, "tree": entries})
    s, newc = call("POST", "/repos/%s/%s/git/commits" % (OWNER, REPO), token,
                   {"message": msg, "tree": tree["sha"], "parents": [base_commit]})
    s, upd = call("PATCH", "/repos/%s/%s/git/refs/heads/main" % (OWNER, REPO), token,
                  {"sha": newc["sha"]})
    print("已提交:", newc["sha"])
    print("https://github.com/%s/%s/commit/%s" % (OWNER, REPO, newc["sha"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
