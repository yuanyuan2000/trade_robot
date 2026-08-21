"""富途牛牛自编指标：相对 ATR（RATR）。

公式与本项目 ``services/indicator_service.py`` 保持一致：

    RATR[i] = (Close[i] - Close[i - N]) / WilderATR[i - 1]

分母故意使用前一根 K 线已经确定的 Wilder ATR，避免当前 K 线尚未完成时
使用其最终高低价。N 可在富途牛牛的指标参数中调整，默认值为 13。
"""

import math


indicator("RATR", "相对 ATR", False, "N 周期价格位移除以前一根 K 线已知的 Wilder ATR(N)。")


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


def relative_atr(period=13):
    """计算项目口径的相对 ATR 序列。"""
    period = int(period)
    close_values = list(close())
    high_values = list(high())
    low_values = list(low())
    data_len = len(close_values)
    result_values = [math.nan] * data_len

    if period < 1 or data_len < period + 2:
        return _to_sequence(result_values)

    # TR 从第 2 根 K 线开始，因为它需要前一根收盘价。
    true_ranges = [math.nan] * data_len
    for index in range(1, data_len):
        previous_close = close_values[index - 1]
        current_high = high_values[index]
        current_low = low_values[index]
        if not (_is_finite(previous_close) and _is_finite(current_high) and _is_finite(current_low)):
            continue
        true_ranges[index] = max(current_high - current_low, abs(current_high - previous_close), abs(current_low - previous_close))

    # 第一个 Wilder ATR 是前 period 个 TR 的算术平均；之后按 1/period 递推。
    seed = true_ranges[1:period + 1]
    atr_values = [math.nan] * data_len
    seed_is_valid = True
    for value in seed:
        if not _is_finite(value):
            seed_is_valid = False
            break
    if seed_is_valid:
        atr_values[period] = sum(seed) / period

    for index in range(period + 1, data_len):
        previous_atr = atr_values[index - 1]
        current_tr = true_ranges[index]
        if _is_finite(previous_atr) and _is_finite(current_tr):
            atr_values[index] = (previous_atr * (period - 1) + current_tr) / period

    # 使用 atr_values[index - 1]，不把当前 K 线的高低价放进分母。
    for index in range(period + 1, data_len):
        previous_atr = atr_values[index - 1]
        current_close = close_values[index]
        base_close = close_values[index - period]
        if _is_finite(previous_atr) and previous_atr > 0 and _is_finite(current_close) and _is_finite(base_close):
            result_values[index] = (current_close - base_close) / previous_atr

    return _to_sequence(result_values)


if __name__ == "__main__":
    window = input_parameter("窗口长度 N", 13)
    ratr_result = relative_atr(window)

    plot("RATR", ratr_result, color=Color.hex("#0CAEE6"), linewidth=2)
    plot_hline("ZERO", 0, color=Color.gray, style=Line.line_dashed)

    output_parameter(RATR=ratr_result)
