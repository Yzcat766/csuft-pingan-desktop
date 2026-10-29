"""Run the offline suite with its fixed service-date fixture and a network guard."""

import unittest
import os
from datetime import datetime
from unittest.mock import patch
from storage import SHANGHAI


def main():
    # Existing simulated school responses intentionally use 2026-10-10.
    # Freeze before importing test modules, so overnight builds remain deterministic.
    with patch.dict(os.environ, {"PINGAN_API_AUTHORIZATION": "Basic bW9jazptb2Nr"}), patch(
        "storage.now_shanghai", return_value=datetime(2026, 10, 10, 12, tzinfo=SHANGHAI)
    ), patch(
        "requests.Session.request",
        side_effect=AssertionError("Real network forbidden during offline tests"),
    ):
        suite = unittest.defaultTestLoader.discover("tests")
        return (
            0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
        )


if __name__ == "__main__":
    raise SystemExit(main())
