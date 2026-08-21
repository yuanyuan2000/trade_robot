"""聚宽版：急跌回避与 WTME 动量轮动（对应项目策略 v1.1.0）。

直接复制本文件到聚宽策略编辑器并使用分钟级回测。策略信号使用定时函数执行
前一根完整分钟线的收盘价；撮合、复权、交易单位、涨跌停和费用均由聚宽处理。
"""

import builtins
import math

import numpy as np
from jqdata import *


# ==================== 用户参数区 ====================

# haha.py 当前启用的小池。
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

# 当前项目 RapidDropWtmeRotationStrategy v1.1.0 的参数。
WTME_PERIOD = 13
WTME_HALF_LIFE = 6.0
WTME_EPSILON = 1e-8
ENABLE_PERCENT_DROP_FILTER = True
DROP_THRESHOLD_PERCENT = 5.0
DROP_LOOKBACK_SESSIONS = 5
RISK_CHECK_TIME = "09:50"
SELECTION_TIME = "10:00"
TARGET_WEIGHT = 100.0

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
        "WTME 策略初始化：候选 %d 只，N=%d，h=%g，急跌检查 %s，轮动 %s"
        % (len(g.etf_pool), WTME_PERIOD, WTME_HALF_LIFE,
           RISK_CHECK_TIME, SELECTION_TIME)
    )


def _validate_parameters():
    if not ETF_POOL or len(set(ETF_POOL)) != len(ETF_POOL):
        raise ValueError("ETF_POOL 不能为空且不能包含重复代码")
    if WTME_PERIOD < 2 or WTME_HALF_LIFE <= 0 or WTME_EPSILON <= 0:
        raise ValueError("WTME_PERIOD、WTME_HALF_LIFE、WTME_EPSILON 参数无效")
    if DROP_LOOKBACK_SESSIONS < 1 or DROP_THRESHOLD_PERCENT <= 0:
        raise ValueError("急跌观察期和阈值必须为正数")
    if not isinstance(ENABLE_PERCENT_DROP_FILTER, bool):
        raise ValueError("ENABLE_PERCENT_DROP_FILTER 必须为 True 或 False")
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
    """取得定时函数运行前一根完整分钟线的收盘价。"""
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


def _daily_history(security, count):
    try:
        frame = attribute_history(
            security,
            count,
            unit="1d",
            fields=["open", "high", "low", "close"],
            skip_paused=True,
            df=True,
            fq="pre",
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


def _submit_target_value(context, security, target_value, reason):
    """只做可交易性预检；成交数量、价格与限制规则全部交给聚宽。"""
    data = get_current_data()[security]
    if data.paused or not _is_finite_positive(data.last_price):
        log.info("跳过 %s：停牌或无有效价格（%s）" % (security, reason))
        return None

    current_amount = _position_amount(context, security)
    current_value = current_amount * float(data.last_price)
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


# ==================== WTME 与策略逻辑 ====================

def _calculate_wtme_components(frame, current_price):
    """与项目 calculate_wtme_components 完全相同的 N 个观测公式。"""
    previous_close = float(frame["close"].iloc[-1])
    opens = list(frame["open"].astype(float).values) + [previous_close]
    highs = list(frame["high"].astype(float).values) + [max(previous_close, current_price)]
    lows = list(frame["low"].astype(float).values) + [min(previous_close, current_price)]
    closes = list(frame["close"].astype(float).values) + [current_price]

    # N 根完整日线加当前点时观测，共 N+1 根 bar、N 个收益/TR 观测。
    raw_weights = [
        2 ** (-(WTME_PERIOD - 1 - index) / WTME_HALF_LIFE)
        for index in range(WTME_PERIOD)
    ]
    total_weight = sum(raw_weights)
    weights = [value / total_weight for value in raw_weights]
    returns = []
    normalized_true_ranges = []
    for index in range(1, len(closes)):
        prior_close = closes[index - 1]
        current_close = closes[index]
        true_range = max(
            highs[index] - lows[index],
            abs(highs[index] - prior_close),
            abs(lows[index] - prior_close),
        )
        returns.append((current_close - prior_close) / prior_close)
        normalized_true_ranges.append(true_range / prior_close)

    weighted_return = sum(w * value for w, value in zip(weights, returns))
    weighted_true_range = sum(
        w * value for w, value in zip(weights, normalized_true_ranges)
    )
    score = 100 * weighted_return / (weighted_true_range + WTME_EPSILON)
    return {
        "score": score,
        "weighted_return": weighted_return,
        "weighted_true_range": weighted_true_range,
    }


def risk_check(context):
    today = context.current_dt.date()
    prices = _signal_prices(g.etf_pool)
    flagged = set()
    threshold = -DROP_THRESHOLD_PERCENT / 100.0

    for security in g.etf_pool:
        price = prices.get(security)
        frame = _daily_history(security, DROP_LOOKBACK_SESSIONS)
        if price is None or frame is None:
            flagged.add(security)
            log.info("%s 风险检查：分钟价或历史数据不足，当日排除" % security)
            continue

        closes = list(frame["close"].astype(float).values)
        current_prices = closes[1:] + [price]
        changes = [
            current_value / previous_close - 1
            for previous_close, current_value in zip(closes, current_prices)
        ]
        # jqdata 的星号导入可能覆盖 Python 内置 any；必须显式调用 builtins.any。
        # 否则把生成器交给其他 any 实现时，生成器对象本身可能被当成 True，
        # 导致所有候选无条件进入急跌过滤。
        triggered = bool(
            ENABLE_PERCENT_DROP_FILTER
            and builtins.any(value <= threshold for value in changes)
        )
        if triggered:
            flagged.add(security)
            if _position_amount(context, security) > 0:
                _submit_target_value(
                    context,
                    security,
                    0,
                    "%s 百分比急跌检查命中，当日回避" % RISK_CHECK_TIME,
                )
        log.info(
            "%s 风险检查：%s，最差单日涨跌 %.2f%%，触发阈值 %.2f%%"
            % (
                security,
                "过滤" if triggered else "通过",
                min(changes) * 100,
                threshold * 100,
            )
        )

    g.risk_off_date = today
    g.risk_off = flagged


def select_and_rotate(context):
    today = context.current_dt.date()
    flagged = g.risk_off if g.risk_off_date == today else set()
    prices = _signal_prices(g.etf_pool)
    eligible_scores = []

    for security in g.etf_pool:
        price = prices.get(security)
        frame = _daily_history(security, WTME_PERIOD)
        if price is None or frame is None:
            log.info("%s WTME：分钟价或历史数据不足，排除" % security)
            continue
        components = _calculate_wtme_components(frame, price)
        filtered = security in flagged
        if not filtered:
            eligible_scores.append((float(components["score"]), security))
        log.info(
            "%s WTME=%.8f，Rw=%.8f，Aw=%.8f，%s"
            % (
                security,
                components["score"],
                components["weighted_return"],
                components["weighted_true_range"],
                "急跌过滤" if filtered else "合格",
            )
        )

    # 当前项目以“分数降序、代码升序”处理完全相同的 WTME 分数。
    eligible_scores.sort(key=lambda item: (-item[0], item[1]))
    target = eligible_scores[0][1] if eligible_scores else None

    # 与当前项目一致：先清仓非目标，再仅在目标尚未持有时建仓；不日常再平衡。
    for security in g.etf_pool:
        if security != target and _position_amount(context, security) > 0:
            _submit_target_value(
                context,
                security,
                0,
                "%s WTME 轮动，目标为 %s"
                % (SELECTION_TIME, target if target is not None else "现金"),
            )

    if target is not None and _position_amount(context, target) <= 0:
        target_value = context.portfolio.total_value * TARGET_WEIGHT / 100.0
        _submit_target_value(
            context,
            target,
            target_value,
            "%s 未过滤标的 WTME 最高" % SELECTION_TIME,
        )
    elif target is None:
        log.info("无合格 WTME 标的，保持现金")
