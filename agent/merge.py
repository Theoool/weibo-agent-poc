"""事件合并：把同一件事的不同话题标签并成一个事件。

为什么必须有这一步：模型（和 hashtag 聚类）倾向照抄话题标签，
于是「APEC」和「深圳APEC」会被输出成两个事件，直接违反
"不要把同一事件拆成多个事件"这条硬要求。

这里用纯规则做，不调模型：
1. 归一化名字（去 #、标点、空白，统一小写）；
2. 互为子串且短名覆盖长名 40% 以上，判同一事件；
3. 否则比较字符集 Jaccard，超过阈值才合并。

保守优先：宁可少合并，也不要错误地把两个事件并成一个。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .schemas import Event

# 相似度阈值。低于此值视为不同事件
MERGE_THRESHOLD = 0.75

# 话题符号与中英标点，全部剥掉再比
_PUNCT = re.compile(
    r"[\s#＃\-—_·、，,。.！!？?：:；;“”\"'‘’（）()\[\]【】<>《》/\\|~`@$%^&*+=]+"
)

_HEAT_ORDER = {"unknown": 0, "low": 1, "medium": 2, "high": 3}


def normalize(name: str) -> str:
    """归一化事件名，只保留有区分度的字符。"""
    s = (name or "").strip().replace("＃", "#")
    s = s.strip("#")
    s = _PUNCT.sub("", s)
    return s.lower()


def similarity(a: str, b: str) -> float:
    """两个事件名的相似度，0~1。"""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0

    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    # 子串关系要同时满足"够长"和"占比够大"，否则「APEC」会把所有含 APEC 的
    # 长标题都吞掉——那是过度合并，比不合并更糟。
    if len(short) >= 4 and short in long_:
        cover = len(short) / len(long_)
        if cover >= 0.4:
            return 0.8 + 0.2 * cover

    sa, sb = set(na), set(nb)
    return len(sa & sb) / len(sa | sb)


@dataclass
class MergeRecord:
    """合并留痕，写进 trace 和报告的 merged_from。"""

    kept: str
    absorbed: str
    similarity: float


def _merge_two(a: Event, b: Event) -> Event:
    """合并两个事件，返回新对象（不改动入参，便于回放）。"""
    keep, drop = (a, b) if len(normalize(a.name)) >= len(normalize(b.name)) else (b, a)
    names = list(dict.fromkeys([a.name, b.name, *a.merged_from, *b.merged_from]))

    facts: dict[str, object] = {}
    for fact in [*keep.facts, *drop.facts]:
        facts.setdefault(fact.text, fact)

    opinions = list(dict.fromkeys([*keep.opinions, *drop.opinions]))
    specs = list(dict.fromkeys([*keep.speculations, *drop.speculations]))

    evidence: dict[str, object] = {}
    for item in [*keep.evidence, *drop.evidence]:
        key = item.mid or item.url or item.content[:40]
        if key:
            evidence.setdefault(key, item)

    official: dict[str, object] = {}
    for item in [*keep.official_sources, *drop.official_sources]:
        key = item.url or item.content[:40]
        if key:
            official.setdefault(key, item)

    return Event(
        name=keep.name,
        summary="；".join(x for x in (keep.summary, drop.summary) if x),
        heat=max([keep.heat, drop.heat], key=lambda h: _HEAT_ORDER.get(h, 0)),
        heat_basis="；".join(dict.fromkeys(x for x in (keep.heat_basis, drop.heat_basis) if x)),
        facts=list(facts.values()),  # type: ignore[arg-type]
        opinions=opinions,
        speculations=specs,
        evidence=list(evidence.values()),  # type: ignore[arg-type]
        official_sources=list(official.values()),  # type: ignore[arg-type]
        judgement="\n".join(x for x in (keep.judgement, drop.judgement) if x),
        keywords=list(dict.fromkeys([*keep.keywords, *drop.keywords])),
        merged_from=[n for n in names if n != keep.name],
    )


def merge_events(
    events: list[Event], threshold: float = MERGE_THRESHOLD
) -> tuple[list[Event], list[MergeRecord]]:
    """反复合并相似度最高的一对，直到没有可合并的。

    每次都取全局最高相似度的一对，而不是顺序扫描，避免"先遇到的先并"
    导致合并结果依赖输入顺序。
    """
    pool = list(events)
    records: list[MergeRecord] = []

    while len(pool) > 1:
        best_sim, bi, bj = 0.0, -1, -1
        for i in range(len(pool)):
            for j in range(i + 1, len(pool)):
                s = similarity(pool[i].name, pool[j].name)
                if s > best_sim:
                    best_sim, bi, bj = s, i, j

        if bi < 0 or best_sim < threshold:
            break

        left, right = pool[bi], pool[bj]
        merged = _merge_two(left, right)
        absorbed = right.name if merged.name == left.name else left.name
        records.append(
            MergeRecord(kept=merged.name, absorbed=absorbed, similarity=round(best_sim, 3))
        )

        # 合并结果放回 bi 的位置，保持原有热度顺序
        pool = [merged if k == bi else pool[k] for k in range(len(pool)) if k != bj]

    return pool, records
