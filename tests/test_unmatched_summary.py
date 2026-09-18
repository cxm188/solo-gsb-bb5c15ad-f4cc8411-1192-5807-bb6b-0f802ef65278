"""API-level tests for POST /reconcile/unmatched-summary."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PATH = "/reconcile/unmatched-summary"


def txn(id: str, amount: int, time: str) -> dict:
    return {"id": id, "time": time, "amount": amount}


def post_summary(left, right, tz, **extra):
    body = {"left": left, "right": right, "timezone": tz, **extra}
    return client.post(PATH, json=body)


def pos(count, amount):
    return {"count": count, "amount": amount}


def neg(count, amount):
    return {"count": count, "amount": amount}


def day(d, positive, negative):
    return {"date": d, "positive": positive, "negative": negative}


# --------------------------------------------------------------------- #
# aggregation basics
# --------------------------------------------------------------------- #
def test_empty_batch_summary():
    resp = post_summary([], [], "UTC")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "timezone": "UTC",
        "days": [],
        "total": {"count": 0, "amount": 0},
        "positive": {"count": 0, "amount": 0},
        "negative": {"count": 0, "amount": 0},
    }


def test_matched_flows_excluded_unmatched_aggregated():
    # L1/R1 reconcile; L2 (+200) and R2 (-70) stay unmatched on the same day
    resp = post_summary(
        [txn("L1", 100, "2026-09-17T00:00:00Z"),
         txn("L2", 200, "2026-09-17T12:00:00Z")],
        [txn("R1", 100, "2026-09-17T01:00:00Z"),
         txn("R2", -70, "2026-09-17T13:00:00Z")],
        "UTC",
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "timezone": "UTC",
        "days": [
            day("2026-09-17", pos(1, 200), neg(1, 70)),
        ],
        "total": pos(2, 270),
        "positive": pos(1, 200),
        "negative": neg(1, 70),
    }


def test_groups_by_local_date_and_sign_with_absolute_amounts():
    # four unmatched: +100 and +50 on 09-17 UTC, -30 on 09-17 UTC, -40 on 09-18 UTC
    resp = post_summary(
        [
            txn("L1", 100, "2026-09-17T01:00:00Z"),
            txn("L2", 50, "2026-09-17T02:00:00Z"),
            txn("L3", -40, "2026-09-18T03:00:00Z"),
        ],
        [txn("R1", -30, "2026-09-17T04:00:00Z")],
        "UTC",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["days"] == [
        day("2026-09-17", pos(2, 150), neg(1, 30)),
        day("2026-09-18", pos(0, 0), neg(1, 40)),
    ]
    assert body["total"] == pos(4, 220)
    assert body["positive"] == pos(2, 150)
    assert body["negative"] == neg(2, 70)


def test_both_sides_merged_into_one_summary():
    resp = post_summary(
        [txn("L1", 100, "2026-09-17T00:00:00Z")],
        [txn("R1", 200, "2026-09-17T00:00:00Z")],
        "UTC",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["days"] == [day("2026-09-17", pos(2, 300), neg(0, 0))]
    assert body["total"] == pos(2, 300)


def test_days_sorted_ascending():
    resp = post_summary(
        [
            txn("L2", 100, "2026-09-19T00:00:00Z"),
            txn("L1", 100, "2026-09-17T00:00:00Z"),
            txn("L3", 100, "2026-09-18T00:00:00Z"),
        ],
        [],
        "UTC",
    )
    assert resp.status_code == 200, resp.text
    assert [d["date"] for d in resp.json()["days"]] == [
        "2026-09-17",
        "2026-09-18",
        "2026-09-19",
    ]


# --------------------------------------------------------------------- #
# time zones and the date line
# --------------------------------------------------------------------- #
def test_instant_converted_into_requested_zone_local_date():
    # 2026-09-17T20:00:00Z is still the 17th in UTC but already the 18th in
    # Shanghai (UTC+8, no DST)
    resp = post_summary(
        [txn("L1", 100, "2026-09-17T20:00:00Z")],
        [],
        "Asia/Shanghai",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["timezone"] == "Asia/Shanghai"
    assert body["days"] == [day("2026-09-18", pos(1, 100), neg(0, 0))]

    west = post_summary(
        [txn("L1", 100, "2026-09-17T02:00:00Z")],
        [],
        "America/New_York",
    )
    assert west.status_code == 200, west.text
    assert west.json()["days"] == [day("2026-09-16", pos(1, 100), neg(0, 0))]


def test_naive_datetime_treated_as_utc_before_zone_conversion():
    resp = post_summary(
        [txn("L1", 100, "2026-09-17T20:00:00")],  # naive -> UTC
        [],
        "Asia/Shanghai",
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["days"][0]["date"] == "2026-09-18"


def test_offset_aware_inputs_normalised_to_utc():
    # 2026-09-18T04:00:00+08:00 == 2026-09-17T20:00:00Z -> 2026-09-18 in Shanghai
    resp = post_summary(
        [txn("L1", 100, "2026-09-18T04:00:00+08:00")],
        [],
        "Asia/Shanghai",
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["days"][0]["date"] == "2026-09-18"


def test_same_instant_attributed_differently_by_zone():
    # the two flows never match (100 vs 200), so both are unmatched on the 18th
    # in Shanghai but the 17th in New York
    resp = post_summary(
        [txn("L1", 100, "2026-09-17T20:00:00Z")],
        [txn("R1", 200, "2026-09-17T20:00:00Z")],
        "Asia/Shanghai",
    )
    assert resp.status_code == 200, resp.text
    assert [d["date"] for d in resp.json()["days"]] == ["2026-09-18"]

    resp = post_summary(
        [txn("L1", 100, "2026-09-17T20:00:00Z")],
        [txn("R1", 200, "2026-09-17T20:00:00Z")],
        "America/New_York",
    )
    assert resp.status_code == 200, resp.text
    assert [d["date"] for d in resp.json()["days"]] == ["2026-09-17"]


# --------------------------------------------------------------------- #
# DST transition days must use the real local date
# --------------------------------------------------------------------- #
def test_fall_back_day_uses_real_local_date():
    # In America/New_York DST 2026 ends at 06:00 UTC on Nov 1:
    #   04:30 UTC -> 00:30 local (EDT, UTC-4)
    #   06:30 UTC -> 01:30 local (EST, UTC-5)
    # Both belong to the same real local calendar date 2026-11-01 even though
    # they sit on opposite sides of the offset change; a fixed -4/-5 offset
    # would split them across two dates.
    resp = post_summary(
        [
            txn("L1", 100, "2026-11-01T04:30:00Z"),
            txn("L2", -100, "2026-11-01T06:30:00Z"),
        ],
        [],
        "America/New_York",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["days"] == [day("2026-11-01", pos(1, 100), neg(1, 100))]
    assert body["total"] == pos(2, 200)


def test_spring_forward_day_uses_real_local_date():
    # 2026-03-08T06:30 UTC -> 01:30 EST before the 02:00->03:00 spring-forward
    # in New York; the real local date is 2026-03-08
    resp = post_summary(
        [txn("L1", 100, "2026-03-08T06:30:00Z")],
        [],
        "America/New_York",
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["days"] == [day("2026-03-08", pos(1, 100), neg(0, 0))]


def test_local_midnight_straddles_dst_transition():
    # 04:30 and 06:30 UTC both map onto Nov 1 local time in New York, while a
    # fixed-offset view (UTC-4) would push 06:30 onto Nov 2; in UTC they are
    # the same date, in Shanghai they are Nov 1 -- only zone-aware conversion
    # keeps both on the New York Nov 1.
    times = ["2026-11-01T04:30:00Z", "2026-11-01T06:30:00Z"]
    ny = post_summary(
        [txn(f"L{i}", 100, t) for i, t in enumerate(times)],
        [],
        "America/New_York",
    )
    assert ny.status_code == 200, ny.text
    assert [d["date"] for d in ny.json()["days"]] == ["2026-11-01"]

    utc = post_summary(
        [txn(f"L{i}", 100, t) for i, t in enumerate(times)],
        [],
        "UTC",
    )
    assert [d["date"] for d in utc.json()["days"]] == ["2026-11-01"]


# --------------------------------------------------------------------- #
# reusing the reconciliation solve and its validation
# --------------------------------------------------------------------- #
def test_excluded_pair_reroutes_unmatched_set():
    # natural optimum is (A+B,X)=150; forbidding (A,X) leaves only (A,Y)=100,
    # so B (+50) and X (+150) stay unmatched
    resp = post_summary(
        [txn("A", 100, "2026-09-17T00:00:00Z"), txn("B", 50, "2026-09-17T00:00:00Z")],
        [txn("X", 150, "2026-09-17T00:00:00Z"), txn("Y", 100, "2026-09-17T00:00:00Z")],
        "UTC",
        excluded_pairs=[{"left_id": "A", "right_id": "X"}],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["days"] == [day("2026-09-17", pos(2, 200), neg(0, 0))]
    assert body["total"] == pos(2, 200)


def test_locked_group_changes_unmatched_set():
    # natural optimum is (A+B,X)=150; locking (A,Y)=100 leaves B and X unmatched
    resp = post_summary(
        [txn("A", 100, "2026-09-17T00:00:00Z"), txn("B", 50, "2026-09-17T00:00:00Z")],
        [txn("X", 150, "2026-09-17T00:00:00Z"), txn("Y", 100, "2026-09-17T00:00:00Z")],
        "UTC",
        locked_groups=[{"left_ids": ["A"], "right_ids": ["Y"]}],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["days"] == [day("2026-09-17", pos(2, 200), neg(0, 0))]
    assert body["total"] == pos(2, 200)


def test_duplicate_ids_still_return_400():
    resp = post_summary(
        [txn("X", 1, "2026-09-17T00:00:00Z"), txn("X", 2, "2026-09-17T00:00:00Z")],
        [],
        "UTC",
    )
    assert resp.status_code == 400


def test_bad_locked_group_still_returns_422_with_index():
    resp = post_summary(
        [txn("A", 100, "2026-09-17T00:00:00Z")],
        [txn("X", 100, "2026-09-17T00:00:00Z")],
        "UTC",
        locked_groups=[{"left_ids": ["A"], "right_ids": ["NOPE"]}],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "locked_groups", 0]


def test_bad_excluded_pair_still_returns_422_with_index():
    resp = post_summary(
        [txn("A", 100, "2026-09-17T00:00:00Z")],
        [txn("X", 100, "2026-09-17T00:00:00Z")],
        "UTC",
        excluded_pairs=[{"left_id": "NOPE", "right_id": "X"}],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "excluded_pairs", 0]


def test_batch_schema_validation_reused():
    # zero amount is rejected by the shared transaction schema
    resp = post_summary(
        [txn("L1", 0, "2026-09-17T00:00:00Z")],
        [],
        "UTC",
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------- #
# timezone validation
# --------------------------------------------------------------------- #
def test_unknown_timezone_returns_422():
    resp = post_summary([], [], "Not/AZone")
    assert resp.status_code == 422
    err = resp.json()["detail"][0]
    assert err["loc"] == ["body", "timezone"]
    assert "timezone" in err["msg"]


def test_fixed_offset_and_non_iana_names_are_rejected():
    # numeric offsets and non-IANA abbreviations must not be accepted; the
    # lookup is case-sensitive (the alias "GMT" is fine, "gmt"/"utc" are not)
    for bad in ("+08:00", "UTC+8", "CST", "utc", "gmt", "Asia/No_City"):
        resp = post_summary([], [], bad)
        assert resp.status_code == 422, bad
        assert resp.json()["detail"][0]["loc"] == ["body", "timezone"]


def test_iana_aliases_accepted():
    for z in ("UTC", "GMT", "Etc/UTC", "Zulu", "Etc/GMT+8"):
        resp = post_summary([], [], z)
        assert resp.status_code == 200, z
        assert resp.json()["timezone"] == z


def test_empty_and_non_string_timezone_422():
    resp = client.post(PATH, json={"left": [], "right": [], "timezone": ""})
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "timezone"]

    resp = client.post(PATH, json={"left": [], "right": [], "timezone": 123})
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "timezone"]


def test_missing_timezone_422():
    resp = client.post(PATH, json={"left": [], "right": []})
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "timezone"]


def test_utc_alias_zones_accepted():
    resp = post_summary([], [], "Zulu")
    assert resp.status_code == 200
    assert resp.json()["timezone"] == "Zulu"
