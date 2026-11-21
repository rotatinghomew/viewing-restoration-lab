"""读取表格并检查列与标量格式。"""
import argparse
import csv
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def boolean(text):
    if text not in {"0", "1"}:
        raise ValueError("布尔值应为 0 或 1")
    return text == "1"


def finite_number(text):
    result = float(text)
    if not math.isfinite(result):
        raise ValueError("数值应为有限小数")
    return result


CONVERT = {"str": str, "int": int, "float": finite_number, "bool": boolean}


def read_tables(directory):
    schema = json.loads((ROOT / "table-format.json").read_text(encoding="utf-8"))
    tables = {}
    for table, columns in schema.items():
        path = Path(directory) / (table + ".csv")
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != list(columns):
                raise ValueError(f"{path.name}：表头和格式约定不一致")
            rows = []
            for index, record in enumerate(reader, start=2):
                if None in record or None in record.values():
                    raise ValueError(f"{path.name}:{index}：列数不符")
                try:
                    rows.append({field: CONVERT[kind](record[field]) for field, kind in columns.items()})
                except ValueError as error:
                    raise ValueError(f"{path.name}:{index}：字段值无效") from error
            tables[table] = rows
    return tables


def main():
    parser = argparse.ArgumentParser(description="检查输入记录格式并列出各表行数")
    parser.add_argument("directory", type=Path, nargs="?", default=ROOT / "sample")
    arguments = parser.parse_args()
    try:
        tables = read_tables(arguments.directory)
    except (OSError, ValueError) as error:
        parser.exit(2, f"资料读取失败：{error}\n")
    print(json.dumps({name: len(rows) for name, rows in tables.items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
