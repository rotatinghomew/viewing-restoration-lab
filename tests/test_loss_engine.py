"""Nuanced loss-bound, calibration, receipt and replay tests."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from intake import ROOT, read_tables
from loss_service.engine import LossService, assess_entitlement, calibration_domains
from loss_service.store import Store, read_csv_subset


def minimal_tables() -> dict[str, list[dict]]:
    return {
        "programs": [
            {
                "id": "g",
                "start_utc": 0,
                "end_utc": 1000,
                "local_day_boundary_utc": 400,
                "critical_from": 100,
                "critical_to": 200,
                "threshold": 100,
                "credit": 50,
            }
        ],
        "entitlements": [
            {
                "order": "o",
                "person": "p",
                "region": "R",
                "program": "g",
                "buy_utc": -10,
                "source_scope": "s",
            }
        ],
        "outages": [
            {"id": "o1", "source": "s", "program": "g", "from_pos": 0, "to_pos": 300, "known_at": 0}
        ],
        "playback": [],
        "session_limits": [],
        "calibration": [],
        "rewatched": [],
        "replays": [],
        "receipts": [],
    }


class LossBoundTest(unittest.TestCase):
    def test_one_shared_offset_proves_seconds_but_not_a_named_segment(self):
        tables = minimal_tables()
        tables["session_limits"].append(
            {"session": "a", "offset_min": 0, "offset_max": 100}
        )
        tables["playback"].append(
            {
                "id": "x",
                "person": "p",
                "program": "g",
                "source": "s",
                "from_pos": 0,
                "to_pos": 100,
                "state": "stalled",
                "known_at": 0,
                "session": "a",
            }
        )
        ent = tables["entitlements"][0]
        report = assess_entitlement(tables, ent, 0)
        loss = report["loss"]
        # Every offset interpretation has a 100-second stalled interval.
        self.assertEqual(loss["confirmed_seconds"], 100)
        # But offsets 0..100 make the two extremes disjoint at their endpoint.
        self.assertEqual(loss["definite"], [])
        self.assertEqual(loss["definite_named_seconds"], 0)
        self.assertEqual(loss["observed_only_upper_seconds"], 100)
        self.assertEqual(loss["observed_possible"], [[0, 200]])
        # The broad attributable outage ceiling is separately visible.
        self.assertGreaterEqual(loss["upper_seconds"], 100)

    def test_partial_offset_range_leaves_core_and_shift(self):
        tables = minimal_tables()
        tables["session_limits"].append({"session": "a", "offset_min": 0, "offset_max": 50})
        tables["playback"].append(
            {
                "id": "x",
                "person": "p",
                "program": "g",
                "source": "s",
                "from_pos": 0,
                "to_pos": 100,
                "state": "stalled",
                "known_at": 0,
                "session": "a",
            }
        )
        report = assess_entitlement(tables, tables["entitlements"][0], 0)
        self.assertEqual(report["loss"]["confirmed_seconds"], 100)
        self.assertEqual(report["loss"]["definite"], [[50, 100]])
        self.assertEqual(report["loss"]["definite_named_seconds"], 50)


class CalibrationTest(unittest.TestCase):
    def test_incompatible_anchor_identifies_session_and_keeps_direct_fact(self):
        tables = minimal_tables()
        tables["playback"].append(
            {
                "id": "direct",
                "person": "p",
                "program": "g",
                "source": "s",
                "from_pos": 10,
                "to_pos": 20,
                "state": "stalled",
                "known_at": 0,
                "session": "-",
            }
        )
        tables["session_limits"].append(
            {"session": "bad", "offset_min": 0, "offset_max": 100}
        )
        tables["calibration"].extend(
            [
                {"session": "bad", "anchor": "a", "offset_min": 0, "offset_max": 10, "known_at": 0},
                {"session": "bad", "anchor": "b", "offset_min": 50, "offset_max": 60, "known_at": 1},
            ]
        )
        tables["playback"].append(
            {
                "id": "badrow",
                "person": "p",
                "program": "g",
                "source": "s",
                "from_pos": 0,
                "to_pos": 5,
                "state": "stalled",
                "known_at": 0,
                "session": "bad",
            }
        )
        at = 2
        domains = calibration_domains(tables, at)
        self.assertEqual(domains["bad"].status, "incompatible")
        report = assess_entitlement(tables, tables["entitlements"][0], at)
        self.assertTrue(any(x["code"] == "calibration_unaligned" for x in report["manual_items"]))
        self.assertEqual(report["loss"]["definite"], [[10, 20]])
        self.assertEqual(report["loss"]["confirmed_seconds"], 10)


class SampleBehaviorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = read_tables(ROOT / "sample")
        cls.service = LossService(cls.tables)

    def test_sample_loads_all_orders(self):
        queue = self.service.queue(18000)
        orders = {x["order"] for x in queue["items"]}
        # b1/b3 upper bounds are below the g11 threshold and have no open
        # receipt; they remain queryable but do not enter the review queue.
        self.assertEqual(orders, {"b2", "b4", "b5"})

    def test_package_does_not_spread_damage_to_other_program(self):
        report = self.service.order("b1", 18000)
        by_program = {a["program"]: a for a in report["assessments"]}
        self.assertEqual(by_program["g12"]["decision"], "no_action")
        self.assertEqual(by_program["g12"]["loss"]["confirmed_seconds"], 0)
        self.assertTrue(by_program["g11"]["crosses_local_day_boundary"])

    def test_b2_receipt_states_and_historical_incompatibility(self):
        at_issue = self.service.order("b2", 6500)["assessments"][0]
        d1 = next(e for e in at_issue["receipts"] if e["event"] == "d1")
        self.assertEqual(d1["state"], "posted")
        self.assertGreaterEqual(d1["decision_snapshot"]["loss"]["confirmed_seconds"], 900)
        self.assertFalse(d1["decision_snapshot"]["receipt_interval_supported"]["clear_named_coverage"])

        conflict = self.service.order("b2", 11500)["assessments"][0]
        self.assertTrue(any(c["status"] == "incompatible" and c["session"] == "watch_a" for c in conflict["calibration"]))

        current = self.service.order("b2", 18000)["assessments"][0]
        events = {e["event"]: e for e in current["receipts"]}
        self.assertEqual(events["d5"]["state"], "declined")
        self.assertEqual(events["d1"]["state"], "posted")
        self.assertIn("posted_receipt_correction_gap", [m["code"] for m in current["manual_items"]])

    def test_b3_replay_window_and_decline_release(self):
        at_hold = self.service.order("b3", 8500)["assessments"][0]
        d2 = at_hold["receipts"][0]
        check = d2["decision_snapshot"]["replay_check"][0]
        self.assertFalse(check["fulfillable"])
        self.assertEqual(check["available_watch_seconds"], 1200)
        self.assertEqual(check["seconds"], 1400)

        current = self.service.order("b3", 18000)["assessments"][0]
        self.assertEqual(current["receipts"][0]["state"], "declined")
        self.assertTrue(current["rewatched"]["intervals"])

    def test_b5_has_fulfillable_east_replay(self):
        report = self.service.order("b5", 18000)["assessments"][0]
        rec = report["compensation"]["replay_recommendations"][0]
        self.assertEqual(rec["suggested_replay"], "east_late")
        self.assertTrue(rec["fulfillment"]["fulfillable"])
        self.assertGreaterEqual(report["loss"]["confirmed_seconds"], report["threshold"])

    def test_b4_package_contains_both_programs_and_declined_credit(self):
        report = self.service.order("b4", 18000)
        self.assertEqual({a["program"] for a in report["assessments"]}, {"g11", "g13"})
        g13 = next(a for a in report["assessments"] if a["program"] == "g13")
        self.assertEqual(g13["receipts"][0]["state"], "declined")
        self.assertEqual(g13["loss"]["confirmed_seconds"], 400)


class CalibrationNarrowingTest(unittest.TestCase):
    def test_same_total_can_move_into_replay_coverage(self):
        tables = minimal_tables()
        tables["programs"][0].update(
            {
                "end_utc": 1000,
                "critical_from": 900,
                "critical_to": 950,
                "threshold": 100,
            }
        )
        tables["outages"][0].update({"from_pos": 0, "to_pos": 1000})
        tables["session_limits"].append(
            {"session": "a", "offset_min": 0, "offset_max": 150}
        )
        tables["playback"].append(
            {
                "id": "stall",
                "person": "p",
                "program": "g",
                "source": "s",
                "from_pos": 100,
                "to_pos": 300,
                "state": "stalled",
                "known_at": 0,
                "session": "a",
            }
        )
        tables["replays"].append(
            {
                "id": "rp",
                "region": "R",
                "programs": "g",
                "start": 0,
                "until": 10_000,
                "covered_from": 300,
                "covered_to": 500,
            }
        )

        before = assess_entitlement(tables, tables["entitlements"][0], 1)
        self.assertEqual(before["loss"]["confirmed_seconds"], 200)
        self.assertEqual(before["compensation"]["replay_recommendations"], [])

        tables["calibration"].append(
            {
                "session": "a",
                "anchor": "edge",
                "offset_min": 150,
                "offset_max": 150,
                "known_at": 5,
            }
        )
        after = assess_entitlement(tables, tables["entitlements"][0], 6)
        self.assertEqual(after["loss"]["confirmed_seconds"], 200)
        rec = after["compensation"]["replay_recommendations"][0]
        self.assertEqual(rec["segment"], [300, 450])
        self.assertEqual(rec["suggested_replay"], "rp")


class StoreTest(unittest.TestCase):
    def test_incremental_json_and_directory_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            with Store(db) as store:
                stats = store.ingest_directory(ROOT / "sample")
                self.assertTrue(all(v > 0 for v in stats.values()))
                before = len(store.table("playback"))
                # Re-importing the same rows is idempotent.
                store.ingest_directory(ROOT / "sample")
                self.assertEqual(len(store.table("playback")), before)
                inserted = store.ingest_json_records(
                    "playback",
                    [
                        {
                            "id": "later",
                            "person": "p5",
                            "program": "g13",
                            "source": "alpha",
                            "from_pos": 2290,
                            "to_pos": 2300,
                            "state": "stalled",
                            "known_at": 20000,
                            "session": "-",
                        }
                    ],
                )
                self.assertEqual(inserted, 1)
                service = LossService.from_store(store)
                report = service.order("b5", 20000)["assessments"][0]
                self.assertEqual(report["loss"]["confirmed_seconds"], 780)

    def test_subset_reader_rejects_bad_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "batch"
            target.mkdir()
            (target / "programs.csv").write_text("bad,header\n1,2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "表头"):
                read_csv_subset(target)


if __name__ == "__main__":
    unittest.main()
