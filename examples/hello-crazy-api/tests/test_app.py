import unittest

from app import health, version


class HelloCrazyApiTests(unittest.TestCase):
    def test_health(self) -> None:
        self.assertEqual(health(), {"status": "ok"})

    def test_version(self) -> None:
        self.assertIn("version", version())


if __name__ == "__main__":
    unittest.main()
