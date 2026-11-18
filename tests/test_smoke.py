import unittest

from tests import helpers  # noqa: F401  (puts scripts/ on sys.path)

import ats


class SmokeTest(unittest.TestCase):
    def test_version(self):
        self.assertEqual(ats.VERSION, "0.1.0")


if __name__ == "__main__":
    unittest.main()
