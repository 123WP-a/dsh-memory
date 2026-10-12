# -*- coding: utf-8 -*-
"""守卫族「执行面」元守卫（issue #129）。

判据：本仓每一个守卫（`scripts/check_*.py`，以及 NAMED_GUARDS 里登记的具名守卫）
都必须在**至少一个执行面**上被引用；否则必须登记在 EXCEPTIONS 并写明理由。

执行面（三者任一即可，机器可判）：
  ① `package.json` 的 `gate` / `prepublishOnly` 脚本串里点名；
  ② `.github/workflows/*.yml` 任一 workflow 里点名；
  ③ **被 `scripts/run_tests.py` 收集的配对测试**：`scripts/test_<stem>.py` 落在它的
     glob 面内（CI 的 python-tests 会跑 run_tests.py）—— 这是本元守卫相对 #129 报告
     的口径修正：**有配对测试即算被执行**，不能只看 gate/CI 字面点名。

退出码（机器可判，**绝不静默退 0**）：
  0  全部守卫有执行面，或已在登记表内且写明理由
  1  存在**未登记的孤儿守卫**（既无执行面、又不在登记表）
  2  环境/解析错误（fail-closed：读不到 package.json / workflows / 收集面）
  3  登记表格式错误（缺理由或缺复核人）

用法：
  python -X utf8 scripts/check_guard_coverage.py                 # 人读覆盖矩阵
  python -X utf8 scripts/check_guard_coverage.py --json          # 机器读
  python -X utf8 scripts/check_guard_coverage.py --selftest      # 合成语料自检
"""

from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 「具名守卫」：不在 scripts/check_*.py 命名面内、但同属须有执行面的判据件。
NAMED_GUARDS = (
    "scripts/cogmap_sync.py",
    "scripts/workspace_index.py",
    "scripts/verify_discipline.py",
    "scripts/judgment_manifest.py",
    "scripts/interop_gate.py",
    "scripts/link_check.py",
)

# run_tests.py 的收集 glob（与之同源；改动 run_tests.py 的收集面须同步此处）。
COLLECT_GLOBS = (
    "scripts/test_*.py",
    "md_cg/test_*.py",
    "hive/test_*.py",
)

# 孤儿登记表：文件名 -> {"reason": 为何暂无执行面, "owner": 复核人, "review": 复核日期}
# 规则：**新增孤儿必须改这里**，从而在 code review 里可见；数量不得增加。
EXCEPTIONS: dict[str, dict[str, str]] = {
    # 2026-10-12 首次登记（issue #129）。规则：**新增孤儿必须改这里**，
    # 使「又多了一个没人跑的守卫」在 code review 里可见；登记数不得增加。
    "check_placeholder_leak.py": {
        "reason": "全仓最贵守卫（实测 36.2s）。接进 CI 会显著拉长 python-tests；"
                  "其判据是「占位符泄漏」，非发布阻塞项。待设计者定：接 gate / 接 CI / 降频独立 workflow。",
        "owner": "设计者（待裁）",
        "review": "2026-10-12",
    },
    "check_registry_tarball.py": {
        "reason": "需先产出 registry tarball 才有判据对象；裸 clone / CI 上无该输入，"
                  "直接接 CI 会因缺输入而 fail-closed 变红。待定：是否在 CI 里先构建 tarball。",
        "owner": "设计者（待裁）",
        "review": "2026-10-12",
    },
    "interop_gate.py": {
        "reason": "其唯一消费方（冻结凭证）在生产零调用点（另见 issue #126），"
                  "当前接入 CI 无实际判据作用。**应先解 #126，再接执行面**。",
        "owner": "设计者（待裁）",
        "review": "2026-10-12",
    },
}

EXIT_MEANING = {
    0: "全部守卫有执行面，或已登记例外",
    1: "存在未登记的孤儿守卫",
    2: "环境/解析错误（fail-closed）",
    3: "登记表格式错误",
}


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def gather_guards(repo: str) -> list[str]:
    """收集守卫清单（仓内相对路径，/ 分隔，已排序）。"""
    out: list[str] = []
    d = os.path.join(repo, "scripts")
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if name.startswith("check_") and name.endswith(".py"):
                out.append("scripts/" + name)
    for rel in NAMED_GUARDS:
        if os.path.isfile(os.path.join(repo, rel)) and rel not in out:
            out.append(rel)
    return sorted(set(out))


def gather_surfaces(repo: str) -> dict:
    """解析执行面。任一项读不到即抛 OSError（fail-closed）。"""
    pj_path = os.path.join(repo, "package.json")
    if not os.path.isfile(pj_path):
        raise OSError("package.json 不存在：" + pj_path)
    pj = json.loads(_read(pj_path))
    scripts = pj.get("scripts") or {}
    gate_text = " ".join(
        str(scripts.get(k, "")) for k in ("gate", "prepublishOnly", "test")
    )

    wf_dir = os.path.join(repo, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        raise OSError("workflows 目录不存在：" + wf_dir)
    ci_text = ""
    wf_names = []
    for name in sorted(os.listdir(wf_dir)):
        if name.endswith((".yml", ".yaml")):
            wf_names.append(name)
            ci_text += _read(os.path.join(wf_dir, name)) + "\n"
    if not wf_names:
        raise OSError("workflows 目录为空：" + wf_dir)

    collected_tests: set[str] = set()
    for rel in COLLECT_GLOBS:
        d, _, pat = rel.rpartition("/")
        base = os.path.join(repo, d) if d else repo
        if not os.path.isdir(base):
            continue
        pre, _, suf = pat.partition("*")
        for name in os.listdir(base):
            if name.startswith(pre) and name.endswith(suf):
                collected_tests.add((d + "/" if d else "") + name)

    return {
        "gate_text": gate_text,
        "ci_text": ci_text,
        "workflows": wf_names,
        "collected_tests": collected_tests,
    }


def classify(guard: str, sf: dict) -> tuple[str, str]:
    """返回 (状态, 依据串)。状态 ∈ covered_direct / covered_test / orphan。"""
    base = os.path.basename(guard)
    stem = base[:-3]  # 去掉 .py
    if base in sf["gate_text"] or stem in sf["gate_text"]:
        return "covered_direct", "package.json gate/prepublishOnly 点名"
    if base in sf["ci_text"] or stem in sf["ci_text"]:
        return "covered_direct", "CI workflow 点名"
    paired = "scripts/test_" + stem + ".py"
    if paired in sf["collected_tests"]:
        return "covered_test", "配对测试 " + paired + " 被 run_tests.py 收集（CI 跑它）"
    return "orphan", "无执行面"


def build(repo: str) -> dict:
    guards = gather_guards(repo)
    sf = gather_surfaces(repo)
    rows = []
    orphans = []
    bad_registry = []
    for g in guards:
        state, why = classify(g, sf)
        base = os.path.basename(g)
        reg = EXCEPTIONS.get(base)
        if state == "orphan" and reg is None:
            orphans.append(base)
        if reg is not None:
            miss = [k for k in ("reason", "owner", "review") if not reg.get(k)]
            if miss:
                bad_registry.append(base + " 缺字段 " + ",".join(miss))
        rows.append({
            "guard": g,
            "state": state,
            "why": why,
            "registered_exception": bool(reg),
            "exception_reason": (reg or {}).get("reason", ""),
        })
    return {
        "repo": repo,
        "guards": rows,
        "n_guards": len(guards),
        "n_covered": sum(1 for r in rows if r["state"].startswith("covered")),
        "n_exception": sum(1 for r in rows if r["registered_exception"]),
        "orphans": orphans,
        "bad_registry": bad_registry,
        "workflows": sf["workflows"],
        "n_collected_tests": len(sf["collected_tests"]),
    }


def render(info: dict) -> None:
    print("  守卫族「执行面」覆盖矩阵（元守卫 · issue #129）")
    print("  仓：%s" % info["repo"])
    print("  执行面：package.json gate + %d 个 workflow + %d 个被收集的测试文件"
          % (len(info["workflows"]), info["n_collected_tests"]))
    print("")
    print("  %-36s %-14s %s" % ("守卫", "状态", "依据"))
    for r in info["guards"]:
        mark = {"covered_direct": "覆盖(直接)", "covered_test": "覆盖(配对测试)",
                "orphan": "孤儿"}[r["state"]]
        extra = ""
        if r["registered_exception"]:
            extra = "  [已登记例外] " + r["exception_reason"][:40]
        print("  %-36s %-14s %s%s" % (r["guard"], mark, r["why"][:56], extra))
    print("")
    print("  小计：守卫 %d ｜ 有执行面 %d ｜ 登记例外 %d ｜ 未登记孤儿 %d"
          % (info["n_guards"], info["n_covered"], info["n_exception"], len(info["orphans"])))
    if info["bad_registry"]:
        print("  [登记表格式错误] " + "；".join(info["bad_registry"]))
    for o in info["orphans"]:
        print("  [孤儿] %s —— 既无执行面又未登记；请接进 gate/CI，或登记理由" % o)


def exit_code(info: dict) -> int:
    if info["bad_registry"]:
        return 3
    if info["orphans"]:
        return 1
    return 0


def selftest() -> int:
    """合成语料自检：不依赖本仓现状，验证判据有判别力。"""
    fails = []
    # 1) 合成一个仓：guard 有配对测试 -> covered_test
    import tempfile
    root = tempfile.mkdtemp(prefix="guardcov_")
    os.makedirs(os.path.join(root, "scripts"))
    os.makedirs(os.path.join(root, ".github", "workflows"))
    open(os.path.join(root, "package.json"), "w", encoding="utf-8").write(
        json.dumps({"scripts": {"gate": "python scripts/check_alpha.py"}}))
    open(os.path.join(root, ".github", "workflows", "w.yml"), "w", encoding="utf-8").write(
        "run: python scripts/check_beta.py\n")
    for n in ("check_alpha.py", "check_beta.py", "check_gamma.py", "check_delta.py"):
        open(os.path.join(root, "scripts", n), "w", encoding="utf-8").write("# x\n")
    open(os.path.join(root, "scripts", "test_check_gamma.py"), "w", encoding="utf-8").write("# x\n")

    info = build(root)
    st = {os.path.basename(r["guard"]): r["state"] for r in info["guards"]}
    if st.get("check_alpha.py") != "covered_direct":
        fails.append("alpha 应判 covered_direct，实为 %s" % st.get("check_alpha.py"))
    if st.get("check_beta.py") != "covered_direct":
        fails.append("beta 应判 covered_direct，实为 %s" % st.get("check_beta.py"))
    if st.get("check_gamma.py") != "covered_test":
        fails.append("gamma(有配对测试) 应判 covered_test，实为 %s" % st.get("check_gamma.py"))
    if st.get("check_delta.py") != "orphan":
        fails.append("delta(无执行面) 应判 orphan，实为 %s" % st.get("check_delta.py"))
    if exit_code(info) != 1:
        fails.append("含孤儿时应退 1，实为 %d" % exit_code(info))
    if "check_delta.py" not in info["orphans"]:
        fails.append("orphans 未包含 delta")

    # 2) 登记表缺字段 -> 退 3
    saved = dict(EXCEPTIONS)
    try:
        EXCEPTIONS["check_delta.py"] = {"reason": "x"}
        info2 = build(root)
        if exit_code(info2) != 3:
            fails.append("登记表缺字段应退 3，实为 %d" % exit_code(info2))
        EXCEPTIONS["check_delta.py"] = {"reason": "r", "owner": "o", "review": "d"}
        info3 = build(root)
        if exit_code(info3) != 0:
            fails.append("登记完整后应退 0，实为 %d" % exit_code(info3))
        if info3["orphans"]:
            fails.append("登记后仍报孤儿：%s" % info3["orphans"])
    finally:
        EXCEPTIONS.clear()
        EXCEPTIONS.update(saved)

    # 3) fail-closed：无 package.json -> OSError
    bad = tempfile.mkdtemp(prefix="guardcov_bad_")
    try:
        build(bad)
        fails.append("缺 package.json 应抛 OSError（fail-closed），实为未抛")
    except OSError:
        pass

    for f in fails:
        print("  [FAIL] " + f)
    print("")
    if fails:
        print("  自检：%d 项失败" % len(fails))
        return 1
    print("  自检：全部通过（判据有判别力：覆盖/配对测试/孤儿/登记/ fail-closed 五面）")
    return 0


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="守卫族执行面元守卫（issue #129）")
    ap.add_argument("--repo", default=REPO, help="仓路径（默认本文件所在仓）")
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON")
    ap.add_argument("--selftest", action="store_true", help="合成语料自检，不看本仓")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()

    try:
        info = build(a.repo)
    except OSError as ex:
        print("  VERDICT=FAIL(环境)：%s" % ex)
        return 2
    except Exception as ex:  # 解析面异常同样 fail-closed
        print("  VERDICT=FAIL(解析)：%s: %s" % (type(ex).__name__, ex))
        return 2

    code = exit_code(info)
    if a.json:
        info2 = dict(info)
        info2["exit_code"] = code
        info2["exit_meaning"] = EXIT_MEANING[code]
        print(json.dumps(info2, ensure_ascii=False, indent=1))
    else:
        render(info)
    print("")
    print("  退出码 %d：%s" % (code, EXIT_MEANING[code]))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
