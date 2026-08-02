from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, getcontext
from typing import Iterable

from services.backtest.errors import BacktestOrderError


getcontext().prec = 28
ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
EPSILON = Decimal("0.00000001")


def D(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass
class Lot:
    quantity: Decimal
    unit_cost: Decimal


@dataclass
class Position:
    symbol: str
    quantity: Decimal = ZERO
    lots: list[Lot] = field(default_factory=list)

    @property
    def cost_basis(self) -> Decimal:
        return sum((lot.quantity * lot.unit_cost for lot in self.lots), ZERO)

    @property
    def average_cost(self) -> Decimal:
        if self.quantity <= ZERO:
            return ZERO
        return self.cost_basis / self.quantity


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    action: str
    sizing_mode: str
    value_percent: float
    reason: str
    minimum_trade_value: float = 0.0


class Portfolio:
    def __init__(
        self,
        initial_cash: float,
        *,
        commission_per_share: float = 0,
        minimum_commission: float = 0,
        slippage_bps: float = 0,
        allow_fractional_shares: bool = False,
        quantity_steps: dict[str, float] | None = None,
    ):
        if D(initial_cash) <= ZERO:
            raise BacktestOrderError("初始资金必须大于 0。")
        self.initial_cash = D(initial_cash)
        self.cash = D(initial_cash)
        self.commission_per_share = D(commission_per_share)
        self.minimum_commission = D(minimum_commission)
        self.slippage_bps = D(slippage_bps)
        self.allow_fractional_shares = bool(allow_fractional_shares)
        self.quantity_steps = {
            symbol: D(step)
            for symbol, step in (quantity_steps or {}).items()
            if D(step) > ZERO
        }
        self.positions: dict[str, Position] = {}
        self.total_commission = ZERO
        self.total_slippage = ZERO
        self.realized_pnl = ZERO
        self.receivables: list[dict] = []

    def _position(self, symbol: str) -> Position:
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol=symbol)
        return self.positions[symbol]

    def quantity(self, symbol: str) -> Decimal:
        return self._position(symbol).quantity

    def commission(self, quantity: Decimal) -> Decimal:
        if quantity <= ZERO:
            return ZERO
        return max(quantity * self.commission_per_share, self.minimum_commission)

    def fill_price(self, side: str, reference_price: float | Decimal) -> Decimal:
        reference = D(reference_price)
        if reference <= ZERO:
            raise BacktestOrderError("成交参考价格必须大于 0。")
        rate = self.slippage_bps / Decimal("10000")
        return reference * (ONE + rate if side == "BUY" else ONE - rate)

    def market_value(self, marks: dict[str, float | Decimal]) -> Decimal:
        total = ZERO
        for symbol, position in self.positions.items():
            if position.quantity <= ZERO:
                continue
            if symbol not in marks:
                raise BacktestOrderError(f"缺少 {symbol} 的盯市价格。")
            total += position.quantity * D(marks[symbol])
        return total

    @property
    def receivable_value(self) -> Decimal:
        return sum((item["amount"] for item in self.receivables), ZERO)

    def equity(self, marks: dict[str, float | Decimal]) -> Decimal:
        return self.cash + self.receivable_value + self.market_value(marks)

    def weight(self, symbol: str, marks: dict[str, float | Decimal]) -> Decimal:
        equity = self.equity(marks)
        if equity <= ZERO:
            return ZERO
        return self.quantity(symbol) * D(marks[symbol]) / equity

    def snapshot(self, marks: dict[str, float | Decimal]) -> dict:
        result = {}
        equity = self.equity(marks)
        for symbol, position in sorted(self.positions.items()):
            if position.quantity <= ZERO:
                continue
            price = D(marks[symbol])
            market_value = position.quantity * price
            result[symbol] = {
                "quantity": float(position.quantity),
                "average_cost": float(position.average_cost),
                "price": float(price),
                "market_value": float(market_value),
                "weight": float(market_value / equity) if equity > ZERO else 0.0,
                "unrealized_pnl": float(market_value - position.cost_basis),
            }
        return result

    def apply_corporate_actions(
        self,
        actions: list[dict],
        *,
        trading_date: str,
    ) -> list[dict]:
        events: list[dict] = []
        for action in actions:
            symbol = action["symbol"]
            position = self._position(symbol)
            if action["action_type"] in {"forward_split", "reverse_split"}:
                old_rate = D(action["old_rate"])
                new_rate = D(action["new_rate"])
                if old_rate <= ZERO or new_rate <= ZERO:
                    raise BacktestOrderError(f"{symbol} 拆股比例无效。")
                ratio = new_rate / old_rate
                quantity_before = position.quantity
                if quantity_before > ZERO:
                    quantity_after = position.quantity * ratio
                    if (
                        not self.allow_fractional_shares
                        and quantity_after
                        != quantity_after.to_integral_value()
                    ):
                        raise BacktestOrderError(
                            f"{symbol} 拆股会产生零股，但公司行动数据没有现金替代金额；"
                            "为避免虚构成交，回测已停止。可启用碎股后重新运行。"
                        )
                    position.quantity = quantity_after
                    for lot in position.lots:
                        lot.quantity *= ratio
                        lot.unit_cost /= ratio
                events.append(
                    {
                        "type": "split",
                        "symbol": symbol,
                        "ratio": float(ratio),
                        "quantity_before": float(quantity_before),
                        "quantity_after": float(position.quantity),
                        "date": trading_date,
                    }
                )
            elif action["action_type"] == "cash_dividend":
                rate = D(action["cash_rate"])
                if rate < ZERO:
                    raise BacktestOrderError(f"{symbol} 现金分红金额无效。")
                amount = position.quantity * rate
                if amount <= ZERO:
                    continue
                payable_date = action.get("payable_date") or trading_date
                item = {
                    "symbol": symbol,
                    "amount": amount,
                    "payable_date": payable_date,
                    "ex_date": trading_date,
                    "rate": rate,
                }
                if payable_date <= trading_date:
                    self.cash += amount
                else:
                    self.receivables.append(item)
                events.append(
                    {
                        "type": "cash_dividend",
                        "symbol": symbol,
                        "rate": float(rate),
                        "amount": float(amount),
                        "payable_date": payable_date,
                        "date": trading_date,
                    }
                )
        return events

    def settle_receivables(self, trading_date: str) -> list[dict]:
        paid: list[dict] = []
        pending: list[dict] = []
        for item in self.receivables:
            if item["payable_date"] <= trading_date:
                self.cash += item["amount"]
                paid.append(
                    {
                        **item,
                        "amount": float(item["amount"]),
                        "rate": float(item["rate"]),
                        "paid_date": trading_date,
                    }
                )
            else:
                pending.append(item)
        self.receivables = pending
        return paid

    def execute(
        self,
        intent: OrderIntent,
        *,
        reference_price: float,
        marks: dict[str, float | Decimal],
        max_weight_percent: float = 100,
        event_time: str,
    ) -> dict | None:
        action = intent.action.upper()
        sizing_mode = intent.sizing_mode.upper()
        if action not in {"BUY", "SELL"}:
            return None
        if sizing_mode not in {"TARGET", "DELTA"}:
            raise BacktestOrderError("订单仓位模式必须为 TARGET 或 DELTA。")
        requested = D(intent.value_percent) / HUNDRED
        if requested < ZERO or requested > ONE:
            raise BacktestOrderError("订单仓位比例必须在 0% 至 100%。")
        maximum = D(max_weight_percent) / HUNDRED
        position = self._position(intent.symbol)
        current_equity = self.equity(marks)
        if current_equity <= ZERO:
            raise BacktestOrderError("账户权益不为正，无法继续交易。")
        current_value = position.quantity * D(reference_price)
        current_weight = current_value / current_equity

        if action == "BUY":
            target_weight = (
                requested
                if sizing_mode == "TARGET"
                else current_weight + requested
            )
            if target_weight > maximum + EPSILON:
                raise BacktestOrderError(
                    f"{intent.symbol} 订单目标仓位 {float(target_weight * HUNDRED):.4f}% "
                    f"超过上限 {float(maximum * HUNDRED):.4f}%。"
                )
            if target_weight <= current_weight + EPSILON:
                return None
            desired_value = current_equity * target_weight - current_value
            return self._buy(
                intent,
                desired_value=desired_value,
                reference_price=D(reference_price),
                marks=marks,
                event_time=event_time,
                current_equity=current_equity,
                current_value=current_value,
                target_weight=target_weight,
            )

        target_weight = (
            requested
            if sizing_mode == "TARGET"
            else current_weight - requested
        )
        if target_weight < -EPSILON:
            raise BacktestOrderError(
                f"{intent.symbol} 卖出订单会使目标仓位低于 0%。"
            )
        target_weight = max(ZERO, target_weight)
        if target_weight >= current_weight - EPSILON:
            return None
        return self._sell(
            intent,
            reference_price=D(reference_price),
            marks=marks,
            event_time=event_time,
            sell_all=target_weight == ZERO,
            current_equity=current_equity,
            current_value=current_value,
            target_weight=target_weight,
        )

    def _quantity_step(self, symbol: str) -> Decimal:
        if symbol in self.quantity_steps:
            return self.quantity_steps[symbol]
        return Decimal("0.000001") if self.allow_fractional_shares else ONE

    def _rounded_quantity(
        self,
        symbol: str,
        raw: Decimal,
        *,
        round_up: bool = False,
    ) -> Decimal:
        step = self._quantity_step(symbol)
        units = (raw / step).to_integral_value(
            rounding=ROUND_CEILING if round_up else ROUND_FLOOR
        )
        return units * step

    def _buy(
        self,
        intent: OrderIntent,
        *,
        desired_value: Decimal,
        reference_price: Decimal,
        marks: dict[str, float | Decimal],
        event_time: str,
        current_equity: Decimal,
        current_value: Decimal,
        target_weight: Decimal,
    ) -> dict | None:
        fill = self.fill_price("BUY", reference_price)
        raw_upper = min(desired_value / fill, self.cash / fill)

        def allowed(quantity: Decimal) -> bool:
            if quantity <= ZERO:
                return True
            commission = self.commission(quantity)
            total_cost = quantity * fill + commission
            slippage = (fill - reference_price) * quantity
            projected_equity = current_equity - commission - slippage
            projected_value = current_value + quantity * reference_price
            return (
                total_cost <= self.cash + EPSILON
                and
                projected_equity > ZERO
                and projected_value / projected_equity <= target_weight + EPSILON
            )

        step = self._quantity_step(intent.symbol)
        low = 0
        high = max(0, int((raw_upper / step).to_integral_value(rounding=ROUND_FLOOR)))
        best = 0
        while low <= high:
            midpoint = (low + high) // 2
            if allowed(D(midpoint) * step):
                best = midpoint
                low = midpoint + 1
            else:
                high = midpoint - 1
        quantity = D(best) * step
        if quantity <= ZERO:
            return None
        if quantity * reference_price < D(intent.minimum_trade_value):
            return None
        commission = self.commission(quantity)
        gross = quantity * fill
        total_cost = gross + commission
        if total_cost > self.cash + EPSILON:
            raise BacktestOrderError("可用现金不足以支付买入金额和手续费。")

        position = self._position(intent.symbol)
        position.lots.append(
            Lot(quantity=quantity, unit_cost=(gross + commission) / quantity)
        )
        position.quantity += quantity
        self.cash -= total_cost
        slippage = (fill - reference_price) * quantity
        self.total_commission += commission
        self.total_slippage += slippage
        return self._trade_dict(
            intent,
            event_time=event_time,
            side="BUY",
            quantity=quantity,
            reference_price=reference_price,
            fill_price=fill,
            gross=gross,
            commission=commission,
            slippage=slippage,
            realized_pnl=None,
            marks=marks,
        )

    def _sell(
        self,
        intent: OrderIntent,
        *,
        reference_price: Decimal,
        marks: dict[str, float | Decimal],
        event_time: str,
        sell_all: bool,
        current_equity: Decimal,
        current_value: Decimal,
        target_weight: Decimal,
    ) -> dict | None:
        position = self._position(intent.symbol)
        if position.quantity <= ZERO:
            return None
        fill = self.fill_price("SELL", reference_price)
        if sell_all:
            quantity = position.quantity
        else:
            def allowed(candidate: Decimal) -> bool:
                if candidate <= ZERO:
                    return False
                commission = self.commission(candidate)
                gross = candidate * fill
                slippage = (reference_price - fill) * candidate
                projected_equity = current_equity - commission - slippage
                projected_value = current_value - candidate * reference_price
                return (
                    self.cash + gross >= commission - EPSILON
                    and projected_equity > ZERO
                    and projected_value / projected_equity
                    <= target_weight + EPSILON
                )

            step = self._quantity_step(intent.symbol)
            low = 1
            high = int((position.quantity / step).to_integral_value(rounding=ROUND_FLOOR))
            best: int | None = None
            while low <= high:
                midpoint = (low + high) // 2
                if allowed(D(midpoint) * step):
                    best = midpoint
                    high = midpoint - 1
                else:
                    low = midpoint + 1
            if best is None:
                raise BacktestOrderError(
                    f"{intent.symbol} 无法在支付手续费后达到目标仓位。"
                )
            quantity = D(best) * step
            if not allowed(quantity):
                raise BacktestOrderError(
                    f"{intent.symbol} 舍入后无法达到目标仓位。"
                )
            if quantity * reference_price < D(intent.minimum_trade_value):
                return None
        if quantity <= ZERO:
            return None
        commission = self.commission(quantity)
        gross = quantity * fill
        if self.cash + gross < commission - EPSILON:
            raise BacktestOrderError("卖出所得与现金不足以支付最低手续费。")

        remaining = quantity
        removed_cost = ZERO
        while remaining > EPSILON:
            lot = position.lots[0]
            consumed = min(lot.quantity, remaining)
            removed_cost += consumed * lot.unit_cost
            lot.quantity -= consumed
            remaining -= consumed
            if lot.quantity <= EPSILON:
                position.lots.pop(0)
        position.quantity -= quantity
        if position.quantity <= EPSILON:
            position.quantity = ZERO
            position.lots.clear()
        self.cash += gross - commission
        realized = gross - commission - removed_cost
        slippage = (reference_price - fill) * quantity
        self.realized_pnl += realized
        self.total_commission += commission
        self.total_slippage += slippage
        return self._trade_dict(
            intent,
            event_time=event_time,
            side="SELL",
            quantity=quantity,
            reference_price=reference_price,
            fill_price=fill,
            gross=gross,
            commission=commission,
            slippage=slippage,
            realized_pnl=realized,
            marks=marks,
        )

    def _trade_dict(
        self,
        intent: OrderIntent,
        *,
        event_time: str,
        side: str,
        quantity: Decimal,
        reference_price: Decimal,
        fill_price: Decimal,
        gross: Decimal,
        commission: Decimal,
        slippage: Decimal,
        realized_pnl: Decimal | None,
        marks: dict[str, float | Decimal],
    ) -> dict:
        position = self._position(intent.symbol)
        equity = self.equity(marks)
        mark_price = D(marks[intent.symbol])
        position_value = position.quantity * mark_price
        return {
            "event_time": event_time,
            "symbol": intent.symbol,
            "side": side,
            "quantity": float(quantity),
            "reference_price": float(reference_price),
            "fill_price": float(fill_price),
            "gross_amount": float(gross),
            "commission": float(commission),
            "slippage_amount": float(slippage),
            "realized_pnl": float(realized_pnl) if realized_pnl is not None else None,
            "cash_after": float(self.cash),
            "position_quantity_after": float(position.quantity),
            "position_value_after": float(position_value),
            "position_weight_after": (
                float(position_value / equity) if equity > ZERO else 0.0
            ),
            "reason": intent.reason,
        }
