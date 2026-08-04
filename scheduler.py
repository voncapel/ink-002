"""Recurring print rules for the S002.

A rule describes when to print a short message and which messages to use. The
module computes the print times and calls an injected printer callback. It does
not import Flask or the printer transport, so the schedule math stays pure and
testable.

Three trigger kinds cover the common cases:

- ``interval``: every N minutes inside a daily window.
- ``at``: a fixed list of clock times.
- ``random``: N semi-random times spread across a daily window.

Random times are derived from a seed built with the rule id and the date. The
same day always gives the same times, so a restart does not print twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TRIGGER_KINDS = ("interval", "at", "random")
PICK_MODES = ("sequential", "random")
ALIGNMENTS = ("left", "center", "right")
DENSITIES = (7, 12, 15)

MAX_RULES = 24
MAX_MESSAGES = 12
MAX_MESSAGE_LENGTH = 240
MAX_NAME_LENGTH = 40
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 12 * 60
MAX_DAILY_PRINTS = 48
MAX_FIXED_TIMES = 12
GRACE_SECONDS = 300
LOOKAHEAD_DAYS = 8

PRESETS = (
    {
        "name": "Repos des yeux",
        "messages": ["REGARDE AU LOIN\n30 SECONDES"],
        "font_size": 44,
        "kind": "interval",
        "every_minutes": 45,
        "start": "09:00",
        "end": "18:30",
        "days": [1, 2, 3, 4, 5],
        "max_per_day": 12,
    },
    {
        "name": "Squats",
        "messages": ["DEBOUT\n10 SQUATS", "DEBOUT\n15 SQUATS", "DEBOUT\n20 SQUATS"],
        "pick": "random",
        "font_size": 48,
        "kind": "random",
        "count": 4,
        "min_gap_minutes": 75,
        "start": "09:30",
        "end": "18:00",
        "days": [1, 2, 3, 4, 5],
        "max_per_day": 4,
    },
    {
        "name": "Hydratation",
        "messages": ["BOIS UN VERRE D'EAU"],
        "font_size": 40,
        "kind": "interval",
        "every_minutes": 90,
        "start": "09:00",
        "end": "19:00",
        "days": [1, 2, 3, 4, 5, 6, 7],
        "max_per_day": 8,
    },
    {
        "name": "Posture",
        "messages": ["REDRESSE LE DOS\nEPAULES BASSES"],
        "font_size": 42,
        "kind": "at",
        "times": ["10:30", "14:30", "16:30"],
        "days": [1, 2, 3, 4, 5],
        "max_per_day": 3,
    },
)


class ScheduleError(ValueError):
    """Raised when a rule payload is invalid."""


def _system_timezone() -> str:
    """Return the machine time-zone name, or an empty string when unknown."""
    local = datetime.now().astimezone().tzinfo
    key = getattr(local, "key", None)
    if key:
        return str(key)
    link = Path("/etc/localtime")
    if link.is_symlink():
        target = str(link.readlink())
        marker = "/zoneinfo/"
        if marker in target:
            return target.split(marker, 1)[1]
    return ""


def resolve_timezone(name: str | None = None) -> ZoneInfo:
    """Return the local time zone: the setting, then the machine, then UTC."""
    candidates = (
        name or "",
        os.environ.get("S002_TIMEZONE") or "",
        os.environ.get("TZ") or "",
        _system_timezone(),
        "UTC",
    )
    for value in candidates:
        if not value:
            continue
        try:
            return ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def _parse_hhmm(value: object, label: str) -> time:
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ScheduleError(f"{label} doit utiliser le format HH:MM")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ScheduleError(f"{label} doit utiliser le format HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ScheduleError(f"{label} n’est pas une heure valide")
    return time(hour, minute)


def _format_hhmm(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def _clamp(value: object, low: int, high: int, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ScheduleError(f"{label} doit être un nombre entier") from exc
    return max(low, min(high, number))


@dataclass
class Rule:
    """One recurring print rule."""

    id: str
    name: str
    messages: list[str]
    enabled: bool = True
    pick: str = "sequential"
    font_size: int = 44
    align: str = "center"
    density: int = 7
    days: list[int] = field(default_factory=lambda: [1, 2, 3, 4, 5])
    start: time = time(9, 0)
    end: time = time(18, 0)
    kind: str = "interval"
    every_minutes: int = 45
    times: list[time] = field(default_factory=list)
    count: int = 4
    min_gap_minutes: int = 60
    max_per_day: int = 12

    @classmethod
    def from_dict(cls, raw: dict, *, rule_id: str | None = None) -> Rule:
        if not isinstance(raw, dict):
            raise ScheduleError("Un rituel doit être un objet")

        name = str(raw.get("name", "")).strip()[:MAX_NAME_LENGTH]
        if not name:
            raise ScheduleError("Donnez un nom au rituel")

        raw_messages = raw.get("messages") or []
        if isinstance(raw_messages, str):
            raw_messages = [raw_messages]
        messages = [
            str(item).strip()[:MAX_MESSAGE_LENGTH]
            for item in raw_messages
            if str(item).strip()
        ][:MAX_MESSAGES]
        if not messages:
            raise ScheduleError("Ajoutez au moins un message à imprimer")

        kind = str(raw.get("kind", "interval"))
        if kind not in TRIGGER_KINDS:
            raise ScheduleError("Type de déclenchement inconnu")

        pick = str(raw.get("pick", "sequential"))
        if pick not in PICK_MODES:
            raise ScheduleError("Mode de tirage des messages inconnu")

        align = str(raw.get("align", "center"))
        if align not in ALIGNMENTS:
            raise ScheduleError("Alignement de texte invalide")

        density = _clamp(raw.get("density", 7), 7, 15, "La densité")
        if density not in DENSITIES:
            raise ScheduleError("La densité doit valoir 7, 12 ou 15")

        days_raw = raw.get("days")
        if days_raw is None:
            days_raw = [1, 2, 3, 4, 5]
        try:
            days = sorted({int(item) for item in days_raw if 1 <= int(item) <= 7})
        except (TypeError, ValueError) as exc:
            raise ScheduleError("Les jours doivent être des nombres de 1 à 7") from exc
        if not days:
            raise ScheduleError("Sélectionnez au moins un jour")

        start = _parse_hhmm(raw.get("start", "09:00"), "L’heure de début")
        end = _parse_hhmm(raw.get("end", "18:00"), "L’heure de fin")

        times_raw = raw.get("times") or []
        times = sorted({_parse_hhmm(item, "Un horaire fixe") for item in times_raw})[
            :MAX_FIXED_TIMES
        ]

        if kind == "at":
            if not times:
                raise ScheduleError("Ajoutez au moins un horaire fixe")
        elif end <= start:
            raise ScheduleError("L’heure de fin doit suivre l’heure de début")

        rule = cls(
            id=rule_id or str(raw.get("id") or uuid.uuid4().hex[:12]),
            name=name,
            messages=messages,
            enabled=bool(raw.get("enabled", True)),
            pick=pick,
            font_size=_clamp(raw.get("font_size", 44), 14, 72, "La taille de police"),
            align=align,
            density=density,
            days=days,
            start=start,
            end=end,
            kind=kind,
            every_minutes=_clamp(
                raw.get("every_minutes", 45),
                MIN_INTERVAL_MINUTES,
                MAX_INTERVAL_MINUTES,
                "L’intervalle",
            ),
            times=times,
            count=_clamp(raw.get("count", 4), 1, 24, "Le nombre d’impressions"),
            min_gap_minutes=_clamp(
                raw.get("min_gap_minutes", 60), 0, MAX_INTERVAL_MINUTES, "L’écart minimum"
            ),
            max_per_day=_clamp(
                raw.get("max_per_day", 12), 1, MAX_DAILY_PRINTS, "La limite quotidienne"
            ),
        )
        return rule

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "messages": list(self.messages),
            "enabled": self.enabled,
            "pick": self.pick,
            "font_size": self.font_size,
            "align": self.align,
            "density": self.density,
            "days": list(self.days),
            "start": _format_hhmm(self.start),
            "end": _format_hhmm(self.end),
            "kind": self.kind,
            "every_minutes": self.every_minutes,
            "times": [_format_hhmm(item) for item in self.times],
            "count": self.count,
            "min_gap_minutes": self.min_gap_minutes,
            "max_per_day": self.max_per_day,
        }

    def signature(self) -> str:
        """Return a stable digest of the fields that shape the random plan."""
        source = "|".join(
            [
                self.id,
                str(self.count),
                _format_hhmm(self.start),
                _format_hhmm(self.end),
                str(self.min_gap_minutes),
            ]
        )
        return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _random_plan(rule: Rule, day: date, start: datetime, end: datetime) -> list[datetime]:
    """Spread ``rule.count`` times across the window, one per equal slot."""
    seed = f"{rule.signature()}|{day.isoformat()}"
    rng = random.Random(hashlib.sha256(seed.encode("utf-8")).hexdigest())
    count = max(1, min(rule.count, MAX_DAILY_PRINTS))
    low_bound = start.timestamp()
    slot = (end.timestamp() - low_bound) / count
    gap = rule.min_gap_minutes * 60
    picks: list[float] = []
    for index in range(count):
        low = low_bound + index * slot
        high = low + slot
        if picks:
            low = max(low, picks[-1] + gap)
        if low > high:
            low = high
        picks.append(rng.uniform(low, high))
    return [datetime.fromtimestamp(value, tz=start.tzinfo) for value in picks]


def occurrences_for_day(rule: Rule, day: date, tz: ZoneInfo) -> list[datetime]:
    """Return every print time of ``rule`` on ``day``, in ascending order."""
    if day.isoweekday() not in rule.days:
        return []

    if rule.kind == "at":
        planned = [datetime.combine(day, item, tz) for item in sorted(rule.times)]
        return planned[: rule.max_per_day]

    start = datetime.combine(day, rule.start, tz)
    end = datetime.combine(day, rule.end, tz)
    if end <= start:
        return []

    if rule.kind == "interval":
        planned = []
        step = timedelta(minutes=rule.every_minutes)
        cursor = start
        while cursor <= end and len(planned) < MAX_DAILY_PRINTS:
            planned.append(cursor)
            cursor += step
    else:
        planned = _random_plan(rule, day, start, end)

    return planned[: rule.max_per_day]


def count_before_cap(rule: Rule, day: date, tz: ZoneInfo) -> int:
    """Return how many times the rule would fire without its daily limit."""
    if day.isoweekday() not in rule.days:
        return 0
    if rule.kind == "at":
        return len(rule.times)
    start = datetime.combine(day, rule.start, tz)
    end = datetime.combine(day, rule.end, tz)
    if end <= start:
        return 0
    if rule.kind == "random":
        return min(rule.count, MAX_DAILY_PRINTS)
    span = (end - start).total_seconds() / 60
    return min(int(span // rule.every_minutes) + 1, MAX_DAILY_PRINTS)


def next_occurrence(rule: Rule, tz: ZoneInfo, after: datetime) -> datetime | None:
    """Return the first print time strictly after ``after``, or ``None``."""
    day = after.astimezone(tz).date()
    for offset in range(LOOKAHEAD_DAYS):
        for moment in occurrences_for_day(rule, day + timedelta(days=offset), tz):
            if moment > after:
                return moment
    return None


def preview_day(rule: Rule, tz: ZoneInfo, day: date) -> list[str]:
    """Return the print times of one day as HH:MM strings, for the interface."""
    return [moment.strftime("%H:%M") for moment in occurrences_for_day(rule, day, tz)]


@dataclass
class RuleState:
    last_fired_at: str | None = None
    fired_day: str | None = None
    fired_count: int = 0
    cursor: int = 0

    @classmethod
    def from_dict(cls, raw: dict | None) -> RuleState:
        raw = raw or {}
        return cls(
            last_fired_at=raw.get("last_fired_at") or None,
            fired_day=raw.get("fired_day") or None,
            fired_count=int(raw.get("fired_count") or 0),
            cursor=int(raw.get("cursor") or 0),
        )

    def to_dict(self) -> dict:
        return {
            "last_fired_at": self.last_fired_at,
            "fired_day": self.fired_day,
            "fired_count": self.fired_count,
            "cursor": self.cursor,
        }


class Scheduler:
    """Hold the rules, decide what is due, and call the printer callback."""

    def __init__(
        self,
        path: Path,
        printer,
        *,
        tz: ZoneInfo | None = None,
        logger=None,
        grace_seconds: int = GRACE_SECONDS,
    ) -> None:
        self.path = Path(path)
        self.printer = printer
        self.tz = tz or resolve_timezone()
        self.logger = logger
        self.grace = timedelta(seconds=grace_seconds)
        self.lock = threading.RLock()
        self.paused = False
        self.rules: dict[str, Rule] = {}
        self.states: dict[str, RuleState] = {}
        self.load()

    # ------------------------------------------------------------------ store

    def load(self) -> None:
        with self.lock:
            self.rules = {}
            self.states = {}
            self.paused = False
            if not self.path.is_file():
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._log("warning", "unreadable schedule file %s", self.path)
                return
            self.paused = bool(raw.get("paused", False))
            for item in raw.get("rules", [])[:MAX_RULES]:
                try:
                    rule = Rule.from_dict(item)
                except ScheduleError:
                    self._log("warning", "dropped an invalid schedule rule")
                    continue
                self.rules[rule.id] = rule
            states = raw.get("states") or {}
            for rule_id in self.rules:
                self.states[rule_id] = RuleState.from_dict(states.get(rule_id))

    def save(self) -> None:
        with self.lock:
            payload = {
                "paused": self.paused,
                "rules": [rule.to_dict() for rule in self.rules.values()],
                "states": {key: value.to_dict() for key, value in self.states.items()},
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)

    # ------------------------------------------------------------------- CRUD

    def public(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(self.tz)
        today = now.astimezone(self.tz).date()
        with self.lock:
            rules = []
            for rule in self.rules.values():
                state = self.states.get(rule.id, RuleState())
                upcoming = next_occurrence(rule, self.tz, now)
                rules.append(
                    {
                        **rule.to_dict(),
                        "next_at": upcoming.isoformat() if upcoming else None,
                        "last_fired_at": state.last_fired_at,
                        "fired_today": state.fired_count if state.fired_day == today.isoformat() else 0,
                        "today_plan": preview_day(rule, self.tz, today),
                    }
                )
            return {
                "paused": self.paused,
                "timezone": str(self.tz),
                "now": now.astimezone(self.tz).isoformat(timespec="seconds"),
                "rules": rules,
                "presets": [dict(item) for item in PRESETS],
            }

    def add(self, payload: dict) -> Rule:
        with self.lock:
            if len(self.rules) >= MAX_RULES:
                raise ScheduleError(f"La limite de {MAX_RULES} rituels est atteinte")
            rule = Rule.from_dict(payload, rule_id=uuid.uuid4().hex[:12])
            self.rules[rule.id] = rule
            self.states[rule.id] = RuleState()
            self.save()
            return rule

    def update(self, rule_id: str, payload: dict) -> Rule:
        with self.lock:
            if rule_id not in self.rules:
                raise KeyError(rule_id)
            merged = {**self.rules[rule_id].to_dict(), **payload}
            rule = Rule.from_dict(merged, rule_id=rule_id)
            self.rules[rule_id] = rule
            self.states.setdefault(rule_id, RuleState())
            self.save()
            return rule

    def delete(self, rule_id: str) -> None:
        with self.lock:
            if rule_id not in self.rules:
                raise KeyError(rule_id)
            del self.rules[rule_id]
            self.states.pop(rule_id, None)
            self.save()

    def set_paused(self, paused: bool) -> bool:
        with self.lock:
            self.paused = bool(paused)
            self.save()
            return self.paused

    # ----------------------------------------------------------------- firing

    def next_message(self, rule: Rule, state: RuleState) -> str:
        if rule.pick == "random" and len(rule.messages) > 1:
            return random.choice(rule.messages)
        message = rule.messages[state.cursor % len(rule.messages)]
        state.cursor = (state.cursor + 1) % len(rule.messages)
        return message

    def run_now(self, rule_id: str) -> str:
        """Print a rule immediately without touching its schedule counters."""
        with self.lock:
            rule = self.rules.get(rule_id)
            if rule is None:
                raise KeyError(rule_id)
            state = self.states.setdefault(rule_id, RuleState())
            message = self.next_message(rule, state)
            self.save()
        return self.printer(rule, message)

    def due_moment(self, rule: Rule, state: RuleState, now: datetime) -> datetime | None:
        """Return the print time that must fire now, or ``None``."""
        last = None
        if state.last_fired_at:
            try:
                last = datetime.fromisoformat(state.last_fired_at)
            except ValueError:
                last = None
        today = now.astimezone(self.tz).date()
        due = None
        for offset in (1, 0):
            for moment in occurrences_for_day(rule, today - timedelta(days=offset), self.tz):
                if moment > now:
                    break
                if last is not None and moment <= last:
                    continue
                if now - moment > self.grace:
                    continue
                due = moment
        return due

    def tick(self, now: datetime | None = None) -> list[dict]:
        """Fire every rule that is due. Return one record per print."""
        now = now or datetime.now(self.tz)
        fired: list[dict] = []
        with self.lock:
            if self.paused:
                return fired
            today = now.astimezone(self.tz).date().isoformat()
            pending: list[tuple[Rule, RuleState, datetime, str]] = []
            for rule in self.rules.values():
                if not rule.enabled:
                    continue
                state = self.states.setdefault(rule.id, RuleState())
                if state.fired_day != today:
                    state.fired_day = today
                    state.fired_count = 0
                if state.fired_count >= rule.max_per_day:
                    continue
                moment = self.due_moment(rule, state, now)
                if moment is None:
                    continue
                message = self.next_message(rule, state)
                state.last_fired_at = now.isoformat()
                state.fired_count += 1
                pending.append((rule, state, moment, message))
            if pending:
                self.save()

        for rule, _state, moment, message in pending:
            try:
                job_id = self.printer(rule, message)
            except Exception as exc:  # printing must never kill the loop
                self._log("exception", "schedule rule %s failed to print", rule.id)
                fired.append({"rule_id": rule.id, "error": f"{type(exc).__name__}: {exc}"})
                continue
            fired.append(
                {
                    "rule_id": rule.id,
                    "job_id": job_id,
                    "message": message,
                    "planned_at": moment.isoformat(),
                }
            )
        return fired

    def _log(self, level: str, message: str, *args) -> None:
        if self.logger is not None:
            getattr(self.logger, level)(message, *args)
