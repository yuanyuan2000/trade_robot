from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import services.alpaca_data_client as alpaca
from services.api_errors import MissingAlpacaCredentialsError


def response(payload: dict, remaining: int) -> Mock:
    value = Mock()
    value.status_code = 200
    value.headers = {
        "X-RateLimit-Limit": "200",
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": "1800000000",
    }
    value.json.return_value = payload
    return value


class AlpacaDataClientTests(unittest.TestCase):
    @patch.object(alpaca, "ALPACA_SECRET", "")
    @patch.object(alpaca, "ALPACA_API_KEY", "")
    def test_missing_credentials_are_reported(self) -> None:
        with self.assertRaises(MissingAlpacaCredentialsError):
            alpaca.fetch_stock_bars("GLD")

    @patch.object(alpaca, "_wait_for_request_slot")
    @patch.object(alpaca, "_http_session")
    @patch.object(alpaca, "ALPACA_SECRET", "test-secret")
    @patch.object(alpaca, "ALPACA_API_KEY", "test-key")
    def test_bars_are_parsed_and_pages_are_followed(
            self,
            http_session,
            _wait_for_slot,
    ) -> None:
        request_get = http_session.return_value.get
        request_get.side_effect = [
            response(
                {
                    "bars": {
                        "GLD": [{
                            "t": "2020-01-02T14:30:00Z",
                            "o": 143.86,
                            "h": 143.9,
                            "l": 143.8,
                            "c": 143.89,
                            "v": 100,
                            "n": 5,
                            "vw": 143.87,
                        }]
                    },
                    "next_page_token": "page-2",
                },
                199,
            ),
            response(
                {
                    "bars": {
                        "GLD": [{
                            "t": "2020-01-02T14:31:00Z",
                            "o": 143.89,
                            "h": 143.95,
                            "l": 143.88,
                            "c": 143.94,
                            "v": 120,
                            "n": 6,
                            "vw": 143.92,
                        }]
                    },
                    "next_page_token": None,
                },
                198,
            ),
        ]

        result = alpaca.fetch_stock_bars(
            "gld",
            timeframe="1min",
            start="2020-01-02",
            end="2020-01-03",
            limit=1,
            max_pages=2,
        )

        self.assertEqual(result["symbol"], "GLD")
        self.assertEqual(result["timeframe"], "1Min")
        self.assertEqual(result["feed"], "sip")
        self.assertEqual(result["data_count"], 2)
        self.assertTrue(result["pagination"]["complete"])
        self.assertEqual(result["rate_limit"]["limit"], 200)
        self.assertEqual(
            request_get.call_args_list[1].kwargs["params"]["page_token"],
            "page-2",
        )
        self.assertEqual(
            request_get.call_args_list[0].kwargs["headers"],
            {
                "APCA-API-KEY-ID": "test-key",
                "APCA-API-SECRET-KEY": "test-secret",
            },
        )

    @patch.object(alpaca, "_wait_for_request_slot")
    @patch.object(alpaca, "_http_session")
    @patch.object(alpaca, "ALPACA_SECRET", "test-secret")
    @patch.object(alpaca, "ALPACA_API_KEY", "test-key")
    def test_default_one_page_returns_resume_token(
            self,
            http_session,
            _wait_for_slot,
    ) -> None:
        request_get = http_session.return_value.get
        request_get.return_value = response(
            {
                "bars": {
                    "GLD": [{
                        "t": "2020-01-02T14:30:00Z",
                        "o": 1,
                        "h": 2,
                        "l": 1,
                        "c": 2,
                        "v": 10,
                    }]
                },
                "next_page_token": "resume-here",
            },
            199,
        )

        result = alpaca.fetch_stock_bars(
            "GLD",
            start="2020-01-02",
            end="2020-01-03",
        )

        self.assertFalse(result["pagination"]["complete"])
        self.assertEqual(
            result["pagination"]["next_page_token"],
            "resume-here",
        )
        request_get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
