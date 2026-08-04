from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from scheduler import (
    Rule,
    ScheduleError,
    Scheduler,
    count_before_cap,
    next_occurrence,
    occurrences_for_day,
    resolve_timezone,
)
from scheduler import _system_timezone

TZ = ZoneInfo("Europe/Paris")
MONDAY = date(2026, 8, 3)
TUESDAY = date(2026, 8, 4)
SATURDAY = date(2026, 8, 8)


def make_rule(**overrides) -> Rule:
    payload = {
        "name": "Repos des yeux",
        "messages": ["REGARDE AU LOIN"],
        "kind": "interval",
        "every_minutes": 45,
        "start": "09:00",
        "end": "18:30",
        "days": [1, 2, 3, 4, 5],
        "max_per_day": 12,
    }
    payload.update(overrides)
    return Rule.from_dict(payload)


def clock(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=TZ)


def times_of(rule: Rule, day: date) -> list[str]:
    return [moment.strftime("%H:%M") for moment in occurrences_for_day(rule, day, TZ)]


# ------------------------------------------------------------------ validation


def test_rule_rejects_an_empty_message_list():
    with pytest.raises(ScheduleError):
        make_rule(messages=[" ", ""])


def test_rule_rejects_a_window_that_ends_before_it_starts():
    with pytest.raises(ScheduleError):
        make_rule(start="18:00", end="09:00")


def test_fixed_times_rule_needs_at_least_one_time():
    with pytest.raises(ScheduleError):
        make_rule(kind="at", times=[])


def test_fixed_times_rule_ignores_the_window_order():
    rule = make_rule(kind="at", times=["14:30", "10:30"], start="18:00", end="09:00")
    assert times_of(rule, MONDAY) == ["10:30", "14:30"]


def test_rule_clamps_out_of_range_numbers():
    rule = make_rule(every_minutes=1, font_size=999, max_per_day=999)
    assert rule.every_minutes == 5
    assert rule.font_size == 72
    assert rule.max_per_day == 48


def test_round_trip_through_to_dict_keeps_every_field():
    rule = make_rule(
        kind="at",
        times=["10:30", "14:30"],
        density=15,
        font_size=60,
        align="left",
        pick="random",
        max_per_day=9,
        min_gap_minutes=30,
        count=6,
        every_minutes=30,
    )
    stored = rule.to_dict()
    assert Rule.from_dict(stored, rule_id=rule.id) == rule
    assert stored["density"] == 15
    assert stored["font_size"] == 60
    assert stored["align"] == "left"
    assert stored["pick"] == "random"


def test_to_dict_uses_the_same_keys_that_from_dict_reads():
    stored = make_rule().to_dict()
    defaults = Rule.from_dict({"name": "x", "messages": ["A"]}).to_dict()
    assert set(stored) == set(defaults)


# -------------------------------------------------------------------- planning


def test_interval_rule_walks_the_window():
    rule = make_rule(every_minutes=45, max_per_day=48)
    planned = times_of(rule, MONDAY)
    assert planned[:3] == ["09:00", "09:45", "10:30"]
    assert planned[-1] == "18:00"


def test_a_rule_does_not_run_outside_its_days():
    assert times_of(make_rule(), SATURDAY) == []


def test_the_daily_limit_truncates_the_plan():
    rule = make_rule(every_minutes=30, max_per_day=4)
    assert times_of(rule, MONDAY) == ["09:00", "09:30", "10:00", "10:30"]
    assert count_before_cap(rule, MONDAY, TZ) == 20


def test_random_times_stay_stable_for_one_day():
    rule = make_rule(kind="random", count=4, min_gap_minutes=75, start="09:30", end="18:00")
    first = times_of(rule, MONDAY)
    assert first == times_of(rule, MONDAY)
    assert len(first) == 4
    assert first == sorted(first)


def test_random_times_differ_between_days():
    rule = make_rule(kind="random", count=4, min_gap_minutes=75, start="09:30", end="18:00")
    assert times_of(rule, MONDAY) != times_of(rule, TUESDAY)


def test_random_times_respect_the_minimum_gap():
    rule = make_rule(kind="random", count=4, min_gap_minutes=75, start="09:30", end="18:00")
    moments = occurrences_for_day(rule, MONDAY, TZ)
    gaps = [
        (second - first).total_seconds() / 60
        for first, second in zip(moments, moments[1:], strict=False)
    ]
    assert all(gap >= 75 for gap in gaps)


def test_random_times_stay_inside_the_window():
    rule = make_rule(kind="random", count=6, min_gap_minutes=0, start="09:30", end="18:00")
    for moment in occurrences_for_day(rule, MONDAY, TZ):
        assert clock(MONDAY, 9, 30) <= moment <= clock(MONDAY, 18, 0)


def test_next_occurrence_skips_to_the_following_working_day():
    rule = make_rule(kind="at", times=["10:30"])
    upcoming = next_occurrence(rule, TZ, clock(SATURDAY, 12, 0))
    assert upcoming == clock(date(2026, 8, 10), 10, 30)


def test_next_occurrence_returns_none_when_no_day_matches():
    rule = make_rule(kind="at", times=["10:30"], days=[7])
    rule.days = []
    assert next_occurrence(rule, TZ, clock(MONDAY, 12, 0)) is None


# --------------------------------------------------------------------- firing


class Recorder:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, rule: Rule, message: str) -> str:
        self.calls.append((rule.name, message))
        return f"job{len(self.calls)}"


def make_scheduler(tmp_path, printer=None) -> Scheduler:
    return Scheduler(tmp_path / "schedules.json", printer or Recorder(), tz=TZ)


def test_tick_prints_a_due_rule_once(tmp_path):
    recorder = Recorder()
    scheduler = make_scheduler(tmp_path, recorder)
    scheduler.add(make_rule(days=[1, 2, 3, 4, 5, 6, 7]).to_dict())

    assert len(scheduler.tick(clock(MONDAY, 9, 0, 10))) == 1
    assert scheduler.tick(clock(MONDAY, 9, 0, 40)) == []
    assert len(recorder.calls) == 1


def test_tick_fires_the_next_slot(tmp_path):
    recorder = Recorder()
    scheduler = make_scheduler(tmp_path, recorder)
    scheduler.add(make_rule(days=[1, 2, 3, 4, 5, 6, 7]).to_dict())

    scheduler.tick(clock(MONDAY, 9, 0, 10))
    scheduler.tick(clock(MONDAY, 9, 45, 5))
    assert len(recorder.calls) == 2


def test_tick_does_not_catch_up_a_missed_slot(tmp_path):
    recorder = Recorder()
    scheduler = make_scheduler(tmp_path, recorder)
    scheduler.add(make_rule(days=[1, 2, 3, 4, 5, 6, 7]).to_dict())

    assert scheduler.tick(clock(MONDAY, 11, 10)) == []
    assert recorder.calls == []


def test_a_disabled_rule_never_fires(tmp_path):
    recorder = Recorder()
    scheduler = make_scheduler(tmp_path, recorder)
    scheduler.add(make_rule(days=[1, 2, 3, 4, 5, 6, 7], enabled=False).to_dict())

    assert scheduler.tick(clock(MONDAY, 9, 0, 10)) == []


def test_a_paused_scheduler_never_fires(tmp_path):
    recorder = Recorder()
    scheduler = make_scheduler(tmp_path, recorder)
    scheduler.add(make_rule(days=[1, 2, 3, 4, 5, 6, 7]).to_dict())
    scheduler.set_paused(True)

    assert scheduler.tick(clock(MONDAY, 9, 0, 10)) == []


def test_the_daily_limit_stops_further_prints(tmp_path):
    recorder = Recorder()
    scheduler = make_scheduler(tmp_path, recorder)
    scheduler.add(
        make_rule(days=[1, 2, 3, 4, 5, 6, 7], every_minutes=45, max_per_day=1).to_dict()
    )

    scheduler.tick(clock(MONDAY, 9, 0, 10))
    scheduler.tick(clock(MONDAY, 9, 45, 5))
    assert len(recorder.calls) == 1


def test_the_daily_counter_resets_on_the_next_day(tmp_path):
    recorder = Recorder()
    scheduler = make_scheduler(tmp_path, recorder)
    scheduler.add(
        make_rule(days=[1, 2, 3, 4, 5, 6, 7], every_minutes=45, max_per_day=1).to_dict()
    )

    scheduler.tick(clock(MONDAY, 9, 0, 10))
    scheduler.tick(clock(TUESDAY, 9, 0, 10))
    assert len(recorder.calls) == 2


def test_messages_rotate_in_order(tmp_path):
    recorder = Recorder()
    scheduler = make_scheduler(tmp_path, recorder)
    scheduler.add(
        make_rule(days=[1, 2, 3, 4, 5, 6, 7], messages=["A", "B"], every_minutes=45).to_dict()
    )

    scheduler.tick(clock(MONDAY, 9, 0, 10))
    scheduler.tick(clock(MONDAY, 9, 45, 5))
    assert [message for _name, message in recorder.calls] == ["A", "B"]


def test_a_failing_printer_does_not_stop_the_loop(tmp_path):
    def broken(_rule, _message):
        raise RuntimeError("printer offline")

    scheduler = make_scheduler(tmp_path, broken)
    scheduler.add(make_rule(days=[1, 2, 3, 4, 5, 6, 7]).to_dict())

    fired = scheduler.tick(clock(MONDAY, 9, 0, 10))
    assert fired[0]["error"].startswith("RuntimeError")


def test_run_now_prints_without_touching_the_daily_counter(tmp_path):
    recorder = Recorder()
    scheduler = make_scheduler(tmp_path, recorder)
    rule = scheduler.add(make_rule(days=[1, 2, 3, 4, 5, 6, 7]).to_dict())

    scheduler.run_now(rule.id)
    assert len(recorder.calls) == 1
    assert scheduler.states[rule.id].fired_count == 0


# -------------------------------------------------------------------- storage


def test_the_state_survives_a_restart(tmp_path):
    recorder = Recorder()
    scheduler = make_scheduler(tmp_path, recorder)
    scheduler.add(make_rule(days=[1, 2, 3, 4, 5, 6, 7]).to_dict())
    scheduler.tick(clock(MONDAY, 9, 0, 10))

    restarted = make_scheduler(tmp_path, recorder)
    assert restarted.tick(clock(MONDAY, 9, 0, 40)) == []
    assert len(recorder.calls) == 1


def test_an_unreadable_file_leaves_an_empty_scheduler(tmp_path):
    path = tmp_path / "schedules.json"
    path.write_text("{ not json", encoding="utf-8")
    scheduler = Scheduler(path, Recorder(), tz=TZ)
    assert scheduler.rules == {}


def test_an_invalid_stored_rule_is_dropped(tmp_path):
    path = tmp_path / "schedules.json"
    path.write_text('{"rules": [{"name": "broken"}], "states": {}}', encoding="utf-8")
    scheduler = Scheduler(path, Recorder(), tz=TZ)
    assert scheduler.rules == {}


def test_update_and_delete_change_the_stored_rules(tmp_path):
    scheduler = make_scheduler(tmp_path)
    rule = scheduler.add(make_rule().to_dict())

    scheduler.update(rule.id, {"name": "Pause"})
    assert Scheduler(scheduler.path, Recorder(), tz=TZ).rules[rule.id].name == "Pause"

    scheduler.delete(rule.id)
    assert Scheduler(scheduler.path, Recorder(), tz=TZ).rules == {}


def test_update_rejects_an_unknown_rule(tmp_path):
    scheduler = make_scheduler(tmp_path)
    with pytest.raises(KeyError):
        scheduler.update("missing", {"name": "x"})


def test_public_reports_the_plan_and_the_next_time(tmp_path):
    scheduler = make_scheduler(tmp_path)
    scheduler.add(make_rule(days=[1, 2, 3, 4, 5, 6, 7]).to_dict())

    state = scheduler.public(clock(MONDAY, 12, 0))
    reported = state["rules"][0]
    assert reported["today_plan"][0] == "09:00"
    assert reported["next_at"].startswith("2026-08-03T12:45")
    assert state["paused"] is False


def test_public_counts_only_the_prints_of_the_current_day(tmp_path):
    scheduler = make_scheduler(tmp_path)
    scheduler.add(make_rule(days=[1, 2, 3, 4, 5, 6, 7]).to_dict())
    scheduler.tick(clock(MONDAY, 9, 0, 10))

    assert scheduler.public(clock(MONDAY, 10, 0))["rules"][0]["fired_today"] == 1
    assert scheduler.public(clock(MONDAY + timedelta(days=1), 10, 0))["rules"][0]["fired_today"] == 0


# ------------------------------------------------------------------- timezone


def test_an_explicit_name_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("S002_TIMEZONE", "Europe/Lisbon")
    assert str(resolve_timezone("Asia/Tokyo")) == "Asia/Tokyo"


def test_the_environment_wins_over_the_machine(monkeypatch):
    monkeypatch.setenv("S002_TIMEZONE", "Asia/Tokyo")
    assert str(resolve_timezone()) == "Asia/Tokyo"


def test_an_unset_environment_falls_back_to_the_machine(monkeypatch):
    monkeypatch.delenv("S002_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    detected = _system_timezone()
    if not detected:
        pytest.skip("this machine does not expose a time-zone name")
    assert str(resolve_timezone()) == detected


def test_an_unknown_name_never_raises(monkeypatch):
    monkeypatch.delenv("S002_TIMEZONE", raising=False)
    assert resolve_timezone("Not/AZone") is not None
