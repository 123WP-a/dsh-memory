# -*- coding: utf-8 -*-
"""scripts/check_placeholder_leak.py —— 占位符泄漏守卫（fail-closed）

# 功能名：占位符泄漏守卫——扫文本产物里**未替换的构造占位符**（命中即退出码 1）
# 生效条件：凡交付 .md/.py/.json/.html 产物前；凡把长文本经占位符构造再落盘时；
#           凡接入 pre-commit / resolve 链时。触发语=占位符泄漏、落盘前自检。
# 子功能：① 递归扫给定根（按**路径前缀**排除，逐条有理由）② 命中即逐处点名（文件:行:列 + 上下文）
#         ③ 退出码 fail-closed（命中=1）④ **不自指**（占位符由字符拼出，源码里不出现字面量）
# 执行：python -X utf8 scripts/check_placeholder_leak.py [--root DIR ...] [--exclude PATH ...]
# 验证方式：含泄漏的临时树上跑 ⇒ 退出码 1 且逐处点名；本仓跑 ⇒ 退出码 0 并打印扫描根与排除项
# 不适用条件：不适用于 _protected_history 等**审计快照**（默认排除——记录的是当时真实状态，改了毁审计）；
#            不适用于 hive/jobs 运行态记录与提交信息里**引用**该字样处（那是引用不是泄漏）。
"""

from __future__ import annotations

import argparse
import io
import os
import sys

#: 节标记（用字符拼出，**避免源码里出现字面占位符** ⇒ 守卫不自指）。
_SEC = "\u00a7"

#: 需要守的占位符（构造长文本时的临时记号；落盘前必须全部替换）。
PLACEHOLDERS = (_SEC + "BT" + _SEC, _SEC + "NL" + _SEC)

#: 默认不扫的**路径前缀**（相对根）——**逐条有理由**，不是「看不见就算了」。
DEFAULT_EXCLUDE = (
    ".git", "node_modules", "target", "__pycache__", "dist", "build", ".venv",
    ".tmp",
    "hive/jobs",           # 蜂巢运行态记录：会**引用**占位符字样（如提交信息原文）
    "_protected_history",  # 审计快照：记录当时真实状态，改了反而毁审计
    "trash",               # 软删区
    "_md_cg_p27",          # 非活动实验库（历史遗留，待使用者裁定）
)

#: 只扫这些后缀（文本产物）。
SUFFIXES = (".md", ".py", ".json", ".html", ".yml", ".yaml", ".txt", ".ts", ".js")


def _excluded(rel: str, exclude) -> bool:
    rel = rel.replace(os.sep, "/")
    for ex in exclude:
        ex = ex.replace(os.sep, "/").strip("/")
        if rel == ex or rel.startswith(ex + "/") or ("/" + ex + "/") in ("/" + rel):
            return True
    return False


def scan(root: str, exclude):
    """扫 root → [(相对路径, 行号, 列号, 占位符, 该行前 120 字)]。"""
    hits = []
    root = os.path.abspath(root)
    for dp, dn, fn in os.walk(root):
        rel_dir = os.path.relpath(dp, root)
        dn[:] = [d for d in dn if not _excluded(os.path.join(rel_dir, d), exclude)]
        if _excluded(rel_dir, exclude):
            continue
        for f in fn:
            if not f.endswith(SUFFIXES):
                continue
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, root)
            if _excluded(rel, exclude):
                continue
            try:
                with io.open(p, encoding="utf-8", errors="replace") as fh:
                    for ln, line in enumerate(fh, 1):
                        for ph in PLACEHOLDERS:
                            col = line.find(ph)
                            if col >= 0:
                                hits.append((rel.replace(os.sep, "/"), ln, col + 1, ph,
                                             line.strip()[:120]))
            except Exception:
                continue
    return hits


def main(argv=None):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="占位符泄漏守卫（命中即退出码 1）")
    ap.add_argument("--root", action="append", default=None, help="扫描根（可多次；缺省=本仓根）")
    ap.add_argument("--exclude", action="append", default=None, help="额外排除的路径前缀（可多次）")
    args = ap.parse_args(argv)

    roots = args.root or [here]
    exclude = set(DEFAULT_EXCLUDE) | set(args.exclude or ())
    total = 0
    for r in roots:
        hits = scan(r, exclude)
        total += len(hits)
        if hits:
            print("!! 占位符泄漏 %d 处（%s）" % (len(hits), r))
            for rel, ln, col, ph, ctx in hits:
                print("   %s:%d:%d  %s   | %s" % (rel, ln, col, ph, ctx))
    if total:
        print("=> 共 %d 处未替换占位符：落盘前必须全部替换（退出码 1）" % total)
        return 1
    print("占位符守卫：%d 个根零命中 ✓" % len(roots))
    print("   排除前缀：%s" % ",".join(sorted(exclude)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
