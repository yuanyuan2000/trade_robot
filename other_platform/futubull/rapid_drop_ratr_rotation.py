class Strategy(StrategyBase):

    def initialize(self):
        declare_strategy_type(AlgoStrategyType.SECURITY)
        self.trigger_symbols()
        self.global_variables()
        self.custom_indicator()
        self.candidates = [self.candidate_1, self.candidate_2, self.candidate_3, self.candidate_4, self.candidate_5]
        self.risk_off = []
        self.last_risk_date = -1
        self.last_selection_date = -1

    def trigger_symbols(self):
        # 回测参数中依次绑定：GLD、SLV、XLE、SPY、QQQ、SOXX、FXI；也可换成同市场标的。
        self.candidate_1 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_2 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_3 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_4 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_5 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_6 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_7 = declare_trig_symbol(is_trade_symbol=True)

    def global_variables(self):
        self.holdings_num = show_variable(1, GlobalType.INT)
        self.enable_percent_drop_filter = show_variable(True, GlobalType.BOOL)
        self.drop_threshold_percent = show_variable(5.0, GlobalType.FLOAT)
        self.enable_atr_drop_filter = show_variable(False, GlobalType.BOOL)
        self.drop_threshold_atr = show_variable(2.0, GlobalType.FLOAT)
        self.drop_lookback_sessions = show_variable(3, GlobalType.INT)
        self.risk_check_hour = show_variable(9, GlobalType.INT)
        self.risk_check_minute = show_variable(40, GlobalType.INT)
        self.selection_hour = show_variable(10, GlobalType.INT)
        self.selection_minute = show_variable(0, GlobalType.INT)
        self.momentum_lookback_sessions = show_variable(5, GlobalType.INT)
        self.atr_period = show_variable(5, GlobalType.INT)
        # 1=Wilder，2=EMA，3=线性加权，4=简单平均。
        self.atr_weighting_mode = show_variable(1, GlobalType.INT)
        self.atr_warmup_sessions = show_variable(250, GlobalType.INT)
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
        if int(self.holdings_num) < 1 or int(self.holdings_num) > len(self.candidates):
            return False
        if int(self.drop_lookback_sessions) < 1 or int(self.momentum_lookback_sessions) < 2 or int(self.atr_period) < 2:
            return False
        if int(self.atr_weighting_mode) < 1 or int(self.atr_weighting_mode) > 4:
            return False
        if float(self.drop_threshold_percent) <= 0 or float(self.drop_threshold_atr) <= 0:
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
        # select=1 是当日未完成日线；从 select=2 开始只读取完整日线。
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

    def _atr_series(self, rows, period, mode):
        result = []
        for unused in rows:
            result.append(None)
        true_ranges = []
        row_index = 1
        while row_index < len(rows):
            high_value = rows[row_index][0]
            low_value = rows[row_index][1]
            previous_close = rows[row_index - 1][2]
            true_range = max(high_value - low_value, abs(high_value - previous_close), abs(low_value - previous_close))
            true_ranges.append(true_range)
            row_index += 1
        period = int(period)
        mode = int(mode)
        if len(true_ranges) < period:
            return result
        if mode == 3 or mode == 4:
            row_index = period
            while row_index < len(rows):
                total = 0.0
                offset = 0
                while offset < period:
                    true_range = true_ranges[row_index - period + offset]
                    if mode == 3:
                        total += (offset + 1) * true_range
                    else:
                        total += true_range
                    offset += 1
                if mode == 3:
                    result[row_index] = total / (period * (period + 1) / 2.0)
                else:
                    result[row_index] = total / period
                row_index += 1
            return result
        seed = 0.0
        offset = 0
        while offset < period:
            seed += true_ranges[offset]
            offset += 1
        atr_value = seed / period
        result[period] = atr_value
        if mode == 2:
            alpha = 2.0 / (period + 1.0)
        else:
            alpha = 1.0 / period
        row_index = period + 1
        while row_index < len(rows):
            atr_value = alpha * true_ranges[row_index - 1] + (1.0 - alpha) * atr_value
            result[row_index] = atr_value
            row_index += 1
        return result

    def _risk_rows_count(self):
        minimum_count = int(self.drop_lookback_sessions)
        if self.enable_atr_drop_filter:
            atr_minimum = int(self.drop_lookback_sessions) + int(self.atr_period) + 1
            if atr_minimum > minimum_count:
                minimum_count = atr_minimum
        requested_count = minimum_count
        if self.enable_atr_drop_filter and int(self.atr_warmup_sessions) > requested_count:
            requested_count = int(self.atr_warmup_sessions)
        return requested_count, minimum_count

    def _run_risk_check(self):
        self.risk_off = []
        if not self.enable_percent_drop_filter and not self.enable_atr_drop_filter:
            return
        requested_count, minimum_count = self._risk_rows_count()
        lookback = int(self.drop_lookback_sessions)
        for symbol in self.candidates:
            event_price = self._signal_price(symbol)
            rows = self._daily_rows(symbol, requested_count, minimum_count)
            triggered = False
            if event_price is None or rows is None:
                triggered = True
            else:
                atr_values = self._atr_series(rows, self.atr_period, self.atr_weighting_mode)
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
                    if self.enable_percent_drop_filter and percent_change <= -float(self.drop_threshold_percent) / 100.0:
                        triggered = True
                    atr_value = atr_values[row_index]
                    if self.enable_atr_drop_filter and atr_value is not None and atr_value > 0:
                        atr_change = (current_value - previous_close) / atr_value
                        if atr_change <= -float(self.drop_threshold_atr):
                            triggered = True
                    segment_index += 1
            if triggered:
                self.risk_off.append(symbol)
                if position_holding_qty(symbol=symbol) > 0:
                    close_positions(symbol=symbol)

    def _sort_scores(self, scores):
        first = 0
        while first < len(scores):
            best = first
            other = first + 1
            while other < len(scores):
                higher_score = scores[other][0] > scores[best][0]
                same_score_lower_code = scores[other][0] == scores[best][0] and scores[other][2] < scores[best][2]
                if higher_score or same_score_lower_code:
                    best = other
                other += 1
            if best != first:
                temporary = scores[first]
                scores[first] = scores[best]
                scores[best] = temporary
            first += 1
        return scores

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
        momentum = int(self.momentum_lookback_sessions)
        period = int(self.atr_period)
        minimum_count = momentum
        if period + 1 > minimum_count:
            minimum_count = period + 1
        requested_count = int(self.atr_warmup_sessions)
        if requested_count < minimum_count:
            requested_count = minimum_count
        scores = []
        for symbol in self.candidates:
            event_price = self._signal_price(symbol)
            rows = self._daily_rows(symbol, requested_count, minimum_count)
            if event_price is not None and rows is not None and not self._contains(self.risk_off, symbol):
                atr_values = self._atr_series(rows, period, self.atr_weighting_mode)
                atr_value = atr_values[-1]
                base_price = rows[-momentum][2]
                if atr_value is not None and atr_value > 0:
                    score = (event_price - base_price) / atr_value
                    scores.append([score, symbol, get_symbol_code(symbol=symbol)])
        self._sort_scores(scores)
        target_count = int(self.holdings_num)
        if target_count < 1:
            target_count = 1
        if target_count > len(self.candidates):
            target_count = len(self.candidates)
        targets = []
        score_index = 0
        while score_index < len(scores) and score_index < target_count:
            targets.append(scores[score_index][1])
            score_index += 1
        for symbol in self.candidates:
            if not self._contains(targets, symbol) and position_holding_qty(symbol=symbol) > 0:
                close_positions(symbol=symbol)
        target_percent = float(self.target_weight_percent) / target_count
        for symbol in targets:
            if position_holding_qty(symbol=symbol) <= 0:
                quantity = self._target_quantity(symbol, target_percent)
                if quantity > 0:
                    place_market(symbol=symbol, qty=quantity, side=OrderSide.BUY)
