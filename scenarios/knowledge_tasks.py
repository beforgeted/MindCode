"""Artificial paired tasks; the private oracle is never part of the model snapshot."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KnowledgeTask:
    name: str
    level: str
    instruction: str
    files: dict[str, str]
    editable: tuple[str, ...]
    oracle: str
    reference: dict[str, str]


def tasks() -> tuple[KnowledgeTask, ...]:
    cases = [
        KnowledgeTask(
            'addition', 'easy', '修复 safe_add 的计算错误，保持现有接口。',
            {'arithmetic.py': 'def safe_add(a, b):\n    return a - b\n',
             'docs/current.md': '# 加法\nsafe_add 返回两个数的和，支持负数和零。\n'},
            ('arithmetic.py',),
            'from arithmetic import safe_add\n'
            'for a,b in [(2,3),(-3,2),(0,0),(100,-100),(-7,-9)]:\n'
            '    assert safe_add(a,b)==a+b\n',
            {'arithmetic.py': 'def safe_add(a, b):\n    return a + b\n'},
        ),
        KnowledgeTask(
            'clamp', 'easy', '修复 clamp_value，让边界和非法区间符合项目当前文档。',
            {'numeric.py': 'def clamp_value(value, lower, upper):\n'
                           '    return min(lower, max(upper, value))\n',
             'docs/current.md': '# 限幅\nclamp_value 将数值限制在闭区间，'
                                'lower 大于 upper 时抛出 ValueError。\n'},
            ('numeric.py',),
            'from numeric import clamp_value\n'
            'for x,lo,hi,w in [(-4,0,8,0),(3,0,8,3),(10,0,8,8),(2,2,2,2)]:\n'
            '    assert clamp_value(x,lo,hi)==w\n'
            'try: clamp_value(1,5,2)\n'
            'except ValueError: pass\n'
            'else: raise AssertionError("inverted interval")\n',
            {'numeric.py': 'def clamp_value(value, lower, upper):\n'
                           '    if lower > upper:\n        raise ValueError("invalid interval")\n'
                           '    return min(upper, max(lower, value))\n'},
        ),
        KnowledgeTask(
            'quote', 'medium', '修复对外 quote 的折扣和税额计算，按当前计价文档执行。',
            {'billing/__init__.py': '',
             'billing/core.py': 'from decimal import Decimal\n\n'
                                'def quote(base, discount, tax):\n'
                                '    return Decimal(str(base)) * (1 + Decimal(str(tax)))\n',
             'api.py': 'from billing.core import quote\n',
             'docs/current.md': '# 折扣计价\nquote 将参数通过 str 转为 Decimal。'
                                'discount 是减免比例（0为不减免，1为全部减免），'
                                '折后金额为 base*(1-discount)，再对折后金额按 tax 比例加税；'
                                '仅最终金额按 ROUND_HALF_UP 保留两位小数。\n'},
            ('billing/core.py',),
            'from api import quote\nfrom decimal import Decimal\n'
            'for a,d,t,w in [(100,.1,.2,"108.00"),(1.005,0,0,"1.01"),'
            '(0,.3,.5,"0.00"),(19.99,.15,.07,"18.18"),(50,1,.2,"0.00")]:\n'
            '    result=quote(a,d,t)\n'
            '    assert isinstance(result,Decimal) and result==Decimal(w)\n'
            '    assert result.as_tuple().exponent==-2\n',
            {'billing/core.py': 'from decimal import Decimal, ROUND_HALF_UP\n\n'
                                'def quote(base, discount, tax):\n'
                                '    b,d,t = (Decimal(str(v)) for v in (base,discount,tax))\n'
                                '    return (b*(1-d)*(1+t)).quantize('
                                'Decimal("0.01"),rounding=ROUND_HALF_UP)\n'},
        ),
        KnowledgeTask(
            'weights', 'medium', '修复 public normalize_weights，按当前文档处理启用项、'
                                 '负权重和零总量，不修改传入记录。',
            {'ranking/__init__.py': '',
             'ranking/policy.py': 'def normalize_weights(records):\n'
                                  '    total = sum(r["weight"] for r in records)\n'
                                  '    return {r["id"]: r["weight"]/total for r in records}\n',
             'public.py': 'from ranking.policy import normalize_weights\n',
             'docs/current.md': '# 权重归一\n只选择 enabled 为 True 的项；'
                                '缺失 enabled 视为 True。有效权重为 max(0, weight)。'
                                '无启用项返回空字典，总量为零时给启用项均匀分配。'
                                '不修改输入，id 唯一。\n'},
            ('ranking/policy.py',),
            'from public import normalize_weights\nimport copy\n'
            'cases=[([],{}),([{"id":"a","weight":2},{"id":"b","weight":6}],'
            '{"a":.25,"b":.75}),([{"id":"a","weight":0},{"id":"b","weight":-2}],'
            '{"a":.5,"b":.5}),([{"id":"a","weight":2},'
            '{"id":"b","weight":100,"enabled":False}],{"a":1}),'
            '([{"id":"x","weight":5,"enabled":False}],{})]\n'
            'for inp,w in cases:\n'
            '    saved=copy.deepcopy(inp); assert normalize_weights(inp)==w; assert inp==saved\n',
            {'ranking/policy.py': 'def normalize_weights(records):\n'
                                  '    active=[r for r in records if r.get("enabled",True)]\n'
                                  '    if not active: return {}\n'
                                  '    total=sum(max(0,r["weight"]) for r in active)\n'
                                  '    return {r["id"]: max(0,r["weight"])/total '
                                  'if total else 1/len(active) for r in active}\n'},
        ),
        KnowledgeTask(
            'tiered', 'hard', '修复 summarize 的阶梯计价；对照当前规则和调用链，'
                              '保留历史接口，不改输入记录。',
            {'metering/__init__.py': '',
             'metering/rates.py': 'from decimal import Decimal\n\n'
                                  'def charge(units):\n'
                                  '    return Decimal(str(units))*Decimal(".20")\n',
             'metering/report.py': 'from decimal import Decimal\n'
                                   'from metering.rates import charge\n\n'
                                   'def summarize(records, tax=0):\n'
                                   '    return sum(charge(r["units"]) for r in records)\n',
             'api.py': 'from metering.report import summarize\n',
             'docs/current.md': '# 阶梯计价\n按所有 enabled（缺省 True）记录的 units '
                                '合计后应用阶梯，非逐条分别计价。前 10 单位每单位 .20，'
                                '随后 20 单位每单位 .15，超过 30 的部分每单位 .10。'
                                '非启用项不影响结果，启用项有负 units 应抛 ValueError。\n\n'
                                'summarize 返回 Decimal，将合计阶梯金额乘以 1+tax；'
                                '只在最终按 ROUND_HALF_UP 保留两位小数。空输入返回 0.00，'
                                '参数使用 str 转 Decimal，不修改记录。\n'},
            ('metering/rates.py', 'metering/report.py'),
            'from api import summarize\nfrom decimal import Decimal\nimport copy\n'
            'for rec,t,w in [([],0,"0.00"),([{"units":10}],0,"2.00"),'
            '([{"units":8},{"units":12}],0,"3.50"),([{"units":40}],.2,"7.20"),'
            '([{"units":1.025}],0,"0.21"),'
            '([{"units":10},{"units":-7,"enabled":False}],0,"2.00")]:\n'
            '    saved=copy.deepcopy(rec); v=summarize(rec,t)\n'
            '    assert isinstance(v,Decimal) and v==Decimal(w) and v.as_tuple().exponent==-2\n'
            '    assert rec==saved\n'
            'try: summarize([{"units":-1}])\n'
            'except ValueError: pass\n'
            'else: raise AssertionError("negative units")\n',
            {'metering/rates.py': 'from decimal import Decimal\n\n'
                                  'def charge(units):\n'
                                  '    u=Decimal(str(units))\n'
                                  '    if u<0: raise ValueError("negative units")\n'
                                  '    return min(u,10)*Decimal(".20")+'
                                  'min(max(u-10,0),20)*Decimal(".15")+'
                                  'max(u-30,0)*Decimal(".10")\n',
             'metering/report.py': 'from decimal import Decimal, ROUND_HALF_UP\n'
                                   'from metering.rates import charge\n\n'
                                   'def summarize(records, tax=0):\n'
                                   '    values=[Decimal(str(r["units"])) '
                                   'for r in records if r.get("enabled",True)]\n'
                                   '    if any(v<0 for v in values): '
                                   'raise ValueError("negative units")\n'
                                   '    return (charge(sum(values,Decimal(0)))*'
                                   '(1+Decimal(str(tax)))).quantize('
                                   'Decimal(".01"),rounding=ROUND_HALF_UP)\n'},
        ),
        KnowledgeTask(
            'policy_weights', 'hard', '修复 route_weights 的渠道权重计算，'
                                       '按当前租户覆盖、别名与默认规则执行。',
            {'routing/__init__.py': '',
             'routing/catalog.py': 'ALIASES={"quick":"fast","rapid":"fast",'
                                    '"eco":"economy"}\nDEFAULTS={"fast":2,"economy":1}\n',
             'routing/resolve.py': 'def resolve(name, aliases):\n    return name\n',
             'routing/weights.py': 'from routing.catalog import ALIASES,DEFAULTS\n'
                                    'from routing.resolve import resolve\n\n'
                                    'def route_weights(records, overrides=None):\n'
                                    '    return {r["channel"]:r.get("weight",1) '
                                    'for r in records}\n',
             'api.py': 'from routing.weights import route_weights\n',
             'docs/current.md': '# 当前渠道合同\n先排除 enabled=False 的记录（缺省 True）；'
                                '用 catalog.ALIASES 一次规范化渠道；结果键为规范名称。'
                                '同名记录合并。每条权重优先使用 overrides 的规范名称值，'
                                '其次记录显式 weight，再次规范名称 DEFAULTS，最后 1。'
                                '显式零不可被默认值覆盖，负值按零计算。\n\n'
                                '聚合后归一；所有合计为零时按不同规范渠道均分，'
                                '无启用渠道返回空字典。不得改变输入 records 或 overrides。\n'},
            ('routing/resolve.py', 'routing/weights.py'),
            'from api import route_weights\nimport copy\n'
            'cases=[([],None,{}),([{"channel":"quick"},{"channel":"eco"}],None,'
            '{"fast":2/3,"economy":1/3}),([{"channel":"quick","weight":0},'
            '{"channel":"fast","weight":2},{"channel":"eco","weight":2}],None,'
            '{"fast":.5,"economy":.5}),([{"channel":"rapid"},{"channel":"eco"}],'
            '{"fast":0},{"fast":0,"economy":1}),([{"channel":"rapid","weight":-9},'
            '{"channel":"eco","weight":0},{"channel":"other","enabled":False}],'
            'None,{"fast":.5,"economy":.5}),([{"channel":"unknown"}],None,{"unknown":1})]\n'
            'for rec,over,w in cases:\n'
            '    saved=copy.deepcopy((rec,over)); got=route_weights(rec,over)\n'
            '    assert got.keys()==w.keys()\n'
            '    assert all(abs(got[k]-w[k])<1e-12 for k in w)\n'
            '    assert (rec,over)==saved\n',
            {'routing/resolve.py': 'def resolve(name, aliases):\n'
                                   '    return aliases.get(name,name)\n',
             'routing/weights.py': 'from routing.catalog import ALIASES,DEFAULTS\n'
                                    'from routing.resolve import resolve\n\n'
                                    'def route_weights(records, overrides=None):\n'
                                    '    totals={}; overrides={} '
                                    'if overrides is None else overrides\n'
                                    '    for r in records:\n'
                                    '        if not r.get("enabled",True): continue\n'
                                    '        name=resolve(r["channel"],ALIASES)\n'
                                    '        w=overrides[name] if name in overrides '
                                    'else r.get("weight",DEFAULTS.get(name,1))\n'
                                    '        totals[name]=totals.get(name,0)+max(0,w)\n'
                                    '    total=sum(totals.values())\n'
                                    '    return {k:v/total if total else 1/len(totals) '
                                    'for k,v in totals.items()}\n'},
        ),
    ]
    # Identical distractors in both arms; no oracle, answer or reference code in candidate.
    for case in cases:
        for i in range(24 if case.level == 'hard' else 8):
            case.files[f'archive/legacy_{i:02d}.py'] = (
                f'# Retired experimental module {i}; not the public interface.\n'
                f'def archived_{i}(value):\n    return value\n'
            )
        case.files['README.md'] = ('# Synthetic project\n当前合同在 docs/current.md，'
                                  'archive 为退役代码。保持对外接口，只改必要实现。\n')
    return tuple(cases)
