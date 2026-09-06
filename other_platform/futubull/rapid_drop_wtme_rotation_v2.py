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
        self.last_targets = []
        self.last_dynamic_symbols = []
        self.last_dynamic_values = []

    def trigger_symbols(self):
        # 依次建议绑定 GLD、SLV、XLE、SPY、QQQ、SOXX、DRAM、FXI。
        self.candidate_1 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_2 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_3 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_4 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_5 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_6 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_7 = declare_trig_symbol(is_trade_symbol=True)
        self.candidate_8 = declare_trig_symbol(is_trade_symbol=True)

    def global_variables(self):
        # 对应项目 RapidDropWtmeRotationStrategy 2.0.0。
        self.enable_percent_drop_filter = show_variable(True, GlobalType.BOOL)
        self.drop_threshold_percent = show_variable(5.0, GlobalType.FLOAT)
        self.drop_lookback_sessions = show_variable(5, GlobalType.INT)
        self.risk_check_hour = show_variable(9, GlobalType.INT)
        self.risk_check_minute = show_variable(50, GlobalType.INT)
        self.wtme_period = show_variable(13, GlobalType.INT)
        self.wtme_half_life = show_variable(6.0, GlobalType.FLOAT)
        self.wtme_epsilon = show_variable(0.00000001, GlobalType.FLOAT)
        self.selection_hour = show_variable(10, GlobalType.INT)
        self.selection_minute = show_variable(0, GlobalType.INT)
        self.buy_top_n = show_variable(1, GlobalType.INT)
        self.buy_condition_operator = show_variable(1, GlobalType.INT)  # 1=且，2=或
        self.buy_score_threshold = show_variable(-15.0, GlobalType.FLOAT)
        self.max_simultaneous_holdings = show_variable(1, GlobalType.INT)
        self.allocation_mode = show_variable(1, GlobalType.INT)  # 1=等权，2=线性，3=等权+k倍，4=线性+k倍
        self.enable_upside_sell_protection = show_variable(False, GlobalType.BOOL)

        # 富途代码策略没有项目式“单标的杠杆层”，故将 1～5 倍映射为 20%～100% 现金利用率。
        self.enable_volat_dynamic_leverage = show_variable(True, GlobalType.BOOL)
        self.volatility_period = show_variable(15, GlobalType.INT)
        self.stress_days = show_variable(10, GlobalType.INT)
        self.max_loss_percent = show_variable(40.0, GlobalType.FLOAT)
        self.max_dynamic_leverage = show_variable(5.0, GlobalType.FLOAT)
        self.rebalance_on_dynamic_leverage_change = show_variable(False, GlobalType.BOOL)

    def custom_indicator(self):
        pass

    def _data_prepare_hints(self):
        # 只供富途回测器静态发现数据依赖，不会执行。
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
        if int(self.buy_top_n) < 1 or int(self.buy_top_n) > len(self.candidates):
            return False
        if int(self.max_simultaneous_holdings) < 1 or int(self.max_simultaneous_holdings) > len(self.candidates):
            return False
        if int(self.buy_condition_operator) not in (1, 2) or int(self.allocation_mode) not in (1, 2, 3, 4):
            return False
        if int(self.volatility_period) < 2 or int(self.volatility_period) > 499 or int(self.stress_days) < 1:
            return False
        if float(self.max_loss_percent) <= 0 or float(self.max_dynamic_leverage) < 1:
            return False
        risk_minutes = int(self.risk_check_hour) * 60 + int(self.risk_check_minute)
        selection_minutes = int(self.selection_hour) * 60 + int(self.selection_minute)
        return risk_minutes < selection_minutes and risk_minutes >= 0 and selection_minutes <= 1439

    def _is_positive(self, value):
        if value is None:
            return False
        number = float(value)
        return number == number and abs(number) < 1.7976931348623157e308 and number > 0

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
        return float(value) if self._is_positive(value) else None

    def _daily_rows(self, symbol, requested_count, minimum_count):
        newest_first = []
        count = min(int(requested_count), 499)
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

    def _wtme_score(self, completed_rows, current_value, period, half_life, epsilon):
        if len(completed_rows) < period:
            return None
        rows = completed_rows[-period:]
        weight_total = 0.0
        index = 0
        while index < period:
            weight_total += power(2.0, -(period - 1 - index) / half_life)
            index += 1
        weighted_return = 0.0
        weighted_range = 0.0
        index = 0
        while index < period:
            weight = power(2.0, -(period - 1 - index) / half_life) / weight_total
            previous_close = rows[index][2]
            if index < period - 1:
                current_close = rows[index + 1][2]
                current_high = rows[index + 1][0]
                current_low = rows[index + 1][1]
                true_range = max(current_high - current_low, abs(current_high - previous_close), abs(current_low - previous_close))
            else:
                current_close = current_value
                true_range = abs(current_value - previous_close)
            weighted_return += weight * ((current_close - previous_close) / previous_close)
            weighted_range += weight * (true_range / previous_close)
            index += 1
        return 100.0 * weighted_return / (weighted_range + epsilon)

    def _dynamic_leverage(self, rows, current_value):
        if not self.enable_volat_dynamic_leverage:
            return 1.0
        period = int(self.volatility_period)
        if rows is None or len(rows) < period:
            return None
        closes = []
        index = len(rows) - period
        while index < len(rows):
            closes.append(rows[index][2])
            index += 1
        returns = []
        index = 1
        while index < len(closes):
            returns.append(math_log(closes[index] / closes[index - 1]))
            index += 1
        returns.append(math_log(float(current_value) / closes[-1]))
        total = sum(returns)
        average = total / period
        squared = 0.0
        for value in returns:
            squared += (value - average) * (value - average)
        volatility = power(squared / (period - 1), 0.5) * power(252.0, 0.5) * 100.0
        stress_loss = volatility * 3.0 * power(float(self.stress_days) / 252.0, 0.5)
        if stress_loss == 0:
            raw = float(self.max_dynamic_leverage)
        else:
            raw = float(self.max_loss_percent) / stress_loss
        bounded = min(float(self.max_dynamic_leverage), max(1.0, raw))
        return max(1.0, int((bounded + 0.000000000001) * 10.0) / 10.0)

    def _last_dynamic(self, symbol, fallback):
        index = 0
        while index < len(self.last_dynamic_symbols):
            if self.last_dynamic_symbols[index] == symbol:
                return self.last_dynamic_values[index]
            index += 1
        return fallback

    def _same_target_set(self, left, right):
        if len(left) != len(right):
            return False
        for symbol in left:
            if not self._contains(right, symbol):
                return False
        return True

    def _same_target_order(self, left, right):
        if len(left) != len(right):
            return False
        index = 0
        while index < len(left):
            if left[index] != right[index]:
                return False
            index += 1
        return True

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
        return int(raw_quantity / float(quantity_step)) * float(quantity_step)

    def _run_risk_check(self):
        self.risk_off = []
        lookback = int(self.drop_lookback_sessions)
        for symbol in self.candidates:
            event_price = self._signal_price(symbol)
            rows = self._daily_rows(symbol, lookback, lookback)
            triggered = event_price is None or rows is None
            if not triggered and self.enable_percent_drop_filter:
                index = len(rows) - lookback
                while index < len(rows):
                    previous_close = rows[index][2]
                    current_value = rows[index + 1][2] if index + 1 < len(rows) else event_price
                    if current_value / previous_close - 1.0 <= -float(self.drop_threshold_percent) / 100.0:
                        triggered = True
                    index += 1
            if triggered:
                self.risk_off.append(symbol)
                if position_holding_qty(symbol=symbol) > 0:
                    close_positions(symbol=symbol)

    def _insert_ranked(self, ranked, item):
        index = 0
        while index < len(ranked):
            existing = ranked[index]
            if item[0] > existing[0] or item[0] == existing[0] and item[1] < existing[1]:
                break
            index += 1
        ranked.insert(index, item)

    def _run_selection(self):
        history_count = max(int(self.wtme_period), int(self.volatility_period))
        ranked = []
        prices = []
        rows_by_symbol = []
        for symbol in self.candidates:
            event_price = self._signal_price(symbol)
            rows = self._daily_rows(symbol, history_count, history_count)
            if event_price is not None and rows is not None and not self._contains(self.risk_off, symbol):
                score = self._wtme_score(rows, event_price, int(self.wtme_period), float(self.wtme_half_life), float(self.wtme_epsilon))
                leverage = self._dynamic_leverage(rows, event_price)
                if score is not None and leverage is not None:
                    code = get_symbol_code(symbol=symbol)
                    self._insert_ranked(ranked, [score, code, symbol, leverage])
            prices.append(event_price)
            rows_by_symbol.append(rows)

        selected = []
        leverages = []
        index = 0
        while index < len(ranked) and len(selected) < int(self.max_simultaneous_holdings):
            rank_ok = index + 1 <= int(self.buy_top_n)
            score_ok = ranked[index][0] > float(self.buy_score_threshold)
            accepted = rank_ok and score_ok if int(self.buy_condition_operator) == 1 else rank_ok or score_ok
            if accepted:
                selected.append(ranked[index][2])
                leverages.append(ranked[index][3])
            index += 1

        protected = []
        for symbol in self.candidates:
            if position_holding_qty(symbol=symbol) <= 0 or self._contains(selected, symbol):
                continue
            protect = False
            if self.enable_upside_sell_protection:
                candidate_index = self.candidates.index(symbol)
                event_price = prices[candidate_index]
                rows = rows_by_symbol[candidate_index]
                protect = event_price is not None and rows is not None and event_price > rows[-1][2]
            if protect:
                protected.append(symbol)
            else:
                close_positions(symbol=symbol)

        active = []
        active_leverages = []
        slots = max(0, int(self.max_simultaneous_holdings) - len(protected))
        index = 0
        while index < len(selected):
            symbol = selected[index]
            if position_holding_qty(symbol=symbol) > 0:
                active.append(symbol)
                active_leverages.append(leverages[index])
            elif slots > 0:
                active.append(symbol)
                active_leverages.append(leverages[index])
                slots -= 1
            index += 1

        dynamic_changed = False
        index = 0
        while index < len(active):
            if abs(active_leverages[index] - self._last_dynamic(active[index], active_leverages[index])) > 0.000000000001:
                dynamic_changed = True
            index += 1
        rank_sensitive = int(self.allocation_mode) in (2, 4)
        should_rebalance = not self._same_target_set(active, self.last_targets)
        if rank_sensitive and not self._same_target_order(active, self.last_targets):
            should_rebalance = True
        if self.rebalance_on_dynamic_leverage_change and dynamic_changed:
            should_rebalance = True

        if should_rebalance and len(active) > 0:
            count = len(selected)
            denominator = count * (count + 1) / 2.0
            index = 0
            while index < len(active):
                symbol = active[index]
                selected_index = selected.index(symbol)
                if int(self.allocation_mode) in (2, 4):
                    base_percent = 100.0 * (count - selected_index) / denominator
                else:
                    base_percent = 100.0 / count
                strategy_leverage = count if int(self.allocation_mode) in (3, 4) else 1
                target_percent = base_percent * strategy_leverage * active_leverages[index] / float(self.max_dynamic_leverage)
                quantity = self._target_quantity(symbol, target_percent)
                current_quantity = position_holding_qty(symbol=symbol)
                if quantity > current_quantity:
                    place_market(symbol=symbol, qty=quantity - current_quantity, side=OrderSide.BUY)
                elif quantity < current_quantity:
                    place_market(symbol=symbol, qty=current_quantity - quantity, side=OrderSide.SELL)
                index += 1

        self.last_targets = list(active)
        self.last_dynamic_symbols = list(active)
        self.last_dynamic_values = list(active_leverages)
