from __future__ import annotations


class BacktestError(Exception):
    code = "BACKTEST_ERROR"

    def __init__(self, message: str, *, detail=None):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


class BacktestValidationError(BacktestError):
    code = "BACKTEST_VALIDATION_ERROR"


class BacktestDataError(BacktestError):
    code = "BACKTEST_DATA_ERROR"


class BacktestOrderError(BacktestError):
    code = "BACKTEST_ORDER_ERROR"


class BacktestCancelled(BacktestError):
    code = "BACKTEST_CANCELLED"
