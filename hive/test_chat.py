#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""hive/test_chat.py —— hive/chat.py 的守卫（断言 ＋ 定点变异自证）

# 功能名：群聊最小实现的守卫——逐条断言 ＋ 每条判据一个定点变异
# 生效条件：凡改动 hive/chat.py 后；凡要把群聊用于实际团队记录前。
# 子功能：① 临时群夹具（自造，确定性）② 11 条断言 ③ 8 条定点变异自证
# 执行：python -X utf8 hive/test_chat.py [--mutate]
# 验证方式：基线全绿；变异模式下每条恰好命中声明红项
# 不适用条件：不适用于真实群的业务正确性（本守卫只测协议实现逻辑）
"""

import io
import json
import os
import shutil
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "chat.py")
_red = []
_quiet = False


def ok(name, cond, detail=""):
    if cond:
        if not _quiet:
            print("  [PASS] " + name)
    else:
        if not _quiet:
            print("  [FAIL] " + name + ("  · " + str(detail)[:160] if detail else ""))
        _red.append(name.split()[0])


def mkcrew(mod):
    d = tempfile.mkdtemp(prefix="chat_fixture_")
    with io.open(os.path.join(d, "crew.json"), "w", encoding="utf-8") as fh:
        json.dump({"id": "t1", "members": {"boss": "owner", "admin": "admin",
                                           "dev": "member", "qa": "member"}},
                  fh, ensure_ascii=False)
    return d


def run_checks(mod):
    d = mkcrew(mod)
    try:
        # ① append-only：源码里不得出现删除消息的接口
        src = io.open(SRC, encoding="utf-8").read()
        has_del = ("def delete_message" in src) or ("def edit_message" in src)
        ok("C1 append-only：不提供删除/修改消息的接口", not has_del, "")

        # ② @ 解析
        ok("C2 @ 解析：多目标、去重保序",
           mod.mentions("@dev 和 @qa 请看 @dev") == ["dev", "qa"],
           mod.mentions("@dev 和 @qa 请看 @dev"))

        # ③ 成员校验
        bad, why = mod.post(d, "outsider", "hello")
        ok("C3 非群成员发言 → 拒", bad is False and "非群成员" in why, why)

        # ④ kind 闭集
        bad2, why2 = mod.post(d, "dev", "hi", kind="unknown")
        ok("C4 未知消息类型 → 拒", bad2 is False and "未知消息类型" in why2, why2)

        # ⑤ @ 必须带 refs
        bad3, why3 = mod.post(d, "admin", "@dev 你看一下")
        msgs = mod.read_messages(d)
        noise = [m for m in msgs if m.get("kind") == "system" and "噪声传唤" in str(m.get("text"))]
        ok("C5 @ 未带 refs → 拒 ＋ 按噪声记账在发起者头上",
           bad3 is False and "噪声" in why3 and len(noise) == 1, (why3, len(noise)))

        # ⑥ 带 refs 的 @ 通过
        good, rec = mod.post(d, "admin", "@dev 请核 issue#7", refs=["issue:7"])
        ok("C6 @ 带 refs → 通过且 refs 落盘",
           good is True and rec.get("refs") == ["issue:7"], rec)

        # ⑦ 幂等
        again = mod.append(d, {"from": "admin", "kind": "text", "text": "@dev 请核 issue#7",
                               "refs": ["issue:7"]})
        n_same = len([m for m in mod.read_messages(d)
                      if m.get("text") == "@dev 请核 issue#7"])
        ok("C7 同内容幂等：不重复追加", n_same == 1 and again.get("id") == rec.get("id"),
           (n_same, again.get("id"), rec.get("id")))

        # ⑧ PASS 也落盘
        pok, prec = mod.do_pass(d, "qa")
        n_pass = len([m for m in mod.read_messages(d) if m.get("kind") == "pass"])
        ok("C8 PASS 也落盘（否则「没说话」与「没参与」不可区分）",
           pok is True and n_pass == 1 and prec.get("kind") == "pass", n_pass)

        # ⑨ 撤回＝事件，不删原文
        before = len(mod.read_messages(d))
        rok, rrec = mod.retract(d, "admin", rec["id"])
        after = mod.read_messages(d)
        still = any(m.get("id") == rec["id"] for m in after)
        ok("C9 撤回＝事件（原文仍在，审计面无空洞）",
           rok is True and len(after) == before + 1 and still
           and rrec.get("kind") == "retract" and rrec.get("refs") == ["msg:" + rec["id"]],
           (before, len(after), still))

        # ⑫ 撤回**不存在**的消息 → 拒（存在性判据；M1 变异即针对本判据）
        nok, nwhy = mod.retract(d, "admin", "msg_不存在")
        ok("C12 撤回不存在的消息 → 拒（存在性判据）",
           nok is False and "不存在" in nwhy, nwhy)

        # ⑩ 令牌互斥
        t1, blk1 = mod.issue_token(d, "dev", "admin")
        t2, blk2 = mod.issue_token(d, "qa", "admin")
        ok("C10 发言令牌互斥：已有人持有时发不出（返回现任持有者）",
           t1 is not None and blk1 is None and t2 is None and blk2 == "dev", (blk1, blk2))

        # ⑪ 令牌过期即无主（不需显式回收）
        future = mod._now() + mod.TOKEN_TTL + 10
        cur_later = mod.load_token(d, now=future)
        t3, blk3 = mod.issue_token(d, "qa", "admin", now=future)
        ok("C11 令牌到期即视为无主（load 返回 None，可重新发放）",
           cur_later is None and t3 is not None and t3.get("holder") == "qa", (cur_later, t3))
    finally:
        shutil.rmtree(d, ignore_errors=True)


_MUTATIONS = (
    ("M1 关撤回的存在性判据（撤不存在的 id 也放行）",
     '    hit = any(m.get("id") == target_id for m in read_messages(crew_dir))',
     '    hit = True  # MUT', {"C12"}),
    ("M2 关 @ 必须带 refs 约束",
     "        if targets and not refs:",
     "        if False:  # MUT", {"C5"}),
    ("M3 关令牌互斥（可重复发放）",
     "    if cur is not None:",
     "    if False:  # MUT", {"C10"}),
    ("M4 关令牌到期判定",
     "    if float(tok.get(\"until\") or 0) <= now:",
     "    if False:  # MUT", {"C11"}),
    ("M5 PASS 不落盘",
     "    rec = append(crew_dir, {\"from\": sender, \"kind\": \"pass\", \"text\": \"\",",
     "    return True, {}  # MUT" + chr(10) + "    rec = append(crew_dir, {\"from\": sender, \"kind\": \"pass\", \"text\": \"\",", {"C8"}),
    ("M6 关 kind 闭集校验（任意类型放行）",
     '    if kind not in KINDS:',
     '    if False:  # MUT', {"C4"}),
    ("M7 关幂等（同内容重复追加）",
     "        if old_probe == probe:",
     "        if False:  # MUT", {"C7"}),
    ("M8 关 @ 解析（返回空）",
     "    out = []" + chr(10) + "    i = 0",
     "    return []  # MUT" + chr(10) + "    out = []" + chr(10) + "    i = 0", {"C2", "C5"}),
)


def _load(text, name="chat_mut"):
    mod = types.ModuleType(name)
    mod.__file__ = SRC
    exec(compile(text, "<chat-mut>", "exec"), mod.__dict__)
    return mod


def _mutate_mode():
    print("!! 定点变异自证：逐条短路一条判据，对应断言必须**恰好**转红" + chr(10))
    base = io.open(SRC, encoding="utf-8").read()
    bad = []
    for name, old, new, expect in _MUTATIONS:
        if base.count(old) != 1:
            print("  ANCHOR-MISS %s：锚点命中 %d 次" % (name, base.count(old)))
            bad.append(name)
            continue
        global _red, _quiet
        _red, _quiet = [], True
        try:
            run_checks(_load(base.replace(old, new)))
        except Exception as e:
            _quiet = False
            print("  [FAIL] %s 运行异常：%s" % (name, str(e)[:120]))
            bad.append(name)
            continue
        _quiet = False
        got = set(_red)
        flag = "PASS" if got == expect else "FAIL"
        print("  [%s] %s：红项 %d（期望 %d）%s" % (flag, name, len(got), len(expect),
              "" if got == expect else " 实=%s 期=%s" % (sorted(got), sorted(expect))))
        if got != expect:
            bad.append(name)
    print()
    if bad:
        print("定点变异：FAIL —— " + "；".join(bad))
        return 1
    print("定点变异：PASS（逐条恰好命中期望红项）")
    return 0


def main(argv):
    global _red          # 必须声明：否则 _red=[] 成为局部变量，汇总恒报「失败 0」
    if "--mutate" in argv:
        return _mutate_mode()
    print("== hive/chat.py 守卫（断言）==")
    _red = []
    run_checks(_load(io.open(SRC, encoding="utf-8").read(), "chat_base"))
    print("=" * 70)
    if _red:
        print("通过 %d / 失败 %d" % (12 - len(_red), len(_red)))
        print("失败项：" + " / ".join(_red))
        return 1
    print("通过 12 / 失败 0")
    print("ALL OK（反面证明见 --mutate）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
