from __future__ import annotations

from database import backtest_repository, realtime_repository
from services.backtest.presets import SEVENSTAR_SMALL
from services.realtime_panel_script import generate_panel_settings
from services.realtime_config import empty_recommendation_state, realtime_settings_from


SEVENSTAR_SMALL_TASK_SEED = "builtin-sevenstar-small-realtime-v1"


def _symbols(strategy: dict) -> list[str]:
    return [
        str(item.get("symbol") or "").upper()
        for item in (strategy.get("definition") or {}).get("symbols", [])
    ]


def ensure_shipped_realtime_tasks() -> dict | None:
    """Install exactly one stopped SevenStar small-pool realtime task."""
    for task in realtime_repository.list_tasks():
        if task.get("panel_settings"):
            continue
        realtime_repository.update_panel_settings(
            task["id"], generate_panel_settings(task["strategy_snapshot"])
        )
    strategy = next(
        (
            item for item in backtest_repository.list_strategies()
            if item.get("code_key") == "sevenstar_etf_rotation"
            and _symbols(item) == SEVENSTAR_SMALL
        ),
        None,
    )
    if strategy is None:
        return None
    for task in realtime_repository.list_tasks(include_deleted=True):
        snapshot = task.get("strategy_snapshot") or {}
        if snapshot.get("code_key") == "sevenstar_etf_rotation" and _symbols(snapshot) == SEVENSTAR_SMALL:
            realtime_repository.claim_task_seed(SEVENSTAR_SMALL_TASK_SEED, task["id"])
            return task
    name = "七星ETF轮动实时决策（小池）"
    existing_names = {
        task["name"] for task in realtime_repository.list_tasks(include_deleted=True)
    }
    if name in existing_names:
        name += "（内置）"
    settings = realtime_settings_from(strategy.get("default_settings"))
    return realtime_repository.seed_task_once(
        SEVENSTAR_SMALL_TASK_SEED,
        name=name,
        strategy=strategy,
        follow_strategy=True,
        settings=settings,
        notification_settings={"enabled": False},
        portfolio_state=empty_recommendation_state(),
        panel_settings=generate_panel_settings(strategy),
    )
