"""聚宽版：急跌回避与相对 ATR 动量轮动（对应项目策略 v1.3.0）。

这里的 RATR 排名分数是“N 日价格位移 / 策略 ATR”。直接复制本文件到聚宽
策略编辑器并使用分钟级回测；行情、成交、整手、涨跌停和费用使用聚宽语义。
"""

import builtins
import math

import numpy as np
from jqdata import *


# ==================== 用户参数区 ====================

SMALL_ETF_POOL = [
    "518880.XSHG",   # 黄金ETF
    "159985.XSHE",   # 豆粕ETF
    "501018.XSHG",   # 南方原油
    "161226.XSHE",   # 白银LOF
    "513100.XSHG",   # 纳指ETF
    "159915.XSHE",   # 创业板ETF
    "511220.XSHG",   # 城投债ETF
]

# haha.py 中的大池。默认不启用；将 ETF_POOL 下一行改为 LARGE_ETF_POOL 即可。
LARGE_ETF_POOL = [
    # 大宗商品 ETF
    "518880.XSHG", "159980.XSHE", "159985.XSHE", "501018.XSHG",
    "161226.XSHE", "159981.XSHE",
    # 国际 ETF
    "513100.XSHG", "159509.XSHE", "513290.XSHG", "513500.XSHG",
    "159529.XSHE", "513400.XSHG", "513520.XSHG", "513030.XSHG",
    "513080.XSHG", "513310.XSHG", "513730.XSHG",
    # 香港 ETF
    "159792.XSHE", "513130.XSHG", "513050.XSHG", "159920.XSHE",
    "513690.XSHG",
    # 指数 ETF
    "510300.XSHG", "510500.XSHG", "510050.XSHG", "510210.XSHG",
    "159915.XSHE", "588080.XSHG", "512100.XSHG", "563360.XSHG",
    "563300.XSHG",
    # 风格 ETF
    "512890.XSHG", "159967.XSHE", "512040.XSHG", "159201.XSHE",
    # 债券 ETF
    "511380.XSHG", "511010.XSHG", "511220.XSHG",
]

ETF_POOL = SMALL_ETF_POOL
# ETF_POOL = LARGE_ETF_POOL

BENCHMARK = "510300.XSHG"

# 当前项目 RapidDropAtrRotationStrategy v1.3.0 的参数。
HOLDINGS_NUM = 1
ENABLE_PERCENT_DROP_FILTER = True
DROP_THRESHOLD_PERCENT = 5.0
ENABLE_ATR_DROP_FILTER = False
DROP_THRESHOLD_ATR = 2.0
DROP_LOOKBACK_SESSIONS = 5
RISK_CHECK_TIME = "09:50"
SELECTION_TIME = "10:00"
MOMENTUM_LOOKBACK_SESSIONS = 5
ATR_PERIOD = 5
# 可选值："wilder"、"ema"、"linear"、"simple"。
ATR_WEIGHTING = "wilder"
TARGET_WEIGHT = 100.0

# Wilder/EMA 是递推值。聚宽每次按当日动态前复权，不能跨日缓存历史行情；默认请求
# 250 根完整日线作为稳定预热，但新上市标的只要达到策略最低窗口也可以参与。
ATR_WARMUP_SESSIONS = 250

# 聚宽平台交易设置；不使用杠杆，也不自行模拟碎股或整手规则。
SLIPPAGE_RATE = 0.0001
OPEN_COMMISSION = 0.0002
CLOSE_COMMISSION = 0.0002
MIN_COMMISSION = 5.0


# ==================== 初始化与调度 ====================

def initialize(context):
    _validate_parameters()
    set_option("avoid_future_data", True)
    set_option("use_real_price", True)
    set_benchmark(BENCHMARK)
    set_slippage(PriceRelatedSlippage(SLIPPAGE_RATE), type="fund")
    set_order_cost(
        OrderCost(
            open_tax=0,
            close_tax=0,
            open_commission=OPEN_COMMISSION,
            close_commission=CLOSE_COMMISSION,
            close_today_commission=0,
            min_commission=MIN_COMMISSION,
        ),
        type="fund",
    )
    log.set_level("order", "error")
    log.set_level("system", "error")
    log.set_level("strategy", "info")

    g.etf_pool = list(ETF_POOL)
    g.risk_off_date = None
    g.risk_off = set()

    run_daily(risk_check, time=RISK_CHECK_TIME, reference_security=BENCHMARK)
    run_daily(select_and_rotate, time=SELECTION_TIME, reference_security=BENCHMARK)
    log.info(
        "RATR 策略初始化：候选 %d 只，持仓 %d 只，ATR(%d, %s)"
        % (len(g.etf_pool), HOLDINGS_NUM, ATR_PERIOD, ATR_WEIGHTING)
    )


def _validate_parameters():
    if not ETF_POOL or len(set(ETF_POOL)) != len(ETF_POOL):
        raise ValueError("ETF_POOL 不能为空且不能包含重复代码")
    if not 1 <= HOLDINGS_NUM <= len(ETF_POOL):
        raise ValueError("HOLDINGS_NUM 必须位于 1 与候选池数量之间")
    if DROP_LOOKBACK_SESSIONS < 1 or DROP_THRESHOLD_PERCENT <= 0:
        raise ValueError("百分比急跌参数无效")
    if not isinstance(ENABLE_PERCENT_DROP_FILTER, bool):
        raise ValueError("ENABLE_PERCENT_DROP_FILTER 必须为 True 或 False")
    if not isinstance(ENABLE_ATR_DROP_FILTER, bool):
        raise ValueError("ENABLE_ATR_DROP_FILTER 必须为 True 或 False")
    if DROP_THRESHOLD_ATR <= 0 or ATR_PERIOD < 2:
        raise ValueError("ATR 周期和急跌倍数必须为正")
    if ATR_WEIGHTING not in {"wilder", "ema", "linear", "simple"}:
        raise ValueError("ATR_WEIGHTING 取值不支持")
    if MOMENTUM_LOOKBACK_SESSIONS < 2:
        raise ValueError("MOMENTUM_LOOKBACK_SESSIONS 必须至少为 2")
    if ATR_WARMUP_SESSIONS < ATR_PERIOD + 1:
        raise ValueError("ATR_WARMUP_SESSIONS 不能小于 ATR_PERIOD + 1")
    if RISK_CHECK_TIME >= SELECTION_TIME:
        raise ValueError("RISK_CHECK_TIME 必须早于 SELECTION_TIME")
    if not 0 < TARGET_WEIGHT <= 100:
        raise ValueError("TARGET_WEIGHT 必须位于 (0, 100] 区间")


# ==================== 聚宽数据与下单适配 ====================

def _is_finite_positive(value):
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _signal_prices(securities):
    securities = list(securities)
    result = {}
    if not securities:
        return result
    current = get_current_data()
    try:
        frame = history(
            1,
            unit="1m",
            field="close",
            security_list=securities,
            df=True,
            skip_paused=False,
            fq="pre",
        )
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


def _daily_history(security, requested_count, minimum_count):
    try:
        frame = attribute_history(
            security,
            requested_count,
            unit="1d",
            fields=["high", "low", "close"],
            skip_paused=True,
            df=True,
            fq="pre",
        )
    except Exception as exc:
        log.warning("%s 日线读取失败：%s" % (security, exc))
        return None
    if frame is None or len(frame) < minimum_count:
        return None
    values = frame[["high", "low", "close"]].values
    if not np.all(np.isfinite(values)) or np.any(values <= 0):
        return None
    return frame


def _position_amount(context, security):
    position = context.portfolio.positions.get(security)
    return float(position.total_amount) if position is not None else 0.0


def _submit_target_value(context, security, target_value, reason):
    data = get_current_data()[security]
    if data.paused or not _is_finite_positive(data.last_price):
        log.info("跳过 %s：停牌或无有效价格（%s）" % (security, reason))
        return None
    current_value = _position_amount(context, security) * float(data.last_price)
    if target_value > current_value and data.last_price >= data.high_limit:
        log.info("跳过买入 %s：已涨停（%s）" % (security, reason))
        return None
    if target_value < current_value and data.last_price <= data.low_limit:
        log.info("跳过卖出 %s：已跌停（%s）" % (security, reason))
        return None
    order_result = order_target_value(security, target_value)
    if order_result is not None:
        log.info("目标市值 %.2f：%s，原因：%s" % (target_value, security, reason))
    return order_result


# ==================== ATR 与策略逻辑 ====================

def _atr_series(frame, period, weighting):
    """复刻项目策略的无未来 ATR 序列，结果索引与日线索引一一对应。"""
    highs = list(frame["high"].astype(float).values)
    lows = list(frame["low"].astype(float).values)
    closes = list(frame["close"].astype(float).values)
    result = [None] * len(closes)
    true_ranges = [
        max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        )
        for index in range(1, len(closes))
    ]
    if len(true_ranges) < period:
        return result

    if weighting in {"simple", "linear"}:
        linear_denominator = period * (period + 1) / 2.0
        for row_index in range(period, len(closes)):
            window = true_ranges[row_index - period:row_index]
            if weighting == "simple":
                result[row_index] = sum(window) / period
            else:
                result[row_index] = sum(
                    weight * true_range
                    for weight, true_range in enumerate(window, start=1)
                ) / linear_denominator
        return result

    atr = sum(true_ranges[:period]) / period
    result[period] = atr
    alpha = 1.0 / period if weighting == "wilder" else 2.0 / (period + 1)
    for row_index, true_range in enumerate(
        true_ranges[period:], start=period + 1
    ):
        atr = alpha * true_range + (1 - alpha) * atr
        result[row_index] = atr
    return result


def risk_check(context):
    today = context.current_dt.date()
    prices = _signal_prices(g.etf_pool)
    flagged = set()

    if ENABLE_ATR_DROP_FILTER:
        minimum_count = DROP_LOOKBACK_SESSIONS + ATR_PERIOD + 1
        requested_count = max(ATR_WARMUP_SESSIONS, minimum_count)
    else:
        minimum_count = DROP_LOOKBACK_SESSIONS
        requested_count = minimum_count

    for security in g.etf_pool:
        price = prices.get(security)
        frame = _daily_history(security, requested_count, minimum_count)
        if price is None or frame is None:
            flagged.add(security)
            log.info("%s 风险检查：分钟价或历史数据不足，当日排除" % security)
            continue

        closes = list(frame["close"].astype(float).values)
        start = len(closes) - DROP_LOOKBACK_SESSIONS
        previous_closes = closes[start:]
        current_prices = previous_closes[1:] + [price]
        percent_changes = [
            current_value / previous_close - 1
            for previous_close, current_value
            in zip(previous_closes, current_prices)
        ]

        # 显式使用 Python 内置 any，避免 jqdata 星号导入覆盖 any 后把生成器
        # 对象本身误判为 True。
        percent_triggered = bool(
            ENABLE_PERCENT_DROP_FILTER
            and builtins.any(
                value <= -DROP_THRESHOLD_PERCENT / 100.0
                for value in percent_changes
            )
        )
        atr_changes = []
        atr_triggered = False
        if ENABLE_ATR_DROP_FILTER:
            atr_values = _atr_series(frame, ATR_PERIOD, ATR_WEIGHTING)
            for row_index, (previous_close, current_value) in enumerate(
                zip(previous_closes, current_prices), start=start
            ):
                atr_value = atr_values[row_index]
                if atr_value is None or atr_value <= 0:
                    continue
                atr_changes.append((current_value - previous_close) / atr_value)
            atr_triggered = builtins.any(
                value <= -DROP_THRESHOLD_ATR for value in atr_changes
            )

        if percent_triggered or atr_triggered:
            flagged.add(security)
            if _position_amount(context, security) > 0:
                _submit_target_value(
                    context,
                    security,
                    0,
                    "%s 急跌检查命中，当日回避" % RISK_CHECK_TIME,
                )

        reasons = []
        if percent_triggered:
            reasons.append("百分比单日急跌")
        if atr_triggered:
            reasons.append("ATR 单日急跌")
        message = "、".join(reasons) if reasons else "通过"
        log.info(
            "%s 风险检查：%s；最差涨跌 %.2f%%%s"
            % (
                security,
                message,
                min(percent_changes) * 100,
                (
                    "；最差 ATR 变化 %.4f" % min(atr_changes)
                    if atr_changes else ""
                ),
            )
        )

    g.risk_off_date = today
    g.risk_off = flagged


def select_and_rotate(context):
    today = context.current_dt.date()
    flagged = g.risk_off if g.risk_off_date == today else set()
    prices = _signal_prices(g.etf_pool)
    minimum_count = max(MOMENTUM_LOOKBACK_SESSIONS, ATR_PERIOD + 1)
    requested_count = max(ATR_WARMUP_SESSIONS, minimum_count)
    eligible_scores = []

    for security in g.etf_pool:
        price = prices.get(security)
        frame = _daily_history(security, requested_count, minimum_count)
        if price is None or frame is None:
            log.info("%s RATR：分钟价或历史数据不足，排除" % security)
            continue
        atr_values = _atr_series(frame, ATR_PERIOD, ATR_WEIGHTING)
        atr = atr_values[-1]
        if atr is None or atr <= 0:
            log.info("%s RATR：ATR 不可计算，排除" % security)
            continue
        base = float(frame["close"].iloc[-MOMENTUM_LOOKBACK_SESSIONS])
        score = (price - base) / atr
        filtered = security in flagged
        if not filtered:
            eligible_scores.append((float(score), security))
        log.info(
            "%s RATR=%.8f，位移=%.6f，ATR=%.6f，%s"
            % (
                security,
                score,
                price - base,
                atr,
                "急跌过滤" if filtered else "合格",
            )
        )

    # 当前项目按“分数降序、代码升序”决定完全同分时的顺序。
    eligible_scores.sort(key=lambda item: (-item[0], item[1]))
    targets = [
        security for _score, security in eligible_scores[:HOLDINGS_NUM]
    ]
    target_set = set(targets)

    for security in g.etf_pool:
        if security not in target_set and _position_amount(context, security) > 0:
            _submit_target_value(
                context,
                security,
                0,
                "%s RATR 轮动，目标为 %s"
                % (SELECTION_TIME, "、".join(targets) if targets else "现金"),
            )

    # 即使合格标的少于 HOLDINGS_NUM，每只仍使用 TARGET_WEIGHT/HOLDINGS_NUM；
    # 这是当前项目 v1.3.0 的精确仓位语义，剩余资金留作现金。
    per_target_weight = TARGET_WEIGHT / HOLDINGS_NUM
    for security in targets:
        if _position_amount(context, security) > 0:
            continue
        target_value = context.portfolio.total_value * per_target_weight / 100.0
        _submit_target_value(
            context,
            security,
            target_value,
            "%s RATR 前 %d 名等权" % (SELECTION_TIME, HOLDINGS_NUM),
        )

    if not targets:
        log.info("无合格 RATR 标的，保持现金")
