# -*- coding: utf-8 -*-
"""scripts/check_guard_coverage.py 的配对测试（issue #129 元守卫自证）。

跑法：
  python -X utf8 scripts/test_check_guard_coverage.py
  python -X utf8 scripts/test_check_guard_coverage.py --mutate=covered|no_registry|glob
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = []
FAIL = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name + ("  · " + detail if detail else ""))


def load_guard():
    p = os.path.join(REPO, "scripts", "check_guard_coverage.py")
    spec = importlib.util.spec_from_file_location("guardcov", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def synth(tmp: str) -> str:
    """合成小仓：alpha 被 gate 点名；beta 被 CI 点名；gamma 有配对测试；delta 孤儿。"""
    os.makedirs(os.path.join(tmp, "scripts"))
    os.makedirs(os.path.join(tmp, ".github", "workflows"))
    with open(os.path.join(tmp, "package.json"), "w", encoding="utf-8") as fh:
        json.dump({"scripts": {"gate": "python scripts/check_alpha.py"}}, fh)
    with open(os.path.join(tmp, ".github", "workflows", "w.yml"), "w", encoding="utf-8") as fh:
        fh.write("run: python scripts/check_beta.py\n")
    for n in ("check_alpha.py", "check_beta.py", "check_gamma.py", "check_delta.py"):
        with open(os.path.join(tmp, "scripts", n), "w", encoding="utf-8") as fh:
            fh.write("# x\n")
    with open(os.path.join(tmp, "scripts", "test_check_gamma.py"), "w", encoding="utf-8") as fh:
        fh.write("# x\n")
    return tmp


def states(mod, root: str) -> dict:
    info = mod.build(root)
    return {os.path.basename(r["guard"]): r["state"] for r in info["guards"]}


def run(mutate: str) -> int:
    """三条变异 + 一套**恒定的核心断言**。

    设计要旨（吸取既往教训）：变异必须**破坏守卫的判据**，然后由**同一套断言**把它逮住
    ——而不是单独为变异写一条「变异生效了」的断言（那是空跑，永远绿）。
    """
    mod = load_guard()
    tmp = tempfile.mkdtemp(prefix="guardcov_test_")
    synth(tmp)

    if mutate == "covered":
        # 判据被破坏：分类恒返回「已覆盖」⇒ 孤儿再也识别不出来
        mod.classify = lambda g, sf: ("covered_direct", "mutated")
    if mutate == "no_registry":
        # 判据被破坏：登记表被清空 ⇒ 真仓的已登记例外重新变成孤儿
        mod.EXCEPTIONS.clear()
    if mutate == "glob":
        # 判据被破坏：配对测试收集面被清空 ⇒ 有配对测试的守卫被误判为孤儿
        mod.COLLECT_GLOBS = ()

    st = states(mod, tmp)

    # ---- 核心断言（与变异无关，恒定执行）----
    ok("alpha 被 gate 点名 ⇒ covered_direct", st.get("check_alpha.py") == "covered_direct",
       str(st.get("check_alpha.py")))
    ok("beta 被 CI 点名 ⇒ covered_direct", st.get("check_beta.py") == "covered_direct",
       str(st.get("check_beta.py")))
    ok("gamma 有配对测试 ⇒ covered_test", st.get("check_gamma.py") == "covered_test",
       str(st.get("check_gamma.py")))
    ok("delta 无执行面 ⇒ orphan（与 gamma 同仓对照）", st.get("check_delta.py") == "orphan",
       str(st.get("check_delta.py")))

    real = mod.build(REPO)
    ok("真仓无未登记孤儿（退出码 0）", mod.exit_code(real) == 0,
       "orphans=" + str(real["orphans"]) + " bad=" + str(real.get("bad_registry")))
    ok("真仓守卫数 >= 8", real["n_guards"] >= 8, str(real["n_guards"]))
    gc = [r for r in real["guards"] if r["guard"].endswith("check_guard_coverage.py")]
    ok("check_guard_coverage 自身有执行面（本测试即其配对测试）",
       bool(gc) and gc[0]["state"] == "covered_test", str(gc[0]["state"]) if gc else "缺失")

    if mutate == "":
        bad = tempfile.mkdtemp(prefix="guardcov_nopj_")
        try:
            mod.build(bad)
            ok("缺 package.json ⇒ OSError（fail-closed）", False, "未抛")
        except OSError:
            ok("缺 package.json ⇒ OSError（fail-closed）", True)
        saved = dict(mod.EXCEPTIONS)
        try:
            mod.EXCEPTIONS["check_delta.py"] = {"reason": "x"}
            ok("登记表缺 owner/review ⇒ 退 3", mod.exit_code(mod.build(tmp)) == 3)
        finally:
            mod.EXCEPTIONS.clear()
            mod.EXCEPTIONS.update(saved)

    print("")
    for x in PASS:
        print("  [PASS] " + x)
    for x in FAIL:
        print("  [FAIL] " + x)
    print("")
    print("  check_guard_coverage 守卫：%d pass / %d fail" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mutate", default="", choices=("", "covered", "no_registry", "glob"))
    a = ap.parse_args()
    return run(a.mutate)


if __name__ == "__main__":
    sys.exit(main())
