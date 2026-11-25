#!/usr/bin/env python3
"""Command line entry point for ingestion and customer-service queries."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from intake import ROOT
from loss_service.engine import LossService, material_time
from loss_service.store import Store

DEFAULT_DB = ROOT / ".runtime" / "loss.sqlite3"


def open_service(db_path: Path, ingest: str | Path | None = None) -> tuple[Store, LossService]:
    fresh = not Path(db_path).exists()
    store = Store(db_path)
    if ingest is not None:
        stats = store.ingest_directory(ingest)
        print(f"已导入：{json.dumps(stats, ensure_ascii=False)}", file=sys.stderr)
    elif fresh:
        sample = ROOT / "sample"
        if sample.exists():
            stats = store.ingest_directory(sample)
            print(f"首次运行已导入 sample：{json.dumps(stats, ensure_ascii=False)}", file=sys.stderr)
    return store, LossService.from_store(store)


def print_order_text(report: dict) -> None:
    print(f"订单 {report['order']}｜会员 {report['person']}｜逻辑时钟 as_of={report['as_of']}｜节目 {report['program_count']} 个")
    for a in report["assessments"]:
        loss = a["loss"]
        comp = a["compensation"]
        print("\n" + "=" * 78)
        print(
            f"节目 {a['program']}｜地区 {a['region']}｜决定 {a['decision']}\n"
            f"  损失下界(相干解释最小)={loss['confirmed_seconds']}s；上界={loss['upper_seconds']}s；"
            f"可点名确定片段合计={loss['definite_named_seconds']}s\n"
            f"  确定错过={fmt_intervals(loss['definite'])}；可能错过={fmt_intervals(loss['possible'])}"
        )
        if loss["confirmed_seconds"] != loss["definite_named_seconds"]:
            print("  注意：下界总秒数大于确定片段长度，差额来自互斥校准解释，不能把不同解释相加。")
        for cal in a["calibration"]:
            if cal["status"] != "ok":
                print(f"  ⚠ 校准异常 {cal['session']}: {cal['status']} {cal['reason'] or ''}")
        rw = a["rewatched"]
        if rw["intervals"]:
            print(f"  已合法回看={fmt_intervals(rw['intervals'])}")
        if a["receipts"]:
            print("  回执：")
            for e in a["receipts"]:
                hist = ",".join(f"{h['state']}@{h['utc']}" for h in e["status_history"])
                print(f"    - {e['event']} {e['kind']} {e['interval']} => {e['state']} ({hist})")
        avail = comp["available_after_holds"]
        print(
            f"  扣除回看/已发/未明回执后：下界 {avail['lower_seconds']}s / 上界 {avail['upper_seconds']}s；"
            f"可补确定片段={fmt_intervals(avail['definite'])}"
        )
        unknown_count = len([e for e in a["receipts"] if e["state"] == "unknown"])
        for rec in comp["replay_recommendations"]:
            f = rec["fulfillment"]
            if not rec.get("can_issue"):
                flag = "门槛/校准未满足，仅可复核"
            elif unknown_count:
                flag = "片段不与未明回执重合，但发放前必须先核对回执"
            elif rec.get("requires_manual_approval_due_to_posted_receipt"):
                flag = "规则可补；因已有已发回执，需人工确认差额，禁止静默重发"
            else:
                flag = "可由客服发放"
            print(
                f"  回看方案（{flag}）：{rec['segment']} ({rec['seconds']}s) -> {rec['suggested_replay']}；"
                f"有效期内可看 {f['available_watch_seconds']}s"
            )
        credit = comp.get("credit_recommendation")
        if credit:
            if not credit.get("can_issue"):
                flag = "门槛/片段清晰度不足，仅可复核"
            elif unknown_count:
                flag = "发放前必须先核对未明回执"
            elif credit.get("requires_manual_approval_due_to_posted_receipt"):
                flag = "已有已发回执，需人工确认是否存在额外差额"
            else:
                flag = "可由客服发放"
            print(f"  积分方案（{flag}）：{credit['amount']} 分，片段 {credit['segments']}")
        for item in a["manual_items"]:
            print(f"  ⚠ 人工项[{item['code']}]：{item['message']}")


def fmt_intervals(values: list[list[int]] | list[tuple[int, int]]) -> str:
    if not values:
        return "无"
    return ", ".join(f"[{int(a)},{int(b)})" for a, b in values)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="观赛损失与补偿客服系统")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 状态库路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="导入同格式 CSV 目录（可重复/增量）")
    p_ingest.add_argument("directory", nargs="?", default=str(ROOT / "sample"))

    p_order = sub.add_parser("order", aliases=["query"], help="按订单查询")
    p_order.add_argument("order")
    p_order.add_argument("--at", type=int, default=None, help="逻辑 UTC 时钟；默认资料中最晚时间")
    p_order.add_argument("--json", action="store_true")
    p_order.add_argument("--ingest", default=None, help="查询前额外导入一个 CSV 目录")

    p_queue = sub.add_parser("queue", help="复核/人工队列")
    p_queue.add_argument("--at", type=int, default=None)
    p_queue.add_argument("--json", action="store_true")

    p_serve = sub.add_parser("serve", help="启动客服查询 HTTP 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db)
    ingest_dir = getattr(args, "ingest", None)
    store, service = open_service(db_path, ingest_dir)
    try:
        if args.command == "ingest":
            stats = store.ingest_directory(args.directory)
            print(json.dumps(stats, ensure_ascii=False, indent=2))
            return 0
        if args.command == "serve":
            from loss_service.web import serve

            httpd = serve(db_path, args.host, args.port)
            print(f"客服查询服务：http://{args.host}:{args.port}/", file=sys.stderr)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\n停止服务", file=sys.stderr)
            finally:
                httpd.server_close()
            return 0
        if args.command in {"order", "query"}:
            try:
                report = service.order(args.order, args.at)
            except KeyError as error:
                print(f"查询失败：{error.args[0] if error.args else error}", file=sys.stderr)
                return 2
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print_order_text(report)
            return 0
        if args.command == "queue":
            report = service.queue(args.at)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print(f"as_of={report['as_of']}，待处理 {len(report['items'])} 单/场")
                for item in report["items"]:
                    print(
                        f"- {item['order']} {item['program']} person={item['person']} "
                        f"{item['decision']} low={item['confirmed_seconds']} up={item['upper_seconds']} "
                        f"codes={','.join(item['manual_codes'])}"
                    )
            return 0
    finally:
        store.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
