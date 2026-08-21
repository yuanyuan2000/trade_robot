"""富途牛牛自编指标：加权真实波幅动量效率（WTME）。

公式与本项目 ``services/indicator_service.py`` 保持一致。窗口 N 和半衰期 h
可以在富途牛牛的指标参数中调整，默认分别为 13 和 6；防除零项固定为 1e-8。
"""

import math


indicator("WTME", "加权真实波幅动量效率", False, "指数半衰加权的方向收益，相对于同期标准化真实波幅的效率。")


WTME_EPSILON = 1e-8


def _is_finite(value):
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _to_sequence(values):
    """用富途 Sequence 承载逐项计算的结果。"""
    result = close()
    for index in range(len(values)):
        result[index] = values[index]
    return result


def wtme(period=13, half_life=6.0):
    """计算 N 个收益/TR 观测组成的 WTME 序列。"""
    period = int(period)
    half_life = float(half_life)
    close_values = list(close())
    high_values = list(high())
    low_values = list(low())
    data_len = len(close_values)
    result_values = [math.nan] * data_len

    if period < 2 or half_life <= 0 or data_len < period + 1:
        return _to_sequence(result_values)

    # 最旧观测到最新观测的权重；最新观测的原始权重恒为 1。
    raw_weights = []
    for index in range(period):
        raw_weights.append(2 ** (-(period - 1 - index) / half_life))
    weight_total = sum(raw_weights)
    weights = []
    for weight in raw_weights:
        weights.append(weight / weight_total)

    # period 个收益/TR 观测需要 period + 1 根 OHLC K 线。
    for end_index in range(period, data_len):
        first_current_index = end_index - period + 1
        weighted_return = 0.0
        weighted_true_range = 0.0
        valid = True

        for weight_index in range(period):
            current_index = first_current_index + weight_index
            previous_index = current_index - 1
            previous_close = close_values[previous_index]
            current_close = close_values[current_index]
            current_high = high_values[current_index]
            current_low = low_values[current_index]

            if not (_is_finite(previous_close) and previous_close > 0 and _is_finite(current_close) and _is_finite(current_high) and _is_finite(current_low)):
                valid = False
                break

            true_range = max(current_high - current_low, abs(current_high - previous_close), abs(current_low - previous_close))
            weight = weights[weight_index]
            weighted_return += weight * ((current_close - previous_close) / previous_close)
            weighted_true_range += weight * (true_range / previous_close)

        if valid:
            result_values[end_index] = 100 * weighted_return / (weighted_true_range + WTME_EPSILON)

    return _to_sequence(result_values)


if __name__ == "__main__":
    window = input_parameter("窗口长度 N", 13)
    decay_half_life = input_parameter("半衰期 h", 6.0)
    wtme_result = wtme(window, decay_half_life)

    plot("WTME", wtme_result, color=Color.hex("#FF8D1E"), linewidth=2)
    plot_hline("ZERO", 0, color=Color.gray, style=Line.line_dashed)

    output_parameter(WTME=wtme_result)
