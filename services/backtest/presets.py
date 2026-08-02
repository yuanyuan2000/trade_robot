from __future__ import annotations

from copy import deepcopy

from database import backtest_repository
from services.backtest.code_strategies import get_code_strategy
from services.backtest.validation import (
    default_backtest_settings,
    validate_strategy_payload,
)


UNIVERSE = ["SPY", "GLD", "NVDA", "MU", "XLE"]
SEVENSTAR_SMALL = ["GLD", "USO", "SPY", "QQQ", "DIA", "IWM", "TLT"]
SEVENSTAR_LARGE = [
    # 商品
    "GLD", "DBB", "SOYB", "USO", "SLV", "PDBC", "COPX",
    # 美国指数与风格
    "SPY", "QQQ", "DIA", "MDY", "IWM", "VUG", "VTV", "USMV", "COWZ", "MAGS",
    # 行业
    "XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLB", "XLRE", "XLK", "XLU",
    # 国际
    "EFA", "EEM", "EWJ", "EWY", "EWH", "KWEB",
    # 主题
    "SOXX", "IBB", "IGV", "UFOX",
    # 债券与加密资产
    "TLT", "CWB", "BTC/USD",
]


def _rule(
    rule_id: str,
    action: str,
    condition: str,
    value: float,
    priority: int,
) -> dict:
    return {
        "id": rule_id,
        "name": rule_id,
        "enabled": True,
        "priority": priority,
        "action": action,
        "sizing_mode": "TARGET",
        "value": value,
        "condition": condition,
        "when": "OPEN",
    }


def shipped_strategy_presets() -> list[tuple[str, dict]]:
    settings = default_backtest_settings()
    code_type = get_code_strategy("rapid_drop_atr_rotation")
    sevenstar_type = get_code_strategy("sevenstar_etf_rotation")
    values = [
        (
            "tested-single-ma10-v1",
            {
                "name": "SPY MA10 单标的测试策略",
                "description": "真实行情回归测试使用的单标的均线策略。",
                "design_mode": "visual",
                "selection_mode": "single",
                "definition": {
                    "symbols": [{"symbol": "SPY", "max_weight": 100}],
                    "rules": [
                        _rule("buy-above-ma10", "BUY", "price > ma(10)", 100, 10),
                        _rule("sell-below-ma10", "SELL", "price < ma(10)", 0, 20),
                    ],
                },
                "default_settings": settings,
            },
        ),
        (
            "tested-distribution-ma5-v1",
            {
                "name": "SPY GLD MA5 独立分配测试策略",
                "description": "真实行情回归测试使用的双标的独立分配策略。",
                "design_mode": "visual",
                "selection_mode": "distribution",
                "definition": {
                    "symbols": [
                        {"symbol": "SPY", "max_weight": 50},
                        {"symbol": "GLD", "max_weight": 50},
                    ],
                    "rules": [
                        _rule("buy-above-ma5", "BUY", "price > ma(5)", 50, 10),
                        _rule("sell-below-ma5", "SELL", "price < ma(5)", 0, 20),
                    ],
                },
                "default_settings": settings,
            },
        ),
        (
            "tested-competition-atr-v1",
            {
                "name": "五标的 ATR 竞争测试策略",
                "description": "真实行情回归测试使用的五标的 ATR 动量竞争策略。",
                "design_mode": "visual",
                "selection_mode": "competition",
                "definition": {
                    "symbols": [
                        {"symbol": symbol, "max_weight": 100}
                        for symbol in UNIVERSE
                    ],
                    "rules": [
                        _rule("risk-exit", "SELL", "price < ma(10)", 0, 10),
                    ],
                    "competition": {
                        "eligibility": "price > ma(5)",
                        "score": "(price - close(5)) / atr(5)",
                        "target_weight": 100,
                        "cash_when_none": True,
                        "when": "OPEN",
                    },
                },
                "default_settings": settings,
            },
        ),
        (
            "builtin-rapid-drop-atr-rotation-v1",
            {
                "name": "急跌回避与ATR动量轮动策略",
                "description": code_type.description,
                "design_mode": "code",
                "selection_mode": "competition",
                "code_key": code_type.key,
                "code_version": code_type.version,
                "definition": {
                    "symbols": deepcopy(code_type.default_symbols),
                    "params": code_type.validate_params({}),
                },
                "default_settings": settings,
            },
        ),
        (
            "builtin-sevenstar-etf-rotation-small-v1",
            {
                "name": "七星ETF轮动策略（小池）",
                "description": sevenstar_type.description + " 小池覆盖商品、宽基、成长、小盘与长债。",
                "design_mode": "code",
                "selection_mode": "competition",
                "code_key": sevenstar_type.key,
                "code_version": sevenstar_type.version,
                "definition": {
                    "symbols": [
                        {"symbol": symbol, "max_weight": 100}
                        for symbol in SEVENSTAR_SMALL
                    ],
                    "params": sevenstar_type.validate_params({}),
                },
                "default_settings": settings,
            },
        ),
        (
            "builtin-sevenstar-etf-rotation-large-v1",
            {
                "name": "七星ETF轮动策略（大池）",
                "description": sevenstar_type.description + " 大池跨商品、风格、行业、国际、主题、债券与 BTC。",
                "design_mode": "code",
                "selection_mode": "competition",
                "code_key": sevenstar_type.key,
                "code_version": sevenstar_type.version,
                "definition": {
                    "symbols": [
                        {"symbol": symbol, "max_weight": 100}
                        for symbol in SEVENSTAR_LARGE
                    ],
                    "params": sevenstar_type.validate_params({}),
                },
                "default_settings": settings,
            },
        ),
    ]
    return [
        (seed_key, validate_strategy_payload(payload, creating=True))
        for seed_key, payload in values
    ]


def ensure_shipped_strategy_presets() -> None:
    for seed_key, payload in shipped_strategy_presets():
        backtest_repository.seed_strategy_once(seed_key, payload)
        if payload.get("code_key") == "sevenstar_etf_rotation":
            backtest_repository.upgrade_seeded_strategy_code_version_once(
                seed_key,
                "sevenstar-effective-w2-r2-v1.0.1",
                code_key="sevenstar_etf_rotation",
                from_versions=("1.0.0",),
                to_version="1.0.1",
            )
            backtest_repository.upgrade_seeded_strategy_code_version_once(
                seed_key,
                "sevenstar-formula-mode-v1.1.0",
                code_key="sevenstar_etf_rotation",
                from_versions=("1.0.0", "1.0.1"),
                to_version=payload["code_version"],
                parameter_defaults={"trend_formula_mode": "consistent_w2"},
            )
        if payload.get("code_key") == "rapid_drop_atr_rotation":
            backtest_repository.upgrade_seeded_strategy_code_version_once(
                seed_key,
                "rapid-drop-multihold-atr-filter-logs-v1.1.0",
                code_key="rapid_drop_atr_rotation",
                from_versions=("1.0.0",),
                to_version="1.1.0",
            )
            backtest_repository.upgrade_seeded_strategy_code_version_once(
                seed_key,
                "rapid-drop-atr-weighting-v1.2.0",
                code_key="rapid_drop_atr_rotation",
                from_versions=("1.0.0", "1.1.0"),
                to_version=payload["code_version"],
            )
        backtest_repository.upgrade_seeded_strategy_settings_once(
            seed_key,
            "runtime-defaults-20260731-v2",
            {
                field: payload["default_settings"][field]
                for field in (
                    "end_date",
                    "commission_per_share",
                    "minimum_commission",
                    "risk_free_rate",
                )
            },
        )
