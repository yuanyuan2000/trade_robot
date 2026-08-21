class Strategy(StrategyBase):

    def initialize(self):
        declare_strategy_type(AlgoStrategyType.SECURITY)
        self.trigger_symbols()
        self.global_variables()
        self.custom_indicator()
        self.candidates = [self.candidate_1, self.candidate_2, self.candidate_3, self.candidate_4, self.candidate_5, self.candidate_6, self.candidate_7]
        self.all_symbols = [self.candidate_1, self.candidate_2, self.candidate_3, self.candidate_4, self.candidate_5, self.candidate_6, self.candidate_7, self.defensive_symbol]
        self.rankings_cache = []
        self.rankings_cache_date = -1
        self.last_profit_date = -1
        self.last_sell_date = -1
        self.last_buy_date = -1

    def trigger_symbols(self):
        # 默认依次绑定：GLD、USO、SPY、QQQ、DIA、IWM、TLT；防御标的绑定 BIL。
        self.candidate_1 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_2 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_3 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_4 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_5 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_6 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_7 = declare_trig_symbol(is_trade_symbol=True)
        self.defensive_symbol = declare_trig_symbol(is_trade_symbol=True)

    def global_variables(self):
        # 1=项目当前 consistent_w2；2=历史 legacy_v1 兼容公式。
        self.trend_formula_mode = show_variable(1, GlobalType.INT)
        self.lookback_days = show_variable(25, GlobalType.INT)
        self.holdings_num = show_variable(1, GlobalType.INT)
        self.min_score_threshold = show_variable(0.0, GlobalType.FLOAT)
        self.max_score_threshold = show_variable(100.0, GlobalType.FLOAT)
        self.rebalance_tolerance_percent = show_variable(5.0, GlobalType.FLOAT)
        self.minimum_trade_value = show_variable(0.0, GlobalType.FLOAT)
        self.enable_profit_protection = show_variable(True, GlobalType.BOOL)
        self.profit_lookback_days = show_variable(1, GlobalType.INT)
        self.profit_drawdown_percent = show_variable(5.0, GlobalType.FLOAT)
        self.profit_check_hour = show_variable(11, GlobalType.INT)
        self.profit_check_minute = show_variable(0, GlobalType.INT)
        self.enable_volume_check = show_variable(True, GlobalType.BOOL)
        self.volume_lookback_days = show_variable(5, GlobalType.INT)
        self.volume_ratio_threshold = show_variable(2.0, GlobalType.FLOAT)
        self.volume_return_limit_percent = show_variable(100.0, GlobalType.FLOAT)
        self.enable_short_momentum_filter = show_variable(True, GlobalType.BOOL)
        self.short_lookback_days = show_variable(10, GlobalType.INT)
        self.short_momentum_threshold_percent = show_variable(0.0, GlobalType.FLOAT)
        self.single_day_loss_percent = show_variable(3.0, GlobalType.FLOAT)
        self.sell_hour = show_variable(14, GlobalType.INT)
        self.sell_minute = show_variable(0, GlobalType.INT)
        self.buy_hour = show_variable(14, GlobalType.INT)
        self.buy_minute = show_variable(1, GlobalType.INT)

    def custom_indicator(self):
        pass

    def _data_prepare_hints(self):
        # 此函数不会执行；显式调用只供富途回测器静态识别每个标的所需的历史周期与最大根数。
        if False:
            bar_close(symbol=self.candidate_1, bar_type=BarType.M1, select=2, session_type=THType.RTH)
            bar_high(symbol=self.candidate_1, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_low(symbol=self.candidate_1, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_1, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_volume(symbol=self.candidate_1, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_volume(symbol=self.candidate_1, bar_type=BarType.D1, select=1, session_type=THType.RTH)
            bar_close(symbol=self.candidate_2, bar_type=BarType.M1, select=2, session_type=THType.RTH)
            bar_high(symbol=self.candidate_2, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_low(symbol=self.candidate_2, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_2, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_volume(symbol=self.candidate_2, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_volume(symbol=self.candidate_2, bar_type=BarType.D1, select=1, session_type=THType.RTH)
            bar_close(symbol=self.candidate_3, bar_type=BarType.M1, select=2, session_type=THType.RTH)
            bar_high(symbol=self.candidate_3, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_low(symbol=self.candidate_3, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_3, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_volume(symbol=self.candidate_3, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_volume(symbol=self.candidate_3, bar_type=BarType.D1, select=1, session_type=THType.RTH)
            bar_close(symbol=self.candidate_4, bar_type=BarType.M1, select=2, session_type=THType.RTH)
            bar_high(symbol=self.candidate_4, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_low(symbol=self.candidate_4, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_4, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_volume(symbol=self.candidate_4, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_volume(symbol=self.candidate_4, bar_type=BarType.D1, select=1, session_type=THType.RTH)
            bar_close(symbol=self.candidate_5, bar_type=BarType.M1, select=2, session_type=THType.RTH)
            bar_high(symbol=self.candidate_5, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_low(symbol=self.candidate_5, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_5, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_volume(symbol=self.candidate_5, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_volume(symbol=self.candidate_5, bar_type=BarType.D1, select=1, session_type=THType.RTH)
            bar_close(symbol=self.candidate_6, bar_type=BarType.M1, select=2, session_type=THType.RTH)
            bar_high(symbol=self.candidate_6, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_low(symbol=self.candidate_6, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_6, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_volume(symbol=self.candidate_6, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_volume(symbol=self.candidate_6, bar_type=BarType.D1, select=1, session_type=THType.RTH)
            bar_close(symbol=self.candidate_7, bar_type=BarType.M1, select=2, session_type=THType.RTH)
            bar_high(symbol=self.candidate_7, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_low(symbol=self.candidate_7, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_7, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_volume(symbol=self.candidate_7, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_volume(symbol=self.candidate_7, bar_type=BarType.D1, select=1, session_type=THType.RTH)
            bar_close(symbol=self.defensive_symbol, bar_type=BarType.M1, select=2, session_type=THType.RTH)
            bar_high(symbol=self.defensive_symbol, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_low(symbol=self.defensive_symbol, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.defensive_symbol, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_volume(symbol=self.defensive_symbol, bar_type=BarType.D1, select=500, session_type=THType.RTH)

    def handle_data(self):
        if not self._parameters_valid():
            return
        now = device_time(TimeZone.ET)
        date_key = now.year * 10000 + now.month * 100 + now.day
        if self.enable_profit_protection and self._at_minute(now, self.profit_check_hour, self.profit_check_minute) and self.last_profit_date != date_key:
            self.last_profit_date = date_key
            self._run_profit_protection()
        if self._at_minute(now, self.sell_hour, self.sell_minute) and self.last_sell_date != date_key:
            self.last_sell_date = date_key
            self.rankings_cache = self._rank_candidates()
            self.rankings_cache_date = date_key
            self._sell_non_targets(self._targets(self.rankings_cache, False))
        if self._at_minute(now, self.buy_hour, self.buy_minute) and self.last_buy_date != date_key:
            self.last_buy_date = date_key
            if self.rankings_cache_date == date_key:
                self._buy_and_rebalance(self._targets(self.rankings_cache, True))

    def _at_minute(self, now, hour, minute):
        return now.hour == int(hour) and now.minute == int(minute)

    def _parameters_valid(self):
        if int(self.trend_formula_mode) < 1 or int(self.trend_formula_mode) > 2:
            return False
        if int(self.lookback_days) < 5 or int(self.lookback_days) > 250:
            return False
        if int(self.holdings_num) < 1 or int(self.holdings_num) > 5:
            return False
        if float(self.min_score_threshold) >= float(self.max_score_threshold):
            return False
        if int(self.profit_lookback_days) < 1 or int(self.volume_lookback_days) < 1 or int(self.short_lookback_days) < 2:
            return False
        if float(self.profit_drawdown_percent) <= 0 or float(self.volume_ratio_threshold) <= 0 or float(self.single_day_loss_percent) <= 0:
            return False
        profit_minutes = int(self.profit_check_hour) * 60 + int(self.profit_check_minute)
        sell_minutes = int(self.sell_hour) * 60 + int(self.sell_minute)
        buy_minutes = int(self.buy_hour) * 60 + int(self.buy_minute)
        if profit_minutes >= sell_minutes or sell_minutes >= buy_minutes or profit_minutes < 0 or buy_minutes > 1439:
            return False
        return True

    def _is_positive(self, value):
        if value is None:
            return False
        numeric_value = float(value)
        return numeric_value == numeric_value and abs(numeric_value) < 1.7976931348623157e308 and numeric_value > 0

    def _is_finite(self, value):
        if value is None:
            return False
        numeric_value = float(value)
        return numeric_value == numeric_value and abs(numeric_value) < 1.7976931348623157e308

    def _contains(self, values, wanted):
        for value in values:
            if value == wanted:
                return True
        return False

    def _signal_price(self, symbol):
        try:
            value = bar_close(symbol=symbol, bar_type=BarType.M1, select=2, session_type=THType.RTH)
        except Exception:
            return None
        if not self._is_positive(value):
            return None
        return float(value)

    def _daily_rows(self, symbol, requested_count, minimum_count):
        newest_first = []
        count = int(requested_count)
        if count > 499:
            count = 499
        select_index = 2
        while select_index <= count + 1:
            try:
                high_value = bar_high(symbol=symbol, bar_type=BarType.D1, select=select_index, session_type=THType.RTH)
                low_value = bar_low(symbol=symbol, bar_type=BarType.D1, select=select_index, session_type=THType.RTH)
                close_value = bar_close(symbol=symbol, bar_type=BarType.D1, select=select_index, session_type=THType.RTH)
                volume_value = bar_volume(symbol=symbol, bar_type=BarType.D1, select=select_index, session_type=THType.RTH)
            except Exception:
                break
            if not self._is_positive(high_value) or not self._is_positive(low_value) or not self._is_positive(close_value):
                break
            if not self._is_finite(volume_value) or float(volume_value) < 0:
                break
            newest_first.append([float(high_value), float(low_value), float(close_value), float(volume_value)])
            select_index += 1
        if len(newest_first) < int(minimum_count):
            return None
        newest_first.reverse()
        return newest_first

    def _history_requirement(self):
        required = int(self.lookback_days)
        if int(self.short_lookback_days) > required:
            required = int(self.short_lookback_days)
        if int(self.volume_lookback_days) > required:
            required = int(self.volume_lookback_days)
        if int(self.profit_lookback_days) > required:
            required = int(self.profit_lookback_days)
        if required < 3:
            required = 3
        return required

    def _profit_triggered(self, symbol):
        if not self.enable_profit_protection:
            return False
        lookback = int(self.profit_lookback_days)
        rows = self._daily_rows(symbol, lookback, lookback)
        current_value = self._signal_price(symbol)
        if rows is None or current_value is None:
            return False
        max_high = rows[0][0]
        row_index = 1
        while row_index < len(rows):
            if rows[row_index][0] > max_high:
                max_high = rows[row_index][0]
            row_index += 1
        threshold = max_high * (1.0 - float(self.profit_drawdown_percent) / 100.0)
        return current_value <= threshold

    def _run_profit_protection(self):
        for symbol in self.all_symbols:
            if position_holding_qty(symbol=symbol) > 0 and self._profit_triggered(symbol):
                close_positions(symbol=symbol)

    def _weighted_trend(self, prices, mode):
        count = len(prices)
        if count < 2:
            return None
        y_values = []
        for price in prices:
            if not self._is_positive(price):
                return None
            y_values.append(math_log(float(price)))
        q_total = 0.0
        qx_total = 0.0
        qy_total = 0.0
        index = 0
        while index < count:
            weight = 1.0 + index / float(count - 1)
            importance = weight * weight
            q_total += importance
            qx_total += importance * index
            qy_total += importance * y_values[index]
            index += 1
        x_mean = qx_total / q_total
        y_mean = qy_total / q_total
        numerator = 0.0
        denominator = 0.0
        index = 0
        while index < count:
            weight = 1.0 + index / float(count - 1)
            importance = weight * weight
            numerator += importance * (index - x_mean) * (y_values[index] - y_mean)
            denominator += importance * (index - x_mean) * (index - x_mean)
            index += 1
        if denominator <= 0:
            return [0.0, 0.0, 0.0]
        slope = numerator / denominator
        intercept = y_mean - slope * x_mean
        exponent = slope * 250.0
        max_float = 1.7976931348623157e308
        if exponent > 709.782712893384:
            annualized = max_float
        else:
            annualized = power(2.718281828459045, exponent) - 1.0
        if int(mode) == 2:
            ordinary_mean = 0.0
            for y_value in y_values:
                ordinary_mean += y_value
            ordinary_mean = ordinary_mean / count
            ss_res = 0.0
            ss_tot = 0.0
            index = 0
            while index < count:
                weight = 1.0 + index / float(count - 1)
                fitted = slope * index + intercept
                ss_res += weight * (y_values[index] - fitted) * (y_values[index] - fitted)
                ss_tot += weight * (y_values[index] - ordinary_mean) * (y_values[index] - ordinary_mean)
                index += 1
            if ss_tot <= 0:
                return [annualized, 0.0, 0.0]
            r_squared = 1.0 - ss_res / ss_tot
            score = annualized * r_squared
            if not self._is_finite(score):
                score_sign = -1.0
                if annualized < 0 and r_squared < 0 or annualized >= 0 and r_squared >= 0:
                    score_sign = 1.0
                score = score_sign * max_float
            return [annualized, r_squared, score]
        ss_res = 0.0
        ss_tot = 0.0
        index = 0
        while index < count:
            weight = 1.0 + index / float(count - 1)
            importance = weight * weight
            fitted = slope * index + intercept
            ss_res += importance * (y_values[index] - fitted) * (y_values[index] - fitted)
            ss_tot += importance * (y_values[index] - y_mean) * (y_values[index] - y_mean)
            index += 1
        weighted_variance = ss_tot / q_total
        if weighted_variance <= 0.0000000000000002220446049250313:
            return [0.0, 0.0, 0.0]
        raw_r_squared = 1.0 - ss_res / ss_tot
        if raw_r_squared < -0.000000000001 or raw_r_squared > 1.000000000001:
            return [annualized, 0.0, 0.0]
        r_squared = raw_r_squared
        if r_squared < 0:
            r_squared = 0.0
        if r_squared > 1:
            r_squared = 1.0
        return [annualized, r_squared, annualized * r_squared]

    def _metrics(self, symbol):
        required = self._history_requirement()
        rows = self._daily_rows(symbol, required, required)
        current_value = self._signal_price(symbol)
        if rows is None or current_value is None:
            return None
        lookback = int(self.lookback_days)
        prices = []
        start_index = len(rows) - lookback
        row_index = start_index
        while row_index < len(rows):
            prices.append(rows[row_index][2])
            row_index += 1
        prices.append(current_value)
        trend = self._weighted_trend(prices, self.trend_formula_mode)
        if trend is None:
            return None
        annualized = trend[0]
        score = trend[2]
        eligible = True
        if self._profit_triggered(symbol):
            eligible = False
        if self.enable_volume_check:
            volume_days = int(self.volume_lookback_days)
            average_volume = 0.0
            row_index = len(rows) - volume_days
            while row_index < len(rows):
                average_volume += rows[row_index][3]
                row_index += 1
            average_volume = average_volume / volume_days
            try:
                current_volume = bar_volume(symbol=symbol, bar_type=BarType.D1, select=1, session_type=THType.RTH)
            except Exception:
                current_volume = 0.0
            volume_ratio = 0.0
            if average_volume > 0 and current_volume is not None:
                volume_ratio = float(current_volume) / average_volume
            if volume_ratio > float(self.volume_ratio_threshold) and annualized > float(self.volume_return_limit_percent) / 100.0:
                eligible = False
        short_days = int(self.short_lookback_days)
        short_base = rows[-short_days][2]
        short_return = current_value / short_base - 1.0
        short_annualized = (1.0 + short_return) ** (250.0 / short_days) - 1.0
        if self.enable_short_momentum_filter and short_annualized < float(self.short_momentum_threshold_percent) / 100.0:
            eligible = False
        loss_factor = 1.0 - float(self.single_day_loss_percent) / 100.0
        ratio_one = current_value / rows[-1][2]
        ratio_two = rows[-1][2] / rows[-2][2]
        ratio_three = rows[-2][2] / rows[-3][2]
        if ratio_one < loss_factor or ratio_two < loss_factor or ratio_three < loss_factor:
            eligible = False
        if int(self.trend_formula_mode) == 1 and annualized <= 0:
            eligible = False
        if not float(self.min_score_threshold) < score or not score < float(self.max_score_threshold):
            eligible = False
        if not self._is_finite(score):
            eligible = False
        return [score, symbol, eligible]

    def _rank_candidates(self):
        rankings = []
        for symbol in self.candidates:
            metrics = self._metrics(symbol)
            if metrics is not None and metrics[2]:
                insert_at = len(rankings)
                index = 0
                while index < len(rankings):
                    if metrics[0] > rankings[index][0]:
                        insert_at = index
                        break
                    index += 1
                rankings.insert(insert_at, metrics)
        return rankings

    def _targets(self, rankings, recheck):
        result = []
        target_count = int(self.holdings_num)
        if target_count < 1:
            target_count = 1
        if target_count > 5:
            target_count = 5
        for item in rankings:
            if len(result) >= target_count:
                break
            symbol = item[1]
            if not recheck or not self._profit_triggered(symbol):
                result.append(symbol)
        if len(result) == 0 and self._signal_price(self.defensive_symbol) is not None:
            result.append(self.defensive_symbol)
        return result

    def _sell_non_targets(self, targets):
        for symbol in self.all_symbols:
            if not self._contains(targets, symbol) and position_holding_qty(symbol=symbol) > 0:
                close_positions(symbol=symbol)

    def _target_quantity(self, symbol, target_fraction):
        try:
            price = current_price(symbol=symbol, price_type=THType.RTH)
            currency = get_symbol_currency(symbol=symbol)
            equity = net_asset(currency=currency)
            quantity_step = lot_size(symbol=symbol)
        except Exception:
            return [0, 0.0]
        if not self._is_positive(price) or not self._is_positive(equity) or not self._is_positive(quantity_step):
            return [0, 0.0]
        raw_quantity = float(equity) * float(target_fraction) / float(price)
        target_quantity = floor(raw_quantity / float(quantity_step)) * float(quantity_step)
        return [target_quantity, float(price)]

    def _buy_and_rebalance(self, targets):
        if len(targets) == 0:
            return
        for symbol in self.all_symbols:
            if position_holding_qty(symbol=symbol) > 0 and not self._contains(targets, symbol):
                return
        target_fraction = 1.0 / len(targets)
        tolerance = float(self.rebalance_tolerance_percent) / 100.0
        for symbol in targets:
            target_data = self._target_quantity(symbol, target_fraction)
            target_quantity = target_data[0]
            price = target_data[1]
            current_quantity = position_holding_qty(symbol=symbol)
            target_value = target_quantity * price
            current_value = current_quantity * price
            if current_quantity > 0 and abs(current_value - target_value) <= target_value * tolerance:
                continue
            difference = target_quantity - current_quantity
            trade_value = abs(difference) * price
            if trade_value < float(self.minimum_trade_value):
                continue
            if difference > 0:
                place_market(symbol=symbol, qty=difference, side=OrderSide.BUY)
            if difference < 0:
                close_positions(symbol=symbol, qty=-difference)
