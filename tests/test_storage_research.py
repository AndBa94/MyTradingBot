import tempfile
import unittest

from app.storage import Store


class StorageResearchTests(unittest.TestCase):
    def test_store_exposes_research_report(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        store = Store(tmp.name)
        try:
            report = store.research_report(minimum_sample=30)
            self.assertEqual(report["status"], "COLLECT_MORE_DATA")
            self.assertEqual(report["sample"]["trades"], 0)
        finally:
            store.db.close()


if __name__ == "__main__":
    unittest.main()
