"""聚宽版：七星 ETF 轮动（项目策略 v1.1.0 + A 股溢价适配）。

直接复制本文件到聚宽策略编辑器并使用分钟级回测。默认公式是项目当前的
consistent_w2；legacy_v1 仅用于复现项目 v1.0.0 的历史混合权重结果。
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
    "518880.XSHG",  # 黄金 ETF
    "159980.XSHE",  # 有色 ETF
    "159985.XSHE",  # 豆粕 ETF
    "501018.XSHG",  # 南方原油
    "161226.XSHE",  # 白银 LOF
    "159981.XSHE",  # 能源化工 ETF
    # 国际 ETF
    "513100.XSHG",  # 纳指 ETF
    "159509.XSHE",  # 纳指科技 ETF
    "513290.XSHG",  # 纳指生物 ETF
    "513500.XSHG",  # 标普 500 ETF
    "159529.XSHE",  # 标普消费
    "513400.XSHG",  # 道琼斯 ETF
    "513520.XSHG",  # 日经 225 ETF
    "513030.XSHG",  # 德国 30 ETF
    "513080.XSHG",  # 法国 ETF
    "513310.XSHG",  # 中韩半导体 ETF
    "513730.XSHG",  # 东南亚 ETF
    # 香港 ETF
    "159792.XSHE",  # 港股互联 ETF
    "513130.XSHG",  # 恒生科技
    "513050.XSHG",  # 中概互联网 ETF
    "159920.XSHE",  # 恒生 ETF
    "513690.XSHG",  # 港股红利
    # 指数 ETF
    "510300.XSHG",  # 沪深 300 ETF
    "510500.XSHG",  # 中证 500 ETF
    "510050.XSHG",  # 上证 50 ETF
    "510210.XSHG",  # 上证 ETF
    "159915.XSHE",  # 创业板 ETF
    "588080.XSHG",  # 科创 50 ETF
    "512100.XSHG",  # 中证 1000 ETF
    "563360.XSHG",  # A500 ETF
    "563300.XSHG",  # 中证 2000 ETF
    # 风格 ETF
    "512890.XSHG",  # 红利低波 ETF
    "159967.XSHE",  # 创业板成长 ETF
    "512040.XSHG",  # 价值 ETF
    "159201.XSHE",  # 自由现金流 ETF
    # 债券 ETF
    "511380.XSHG",  # 可转债 ETF
    "511010.XSHG",  # 国债 ETF
    "511220.XSHG",  # 城投债 ETF
]

ETF_POOL = SMALL_ETF_POOL
# ETF_POOL = LARGE_ETF_POOL

BENCHMARK = "510300.XSHG"
DEFENSIVE_SYMBOL = "511880.XSHG"  # 银华日利，替代项目美股防御标的 BIL

# 当前项目 SevenStarEtfRotationStrategy v1.1.0 的参数。
TREND_FORMULA_MODE = "consistent_w2"  # 或 "legacy_v1"
LOOKBACK_DAYS = 25
HOLDINGS_NUM = 1
MIN_SCORE_THRESHOLD = 0.0
MAX_SCORE_THRESHOLD = 100.0
REBALANCE_TOLERANCE_PERCENT = 5.0
MINIMUM_TRADE_VALUE = 0.0  # 聚宽币种为人民币；完整清仓不受此参数限制

ENABLE_PROFIT_PROTECTION = True
PROFIT_LOOKBACK_DAYS = 1
PROFIT_DRAWDOWN_PERCENT = 5.0
PROFIT_CHECK_TIME = "11:00"

ENABLE_VOLUME_CHECK = True
VOLUME_LOOKBACK_DAYS = 5
VOLUME_RATIO_THRESHOLD = 2.0
VOLUME_RETURN_LIMIT_PERCENT = 100.0

ENABLE_SHORT_MOMENTUM_FILTER = True
SHORT_LOOKBACK_DAYS = 10
SHORT_MOMENTUM_THRESHOLD_PERCENT = 0.0
SINGLE_DAY_LOSS_PERCENT = 3.0

SELL_TIME = "14:00"
BUY_TIME = "14:01"

# 当前项目因美股数据源缺少可审计历史 NAV 而移除了原策略的溢价过滤；聚宽可直接
# 提供场内基金净值，因此 A 股版按 haha.py 恢复该平台规则。关闭后即为项目 v1.1.0
# 的过滤集合。阈值 0.20 表示前一交易日场内收盘价较单位净值溢价超过 20%。
ENABLE_PREMIUM_FILTER = True
PREMIUM_THRESHOLD = 0.20

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
    g.rankings_cache_date = None
    g.rankings_cache = []
    g.premium_cache_date = None
    g.premium_cache = {}

    if ENABLE_PROFIT_PROTECTION:
        run_daily(
            profit_protection_check,
            time=PROFIT_CHECK_TIME,
            reference_security=BENCHMARK,
        )
    run_daily(sell_trade, time=SELL_TIME, reference_security=BENCHMARK)
    run_daily(buy_trade, time=BUY_TIME, reference_security=BENCHMARK)

    log.info(
        "七星策略初始化：候选 %d 只，公式 %s，长期 %d 日，持仓 %d 只"
        % (len(g.etf_pool), TREND_FORMULA_MODE, LOOKBACK_DAYS, HOLDINGS_NUM)
    )


def _validate_parameters():
    if not ETF_POOL or len(set(ETF_POOL)) != len(ETF_POOL):
        raise ValueError("ETF_POOL 不能为空且不能包含重复代码")
    if DEFENSIVE_SYMBOL in ETF_POOL:
        raise ValueError("DEFENSIVE_SYMBOL 不能与候选池重复")
    if TREND_FORMULA_MODE not in {"consistent_w2", "legacy_v1"}:
        raise ValueError("TREND_FORMULA_MODE 取值不支持")
    if LOOKBACK_DAYS < 5 or SHORT_LOOKBACK_DAYS < 2:
        raise ValueError("长期/短期动量窗口无效")
    if not 1 <= HOLDINGS_NUM <= min(5, len(ETF_POOL)):
        raise ValueError("HOLDINGS_NUM 必须位于 1 至 5 且不超过候选池数量")
    if MIN_SCORE_THRESHOLD >= MAX_SCORE_THRESHOLD:
        raise ValueError("MIN_SCORE_THRESHOLD 必须严格小于 MAX_SCORE_THRESHOLD")
    if not 0 <= REBALANCE_TOLERANCE_PERCENT <= 25:
        raise ValueError("REBALANCE_TOLERANCE_PERCENT 必须位于 [0, 25]")
    if MINIMUM_TRADE_VALUE < 0:
        raise ValueError("MINIMUM_TRADE_VALUE 不能为负数")
    if PREMIUM_THRESHOLD <= 0:
        raise ValueError("PREMIUM_THRESHOLD 必须为正数")
    for name, value in (
        ("ENABLE_PROFIT_PROTECTION", ENABLE_PROFIT_PROTECTION),
        ("ENABLE_VOLUME_CHECK", ENABLE_VOLUME_CHECK),
        ("ENABLE_SHORT_MOMENTUM_FILTER", ENABLE_SHORT_MOMENTUM_FILTER),
        ("ENABLE_PREMIUM_FILTER", ENABLE_PREMIUM_FILTER),
    ):
        if not isinstance(value, bool):
            raise ValueError("%s 必须为 True 或 False" % name)
    if PROFIT_LOOKBACK_DAYS < 1 or PROFIT_DRAWDOWN_PERCENT <= 0:
        raise ValueError("高点回撤保护参数无效")
    if VOLUME_LOOKBACK_DAYS < 1 or VOLUME_RATIO_THRESHOLD <= 0:
        raise ValueError("成交量过滤参数无效")
    if PROFIT_CHECK_TIME >= SELL_TIME or SELL_TIME >= BUY_TIME:
        raise ValueError("必须满足 PROFIT_CHECK_TIME < SELL_TIME < BUY_TIME")


# ==================== 聚宽数据与下单适配 ====================

def _is_finite_positive(value):
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _signal_prices(securities):
    """取得回调执行前一根完整分钟线收盘价，与项目事件信号价口径一致。"""
    securities = list(dict.fromkeys(securities))
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


def _daily_history(security, count):
    try:
        frame = attribute_history(
            security,
            count,
            unit="1d",
            fields=["open", "high", "low", "close", "volume"],
            skip_paused=True,
            df=True,
            fq="pre",
        )
    except Exception as exc:
        log.warning("%s 日线读取失败：%s" % (security, exc))
        return None
    if frame is None or len(frame) < count:
        return None
    prices = frame[["open", "high", "low", "close"]].values
    volumes = frame["volume"].values
    if (
        not np.all(np.isfinite(prices))
        or np.any(prices <= 0)
        or not np.all(np.isfinite(volumes))
        or np.any(volumes < 0)
    ):
        return None
    return frame


def _completed_session_minute_count(context):
    """A 股日盘截至当前回调前已经完成的分钟数，不包含当前分钟。"""
    minute = context.current_dt.hour * 60 + context.current_dt.minute
    morning_open = 9 * 60 + 30
    morning_close = 11 * 60 + 30
    afternoon_open = 13 * 60
    market_close = 15 * 60
    if minute <= morning_open:
        return 0
    if minute <= morning_close:
        return minute - morning_open
    if minute < afternoon_open:
        return 120
    if minute <= market_close:
        return 120 + minute - afternoon_open
    return 240


def _cumulative_volume_map(context, securities):
    if not ENABLE_VOLUME_CHECK:
        return {}
    count = _completed_session_minute_count(context)
    if count <= 0:
        return {security: 0.0 for security in securities}
    try:
        frame = history(
            count,
            unit="1m",
            field="volume",
            security_list=list(securities),
            df=True,
            skip_paused=False,
            fq="pre",
        )
    except Exception as exc:
        log.warning("读取当日累计分钟成交量失败：%s" % exc)
        return {}
    result = {}
    for security in securities:
        try:
            values = np.asarray(frame[security].values, dtype=float)
            if len(values) == count and np.all(np.isfinite(values)):
                result[security] = float(np.sum(values))
        except Exception:
            continue
    return result


def _position_amount(context, security):
    position = context.portfolio.positions.get(security)
    return float(position.total_amount) if position is not None else 0.0


def _premium_rate(context, security):
    """使用聚宽前一交易日场内收盘价和单位净值计算溢价率。"""
    if not ENABLE_PREMIUM_FILTER:
        return 0.0
    today = context.current_dt.date()
    if g.premium_cache_date != today:
        g.premium_cache_date = today
        g.premium_cache = {}
    if security in g.premium_cache:
        return g.premium_cache[security]

    rate = None
    try:
        trade_days = get_trade_days(end_date=today, count=2)
        if trade_days is None or len(trade_days) < 2:
            g.premium_cache[security] = None
            return None
        previous_date = trade_days[0]
        price_frame = get_price(
            security,
            start_date=previous_date,
            end_date=previous_date,
            frequency="daily",
            fields=["close"],
            # 溢价必须比较同日真实场内价格与同日单位净值，不能使用复权价。
            fq=None,
        )
        if price_frame is None or len(price_frame) == 0:
            g.premium_cache[security] = None
            return None
        market_close = float(price_frame["close"].iloc[-1])

        net_value = None
        net_frame = get_extras(
            "unit_net_value",
            security,
            start_date=previous_date,
            end_date=previous_date,
            df=True,
        )
        if (
            net_frame is not None
            and len(net_frame) > 0
            and security in net_frame.columns
        ):
            candidate = net_frame[security].iloc[-1]
            if _is_finite_positive(candidate):
                net_value = float(candidate)

        # 与 haha.py 一致：get_extras 无数据时回退到基金净值表。
        if net_value is None:
            query_object = query(finance.FUND_NET_VALUE).filter(
                finance.FUND_NET_VALUE.code == security,
                finance.FUND_NET_VALUE.day == previous_date,
            )
            fallback = finance.run_query(query_object)
            if fallback is not None and len(fallback) > 0:
                candidate = fallback["net_value"].iloc[-1]
                if _is_finite_positive(candidate):
                    net_value = float(candidate)

        if _is_finite_positive(market_close) and _is_finite_positive(net_value):
            rate = (market_close - net_value) / net_value
    except Exception as exc:
        log.warning("%s 溢价率读取失败：%s" % (security, exc))
        rate = None

    g.premium_cache[security] = rate
    return rate


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


# ==================== 七星公式 ====================

def _weighted_trend(prices, lookback):
    """当前 v1.1.0 默认的一致 q=w^2 加权回归与 R²。"""
    if lookback < 1 or len(prices) < lookback + 1:
        return None
    recent = np.asarray(prices[-(lookback + 1):], dtype=float)
    if recent.ndim != 1 or not np.all(np.isfinite(recent)) or np.any(recent <= 0):
        return None
    y = np.log(recent)
    x = np.arange(len(y), dtype=float)
    fit_weights = np.linspace(1.0, 2.0, len(y))
    importance = fit_weights ** 2
    weighted_mean = float(np.average(y, weights=importance))
    centered_y = y - weighted_mean
    ss_tot = float(np.sum(importance * centered_y ** 2))
    weighted_variance = ss_tot / float(np.sum(importance))
    if not math.isfinite(weighted_variance):
        return None
    if weighted_variance <= np.finfo(float).eps:
        return 0.0, 0.0, 0.0

    slope, centered_intercept = np.polyfit(
        x, centered_y, 1, w=fit_weights
    )
    slope = float(slope)
    centered_intercept = float(centered_intercept)
    if not math.isfinite(slope) or not math.isfinite(centered_intercept):
        return None
    exponent = slope * 250
    max_float = float(np.finfo(float).max)
    annualized = (
        max_float if exponent > math.log(max_float) else math.expm1(exponent)
    )
    fitted_centered = slope * x + centered_intercept
    ss_res = float(np.sum(importance * (centered_y - fitted_centered) ** 2))
    if not builtins.all(
        math.isfinite(value) for value in (annualized, ss_res, ss_tot)
    ):
        return None
    raw_r_squared = 1 - ss_res / ss_tot
    tolerance = 1e-12
    if not math.isfinite(raw_r_squared) or not (
        -tolerance <= raw_r_squared <= 1 + tolerance
    ):
        return annualized, 0.0, 0.0
    r_squared = min(1.0, max(0.0, raw_r_squared))
    score = annualized * r_squared
    if not math.isfinite(score):
        return None
    return annualized, r_squared, score


def _legacy_weighted_trend(prices, lookback):
    """历史 v1.0.0：故意保留回归 w²、R² 用 w 和普通均值的混合口径。"""
    if lookback < 1 or len(prices) < lookback + 1:
        return None
    recent = np.asarray(prices[-(lookback + 1):], dtype=float)
    if recent.ndim != 1 or not np.all(np.isfinite(recent)) or np.any(recent <= 0):
        return None
    y = np.log(recent)
    x = np.arange(len(y), dtype=float)
    weights = np.linspace(1.0, 2.0, len(y))
    slope, intercept = np.polyfit(x, y, 1, w=weights)
    slope = float(slope)
    intercept = float(intercept)
    if not math.isfinite(slope) or not math.isfinite(intercept):
        return None
    exponent = slope * 250
    max_float = float(np.finfo(float).max)
    annualized = (
        max_float if exponent >= math.log(max_float) else math.exp(exponent) - 1
    )
    fitted = slope * x + intercept
    ss_res = float(np.sum(weights * (y - fitted) ** 2))
    ss_tot = float(np.sum(weights * (y - float(np.mean(y))) ** 2))
    if not builtins.all(
        math.isfinite(value) for value in (annualized, ss_res, ss_tot)
    ):
        return None
    if ss_tot == 0:
        return annualized, 0.0, 0.0
    r_squared = 1 - ss_res / ss_tot
    if not math.isfinite(r_squared):
        return annualized, 0.0, 0.0
    score = annualized * r_squared
    if not math.isfinite(score):
        score_sign = -1.0 if (annualized < 0) != (r_squared < 0) else 1.0
        score = math.copysign(max_float, score_sign)
    return annualized, r_squared, score


def _profit_triggered(frame, current_price):
    if not ENABLE_PROFIT_PROTECTION or frame is None:
        return False
    max_high = max(
        float(value) for value in frame["high"].iloc[-PROFIT_LOOKBACK_DAYS:]
    )
    return current_price <= max_high * (1 - PROFIT_DRAWDOWN_PERCENT / 100.0)


def _unavailable_metrics(security, reason):
    return {
        "etf": security,
        "eligible": False,
        "score": None,
        "annualized_returns": None,
        "r_squared": None,
        "short_annualized": None,
        "volume_ratio": None,
        "filter_reasons": [reason],
    }


def _metrics(context, security, current_price, current_volume):
    required = max(
        LOOKBACK_DAYS,
        SHORT_LOOKBACK_DAYS,
        PROFIT_LOOKBACK_DAYS,
        VOLUME_LOOKBACK_DAYS,
        3,
    )
    frame = _daily_history(security, required)
    if frame is None or current_price is None:
        return _unavailable_metrics(security, "分钟价或历史数据不足")

    prices = list(frame["close"].astype(float).values) + [current_price]
    filter_reasons = []
    if _profit_triggered(frame, current_price):
        filter_reasons.append("高点回撤保护")

    trend_function = (
        _legacy_weighted_trend
        if TREND_FORMULA_MODE == "legacy_v1"
        else _weighted_trend
    )
    trend = trend_function(prices, LOOKBACK_DAYS)
    if trend is None:
        return _unavailable_metrics(security, "长期趋势不可计算")
    annualized, r_squared, score = trend

    volume_ratio = None
    if ENABLE_VOLUME_CHECK:
        average_volume = float(
            np.mean(frame["volume"].iloc[-VOLUME_LOOKBACK_DAYS:].values)
        )
        if current_volume is None:
            return _unavailable_metrics(security, "盘中累计成交量不可计算")
        volume_ratio = current_volume / average_volume if average_volume > 0 else 0.0
        if (
            volume_ratio > VOLUME_RATIO_THRESHOLD
            and annualized > VOLUME_RETURN_LIMIT_PERCENT / 100.0
        ):
            filter_reasons.append("放量过热")

    short_return = current_price / prices[-(SHORT_LOOKBACK_DAYS + 1)] - 1
    short_annualized = (1 + short_return) ** (250.0 / SHORT_LOOKBACK_DAYS) - 1
    if (
        ENABLE_SHORT_MOMENTUM_FILTER
        and short_annualized < SHORT_MOMENTUM_THRESHOLD_PERCENT / 100.0
    ):
        filter_reasons.append("短期动量不足")

    loss_factor = 1 - SINGLE_DAY_LOSS_PERCENT / 100.0
    recent_ratios = (
        prices[-1] / prices[-2],
        prices[-2] / prices[-3],
        prices[-3] / prices[-4],
    )
    if min(recent_ratios) < loss_factor:
        filter_reasons.append("近三段单日急跌")
    if TREND_FORMULA_MODE == "consistent_w2" and annualized <= 0:
        filter_reasons.append("长期拟合趋势非正")
    if not MIN_SCORE_THRESHOLD < score < MAX_SCORE_THRESHOLD:
        filter_reasons.append("趋势评分超出开区间")

    return {
        "etf": security,
        "eligible": not filter_reasons,
        "score": score,
        "annualized_returns": annualized,
        "r_squared": r_squared,
        "short_annualized": short_annualized,
        "volume_ratio": volume_ratio,
        "filter_reasons": filter_reasons,
    }


# ==================== 每日事件 ====================

def profit_protection_check(context):
    relevant = list(g.etf_pool) + [DEFENSIVE_SYMBOL]
    held = [security for security in relevant if _position_amount(context, security) > 0]
    prices = _signal_prices(held)
    for security in held:
        current_price = prices.get(security)
        frame = _daily_history(security, PROFIT_LOOKBACK_DAYS)
        if current_price is not None and _profit_triggered(frame, current_price):
            _submit_target_value(
                context,
                security,
                0,
                "%s 高点回撤保护触发" % PROFIT_CHECK_TIME,
            )


def _rank(context, prices):
    volumes = _cumulative_volume_map(context, g.etf_pool)
    evaluations = [
        _metrics(context, security, prices.get(security), volumes.get(security))
        for security in g.etf_pool
    ]
    ranked = [item for item in evaluations if item["eligible"]]
    # Python 排序稳定，因此完全同分时保持 ETF_POOL 原顺序，与当前项目一致。
    ranked.sort(key=lambda item: item["score"], reverse=True)

    for item in evaluations:
        score_text = "不可计算" if item["score"] is None else "%.8f" % item["score"]
        reason = "、".join(item["filter_reasons"]) if item["filter_reasons"] else "合格"
        log.info(
            "%s 七星评分=%s，年化=%s，R2=%s，短动量=%s，量比=%s，%s"
            % (
                item["etf"],
                score_text,
                "-" if item["annualized_returns"] is None else "%.4f" % item["annualized_returns"],
                "-" if item["r_squared"] is None else "%.6f" % item["r_squared"],
                "-" if item["short_annualized"] is None else "%.4f" % item["short_annualized"],
                "-" if item["volume_ratio"] is None else "%.4f" % item["volume_ratio"],
                reason,
            )
        )
    return ranked


def _targets(context, rankings, prices, recheck, check_premium):
    targets = []
    for item in rankings:
        if len(targets) >= HOLDINGS_NUM:
            break
        security = item["etf"]
        current_price = prices.get(security)
        if current_price is None:
            continue
        if recheck:
            frame = _daily_history(security, PROFIT_LOOKBACK_DAYS)
            if _profit_triggered(frame, current_price):
                continue
        if check_premium and ENABLE_PREMIUM_FILTER:
            premium_rate = _premium_rate(context, security)
            if premium_rate is None:
                log.info("%s 无法取得溢价率，买入候选排除" % security)
                continue
            if premium_rate > PREMIUM_THRESHOLD:
                log.info(
                    "%s 溢价 %.2f%% 超过阈值 %.2f%%，买入候选排除"
                    % (security, premium_rate * 100, PREMIUM_THRESHOLD * 100)
                )
                continue
        targets.append(security)

    if not targets and prices.get(DEFENSIVE_SYMBOL) is not None:
        targets = [DEFENSIVE_SYMBOL]
    return targets


def sell_trade(context):
    relevant = list(g.etf_pool) + [DEFENSIVE_SYMBOL]
    prices = _signal_prices(relevant)
    rankings = _rank(context, prices)
    g.rankings_cache_date = context.current_dt.date()
    g.rankings_cache = rankings

    targets = _targets(
        context, rankings, prices, recheck=False, check_premium=False
    )
    target_set = set(targets)
    for security in relevant:
        if security not in target_set and _position_amount(context, security) > 0:
            _submit_target_value(
                context,
                security,
                0,
                "%s 七星排名换仓，目标为 %s"
                % (SELL_TIME, "、".join(targets) if targets else "现金"),
            )

    # haha.py 的 A 股平台规则：目标持仓若前一交易日溢价过高，也在卖出事件清仓。
    if ENABLE_PREMIUM_FILTER:
        for security in relevant:
            if security not in target_set or _position_amount(context, security) <= 0:
                continue
            premium_rate = _premium_rate(context, security)
            if premium_rate is not None and premium_rate > PREMIUM_THRESHOLD:
                _submit_target_value(
                    context,
                    security,
                    0,
                    "%s 前一交易日溢价 %.2f%% 超过阈值"
                    % (SELL_TIME, premium_rate * 100),
                )


def buy_trade(context):
    rankings = (
        g.rankings_cache
        if g.rankings_cache_date == context.current_dt.date()
        else []
    )
    relevant = list(g.etf_pool) + [DEFENSIVE_SYMBOL]
    prices = _signal_prices(relevant)
    targets = _targets(
        context, rankings, prices, recheck=True, check_premium=True
    )

    # 与当前项目一致：14:01 仍存在任何非目标持仓时，本次不买入，也不重新排名。
    non_targets = [
        security
        for security in relevant
        if security not in targets and _position_amount(context, security) > 0
    ]
    if non_targets:
        log.info("仍有非目标持仓 %s，本次等待卖出完成后再买" % "、".join(non_targets))
        return
    if not targets:
        log.info("无合格候选且防御标的不可用，保持现金")
        return

    target_value = context.portfolio.total_value / len(targets)
    tolerance = REBALANCE_TOLERANCE_PERCENT / 100.0
    for security in targets:
        current_price = prices.get(security)
        if current_price is None:
            continue
        current_value = _position_amount(context, security) * current_price
        if (
            current_value != 0
            and abs(current_value - target_value) <= target_value * tolerance
        ):
            continue
        trade_value = abs(current_value - target_value)
        if 0 < trade_value < MINIMUM_TRADE_VALUE:
            log.info("跳过 %s：调仓金额 %.2f 低于阈值" % (security, trade_value))
            continue
        _submit_target_value(
            context,
            security,
            target_value,
            "%s 七星目标等权 %.4f%%"
            % (BUY_TIME, 100.0 / len(targets)),
        )
