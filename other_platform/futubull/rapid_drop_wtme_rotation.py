class Strategy(StrategyBase):

    def initialize(self):
        declare_strategy_type(AlgoStrategyType.SECURITY)
        self.trigger_symbols()
        self.global_variables()
        self.custom_indicator()
        self.candidates = [self.candidate_1, self.candidate_2, self.candidate_3, self.candidate_4, self.candidate_5, self.candidate_6, self.candidate_7, self.candidate_8]
        self.risk_off = []
        self.last_risk_date = -1
        self.last_selection_date = -1

    def trigger_symbols(self):
        # 回测参数中前 7 个可依次绑定 GLD、SLV、XLE、SPY、QQQ、SOXX、FXI，第 8 个自行绑定。
        self.candidate_1 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_2 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_3 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_4 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_5 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_6 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_7 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_8 = declare_trig_symbol(is_trade_symbol=True)

    def global_variables(self):
        self.wtme_period = show_variable(13, GlobalType.INT)
        self.wtme_half_life = show_variable(6.0, GlobalType.FLOAT)
        self.wtme_epsilon = show_variable(0.00000001, GlobalType.FLOAT)
        self.enable_percent_drop_filter = show_variable(True, GlobalType.BOOL)
        self.drop_threshold_percent = show_variable(5.0, GlobalType.FLOAT)
        self.drop_lookback_sessions = show_variable(3, GlobalType.INT)
        self.risk_check_hour = show_variable(9, GlobalType.INT)
        self.risk_check_minute = show_variable(50, GlobalType.INT)
        self.selection_hour = show_variable(10, GlobalType.INT)
        self.selection_minute = show_variable(0, GlobalType.INT)
        self.target_weight_percent = show_variable(100.0, GlobalType.FLOAT)

    def custom_indicator(self):
        pass

    def _data_prepare_hints(self):
        # 此函数不会执行；显式调用只供富途回测器静态识别每个标的所需的历史周期与最大根数。
        if False:
            bar_close(symbol=self.candidate_1, bar_type=BarType.M1, select=2, session_type=THType.RTH)
            bar_high(symbol=self.candidate_1, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_low(symbol=self.candidate_1, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_1, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_2, bar_type=BarType.M1, select=2, session_type=THType.RTH)
            bar_high(symbol=self.candidate_2, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_low(symbol=self.candidate_2, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_2, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_3, bar_type=BarType.M1, select=2, session_type=THType.RTH)
            bar_high(symbol=self.candidate_3, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_low(symbol=self.candidate_3, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_3, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_4, bar_type=BarType.M1, select=2, session_type=THType.RTH)
            bar_high(symbol=self.candidate_4, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_low(symbol=self.candidate_4, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_4, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_5, bar_type=BarType.M1, select=2, session_type=THType.RTH)
            bar_high(symbol=self.candidate_5, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_low(symbol=self.candidate_5, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_5, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_6, bar_type=BarType.M1, select=2, session_type=THType.RTH)
            bar_high(symbol=self.candidate_6, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_low(symbol=self.candidate_6, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_6, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_7, bar_type=BarType.M1, select=2, session_type=THType.RTH)
            bar_high(symbol=self.candidate_7, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_low(symbol=self.candidate_7, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_7, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_8, bar_type=BarType.M1, select=2, session_type=THType.RTH)
            bar_high(symbol=self.candidate_8, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_low(symbol=self.candidate_8, bar_type=BarType.D1, select=500, session_type=THType.RTH)
            bar_close(symbol=self.candidate_8, bar_type=BarType.D1, select=500, session_type=THType.RTH)

    def handle_data(self):
        if not self._parameters_valid():
            return
        now = device_time(TimeZone.ET)
        date_key = now.year * 10000 + now.month * 100 + now.day
        if self._at_minute(now, self.risk_check_hour, self.risk_check_minute) and self.last_risk_date != date_key:
            self.last_risk_date = date_key
            self._run_risk_check()
        if self._at_minute(now, self.selection_hour, self.selection_minute) and self.last_selection_date != date_key:
            self.last_selection_date = date_key
            self._run_selection()

    def _at_minute(self, now, hour, minute):
        return now.hour == int(hour) and now.minute == int(minute)

    def _parameters_valid(self):
        if int(self.wtme_period) < 2 or int(self.wtme_period) > 499:
            return False
        if float(self.wtme_half_life) <= 0 or float(self.wtme_epsilon) <= 0:
            return False
        if int(self.drop_lookback_sessions) < 1 or float(self.drop_threshold_percent) <= 0:
            return False
        if float(self.target_weight_percent) <= 0 or float(self.target_weight_percent) > 100:
            return False
        risk_minutes = int(self.risk_check_hour) * 60 + int(self.risk_check_minute)
        selection_minutes = int(self.selection_hour) * 60 + int(self.selection_minute)
        if risk_minutes >= selection_minutes or risk_minutes < 0 or selection_minutes > 1439:
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
            except Exception:
                break
            if not self._is_positive(high_value) or not self._is_positive(low_value) or not self._is_positive(close_value):
                break
            newest_first.append([float(high_value), float(low_value), float(close_value)])
            select_index += 1
        if len(newest_first) < int(minimum_count):
            return None
        newest_first.reverse()
        return newest_first

    def _run_risk_check(self):
        self.risk_off = []
        if not self.enable_percent_drop_filter:
            return
        lookback = int(self.drop_lookback_sessions)
        for symbol in self.candidates:
            event_price = self._signal_price(symbol)
            rows = self._daily_rows(symbol, lookback, lookback)
            triggered = False
            if event_price is None or rows is None:
                triggered = True
            else:
                start_index = len(rows) - lookback
                segment_index = 0
                while segment_index < lookback:
                    row_index = start_index + segment_index
                    previous_close = rows[row_index][2]
                    if row_index + 1 < len(rows):
                        current_value = rows[row_index + 1][2]
                    else:
                        current_value = event_price
                    percent_change = current_value / previous_close - 1.0
                    if percent_change <= -float(self.drop_threshold_percent) / 100.0:
                        triggered = True
                    segment_index += 1
            if triggered:
                self.risk_off.append(symbol)
                if position_holding_qty(symbol=symbol) > 0:
                    close_positions(symbol=symbol)

    def _wtme_score(self, completed_rows, current_value, period, half_life, epsilon):
        if len(completed_rows) < period or half_life <= 0:
            return None
        weight_total = 0.0
        observation_index = 0
        while observation_index < period:
            weight_total += 2.0 ** (-(period - 1 - observation_index) / half_life)
            observation_index += 1
        weighted_return = 0.0
        weighted_true_range = 0.0
        observation_index = 0
        while observation_index < period:
            weight = 2.0 ** (-(period - 1 - observation_index) / half_life) / weight_total
            if observation_index < period - 1:
                previous_close = completed_rows[observation_index][2]
                current_close = completed_rows[observation_index + 1][2]
                current_high = completed_rows[observation_index + 1][0]
                current_low = completed_rows[observation_index + 1][1]
                true_range = max(current_high - current_low, abs(current_high - previous_close), abs(current_low - previous_close))
            else:
                previous_close = completed_rows[-1][2]
                current_close = current_value
                true_range = abs(current_value - previous_close)
            weighted_return += weight * ((current_close - previous_close) / previous_close)
            weighted_true_range += weight * (true_range / previous_close)
            observation_index += 1
        return 100.0 * weighted_return / (weighted_true_range + epsilon)

    def _target_quantity(self, symbol, target_percent):
        try:
            price = current_price(symbol=symbol, price_type=THType.RTH)
            currency = get_symbol_currency(symbol=symbol)
            equity = net_asset(currency=currency)
            quantity_step = lot_size(symbol=symbol)
        except Exception:
            return 0
        if not self._is_positive(price) or not self._is_positive(equity) or not self._is_positive(quantity_step):
            return 0
        raw_quantity = float(equity) * float(target_percent) / 100.0 / float(price)
        return floor(raw_quantity / float(quantity_step)) * float(quantity_step)

    def _run_selection(self):
        period = int(self.wtme_period)
        half_life = float(self.wtme_half_life)
        epsilon = float(self.wtme_epsilon)
        best_symbol = None
        best_score = None
        best_code = None
        for symbol in self.candidates:
            event_price = self._signal_price(symbol)
            rows = self._daily_rows(symbol, period, period)
            if event_price is not None and rows is not None and not self._contains(self.risk_off, symbol):
                score = self._wtme_score(rows, event_price, period, half_life, epsilon)
                if self._is_finite(score):
                    symbol_code = get_symbol_code(symbol=symbol)
                    if best_score is None or score > best_score or score == best_score and symbol_code < best_code:
                        best_symbol = symbol
                        best_score = score
                        best_code = symbol_code
        for symbol in self.candidates:
            if symbol != best_symbol and position_holding_qty(symbol=symbol) > 0:
                close_positions(symbol=symbol)
        if best_symbol is not None and position_holding_qty(symbol=best_symbol) <= 0:
            quantity = self._target_quantity(best_symbol, self.target_weight_percent)
            if quantity > 0:
                place_market(symbol=best_symbol, qty=quantity, side=OrderSide.BUY)
