from __future__ import annotations


class MarketDataError(Exception):
    code = "UNKNOWN_ERROR"
    message = "行情服务发生未知错误。"

    def __init__(self, message: str | None = None, detail: str | None = None):
        super().__init__(message or self.message)
        self.message = message or self.message
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


class MissingApiKeyError(MarketDataError):
    code = "MISSING_API_KEY"
    message = "未配置 Twelve Data API Key，请检查 .env。"


class InvalidApiKeyError(MarketDataError):
    code = "INVALID_API_KEY"
    message = "Twelve Data API Key 无效，请检查配置。"


class MissingAlpacaCredentialsError(MarketDataError):
    code = "MISSING_ALPACA_CREDENTIALS"
    message = "未配置 Alpaca API Key 或 Secret，请检查 .env。"


class InvalidAlpacaCredentialsError(MarketDataError):
    code = "INVALID_ALPACA_CREDENTIALS"
    message = "Alpaca API Key 或 Secret 无效，请检查配置。"


class SymbolNotFoundError(MarketDataError):
    code = "SYMBOL_NOT_FOUND"
    message = "没有找到该股票代码，请确认代码是否正确。"


class RateLimitedError(MarketDataError):
    code = "RATE_LIMITED"
    message = "Twelve Data 请求额度已达上限，请稍后再试。"


class NetworkTimeoutError(MarketDataError):
    code = "NETWORK_TIMEOUT"
    message = "行情服务请求超时，请稍后重试。"


class NetworkError(MarketDataError):
    code = "NETWORK_ERROR"
    message = "暂时无法连接行情服务，请稍后重试。"


class EmptyDataError(MarketDataError):
    code = "EMPTY_DATA"
    message = "行情服务没有返回可用数据。"


class InvalidResponseError(MarketDataError):
    code = "INVALID_RESPONSE"
    message = "行情服务返回格式异常。"


class DataParseError(MarketDataError):
    code = "DATA_PARSE_ERROR"
    message = "行情数据解析失败。"
