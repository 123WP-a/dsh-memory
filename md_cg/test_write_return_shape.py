# -*- coding: utf-8 -*-
'''写返回体判据守卫（issue #109 相邻面）：committed 必须可机器判定。

背景：`mdcg_remember` 工具面的**同一个返回体**在闸门短路分支给 `committed=False`，
成功分支此前**不给 `committed`** ⇒ 按 `committed` 判「是否落盘」的调用方会把**成功**
读成**未提交**；而按 `ok` 判的调用方会把**待审**读成**已提交**（#109 报的那一面）。
两面同源：**工具面的 committed 不是恒在的判据键**。

权威依据：`md_cg/protocol.py:57-59` 的 `WRITE_TRIO` 铁律（write 任一形态必含
ok/id/committed——「客户端据此判定『是否落盘』而非猜测」）与
`docs/mdcg/灵枢记忆动词协议_v1.0-draft.md:28/:31-32/:43`。

本守卫只钉**可判据面**：committed 恒在且与落盘事实一致；pending 态用 committed 可判。
不改写入语义。

运行：python -X utf8 -m md_cg.test_write_return_shape
      python -X utf8 -m md_cg.test_write_return_shape --mutate
'''
import argparse
import io
import json
import os
import shutil
import sys
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix='write_shape_sandbox_')
os.environ['MDCG_AUX_ROOT'] = _SANDBOX
os.environ['MDCG_ROOT'] = os.path.join(_SANDBOX, 'root')
os.environ['MDCG_MASTER_KEY'] = os.urandom(32).hex()
os.environ.pop('MDCG_TEST_LIVE_ROOT', None)
_pol = os.path.join(_SANDBOX, 'policy.json')
with io.open(_pol, 'w', encoding='utf-8') as _f:
    json.dump({'forbidden': ['FORBIDDEN_WORD'], 'required': ['PASSED']}, _f)
os.environ['MDCG_POLICY_FILE'] = _pol

from . import mcp_server, protocol                # noqa: E402
from .mdcos import MdCGOS                          # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_PASS = []
_FAIL = []
_RUN = [0]


def ok(cond, msg, extra=''):
    (_PASS if cond else _FAIL).append(msg)
    print(('  PASS ' if cond else '  FAIL ') + msg
          + (('  <- ' + str(extra)) if (extra and not cond) else ''))


def _lib(tag):
    _RUN[0] += 1
    return MdCGOS(os.path.join(_SANDBOX, '%s_r%d' % (tag, _RUN[0])), autoflush=0)


BODY = '# 功能名：%s' + chr(10) + chr(10) + '# 生效条件：PASSED' + chr(10)


def _args(text, nid):
    return {'content_kind': 'text', 'content': text, 'layer': 'knowledge',
            'node_id': nid, 'verification_basis': 'test'}


def tool_write(cg, text, nid):
    return mcp_server.call_tool(cg, 'mdcg_remember', _args(text, nid))


def verb_write(cg, text, nid):
    a = dict(op='write')
    a.update(_args(text, nid))
    return mcp_server.call_tool(cg, 'cg', a)


def g_tool_success():
    cg = _lib('toolok')
    o = tool_write(cg, BODY % '初版', 'w1')
    ok(o.get('ok') is True, 'G1a 工具面首次写入 ok=True', o.get('ok'))
    ok('committed' in o, 'G1b 工具面成功返回体含 committed（三键铁律）', sorted(o))
    ok(o.get('committed') is True, 'G1c 工具面成功 => committed is True', o.get('committed'))
    ok('初版' in ((cg.get('w1') or {}).get('content') or ''),
       'G1d 与落盘事实一致（正文确在盘上）')


def g_tool_pending():
    cg = _lib('toolpend')
    tool_write(cg, BODY % '初版', 'w2')
    o = tool_write(cg, BODY % '改写版', 'w2')
    ok('committed' in o, 'G2a pending 返回体含 committed', sorted(o))
    ok(o.get('committed') is False, 'G2b pending => committed is False', o.get('committed'))
    ok(bool(o.get('moved_to')), 'G2c 未 committed 必给去向（moved_to）', o.get('moved_to'))
    ok('改写版' not in ((cg.get('w2') or {}).get('content') or ''),
       'G2d 与落盘事实一致（新正文未落盘）')


def g_machine_decidable():
    cg = _lib('decide')
    a = tool_write(cg, BODY % 'A', 'w3')
    b = tool_write(cg, BODY % 'B', 'w3')
    ok(a.get('committed') is True and b.get('committed') is False,
       'G3a 「已提交 / 待审」两态仅凭 committed 即可机器区分（不依赖 ok）',
       (a.get('committed'), b.get('committed')))
    ok(a.get('ok') is True and b.get('ok') is True,
       'G3b 两态 ok 相同 => 印证「只判 ok 必然误判」（#109 要害；本守卫不以 ok 作判据）')


def g_verb_face():
    cg = _lib('verb')
    a = verb_write(cg, BODY % 'VA', 'v1')
    b = verb_write(cg, BODY % 'VB', 'v1')
    for tag, o, want in (('G4a verb 成功', a, True), ('G4b verb 待审', b, False)):
        miss = [k for k in protocol.WRITE_TRIO if k not in o]
        ok(not miss and o.get('committed') is want,
           '%s => 含三键且 committed is %s' % (tag, want), (miss, o.get('committed')))


def _scan_ok_without_committed(src):
    '''找出 return {"ok": True ...} 且同一字典字面量内无 committed 的返回。'''
    bad = []
    lines = src.split(chr(10))
    for n, l in enumerate(lines):
        if 'return {' not in l:
            continue
        if '\"ok\": True' not in l and "'ok': True" not in l:
            continue
        head = chr(10).join(lines[n:n + 6]).split('}')[0]
        if 'committed' not in head:
            bad.append('第 %d 行' % (n + 1))
    return bad


def _write_shapes():
    '''协议里 write 的声明形态表（判据真源，不在本文件另立一份）。'''
    return protocol.VERB_SPECS['write']['shapes']


def _required_of(name):
    return tuple(_write_shapes()[name].get('required') or ())


def _static_violations(keys):
    '''静态面判据（只有键名）：①三键齐 ②命中至少一个声明形态。

    返回 (违规列表, 命中的最具体形态名)。「最具体」＝required 键数最多——
    于是「自治出单」不会被更松的 committed 形态吞掉：形态判定可判据、不靠猜。
    '''
    ks = set(keys)
    bad = []
    miss = [k for k in protocol.WRITE_TRIO if k not in ks]
    if miss:
        bad.append('缺三键%s' % miss)
    hit = [n for n in _write_shapes() if set(_required_of(n)) <= ks]
    if not hit:
        bad.append('不属任何声明形态')
    best = max(hit, key=lambda n: len(_required_of(n))) if hit else None
    return bad, best


def _runtime_violations(body):
    '''运行面判据（有真值）：静态面 + 「未 committed 必给去向（moved_to/gate）」。'''
    bad, best = _static_violations(body)
    if body.get('committed') is not True and not (body.get('moved_to')
                                                 or body.get('gate')):
        bad.append('未 committed 但无去向（moved_to/gate 皆缺）')
    return bad, best


def _dict_returns_in_block(path, tool):
    '''AST 取工具面分支块内的**字典字面量**返回：(行号, 键列表, 源码片段)。

    用 AST 而非正则：单引号/换行/多行字典都要认出（正则漏提＝假绿灯）。
    命名返回（return out / return res）不是字面量——其键集由运行面判据覆盖。
    '''
    import ast
    src = io.open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        c = node.test
        if not (isinstance(c.left, ast.Name) and c.left.id == "name"
                and len(c.comparators) == 1
                and isinstance(c.comparators[0], ast.Constant)
                and c.comparators[0].value == tool):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                ks = [k.value for k in sub.value.keys
                      if isinstance(k, ast.Constant)]
                out.append((sub.lineno, ks,
                            ast.get_source_segment(src, sub.value) or ''))
    return sorted(out)


def g_declared_shapes_full():
    '''G6 按声明形态**全量**校验工具面（静态 AST 面）。

    每个字典字面量返回都必须 ①三键齐 ②命中声明形态；并按锚串钉住**具体形态**
    （锚串不在 ⇒ 判红：实现改了却没同步本表＝判据漂移，不静默放行）。
    '''
    src_path = os.path.join(_HERE, 'mcp_server.py')
    found = _dict_returns_in_block(src_path, 'mdcg_remember')
    ok(len(found) >= 3,
       'G6a 工具面分支块内字典返回数 >= 3（实测 %d 个）' % len(found),
       [r[0] for r in found])
    anchors = {
        'autonomy_forbidden': {'must': protocol.WRITE_TRIO + ('moved_to',)},
        'conflict_deferred': {'must': protocol.WRITE_TRIO + ('moved_to', 'verdict'),
                              'best': 'gate_short_circuit'},
        'review_queue': {'must': _required_of('autonomy_ticket'),
                         'best': 'autonomy_ticket', 'no': ('verdict',)},
    }
    seen = {}
    for ln, keys, seg in found:
        bad, best = _static_violations(keys)
        ok(not bad, 'G6b 行 %d 满足三键铁律且属声明形态（形态=%s）' % (ln, best),
           (bad, sorted(keys)))
        for name, spec in anchors.items():
            if name in seg:
                seen[name] = ln
                miss = [k for k in spec['must'] if k not in keys]
                ok(not miss, 'G6c 行 %d 是 %s 形态且含必需键' % (ln, name), miss)
                if spec.get('best'):
                    ok(best == spec['best'],
                       'G6d 行 %d（%s）命中的最具体形态 == %s'
                       % (ln, name, spec['best']), best)
                for k in spec.get('no', ()):
                    ok(k not in keys,
                       'G6e 行 %d（%s）不得含 %s（无该判据不许编造）'
                       % (ln, name, k), sorted(keys))
    for name in anchors:
        ok(name in seen,
           'G6f 锚点 %s 仍存在于工具面分支块（锚点漂移即判红）' % name,
           sorted(seen))


def g_runtime_shapes():
    '''G7 运行面：逐分支真跑，按声明形态全量校验。'''
    cg = _lib('shapes')
    a = tool_write(cg, BODY % '初版', 's1')
    bad, best = _runtime_violations(a)
    ok(not bad, 'G7a 成功分支满足全量校验（形态=%s）' % best, bad)
    ok(best == 'committed', 'G7b 成功分支最具体形态 == committed', best)

    b = tool_write(cg, BODY % '改写版', 's1')
    bad, best = _runtime_violations(b)
    ok(not bad, 'G7c 自治出单分支满足全量校验（形态=%s）' % best, bad)
    ok(best == 'autonomy_ticket',
       'G7d 自治出单分支最具体形态 == autonomy_ticket（不并入 gate_short_circuit）',
       best)
    ok('verdict' not in b,
       'G7e 自治出单分支不含 verdict（该路径无 audit 判据，不许编造）', sorted(b))
    ok(b.get('committed') is False and b.get('moved_to') == 'review_queue',
       'G7f 自治出单 = 未落盘 + 去向 review_queue + 可轮询 pid',
       (b.get('committed'), b.get('moved_to'), b.get('pid')))

    # on_conflict 必须显式给 defer：工具面缺省是 reject（抛 ConsistencyError），
    # 只有 defer 才会让 cg.add 返回 None 并走冲突 DEFER 出口（本批新增的去向词）。
    _ca = _args('# 功能名：冲突体' + chr(10) * 2
                + '# 生效条件：超时 30s 允许并发' + chr(10)
                + '# 不适用条件：超时' + chr(10), 's2')
    _ca['on_conflict'] = 'defer'
    c = mcp_server.call_tool(cg, 'mdcg_remember', _ca)
    bad, best = _runtime_violations(c)
    ok(not bad, 'G7g 冲突 DEFER 分支满足全量校验（形态=%s）' % best, bad)
    ok(c.get('moved_to') == 'conflict_deferred',
       'G7h 冲突 DEFER 去向词 == conflict_deferred（未落盘也未入队）',
       (c.get('moved_to'), c.get('committed'), sorted(c)))
    ok(c.get('committed') is False and 'pid' not in c,
       'G7i 冲突 DEFER：committed is False 且不给 pid（未入队 ⇒ 不轮询）', sorted(c))


def g_autonomy_forbidden_and_guest():
    '''G8 自治禁止（plan 档）+ 无权限（guest）两面。'''
    from unittest.mock import patch
    from . import autonomy_modes as _am
    # 纪律（autonomy_modes.py:124）：档位 env 名只在唯一入口模块写字面量 ——
    # 本守卫经 AUTONOMY_ENV_KEYS 取键（同 test_autonomy_modes 的 _ENV_MODE 口径），
    # 绝不在此处再写一处带引号的档位 env 字面量。
    with patch.dict(os.environ, {_am.AUTONOMY_ENV_KEYS['mode']: 'plan'}):
        cg = _lib('forbid')
        tool_write(cg, BODY % '初版', 'f1')
        o = tool_write(cg, BODY % '改写版', 'f1')
        bad, best = _runtime_violations(o)
        ok(not bad, 'G8a 自治禁止分支满足全量校验（形态=%s）' % best, bad)
        ok(o.get('moved_to') == 'autonomy_forbidden'
           and o.get('committed') is False,
           'G8b plan 档 => moved_to=autonomy_forbidden + committed False',
           (o.get('moved_to'), o.get('committed')))
    from .mdcos import MdCGSecure
    from .security import AccessDenied, Principal
    root = os.path.join(_SANDBOX, 'guest_root')
    master = bytes(range(32))
    owner = Principal(tenant='t', actor='owner', session='s', clearance='secret',
                      role='designer', can_write=True, can_admin=True)
    cg2 = MdCGSecure(root, master_key=master, principal=owner)
    guest = MdCGSecure(root, master_key=master, principal=Principal(
        tenant='t', actor='guest', session='g', clearance='public',
        role='guest', can_write=False))
    try:
        raised = None
        try:
            mcp_server.call_tool(guest, 'mdcg_remember', _args(BODY % 'guest', 'gx'))
        except Exception as exc:                       # noqa: BLE001
            raised = exc
        ok(isinstance(raised, AccessDenied),
           'G8c 无权限分支=异常面（抛 AccessDenied），不返回形状不全的 dict',
           type(raised).__name__ if raised else None)
    finally:
        guest.close()
        cg2.close()


def g_gated_envelope_exempt():
    '''G9 gated=true 分支的**显式豁免**判据（双向钉，防静默两面）。

    该分支委托 mdcos.remember_gated，返回**闸门裁决信封**（ok/gate/verdict/
    written），不含 id/committed ⇒ 不在三键面。本判据不断言它「合规」，而是钉：
      ① 现状确实缺三键（记录下来，不假装已覆盖）；
      ② 它仍给出机器可判的裁决/落盘判据（gate|verdict|written）；
      ③ 若将来补上三键 ⇒ 必须同时满足声明形态（改善也不许无声明）。
    '''
    cg = _lib('gatedenv')
    o = mcp_server.call_tool(cg, 'mdcg_remember',
                             dict(_args(BODY % 'gated', 'ge1'), gated=True))
    trio_complete = all(k in o for k in protocol.WRITE_TRIO)
    ok(not trio_complete,
       'G9a gated 分支现状不在三键面（实测键=%s）——显式记录，未假装覆盖'
       % sorted(o), sorted(o))
    ok(any(k in o for k in ('gate', 'verdict', 'written')),
       'G9b gated 分支仍给出机器可判的裁决/落盘判据（gate|verdict|written）',
       sorted(o))
    if trio_complete:
        bad, best = _runtime_violations(o)
        ok(not bad,
           'G9c gated 分支已补三键 => 必须满足声明形态（形态=%s）' % best, bad)


def g_tool_face_binding_declared():
    '''G10 声明面：mdcg_remember 已绑定 write 动词（题1 的声明落地）。'''
    bind = getattr(protocol, 'TOOL_FACE_BINDINGS', {}).get('mdcg_remember')
    ok(bool(bind), 'G10a 协议声明了工具面 mdcg_remember → 动词绑定', bind)
    ok(bool(bind) and bind.get('verb') == 'write',
       'G10b 绑定动词 == write（受 WRITE_TRIO 铁律约束）', bind)
    rep = protocol.audit()
    ok(rep.get('ok') is True and not rep.get('shape_errors'),
       'G10c 协议静态对账 ok=True 且零 shape_errors', rep.get('shape_errors'))
    ok('autonomy_ticket' in protocol.VERB_SPECS['write']['shapes'],
       'G10d write 声明形态含 autonomy_ticket（题3b 落地）')
    ok('verdict' not in _required_of('autonomy_ticket'),
       'G10e autonomy_ticket.required 不含 verdict（该路径无 audit 判据）',
       _required_of('autonomy_ticket'))



def g_source_no_silent_gap():
    src = io.open(os.path.join(_HERE, 'mcp_server.py'), encoding='utf-8').read()
    i = src.find('if name == \"mdcg_remember\":')
    j = src.find('if name == \"mdcg_recall\":')
    block = src[i:j] if (i >= 0 and j > i) else ''
    ok(bool(block), 'G5a 定位到 mdcg_remember 工具面分支块', (i, j))
    bad = _scan_ok_without_committed(block)
    ok(not bad,
       'G5b 工具面分支内不存在「ok:True 却不给 committed」的返回（防静默回退）', bad)


_CASES = (g_tool_success, g_tool_pending, g_machine_decidable, g_verb_face,
          g_source_no_silent_gap,
          # issue #109 三题（2026-10-10 裁定）新增判据组：
          g_tool_face_binding_declared,   # G10 声明面：工具面绑定 + autonomy_ticket 落地
          g_declared_shapes_full,         # G6 静态面：按声明形态全量校验工具面
          g_runtime_shapes,               # G7 运行面：成功/自治出单/冲突 DEFER 逐分支
          g_autonomy_forbidden_and_guest,  # G8 自治禁止（plan 档）+ 无权限（异常面）
          g_gated_envelope_exempt)        # G9 gated 分支显式豁免（双向钉）


def _run_all():
    del _PASS[:]
    del _FAIL[:]
    for fn in _CASES:
        try:
            fn()
        except Exception as exc:                       # noqa: BLE001
            ok(False, '%s 组抛异常：%s: %s' % (fn.__name__, type(exc).__name__, exc))
    return list(_FAIL)


_SYN = (
    ('M0 合成合规源码（同一字典内含 committed）',
     'return {"ok": True, "id": nid, "committed": True}', False),
    ('M1 合成缺 committed（本轮修掉的那一态）',
     'return {"ok": True, "id": nid}', True),
    ('M2 合成 committed 换行后仍在同一字典内',
     'return {"ok": True, "id": nid,' + chr(10) + '        "committed": True}', False),
    ('M3 合成 committed 落在字典之外（应判红）',
     'return {"ok": True, "id": nid}' + chr(10) + 'x = {"committed": True}', True),
)


def _syn_shape_report(src):
    '''对**合成源码**跑本批新增的静态形态判据（不读盘、不定点改真实源码）。

    返回 [(违规列表, 最具体形态, 键列表)] —— 一个 return 一行。
    '''
    import ast as _ast
    import textwrap as _tw
    tree = _ast.parse(_tw.dedent(src))
    rows = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Return) and isinstance(node.value, _ast.Dict):
            ks = [k.value for k in node.value.keys if isinstance(k, _ast.Constant)]
            bad, best = _static_violations(ks)
            rows.append((bad, best, ks))
    return rows


#: 新增判据的定点变异表：每行 = (名称, 合成源码, 期望特征)。
#: 期望特征键：violations_red（静态面应否报违规）/ best（应为的最具体形态）/
#:             ticket_verdict_violation（「autonomy_ticket 含 verdict」应否被检出）。
_SYN_SHAPES = (
    ('V0 改前的冲突 DEFER 体（只有 ok/id/verdict/reason）＝本批修掉的那一态',
     'return {"ok": False, "id": nid, "verdict": "DEFER", "reason": "x"}',
     {'violations_red': True}),
    ('V1 改后的冲突 DEFER 体（三键 + moved_to + verdict + hint）',
     'return {"ok": False, "id": nid, "committed": False,'
     ' "moved_to": "conflict_deferred", "verdict": "DEFER", "hint": "h"}',
     {'violations_red': False, 'best': 'gate_short_circuit'}),
    ('V2 自治出单体（真实 8 键、无 verdict）',
     'return {"ok": True, "id": nid, "committed": False,'
     ' "moved_to": "review_queue", "pid": p, "mutation": m,'
     ' "autonomy": a, "hint": "h"}',
     {'violations_red': False, 'best': 'autonomy_ticket',
      'ticket_verdict_violation': False}),
    ('V3 自治出单体被硬补 verdict（编造判据 ⇒ 应被检出）',
     'return {"ok": True, "id": nid, "committed": False,'
     ' "moved_to": "review_queue", "pid": p, "mutation": m,'
     ' "autonomy": a, "hint": "h", "verdict": "DEFER"}',
     {'violations_red': False, 'best': 'autonomy_ticket',
      'ticket_verdict_violation': True}),
    ('V4 自治出单体丢掉 autonomy（形态降级 ⇒ 最具体形态退化为 committed）',
     'return {"ok": True, "id": nid, "committed": False,'
     ' "moved_to": "review_queue", "pid": p, "hint": "h"}',
     {'violations_red': False, 'best': 'committed',
      'body': {'ok': True, 'id': 'x', 'committed': False,
               'moved_to': 'review_queue', 'pid': 'prop_x', 'hint': 'h'},
      'runtime_red': False}),
    ('V5 三键齐但未 committed 且无去向（运行面应判红）',
     'return {"ok": False, "id": nid, "committed": False, "verdict": "DEFER"}',
     {'violations_red': False,
      'body': {'ok': False, 'id': 'x', 'committed': False, 'verdict': 'DEFER'},
      'runtime_red': True}),
)


def _run_shape_mutations():
    print('!! 新增判据定点变异自证（G6/G7 静态+运行面）')
    bad = 0
    for name, src, expect in _SYN_SHAPES:
        rows = _syn_shape_report(src)
        got_red = bool(rows) and bool(rows[0][0])
        got_best = rows[0][1] if rows else None
        ks = rows[0][2] if rows else []
        got_tv = (got_best == 'autonomy_ticket' and 'verdict' in ks)
        # 运行面判据吃**显式真实值的 body**（键集 fromkeys 会把值全变 None，
        # 那是假读：committed=None 既非 True 也非 False，moved_to=None 会被
        # 误当「无去向」）。故用表里给的 body；未给则跳过该项。
        _body = expect.get('body')
        rt_bad = _runtime_violations(_body)[0] if _body is not None else []
        got_rt = bool(rt_bad)
        checks = []
        if 'violations_red' in expect:
            checks.append(('静态面判红=%s' % expect['violations_red'],
                           got_red == expect['violations_red']))
        if 'best' in expect:
            checks.append(('最具体形态=%s' % expect['best'],
                           got_best == expect['best']))
        if 'ticket_verdict_violation' in expect:
            checks.append(('ticket 带 verdict 被检出=%s'
                           % expect['ticket_verdict_violation'],
                           got_tv == expect['ticket_verdict_violation']))
        if 'runtime_red' in expect:
            checks.append(('运行面判红=%s' % expect['runtime_red'],
                           got_rt == expect['runtime_red']))
        good = all(v for _l, v in checks)
        if not good:
            bad += 1
        print('  %s %s' % ('PASS' if good else 'FAIL', name))
        for label, v in checks:
            print('        %s %s' % ('ok ' if v else 'MISS', label))
        if not good:
            print('        实际：red=%s best=%s keys=%s runtime_bad=%s'
                  % (got_red, got_best, sorted(ks), rt_bad))
    print('')
    return bad


def _run_mutations():
    print('!! 定点变异自证：给 G5b 判据体喂合成源码，判别力必须成立')
    bad = 0
    for name, src, expect_red in _SYN:
        got = _scan_ok_without_committed(src)
        good = (bool(got) == expect_red)
        if not good:
            bad += 1
        print('  %s %s' % ('PASS' if good else 'FAIL', name))
        if not good:
            print('        预期有红=%s 实际=%s' % (expect_red, got))
    print('')
    bad += _run_shape_mutations()
    return bad


def main() -> int:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser(description='写返回体判据守卫（issue #109 相邻面）')
    ap.add_argument('--mutate', action='store_true', help='定点变异自证')
    a = ap.parse_args()
    reds = _run_all()
    print('')
    print('SUMMARY: 写返回体判据守卫：%d 通过，%d 失败' % (len(_PASS), len(reds)))
    if reds:
        print('失败判据：' + '; '.join(reds))
    rc = 1 if reds else 0
    if a.mutate:
        rc = 1 if (_run_mutations() or reds) else 0
        print('SUMMARY: 定点变异自证：%s' % ('全部按预期' if rc == 0 else '存在偏差'))
    shutil.rmtree(_SANDBOX, ignore_errors=True)
    return rc


if __name__ == '__main__':
    sys.exit(main())
