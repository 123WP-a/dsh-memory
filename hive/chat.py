# -*- coding: utf-8 -*-
"""hive/chat.py —— 群聊协议 v0.1 的最小可跑件（借 QQ 群聊成熟模型）

# 功能名：群聊最小实现——append-only 消息流 ＋ 三级角色 ＋ @ 传唤 ＋ 发言令牌 ＋ 撤回事件
# 生效条件：凡要让「人可对话的团队入口」落成可跑件时；凡要记录团队待决项与状态展示时。
# 子功能：① 群与成员（crew.json，QQ 三级角色：owner/admin/member）
#         ② 消息流（chat.jsonl，**append-only**：不提供任何删除/修改接口）
#         ③ @ 传唤（**必须带 refs**，否则按噪声记账在发起者头上）
#         ④ 发言令牌（显式租约＋到期，与蜂巢 claimed.lock／悬赏站 lease 同构）
#         ⑤ 撤回＝**事件**（不删原文）⑥ PASS 也落盘（否则「没说话」与「没参与」不可区分）
# 执行：python -X utf8 hive/chat.py --crew DIR post --from admin --text "@dev 核 issue#7" --ref issue:7
#       python -X utf8 hive/chat.py --crew DIR read [--since TS] [--limit N]
# 验证方式：hive/test_chat.py（断言 ＋ 定点变异自证）
# 不适用条件：不适用于对外域（那走悬赏站命令面，外部认领者不进群聊）；不适用于当调度面
#            （群聊不下调度指令，唯一出口是 spec.json 走蜂巢）。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time

# ---- 口径单点（改口径只改这里）------------------------------------------------

#: 群成员三级角色（照搬 QQ：群主／管理员／成员）。
ROLES = ("owner", "admin", "member")

#: 消息类型闭集。
KINDS = ("text", "pass", "system", "retract", "token")

#: 发言令牌默认时长（秒）——15 分钟，与「发言一次」同量级。**可调**。
TOKEN_TTL = 15 * 60

#: 消息 id 前缀。
MSG_PREFIX = "msg_"


# ---- 基础 --------------------------------------------------------------------


def _now() -> float:
    return time.time()


def msg_id(rec: dict) -> str:
    """消息指纹＝剔除 id 后内容的 sha1 前 12 位（同内容幂等去重，同账本纪律）。"""
    body = {k: v for k, v in rec.items() if k != "id"}
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return MSG_PREFIX + hashlib.sha1(blob).hexdigest()[:12]


def mentions(text: str):
    """从正文解析 @ 目标（照搬 QQ 的 @ 语义）：返回被 @ 的名单（去重保序）。"""
    out = []
    i = 0
    s = text or ""
    while i < len(s):
        if s[i] == "@":
            j = i + 1
            while j < len(s) and (s[j].isalnum() or s[j] in "_-."):
                j += 1
            name = s[i + 1:j]
            if name and name not in out:
                out.append(name)
            i = j
        else:
            i += 1
    return out


# ---- 群与成员 ----------------------------------------------------------------


def crew_path(crew_dir: str) -> str:
    return os.path.join(crew_dir, "crew.json")


def load_crew(crew_dir: str) -> dict:
    p = crew_path(crew_dir)
    if not os.path.isfile(p):
        return {"id": os.path.basename(os.path.abspath(crew_dir)), "members": {}}
    with io.open(p, encoding="utf-8") as fh:
        return json.load(fh)


def role_of(crew: dict, who: str) -> str:
    return str((crew.get("members") or {}).get(who) or "")


def is_member(crew: dict, who: str) -> bool:
    return role_of(crew, who) in ROLES


# ---- 消息流（append-only）-----------------------------------------------------


def chat_path(crew_dir: str) -> str:
    return os.path.join(crew_dir, "chat.jsonl")


def read_messages(crew_dir: str, since=None, limit=None):
    """读消息流（只读）。since=epoch 秒下界（不含）；limit=最多条数（取最新 N 后按时间正序）。"""
    p = chat_path(crew_dir)
    if not os.path.isfile(p):
        return []
    out = []
    with io.open(p, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if since is not None and float(rec.get("ts") or 0) <= float(since):
                continue
            out.append(rec)
    if limit is not None and limit >= 0:
        out = out[-int(limit):]
    return out


def append(crew_dir: str, rec: dict):
    """追加一条消息（**唯一写入口**；无删除/修改接口）。返回落盘记录（含 id）。

    幂等：同内容（剔除 id/ts 后）已存在 ⇒ 不重复追加，返回既有记录。
    """
    os.makedirs(crew_dir, exist_ok=True)
    rec = dict(rec)
    rec.setdefault("ts", _now())
    probe = {k: v for k, v in rec.items() if k not in ("id", "ts")}
    for old in read_messages(crew_dir):
        old_probe = {k: v for k, v in old.items() if k not in ("id", "ts")}
        if old_probe == probe:
            return old
    rec["id"] = msg_id(rec)
    p = chat_path(crew_dir)
    with io.open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    return rec


# ---- 发言令牌（显式租约，与 claimed.lock／lease 同构）---------------------------


def token_path(crew_dir: str) -> str:
    return os.path.join(crew_dir, "token.json")


def load_token(crew_dir: str, now=None):
    """读当前令牌；已过期返回 None（**过期即视为无主**，不需显式回收）。"""
    p = token_path(crew_dir)
    if not os.path.isfile(p):
        return None
    try:
        with io.open(p, encoding="utf-8") as fh:
            tok = json.load(fh)
    except Exception:
        return None
    now = _now() if now is None else now
    if float(tok.get("until") or 0) <= now:
        return None
    return tok


def issue_token(crew_dir: str, holder: str, by: str, ttl: int = TOKEN_TTL, now=None):
    """发放发言令牌（**主持人**动作）。已有人持有时返回 (None, 现任持有者)。

    返回 (token, blocking_holder)。
    """
    now = _now() if now is None else now
    cur = load_token(crew_dir, now=now)
    if cur is not None:
        return None, cur.get("holder")
    tok = {"holder": holder, "issued_by": by, "issued": now, "until": now + float(ttl),
           "ttl": int(ttl)}
    os.makedirs(crew_dir, exist_ok=True)
    with io.open(token_path(crew_dir), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(tok, ensure_ascii=False, sort_keys=True) + "\n")
    append(crew_dir, {"from": by, "kind": "token",
                      "text": "令牌发给 @" + holder + "（至 " + str(round(tok["until"], 3)) + "）",
                      "refs": ["token:" + holder], "ts": now})
    return tok, None


def release_token(crew_dir: str, by: str, now=None):
    """交还令牌（发言完毕或 PASS 后）。返回是否确实释放。"""
    now = _now() if now is None else now
    cur = load_token(crew_dir, now=now)
    if cur is None:
        return False
    try:
        os.remove(token_path(crew_dir))
    except OSError:
        return False
    append(crew_dir, {"from": by, "kind": "token", "text": "令牌交还",
                      "refs": ["token:" + str(cur.get("holder"))], "ts": now})
    return True


# ---- 发言（含 @ 约束与 PASS 落盘）---------------------------------------------


def post(crew_dir: str, sender: str, text: str, refs=None, kind: str = "text", now=None):
    """发言。返回 (ok, rec_or_reason)。

    校验依序：① 发送者须为群成员 ② kind 须在闭集 ③ **@ 传唤必须带 refs**
    （否则按噪声记账在发起者头上：记一条 system 事件并拒绝该次发言）。
    """
    now = _now() if now is None else now
    refs = list(refs or [])
    crew = load_crew(crew_dir)
    if not is_member(crew, sender):
        return False, "非群成员：%s" % sender
    if kind not in KINDS:
        return False, "未知消息类型：%s" % kind
    if kind == "text":
        targets = mentions(text)
        if targets and not refs:
            append(crew_dir, {"from": sender, "kind": "system",
                              "text": "噪声传唤被拒（@ " + "、".join(targets) + " 未带待决项）",
                              "refs": ["noise:" + sender], "ts": now})
            return False, "传唤未带 refs（按噪声记账）"
    rec = append(crew_dir, {"from": sender, "kind": kind, "text": text,
                            "refs": refs, "ts": now})
    return True, rec


def do_pass(crew_dir: str, sender: str, now=None):
    """PASS（沉默）——**也要落盘**：否则「没说话」与「没参与」事后不可区分。"""
    now = _now() if now is None else now
    crew = load_crew(crew_dir)
    if not is_member(crew, sender):
        return False, "非群成员：%s" % sender
    rec = append(crew_dir, {"from": sender, "kind": "pass", "text": "",
                            "refs": [], "ts": now})
    return True, rec


def retract(crew_dir: str, sender: str, target_id: str, now=None):
    """撤回＝**记事件，不删原文**（与账本 append-only 铁律一致）。"""
    now = _now() if now is None else now
    crew = load_crew(crew_dir)
    if not is_member(crew, sender):
        return False, "非群成员：%s" % sender
    hit = any(m.get("id") == target_id for m in read_messages(crew_dir))
    if not hit:
        return False, "被撤消息不存在：%s" % target_id
    rec = append(crew_dir, {"from": sender, "kind": "retract", "text": "撤回",
                            "refs": ["msg:" + target_id], "ts": now})
    return True, rec


# ---- CLI ---------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description="群聊最小实现（append-only）")
    ap.add_argument("--crew", required=True, help="群目录")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("post")
    p.add_argument("--from", dest="sender", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--ref", action="append", default=[])
    p.add_argument("--kind", default="text")

    r = sub.add_parser("read")
    r.add_argument("--since", type=float, default=None)
    r.add_argument("--limit", type=int, default=None)

    s = sub.add_parser("pass")
    s.add_argument("--from", dest="sender", required=True)

    t = sub.add_parser("retract")
    t.add_argument("--from", dest="sender", required=True)
    t.add_argument("--target", required=True)

    args = ap.parse_args(argv)
    if args.cmd == "post":
        ok, out = post(args.crew, args.sender, args.text, args.ref, args.kind)
    elif args.cmd == "pass":
        ok, out = do_pass(args.crew, args.sender)
    elif args.cmd == "retract":
        ok, out = retract(args.crew, args.sender, args.target)
    else:
        ok, out = True, read_messages(args.crew, args.since, args.limit)
    if isinstance(out, list):
        for m in out:
            print("%s | %-8s | %-7s | %s" % (round(float(m.get("ts") or 0), 3),
                                             m.get("from"), m.get("kind"), m.get("text")))
    else:
        print(("OK  " if ok else "REJ ") + json.dumps(out, ensure_ascii=False)[:300])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
