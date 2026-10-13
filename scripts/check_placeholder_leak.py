# -*- coding: utf-8 -*-
"""scripts/check_placeholder_leak.py —— 占位符泄漏守卫（fail-closed）

# 功能名：占位符泄漏守卫——扫文本产物里**未替换的构造占位符**（命中即退出码 1）
# 生效条件：凡交付 .md/.py/.json/.html 产物前；凡把长文本经占位符构造再落盘时；
#           凡接入 pre-commit / resolve 链时。触发语=占位符泄漏、落盘前自检。
# 子功能：① 扫给定根（判据面**默认限定 git 追踪面**，见 issue #200）② 命中即逐处点名
#         （文件:行:列 + 上下文）③ 退出码 fail-closed（命中=1）
#         ④ **不自指**（占位符由字符拼出，源码里不出现字面量）
# 执行：python -X utf8 scripts/check_placeholder_leak.py [--root DIR ...] [--exclude PATH ...]
#       [--include-untracked] [--list-surface]
# 验证方式：含泄漏的临时树上跑 ⇒ 退出码 1 且逐处点名；本仓跑 ⇒ 退出码 0 并打印扫描面与排除项
# 不适用条件：不适用于 _protected_history 等**审计快照**（默认排除——记录的是当时真实状态，
#            改了毁审计）；不适用于 hive/jobs 运行态记录与提交信息里**引用**该字样处
#            （那是引用不是泄漏）；不适用于**未追踪**的复审/临时文档（见 issue #200）。
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
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

#: 判据面默认**限定 git 追踪面**（issue #200）。
#:
#: 根因：本守卫原本 os.walk 整个文件系统，于是**任何「描述该守卫自身」的文档都会
#: 让它转红** —— 因为这类文档必须**引用占位符字面量**作为示例（自我指涉不可避免，
#: 不是文档写错）。实测：把一个 42 份复审报告的**未追踪**目录放进仓库树 ⇒ 4 处命中，
#: 全部来自报告本身；移出后立即恢复 0 命中。
#:
#: 修法：判据面 = git ls-files 的文本件。未追踪的复审/临时文档不再污染判据；
#: 而**追踪进仓库的文档**若引用占位符仍会被抓 —— 那是应该抓的。
#: 需要「新写但还没 add 的发布件」也入判据时，显式加 --include-untracked。
TRACKED_BY_DEFAULT = True


def _git_tracked(root: str):
    """git 追踪面（相对 root 的路径列表）；**不可用返回 None**（调用方须响亮回退）。"""
    try:
        chk = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                             cwd=root, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
        if chk.returncode != 0 or (chk.stdout or "").strip() != "true":
            return None
        p = subprocess.run(["git", "ls-files", "-z"], cwd=root,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
    except Exception:                                          # noqa: BLE001
        return None
    if p.returncode != 0:
        return None
    return [x for x in (p.stdout or "").split("\0") if x]


def _excluded(rel: str, exclude) -> bool:
    rel = rel.replace(os.sep, "/")
    for ex in exclude:
        ex = ex.replace(os.sep, "/").strip("/")
        if rel == ex or rel.startswith(ex + "/") or ("/" + ex + "/") in ("/" + rel):
            return True
    return False


def _scan_file(path: str, rel: str):
    """扫单个文件 → [(相对路径, 行号, 列号, 占位符, 该行前 120 字)]。"""
    hits = []
    try:
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            for ln, line in enumerate(fh, 1):
                for ph in PLACEHOLDERS:
                    col = line.find(ph)
                    if col >= 0:
                        hits.append((rel.replace(os.sep, "/"), ln, col + 1, ph,
                                     line.strip()[:120]))
    except Exception:                                          # noqa: BLE001
        pass
    return hits


def scan(root: str, exclude, tracked: bool = True):
    """扫 root → (hits, 判据面口径)。判据面 = tracked / filesystem。"""
    hits = []
    root = os.path.abspath(root)
    files = _git_tracked(root) if tracked else None
    if files is not None:
        for rel in files:
            rel = rel.replace(os.sep, "/")
            if not rel.endswith(SUFFIXES):
                continue
            if _excluded(rel, exclude):
                continue
            hits.extend(_scan_file(os.path.join(root, rel), rel))
        return hits, "tracked"
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
            hits.extend(_scan_file(p, rel))
    return hits, "filesystem"


def main(argv=None):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="占位符泄漏守卫（命中即退出码 1）")
    ap.add_argument("--root", action="append", default=None, help="扫描根（可多次；缺省=本仓根）")
    ap.add_argument("--exclude", action="append", default=None, help="额外排除的路径前缀（可多次）")
    ap.add_argument("--include-untracked", action="store_true",
                    help="判据面含未追踪文件（旧行为；issue #200 的缺陷形态）")
    ap.add_argument("--list-surface", action="store_true",
                    help="只打印判据面口径与文件数，不判红（诊断用）")
    args = ap.parse_args(argv)

    roots = args.root or [here]
    exclude = set(DEFAULT_EXCLUDE) | set(args.exclude or ())
    want_tracked = TRACKED_BY_DEFAULT and not args.include_untracked
    total = 0
    surfaces = []
    for r in roots:
        hits, surface = scan(r, exclude, tracked=want_tracked)
        surfaces.append((r, surface))
        total += len(hits)
        if hits:
            print("!! 占位符泄漏 %d 处（%s，判据面=%s）" % (len(hits), r, surface))
            for rel, ln, col, ph, ctx in hits:
                print("   %s:%d:%d  %s   | %s" % (rel, ln, col, ph, ctx))
    if args.list_surface:
        for r, surface in surfaces:
            print("判据面：%s -> %s" % (r, surface))
        return 0
    if total:
        print("=> 共 %d 处未替换占位符：落盘前必须全部替换（退出码 1）" % total)
        return 1
    print("占位符守卫：%d 个根零命中 ✓" % len(roots))
    for r, surface in surfaces:
        print("   判据面：%s" % surface)
        if surface == "filesystem":
            print("   !! 回退告警：%s 不在 git 工作树内（或 git 不可用），"
                  "本次按**全盘 walk** 判据 —— 未追踪文档会污染判据（issue #200）" % r)
    print("   排除前缀：%s" % ",".join(sorted(exclude)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
