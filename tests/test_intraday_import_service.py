from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import services.intraday_import_service as service


class IntradayImportServiceTests(unittest.TestCase):
    @patch.object(service, "datetime")
    def test_default_end_tracks_delayed_feed_without_hour_rounding(
        self,
        datetime_mock,
    ) -> None:
        datetime_mock.now.return_value = datetime(
            2026,
            7,
            30,
            10,
            47,
            35,
            tzinfo=timezone.utc,
        )

        self.assertEqual(
            service.default_import_end(),
            "2026-07-30T10:31:00Z",
        )


if __name__ == "__main__":
    unittest.main()
