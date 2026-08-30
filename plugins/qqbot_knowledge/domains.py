from __future__ import annotations

import re
from collections.abc import Iterable


DOMAIN_GROUP_BIASES: dict[str, tuple[str, ...]] = {
    "1035445959": ("orbital-ring",),
    "319567534": ("fractionate-everything",),
    "1163635014": ("shapez",),
}

DOMAIN_ALIASES: dict[str, tuple[str, ...]] = {
    "dsp-vanilla": (
        "dsp-vanilla",
        "dyson sphere program",
        "戴森球计划",
        "戴森球",
        "dsp",
        "vanilla",
        "原版",
        "物流塔",
        "分拣器",
        "黑雾",
    ),
    "fractionate-everything": (
        "fractionate-everything",
        "fractionateeverything",
        "万物分馏",
        "fractionate",
        "fractionator",
        "fe",
        "分馏塔",
        "转化塔",
        "记忆源点",
        "增产点数",
        "数据中心",
    ),
    "dsp-mod-tools": (
        "dsp-mod-tools",
        "mlj_dspmods",
        "辅助模组",
        "小工具",
        "工具模组",
        "savedataexporter",
        "save data exporter",
        "存档数据导出",
        "导出存档统计",
        "uxaenhance",
        "uxa enhance",
        "afterbuildevent",
        "after build event",
        "构建发布",
        "本地发布",
        "getdspdata",
        "vanillacurvesim",
        "vanilla curve sim",
        "曲线模拟",
    ),
    "orbital-ring": (
        "orbital-ring",
        "orbitalring",
        "project orbital ring",
        "orbital",
        "ring",
        "星环",
        "休谟",
        "三阶",
        "二阶",
        "火箭",
    ),
    "project-genesis": (
        "project-genesis",
        "projectgenesis",
        "project genesis",
        "genesis",
        "创世",
        "创世之书",
    ),
    "shapez": (
        "shapez",
        "shapez.io",
        "spz",
        "异形工厂",
        "图形",
        "形状",
        "电路",
        "短代码",
        "流形",
    ),
    "factorio": (
        "factorio",
        "异星工厂",
        "quality-cycler",
        "品质循环",
        "蓝图",
        "传送带",
        "物流机器人",
        "lua",
    ),
}

SEARCH_SYNONYMS: dict[str, tuple[str, ...]] = {
    "星环": ("orbitalring", "orbital", "ring"),
    "万物分馏": ("fractionate", "fractionator"),
    "分馏": ("fractionate", "fractionator"),
    "增产点数": ("proliferator", "proliferation", "productivity"),
    "数据中心": ("data center", "datacenter"),
    "异形工厂": ("shapez", "shape"),
    "图形": ("shape", "shapes"),
    "形状": ("shape", "shapes"),
    "电路": ("circuit", "wires"),
    "异星工厂": ("factorio",),
    "短代码": ("blueprint", "bp", "code", "key"),
    "蓝图": ("blueprint", "blueprints"),
    "传送带": ("transport belt", "belt"),
    "物流机器人": ("logistic robot", "logistic bot"),
    "品质": ("quality",),
    "配方": ("recipe", "recipes"),
    "科技": ("tech", "technology"),
    "功率": ("power",),
    "光度": ("luminosity", "power"),
    "系数": ("coefficient", "ratio"),
    "引力系数": ("gravity coefficient", "coefficient"),
    "休谟": ("hume",),
    "数学率": ("mathematical", "rate"),
    "数学率引擎": ("mathematical rate engine", "mathematical"),
    "三阶段": ("三阶", "third", "tier 3"),
    "二阶段": ("二阶", "second", "tier 2"),
    "渲染": ("render", "display"),
    "字段": ("field", "column", "header"),
    "列名": ("column", "header"),
    "表头": ("header", "column"),
    "统计": ("statistic", "summary"),
    "导出": ("export",),
    "存档": ("save", "gamedata"),
    "工作表": ("sheet", "worksheet"),
    "存档数据导出": ("savedataexporter", "save data exporter"),
    "导出存档统计": ("savedataexporter", "save data exporter"),
    "构建发布": ("afterbuildevent", "after build event"),
    "本地发布": ("afterbuildevent", "after build event"),
    "曲线模拟": ("vanillacurvesim", "vanilla curve sim"),
}

SEARCH_SYNONYM_VALUES = frozenset(
    item for synonyms in SEARCH_SYNONYMS.values() for item in synonyms
)
HIGH_VALUE_SEARCH_TERMS = frozenset(SEARCH_SYNONYMS) | {
    "数学率引擎",
    "引力系数",
    "三阶段",
    "二阶段",
    "增产点数",
    "数据中心",
    "渲染",
}

PRECISE_CROSS_DOMAIN_TERMS = frozenset(
    {
        "savedataexporter",
        "save data exporter",
        "uxaenhance",
        "uxa enhance",
        "afterbuildevent",
        "after build event",
        "getdspdata",
        "vanillacurvesim",
        "vanilla curve sim",
    }
)

_STOP_TERMS = frozenset(
    {
        "这个",
        "那个",
        "怎么",
        "什么",
        "为什么",
        "为啥",
        "可以",
        "现在",
        "一下",
        "是不是",
        "请问",
        "知道",
        "告诉",
    }
)


def normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def domains_matching_query(query: str) -> tuple[str, ...]:
    normalized = normalize_text(query)
    matched: list[str] = []
    for domain, aliases in DOMAIN_ALIASES.items():
        if any(_alias_in_query(normalize_text(alias), normalized) for alias in aliases):
            matched.append(domain)
    return tuple(matched)


def _alias_in_query(alias: str, query: str) -> bool:
    if not alias:
        return False
    if re.fullmatch(r"[a-z0-9_. -]+", alias):
        return bool(re.search(rf"(?<![a-z0-9_]){re.escape(alias)}(?![a-z0-9_])", query))
    return alias in query


def resolve_domains(query: str, group_id: str, available_domains: Iterable[str]) -> tuple[str, ...]:
    available = frozenset(available_domains)
    explicit = tuple(domain for domain in domains_matching_query(query) if domain in available)
    if explicit:
        return explicit
    return tuple(domain for domain in DOMAIN_GROUP_BIASES.get(group_id, ()) if domain in available)


def build_search_terms(query: str, *, limit: int = 14) -> tuple[str, ...]:
    normalized = normalize_text(query)
    terms: list[str] = []

    for aliases in DOMAIN_ALIASES.values():
        terms.extend(normalize_text(alias) for alias in aliases if _alias_in_query(normalize_text(alias), normalized))
    terms.extend(re.findall(r"[a-z_][a-z0-9_.-]{1,}|\d+", normalized))
    for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", query):
        if sequence in _STOP_TERMS:
            continue
        if len(sequence) <= 10:
            terms.append(sequence)
        for size in range(min(4, len(sequence)), 1, -1):
            terms.extend(sequence[index : index + size] for index in range(len(sequence) - size + 1))
    for source, synonyms in SEARCH_SYNONYMS.items():
        if normalize_text(source) in normalized:
            terms.append(normalize_text(source))
            terms.extend(normalize_text(item) for item in synonyms)

    filtered = {term for term in terms if len(term) >= 2 and term not in _STOP_TERMS}
    return tuple(sorted(filtered, key=_term_priority))[:limit]


def _term_priority(term: str) -> tuple[int, int, str]:
    if term in PRECISE_CROSS_DOMAIN_TERMS:
        return (0, -len(term), term)
    if term in HIGH_VALUE_SEARCH_TERMS:
        return (1, -len(term), term)
    if term in SEARCH_SYNONYM_VALUES:
        return (2, -len(term), term)
    if re.search(r"[\u4e00-\u9fff]", term):
        return (3, -len(term), term)
    return (4, -len(term), term)
