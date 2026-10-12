# -*- coding: utf-8 -*-
"""stg 第 3 层：结构索引（装载期内存倒排 + 写路径增量维护）。

设计稿：`docs/plans/stg条件化与结构索引_设计_v0.1.md`（2026-10-03 使用者签收）。
本模块只放**结构与取数**：三张表的构建 / 增删 / 取子集 / 一致性自检。

「何时构建、何时回退、回退怎么上报」的编排在读侧（`stg.py`）；
「何时调增删钩」的编排在写侧（`mdcg._set_index_entry` / `mdcg._remove_index_entry`）。
两处都只经本模块的单点，不另写第二份分类口径。

三张表（**不变量：恒为快照 keys 的划分**，见 `verify`）：

    · `by_session: {sid | None | _UNHASHABLE: [id, ...]}`——`None` = 无会话归属
      （`unassigned` 兜底桶，语义与基线 ``fm.get("session") != sid`` 的等值比较一致）；
    · `by_layer:   {layer | _UNHASHABLE: [id, ...]}`；
    · `by_time:    [(t, id), ...]` 升序——`t` 取**观察轴区间起点**，经既有单点
      `trust.time_window_of`（= `stg._interval` 同一实现，不引入第二套时间解析）；
      无有效时间戳 → `untimed`；非有限值（NaN/±inf）→ `nonfinite`（见下）。
    另加 `pos: {id: 物理位序}`——子集按**索引物理序**回排：基线 `_scan` 的候选序
    就是快照物理序，等价性判据（flag 开/关逐位一致）要求取数面能复现它。

**为什么 nonfinite 单列**（不并入 untimed）：基线里 NaN 时间戳节点并非「无时间」——
`_interval` 对 `(nan, nan)` 判非 None，`time_relation` 落到最后一档 `overlaps`，于是
NaN 节点**会**命中时间窗查询；`±inf` 则分别落 `after`/`before`（永不命中）。故 NaN 必须
进时间窗查询的候选面（`subset` 对 time 维一并带上 nonfinite——保守超集，逐条精确判定
仍在 `_scan` 侧），untimed 才与基线「跳过」语义对齐。

不适用条件：非 stg 读侧（`cg(op=read)` 的候选收敛走 `apply_retrieval_gates`，与
本索引不同维、不共用）；跨进程共享（本索引是**进程内**派生结构，各进程自建）。
"""
from __future__ import annotations

import bisect
import math

from . import trust

#: 不可哈希的 session/layer 值（脏盘面才可能出现的形态）统一归此哨兵键。
#: 任何**具体会话/层**查询都查不到它——与基线 `!=` 等值比较同语义（脏值恒不等）。
_UNHASHABLE = object()

#: 维缺省哨兵（`None` 是**合法取值**：session=None 即 unassigned 桶，不可与「未给该维」混同）。
MISSING = object()


# 生效条件：value 可 hash 时原样返回，hash 抛 TypeError（list/dict 等）时返回模块级哨兵 _UNHASHABLE。
def _hashable(value):
    try:
        hash(value)
    except TypeError:
        return _UNHASHABLE
    return value


# 生效条件：entry 为 dict 时以 trust.time_window_of(entry, "observed") 取观察轴两端点，两端均可判定则返回 float(起点)，任一端不可判定（None）或 entry 非 dict 时返回 None；不做第二套解析、不猜测。
def time_of(entry):
    """条目 → 观察轴区间**起点**（epoch 秒）；不可判定 → None。

    与 `stg._interval` **同一单点**：`_scan` 把快照条目拼成 fm 后取到的区间，
    与直接对条目取逐位一致——`_scan` 把 `time_window` 平铺进 `condition_space`
    而 `time_window_of` 两种载体都读（见其「兼容两种载体形态」注释），
    `temporal` 优先级与毫秒归一也同一份 `trust.parse_time`。
    """
    if not isinstance(entry, dict):
        return None
    s, e = trust.time_window_of(entry, "observed")
    if s is None or e is None:
        return None
    return float(s)


# 生效条件：index_obj 为含 dict 型 "nodes" 的快照（或顶层就是 dict）时返回按其**物理序**一次性构建的 StgIndex（O(N)，逐条经 add 分类）；"nodes" 缺失或非 dict 时返回 None（调用方据此回退并上报 reason，不静默）。
def build(index_obj):
    """快照 → 三张表（O(N)）；快照形态不可用返回 None。"""
    nodes = index_obj.get("nodes") if isinstance(index_obj, dict) else None
    if not isinstance(nodes, dict):
        return None
    ix = StgIndex(index_obj)
    for nid in nodes:                      # 快照物理序（dict 插入序）→ pos 即此序
        ix.add(nid, nodes[nid])
    return ix


# 生效条件：ix 为 StgIndex、nodes 为快照 nodes 映射时，逐项核对「三表恒为快照 keys 的划分」——pos 与快照键集双向相等、pos 序 == 快照物理序、by_session/by_layer 各桶两两不交且并集 == 键集、by_time ∪ nonfinite ∪ untimed 同上、by_time 升序且分类与条目实际 session/layer/时间逐条一致、**四个桶的下标表与桶列表互为逆映射（#157，swap-pop 的正确性判据）**；返回不一致说明列表（空 = 不变量成立）。O(N)，守卫/审计用，不在取数热路径。
def verify(ix, nodes):
    """划分不变量自检：返回不一致说明列表（空 = 完好）。"""
    bad = []
    if ix.broken:
        bad.append("broken：增量维护期已记不一致标记（写钩异常）")
    ids = set(nodes)
    if set(ix.pos) != ids:
        bad.append("pos 键集与快照不等：多 %d / 缺 %d"
                   % (len(set(ix.pos) - ids), len(ids - set(ix.pos))))
    if sorted(ix.pos, key=ix.pos.get) != list(nodes):
        bad.append("pos 序与快照物理序不一致（取数面无法复现基线候选序）")
    if len(ix.pos) != len(nodes):
        bad.append("len(pos)=%d ≠ len(nodes)=%d" % (len(ix.pos), len(nodes)))
    for name, table in (("by_session", ix.by_session), ("by_layer", ix.by_layer)):
        seen = []
        for bucket, lst in table.items():
            seen.extend(lst)
            if len(set(lst)) != len(lst):
                bad.append("%s[%r] 桶内重复 id" % (name, bucket))
        if len(seen) != len(set(seen)):
            bad.append("%s 有 id 落在多个桶（非划分）" % name)
        if set(seen) != ids:
            bad.append("%s 并集与快照键集不等（多 %d / 缺 %d）"
                       % (name, len(set(seen) - ids), len(ids - set(seen))))
    # #157：下标表与桶列表必须**互为逆映射**——swap-pop 的正确性只由这条守住。
    # 删错下标若只错在顺序上，集合级判据（上面那些）未必立刻察觉；这里逐位核。
    for name, table, imaps in (("by_session", ix.by_session, ix.ix_session),
                               ("by_layer", ix.by_layer, ix.ix_layer)):
        if set(imaps) != set(table):
            bad.append("%s 下标表的桶键与列表不同步（多 %d / 缺 %d）"
                       % (name, len(set(imaps) - set(table)),
                          len(set(table) - set(imaps))))
        for bucket, lst in table.items():
            imap = imaps.get(bucket)
            if imap is None or len(imap) != len(lst):
                bad.append("%s[%r] 下标表缺失或长度不符（%s != %d）"
                           % (name, bucket,
                              "None" if imap is None else len(imap), len(lst)))
                continue
            for i, x in enumerate(lst):
                if imap.get(x) != i:
                    bad.append("%s[%r] 下标表与列表不符（%s -> %r，期望 %d）"
                               % (name, bucket, x, imap.get(x), i))
                    break
    for name, lst, imap in (("untimed", ix.untimed, ix.ix_untimed),
                            ("nonfinite", ix.nonfinite, ix.ix_nonfinite)):
        if len(imap) != len(lst):
            bad.append("%s 下标表长度不符（%d != %d）" % (name, len(imap), len(lst)))
        elif any(imap.get(x) != i for i, x in enumerate(lst)):
            bad.append("%s 下标表与列表不符" % name)
    timed = [nid for _t, nid in ix.by_time]
    sess_sets = {k: set(v) for k, v in ix.by_session.items()}
    layer_sets = {k: set(v) for k, v in ix.by_layer.items()}
    union = set(timed) | set(ix.nonfinite) | set(ix.untimed)
    if len(timed) != len(set(timed)) or len(set(ix.nonfinite)) != len(ix.nonfinite) \
            or len(set(ix.untimed)) != len(ix.untimed):
        bad.append("by_time/nonfinite/untimed 有重复 id")
    if union != ids:
        bad.append("by_time ∪ nonfinite ∪ untimed 与快照键集不等（多 %d / 缺 %d）"
                   % (len(union - ids), len(ids - union)))
    if [t for t, _ in ix.by_time] != sorted(t for t, _ in ix.by_time):
        bad.append("by_time 非升序")
    for nid in ids:
        e = nodes.get(nid)
        sid, layer, bucket, _t0 = ix.cls.get(nid, (None, None, None, None))
        if sid != _hashable((e or {}).get("session")):
            bad.append("%s 的 by_session 分类与条目不符" % nid)
        if layer != _hashable((e or {}).get("layer")):
            bad.append("%s 的 by_layer 分类与条目不符" % nid)
        t = time_of(e)
        want = 0 if (t is not None and math.isfinite(t)) else (
            1 if t is not None else 2)
        if bucket != want:
            bad.append("%s 的 by_time 分类与条目不符（t=%r）" % (nid, t))
        # 「划分」不只要求并集相等，还要求 id 落在**其分类键对应的桶**里——
        # 否则分桶键取错字段（如 by_layer 按 session 建）时并集仍相等、静默漏判。
        if nid not in sess_sets.get(sid, ()):
            bad.append("%s 不在其分类对应的 by_session 桶里" % nid)
        if nid not in layer_sets.get(layer, ()):
            bad.append("%s 不在其分类对应的 by_layer 桶里" % nid)
        if bucket == 0 and nid not in timed:
            bad.append("%s 应在 by_time 而不在" % nid)
        if bucket == 1 and nid not in ix.nonfinite:
            bad.append("%s 应在 nonfinite 而不在" % nid)
        if bucket == 2 and nid not in ix.untimed:
            bad.append("%s 应在 untimed 而不在" % nid)
    return bad


class StgIndex:
    """快照的结构倒排（三张表 + 物理位序 + 每节点分类记录）。

    维护纪律：增删只经 `add` / `remove`（写侧单点），取数只经 `subset`（读侧单点）。
    #157：四个桶列表各配一张**下标表**（ix_session/ix_layer/ix_untimed/ix_nonfinite），
    摘除走 swap-pop（O(1)）；桶内顺序因此不再保证是物理序，由 `subset` 出口统一
    `_in_pos_order` 回排把契约保住。`by_time` 必须保持升序、不能换位，其 `del`
    仍为 O(表长)——批量删 timed 节点仍是 O(N²)，属本件**未闭合余项**（见
    `_unclassify` 内注释与守卫 G4 对照读数）。
    `cls[nid] = (sid, layer, bucket, t)` 是**增量摘除**的依据（不必回扫桶猜分类）；
    任一步异常 ⇒ `broken = True`（读侧据此回退全量并重建，绝不带着半张表取数）。
    """

    def __init__(self, index_obj):
        self.index_obj = index_obj      # 强引用：读侧按 `is` 判代际（防 id 复用）
        self.by_session = {}
        self.by_layer = {}
        self.by_time = []
        self.untimed = []
        self.nonfinite = []
        self.pos = {}
        self.cls = {}
        # #157：桶内**下标表**（与四个桶列表一一对应）——使 _unclassify 的摘除
        # 从 list.remove 的 O(桶长) 降到 swap-pop 的 O(1)。代价：swap-pop 会打乱
        # 桶内顺序（原先恰好是物理序），故 subset 出口统一按 pos 回排，把既有
        # 「索引物理序」契约在读侧**单点**保住（见 subset 的出口注释）。
        self.ix_session = {}        # sid   -> {nid: 桶内下标}
        self.ix_layer = {}          # layer -> {nid: 桶内下标}
        self.ix_untimed = {}        # nid   -> untimed 内下标
        self.ix_nonfinite = {}      # nid   -> nonfinite 内下标
        self.broken = False
        self._next_pos = 0

    # 生效条件：nid 已在 pos 中时先摘旧分类（**位序不变**——与 dict 原地覆写保序同语义），否则分配下一个位序；随后按 entry 的 session/layer/时间重新分类一次（同键即幂等）；任一步异常置 broken=True（读侧回退重建）。
    def add(self, nid, entry):
        """增量增/改：与 `dict.__setitem__` 同序语义（已有键保位序、新键落尾）。"""
        try:
            if nid in self.pos:
                self._unclassify(nid)       # 位序保留（dict 覆写不移动）
            else:
                self.pos[nid] = self._next_pos
                self._next_pos += 1
            self._classify(nid, entry)
        except Exception:                   # noqa: BLE001  一致性存疑即标记，读侧回退
            self.broken = True

    # 生效条件：nid 不在 pos 中时无操作；在则摘分类并 pop 位序（位序**不回收**——后续新键恒得更大位序，与 dict 删后新增落尾一致）；任一步异常置 broken=True。
    def remove(self, nid):
        """增量删（不回收位序：新键恒落尾，与 dict 删除/追加语义同构）。"""
        try:
            if nid not in self.pos:
                return
            self._unclassify(nid)
            self.pos.pop(nid, None)
        except Exception:                   # noqa: BLE001
            self.broken = True

    # 生效条件：lst 为桶列表、imap 为「nid -> 桶内下标」表、nid 不在 lst 中；恒把 nid 追加到尾部并登记其下标（len-1），返回 None。
    @staticmethod
    def _push(lst, imap, nid):
        lst.append(nid)
        imap[nid] = len(lst) - 1

    # 生效条件：lst 为桶列表、imap 为对应下标表、nid 必在 imap 中；以 swap-pop 摘除该 nid——nid 不在末位时把末位元素搬进空洞并改其下标，在末位时直接 pop；恒返回 None。（O(1)：只动一个元素 + 两次字典写。）
    @staticmethod
    def _swap_pop(lst, imap, nid):
        i = imap.pop(nid)
        last = lst[-1]
        if last != nid:                 # 不在末位：末位搬进空洞、改其下标
            lst[i] = last
            imap[last] = i
        lst.pop()

    def _classify(self, nid, entry):
        e = entry if isinstance(entry, dict) else {}
        sid, layer = _hashable(e.get("session")), _hashable(e.get("layer"))
        t = time_of(e)
        self._push(self.by_session.setdefault(sid, []),
                   self.ix_session.setdefault(sid, {}), nid)
        self._push(self.by_layer.setdefault(layer, []),
                   self.ix_layer.setdefault(layer, {}), nid)
        if t is None:
            bucket = 2
            self._push(self.untimed, self.ix_untimed, nid)
        elif math.isfinite(t):
            bucket = 0
            bisect.insort(self.by_time, (t, nid))
        else:
            bucket = 1                  # NaN/±inf：见模块 docstring「为什么单列」
            self._push(self.nonfinite, self.ix_nonfinite, nid)
        self.cls[nid] = (sid, layer, bucket, t)

    # 生效条件：nid 必在 cls 中；按分类把 nid 从其所在桶以 swap-pop（O(1)）摘除并同步下标表——桶空则连桶键一并删除；bucket==0 走 by_time 的二分定位 + del（升序表不可换位，仍为 O(表长) 的搬移，见下方余项注释）；任一步异常抛给上层置 broken；恒返回 None。
    def _unclassify(self, nid):
        sid, layer, bucket, t = self.cls.pop(nid)
        self._swap_pop(self.by_session[sid], self.ix_session[sid], nid)
        if not self.by_session[sid]:    # 空桶不保留（表规模读数如实）
            self.by_session.pop(sid, None)
            self.ix_session.pop(sid, None)
        self._swap_pop(self.by_layer[layer], self.ix_layer[layer], nid)
        if not self.by_layer[layer]:
            self.by_layer.pop(layer, None)
            self.ix_layer.pop(layer, None)
        if bucket == 0:
            # 二分定位而非全表连比：(t, nid) 在升序表内唯一，命中即该位；
            # 不在该位 ⇒ 状态已不可信，抛错由上层置 broken（绝不摘错条目）。
            # 余项（#157 未闭合部分）：by_time 必须保持升序，不能 swap-pop，
            # 故 del 仍是 O(表长) 的搬移——批量删 timed 节点仍为 O(N²)，
            # 计数见守卫 G4 的前后对照；彻底解法是「升序表 + 惰性删除（墓碑 +
            # 阈值压实）」，属读侧多点改动，留待裁定（本件范围见任务卡）。
            i = bisect.bisect_left(self.by_time, (t, nid))
            if i >= len(self.by_time) or self.by_time[i] != (t, nid):
                raise KeyError(nid)
            del self.by_time[i]
        elif bucket == 1:
            self._swap_pop(self.nonfinite, self.ix_nonfinite, nid)
        else:
            self._swap_pop(self.untimed, self.ix_untimed, nid)

    # 生效条件：lo <= hi 时返回 t ∈ [lo, hi] 的 id 列表（**时间序**，调用方按 pos 回排）；lo > hi 返回空列表（区间为空——调用方在 inverted 查询上先回退，见 stg 侧）。
    def range_ids(self, lo, hi):
        """`by_time` 区间截取（二分）；返回时间序 id 列表。"""
        if lo > hi:
            return []
        a = bisect.bisect_left(self.by_time, (lo,))
        b = bisect.bisect_left(self.by_time, (hi,))
        # 同 t 并列：把 t == hi 的尾巴一并纳入（不假设 id 字符上界，避免脆弱判据）
        while b < len(self.by_time) and self.by_time[b][0] == hi:
            b += 1
        return [nid for _t, nid in self.by_time[a:b]]

    # 生效条件：session/layer 为 MISSING 表示该维不参与（**注意 None 是合法维值**，不表示缺省），time_range 为 None 表示时间维不参与、否则为 (lo, hi)；无任何参与维时返回 (None, ())；否则返回 (候选 id 列表（**索引物理序**）, 实际服务的维名元组)——任一维在表中无键即得空列表（与基线等值比较同语义，非回退）。
    # 生效条件：ids 为可迭代 nid（每个 nid 均应在 self.pos 中，否则 KeyError 抛给上层置 broken）；返回按**物理位序**（pos）升序的**新列表**。为什么出口统一回排（#157）：桶表改为 swap-pop 后不再是物理序，而 subset 的对外契约仍是「索引物理序」——该契约在**读侧单点**（此处）承担 O(k log k)，写侧换来 O(1) 摘除。
    def _in_pos_order(self, ids):
        return sorted(ids, key=self.pos.__getitem__)

    def subset(self, session=MISSING, layer=MISSING, time_range=None):
        """按维直取子集；返回 (ids | None, dims)。"""
        dims, srcs = [], []
        if session is not MISSING:
            dims.append("session")
            srcs.append(self.by_session.get(session, []))
        if layer is not MISSING:
            dims.append("layer")
            srcs.append(self.by_layer.get(layer, []))
        if time_range is not None:
            dims.append("time")
            lo, hi = time_range
            # nonfinite 是保守超集（NaN 在基线上会命中时间窗，见模块 docstring）
            srcs.append(self.range_ids(lo, hi) + list(self.nonfinite))
        if not dims:
            return None, ()
        if len(srcs) > 1:
            # 交集：以**最短表**驱动（成员判定用集合，不看序），结果统一回排。
            k = min(range(len(srcs)), key=lambda i: len(srcs[i]))
            driver = srcs[k]
            others = [set(srcs[i]) for i in range(len(srcs)) if i != k]
            out = [nid for nid in driver if all(nid in o for o in others)]
            return self._in_pos_order(out), tuple(dims)
        return self._in_pos_order(srcs[0]), tuple(dims)

    # 生效条件：恒返回表规模读数 dict——nodes=pos 条数、sessions/layers=两张表的**非空桶**数、timed=by_time 条数、untimed/nonfinite=两表条数；只读、无副作用。
    def size(self):
        """表规模读数（meta 用）。"""
        return {"nodes": len(self.pos), "sessions": len(self.by_session),
                "layers": len(self.by_layer), "timed": len(self.by_time),
                "untimed": len(self.untimed), "nonfinite": len(self.nonfinite)}
