"""聚宽版 WTME V2：对应项目 RapidDropWtmeRotationStrategy 2.0.0。

聚宽普通基金回测不能为每只 ETF 设置项目式动态杠杆层，因此这里把
VOLAT 动态杠杆 1.0～5.0 映射为 20%～100% 的现金仓位利用率：
平台目标仓位 = 项目目标仓位 × 动态杠杆 ÷ 5。
"""

import builtins
import math

import numpy as np
from jqdata import *


SMALL_ETF_POOL = [
    "518880.XSHG",   #黄金
    "161226.XSHE",   #白银
    "501018.XSHG",   #原油
    # "159985.XSHE",   #豆粕
    # "513870.XSHG",   #纳指
    "513100.XSHG",   #纳指
    # "588000.XSHG",   #科创50
    "159915.XSHE",    #创业板
    "511220.XSHG"    #城投债
]
LARGE_ETF_POOL = [
    "518880.XSHG", "159980.XSHE", "159985.XSHE", "501018.XSHG",
    "161226.XSHE", "159981.XSHE", "513100.XSHG", "159509.XSHE",
    "513290.XSHG", "513500.XSHG", "159529.XSHE", "513400.XSHG",
    "513520.XSHG", "513030.XSHG", "513080.XSHG", "513310.XSHG",
    "513730.XSHG", "159792.XSHE", "513130.XSHG", "513050.XSHG",
    "159920.XSHE", "513690.XSHG", "510300.XSHG", "510500.XSHG",
    "510050.XSHG", "510210.XSHG", "159915.XSHE", "588080.XSHG",
    "512100.XSHG", "563360.XSHG", "563300.XSHG", "512890.XSHG",
    "159967.XSHE", "512040.XSHG", "159201.XSHE", "511380.XSHG",
    "511010.XSHG", "511220.XSHG",
]
ETF_POOL = SMALL_ETF_POOL
BENCHMARK = "510300.XSHG"

# 项目 2.0.0 策略参数。
ENABLE_PERCENT_DROP_FILTER = True
DROP_THRESHOLD_PERCENT = 5.0
DROP_LOOKBACK_SESSIONS = 5
RISK_CHECK_TIME = "09:50"
WTME_PERIOD = 13
WTME_HALF_LIFE = 6.0
WTME_EPSILON = 1e-8
SELECTION_TIME = "10:00"
BUY_TOP_N = 1
BUY_CONDITION_OPERATOR = "and"       # "and" / "or"
BUY_SCORE_THRESHOLD = -15.0
MAX_SIMULTANEOUS_HOLDINGS = 1
ALLOCATION_MODE = "equal"           # equal / linear_rank / leveraged_equal / leveraged_linear_rank
ENABLE_UPSIDE_SELL_PROTECTION = False

# 项目运行设置中的 VOLAT 动态杠杆。普通现金回测按上限 5 归一化。
ENABLE_VOLAT_DYNAMIC_LEVERAGE = True
VOLATILITY_PERIOD = 15
STRESS_DAYS = 10
MAX_LOSS_PERCENT = 40.0
MAX_DYNAMIC_LEVERAGE = 5.0
REBALANCE_ON_DYNAMIC_LEVERAGE_CHANGE = False

SLIPPAGE_RATE = 0.0001
OPEN_COMMISSION = 0.0002
CLOSE_COMMISSION = 0.0002
MIN_COMMISSION = 5.0


def initialize(context):
    _validate_parameters()
    set_option("avoid_future_data", True)
    set_option("use_real_price", True)
    set_benchmark(BENCHMARK)
    set_slippage(PriceRelatedSlippage(SLIPPAGE_RATE), type="fund")
    set_order_cost(OrderCost(
        open_tax=0, close_tax=0,
        open_commission=OPEN_COMMISSION,
        close_commission=CLOSE_COMMISSION,
        close_today_commission=0,
        min_commission=MIN_COMMISSION,
    ), type="fund")
    log.set_level("order", "error")
    log.set_level("system", "error")
    log.set_level("strategy", "info")
    g.etf_pool = list(ETF_POOL)
    g.risk_off_date = None
    g.risk_off = set()
    g.last_targets = tuple()
    g.last_dynamic_leverages = {}
    run_daily(risk_check, time=RISK_CHECK_TIME, reference_security=BENCHMARK)
    run_daily(select_and_rotate, time=SELECTION_TIME, reference_security=BENCHMARK)


def _validate_parameters():
    if not ETF_POOL or len(set(ETF_POOL)) != len(ETF_POOL):
        raise ValueError("ETF_POOL 不能为空且不能包含重复代码")
    if WTME_PERIOD < 2 or WTME_HALF_LIFE <= 0 or WTME_EPSILON <= 0:
        raise ValueError("WTME 参数无效")
    if DROP_LOOKBACK_SESSIONS < 1 or DROP_THRESHOLD_PERCENT <= 0:
        raise ValueError("急跌参数无效")
    if RISK_CHECK_TIME >= SELECTION_TIME:
        raise ValueError("风险检查时间必须早于轮动时间")
    if BUY_TOP_N < 1 or BUY_TOP_N > len(ETF_POOL):
        raise ValueError("BUY_TOP_N 超出候选池")
    if MAX_SIMULTANEOUS_HOLDINGS < 1 or MAX_SIMULTANEOUS_HOLDINGS > len(ETF_POOL):
        raise ValueError("MAX_SIMULTANEOUS_HOLDINGS 超出候选池")
    if BUY_CONDITION_OPERATOR not in ("and", "or"):
        raise ValueError("BUY_CONDITION_OPERATOR 只能是 and 或 or")
    if ALLOCATION_MODE not in ("equal", "linear_rank", "leveraged_equal", "leveraged_linear_rank"):
        raise ValueError("ALLOCATION_MODE 无效")
    if VOLATILITY_PERIOD < 2 or STRESS_DAYS < 1:
        raise ValueError("VOLAT 动态杠杆参数无效")
    if MAX_LOSS_PERCENT <= 0 or MAX_DYNAMIC_LEVERAGE < 1:
        raise ValueError("VOLAT 动态杠杆边界无效")


def _is_finite_positive(value):
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _signal_prices(securities):
    result = {}
    current = get_current_data()
    try:
        frame = history(1, unit="1m", field="close", security_list=list(securities),
                        df=True, skip_paused=False, fq="pre")
    except Exception as exc:
        log.warning("读取上一完整分钟价格失败：%s" % exc)
        return result
    for security in securities:
        try:
            if current[security].paused:
                continue
            value = frame[security].iloc[-1]
            if _is_finite_positive(value):
                result[security] = float(value)
        except Exception:
            continue
    return result


def _daily_history(security, count):
    try:
        frame = attribute_history(
            security, count, unit="1d", fields=["open", "high", "low", "close"],
            skip_paused=True, df=True, fq="pre",
        )
    except Exception as exc:
        log.warning("%s 日线读取失败：%s" % (security, exc))
        return None
    if frame is None or len(frame) < count:
        return None
    values = frame[["open", "high", "low", "close"]].values
    if not np.all(np.isfinite(values)) or np.any(values <= 0):
        return None
    return frame


def _position_amount(context, security):
    position = context.portfolio.positions.get(security)
    return float(position.total_amount) if position is not None else 0.0


def _held_symbols(context):
    return set(security for security in g.etf_pool if _position_amount(context, security) > 0)


def _submit_target_value(context, security, target_value, reason):
    data = get_current_data()[security]
    if data.paused or not _is_finite_positive(data.last_price):
        log.info("跳过 %s：停牌或无有效价格（%s）" % (security, reason))
        return None
    current_value = _position_amount(context, security) * float(data.last_price)
    if target_value > current_value and data.last_price >= data.high_limit:
        log.info("跳过买入 %s：已涨停" % security)
        return None
    if target_value < current_value and data.last_price <= data.low_limit:
        log.info("跳过卖出 %s：已跌停" % security)
        return None
    result = order_target_value(security, target_value)
    if result is not None:
        log.info("目标市值 %.2f，%s，原因：%s" % (target_value, security, reason))
    return result


def _calculate_wtme(frame, current_price):
    previous_close = float(frame["close"].iloc[-1])
    highs = list(frame["high"].astype(float).values) + [max(previous_close, current_price)]
    lows = list(frame["low"].astype(float).values) + [min(previous_close, current_price)]
    closes = list(frame["close"].astype(float).values) + [current_price]
    raw = [2 ** (-(WTME_PERIOD - 1 - index) / WTME_HALF_LIFE) for index in range(WTME_PERIOD)]
    total = sum(raw)
    weighted_return = 0.0
    weighted_range = 0.0
    for index in range(1, len(closes)):
        prior = closes[index - 1]
        tr = max(highs[index] - lows[index], abs(highs[index] - prior), abs(lows[index] - prior))
        weight = raw[index - 1] / total
        weighted_return += weight * ((closes[index] - prior) / prior)
        weighted_range += weight * (tr / prior)
    return 100.0 * weighted_return / (weighted_range + WTME_EPSILON)


def _dynamic_leverage(security, current_price):
    if not ENABLE_VOLAT_DYNAMIC_LEVERAGE:
        return 1.0
    frame = _daily_history(security, VOLATILITY_PERIOD)
    if frame is None:
        return None
    closes = list(frame["close"].astype(float).values)
    returns = [math.log(closes[index] / closes[index - 1]) for index in range(1, len(closes))]
    returns.append(math.log(float(current_price) / closes[-1]))
    if len(returns) != VOLATILITY_PERIOD:
        return None
    volatility = float(np.std(returns, ddof=1)) * math.sqrt(252) * 100
    stress_loss = volatility * 3 * math.sqrt(float(STRESS_DAYS) / 252)
    raw = MAX_DYNAMIC_LEVERAGE if stress_loss == 0 else MAX_LOSS_PERCENT / stress_loss
    bounded = min(MAX_DYNAMIC_LEVERAGE, max(1.0, raw))
    return max(1.0, math.floor((bounded + 1e-12) * 10) / 10)


def _target_weights(selected):
    count = len(selected)
    if count == 0:
        return {}
    if ALLOCATION_MODE in ("linear_rank", "leveraged_linear_rank"):
        denominator = count * (count + 1) / 2.0
        return {security: 100.0 * (count - index) / denominator
                for index, security in enumerate(selected)}
    return {security: 100.0 / count for security in selected}


def _platform_target_percent(base_percent, leverage, selected_count):
    strategy_leverage = selected_count if ALLOCATION_MODE in ("leveraged_equal", "leveraged_linear_rank") else 1
    return base_percent * strategy_leverage * leverage / MAX_DYNAMIC_LEVERAGE


def risk_check(context):
    today = context.current_dt.date()
    prices = _signal_prices(g.etf_pool)
    flagged = set()
    threshold = -DROP_THRESHOLD_PERCENT / 100.0
    for security in g.etf_pool:
        price = prices.get(security)
        frame = _daily_history(security, DROP_LOOKBACK_SESSIONS)
        triggered = price is None or frame is None
        changes = []
        if not triggered:
            closes = list(frame["close"].astype(float).values)
            changes = [current / previous - 1 for previous, current in zip(closes, closes[1:] + [price])]
            triggered = bool(ENABLE_PERCENT_DROP_FILTER and builtins.any(value <= threshold for value in changes))
        if triggered:
            flagged.add(security)
            if _position_amount(context, security) > 0:
                _submit_target_value(context, security, 0, "%s 百分比急跌检查命中" % RISK_CHECK_TIME)
    g.risk_off_date = today
    g.risk_off = flagged


def select_and_rotate(context):
    today = context.current_dt.date()
    flagged = g.risk_off if g.risk_off_date == today else set()
    prices = _signal_prices(g.etf_pool)
    ranked = []
    leverages = {}
    frames = {}
    for security in g.etf_pool:
        price = prices.get(security)
        frame = _daily_history(security, max(WTME_PERIOD, VOLATILITY_PERIOD))
        if price is None or frame is None or security in flagged:
            continue
        wtme_frame = frame.iloc[-WTME_PERIOD:]
        score = _calculate_wtme(wtme_frame, price)
        leverage = _dynamic_leverage(security, price)
        if leverage is None or not math.isfinite(score):
            continue
        ranked.append((score, security))
        leverages[security] = leverage
        frames[security] = frame
    ranked.sort(key=lambda item: (-item[0], item[1]))
    candidates = []
    for index, item in enumerate(ranked):
        score, security = item
        rank_ok = index + 1 <= BUY_TOP_N
        score_ok = score > BUY_SCORE_THRESHOLD
        accepted = rank_ok and score_ok if BUY_CONDITION_OPERATOR == "and" else rank_ok or score_ok
        if accepted:
            candidates.append(security)
    selected = candidates[:MAX_SIMULTANEOUS_HOLDINGS]
    weights = _target_weights(selected)

    held = _held_symbols(context)
    protected = set()
    for security in sorted(held - set(selected)):
        protect = False
        if ENABLE_UPSIDE_SELL_PROTECTION and security in prices:
            frame = frames.get(security)
            if frame is None:
                frame = _daily_history(security, 1)
            protect = frame is not None and prices[security] > float(frame["close"].iloc[-1])
        if protect:
            protected.add(security)
        else:
            _submit_target_value(context, security, 0, "%s WTME 轮动清仓" % SELECTION_TIME)

    slots = max(0, MAX_SIMULTANEOUS_HOLDINGS - len(protected))
    active = []
    for security in selected:
        if security in held:
            active.append(security)
        elif slots > 0:
            active.append(security)
            slots -= 1
    current_order = tuple(active)
    dynamic_changed = builtins.any(
        abs(leverages.get(security, 1) - g.last_dynamic_leverages.get(security, leverages.get(security, 1))) > 1e-12
        for security in active
    )
    rank_sensitive = ALLOCATION_MODE in ("linear_rank", "leveraged_linear_rank")
    should_rebalance = set(current_order) != set(g.last_targets) or (rank_sensitive and current_order != g.last_targets)
    should_rebalance = should_rebalance or (REBALANCE_ON_DYNAMIC_LEVERAGE_CHANGE and dynamic_changed)
    if should_rebalance:
        total_value = float(context.portfolio.total_value)
        for security in active:
            target_percent = _platform_target_percent(weights[security], leverages[security], len(selected))
            _submit_target_value(context, security, total_value * target_percent / 100.0,
                                 "%s WTME V2 目标 %.4g%%（动态杠杆 %.1fx/%.1fx）" %
                                 (SELECTION_TIME, target_percent, leverages[security], MAX_DYNAMIC_LEVERAGE))
    g.last_targets = current_order
    g.last_dynamic_leverages = dict(leverages)
