"""检查交换资料的标识、文本和跨表引用。"""

import json
from pathlib import Path
import unittest


DATA_DIR = Path(__file__).resolve().parents[1] / "data"


class SourceDataTest(unittest.TestCase):
    def setUp(self):
        self.tables = {}
        for path in sorted(DATA_DIR.glob("*.json")):
            rows = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(rows, list)
            self.assertTrue(rows, path.name)
            by_id = {}
            for row in rows:
                identifier = row["id"]
                self.assertIsInstance(identifier, str, path.name)
                self.assertNotIn(identifier, by_id, path.name)
                if "text" in row:
                    self.assertIsInstance(row["text"], str, path.name)
                by_id[identifier] = row
            self.tables[path.stem] = by_id

    def test_exchange_tables_present(self):
        self.assertEqual(set(self.tables), {'customers', 'inventory', 'policy'})

    def test_related_source_records(self):
        data = self.tables
        stock = data["inventory"]
        self.assertEqual(stock["K3"]["series"], stock["K5"]["series"])
        self.assertNotEqual(stock["K3"]["store"], stock["K5"]["store"])
        self.assertEqual(stock["K1"]["pricing"], "gram")
        self.assertEqual(stock["K3"]["pricing"], "fixed")
        customers = data["customers"]
        self.assertIn(customers["O2"]["request"], customers)
        self.assertLess(customers["O2"]["at"], data["policy"]["P5"]["at"])
        self.assertLess(data["policy"]["P5"]["at"], customers["O5"]["at"])
