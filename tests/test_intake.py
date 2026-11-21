"""检查数据包可读取，格式破损会被拒绝。"""
import csv
import shutil
import tempfile
import unittest
from pathlib import Path

from intake import ROOT, read_tables


class IntakeTest(unittest.TestCase):
    def test_complete_sample(self):
        result = read_tables(ROOT / "sample")
        self.assertEqual(set(result), {path.stem for path in (ROOT / "sample").glob("*.csv")})
        for name, rows in result.items():
            with (ROOT / "sample" / (name + ".csv")).open(encoding="utf-8", newline="") as stream:
                self.assertEqual(len(rows), sum(1 for _ in csv.DictReader(stream)))
            self.assertTrue(rows, name)

    def test_bad_header(self):
        with tempfile.TemporaryDirectory() as scratch:
            target = Path(scratch) / "sample"
            shutil.copytree(ROOT / "sample", target)
            first = sorted(target.glob("*.csv"))[0]
            first.write_text("破损表头\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "表头"):
                read_tables(target)


if __name__ == "__main__":
    unittest.main()
