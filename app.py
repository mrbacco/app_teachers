"""
TEACHERS APP project 
Timetable allocation tool done with flask and AI powered

mrbacco04@gmail.com
Feb 23, 2026 

"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
import time
import atexit
from collections import defaultdict
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

WEEK_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
LEGACY_YEAR_GROUP_MAP = {
    "First": "Grade 1",
    "Second": "Grade 2",
    "Third": "Grade 3",
    "Fourth": "Grade 4",
    "Fifth": "Grade 5",
}
YEAR_GROUPS = list(LEGACY_YEAR_GROUP_MAP.values())
TIME_ROWS = [
    {"slot": "08:00-09:00", "is_lunch": False},
    {"slot": "09:00-10:00", "is_lunch": False},
    {"slot": "10:00-11:00", "is_lunch": False},
    {"slot": "11:00-12:00", "is_lunch": False},
    {"slot": "12:00-13:00", "is_lunch": True},
    {"slot": "13:00-14:00", "is_lunch": False},
    {"slot": "14:00-15:00", "is_lunch": False},
]
TEACHING_SLOTS = [row["slot"] for row in TIME_ROWS if not row["is_lunch"]]
DAY_SHORT = {
    "Monday": "Mon",
    "Tuesday": "Tue",
    "Wednesday": "Wed",
    "Thursday": "Thu",
    "Friday": "Fri",
}

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///teachers_app.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key")
if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"connect_args": {"timeout": 30}}

BAC_LOG = logging.getLogger("BAC_LOG")
if not BAC_LOG.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("BAC_LOG | %(asctime)s | %(levelname)s | %(message)s"))
    BAC_LOG.addHandler(_handler)
BAC_LOG.setLevel(logging.INFO)
BAC_LOG.propagate = False

db = SQLAlchemy(app)
APP_LOCK_HANDLE = None


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, _connection_record):
    module_name = dbapi_connection.__class__.__module__
    if module_name.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


teacher_skills = db.Table(
    "teacher_skills",
    db.Column("teacher_id", db.Integer, db.ForeignKey("teachers.id", ondelete="CASCADE"), primary_key=True),
    db.Column("skill_id", db.Integer, db.ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True),
)


class Teacher(db.Model):
    __tablename__ = "teachers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    free_slots = db.Column(db.Text, nullable=False, default="")

    skills = db.relationship("Skill", secondary=teacher_skills, back_populates="teachers")
    assigned_sessions = db.relationship("Session", back_populates="assigned_teacher", foreign_keys="Session.assigned_teacher_id")


class Skill(db.Model):
    __tablename__ = "skills"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)

    teachers = db.relationship("Teacher", secondary=teacher_skills, back_populates="skills")
    required_sessions = db.relationship("Session", back_populates="required_skill", foreign_keys="Session.required_skill_id")


class Session(db.Model):
    __tablename__ = "sessions"

    id = db.Column(db.Integer, primary_key=True)
    required_skill_id = db.Column(db.Integer, db.ForeignKey("skills.id", ondelete="RESTRICT"), nullable=False)
    slot = db.Column(db.String(120), nullable=False)
    assigned_teacher_id = db.Column(db.Integer, db.ForeignKey("teachers.id", ondelete="SET NULL"), nullable=True)
    day = db.Column(db.String(20), nullable=True)
    year_group = db.Column(db.String(20), nullable=True)
    # Locked cells keep their teacher when auto-allocation runs.
    is_locked = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

    required_skill =db.relationship("Skill", back_populates="required_sessions", foreign_keys=[required_skill_id])
    assigned_teacher = db.relationship("Teacher", back_populates="assigned_sessions", foreign_keys=[assigned_teacher_id])


def migrate_legacy_year_groups() -> None:
    migrated = 0
    for old_value, new_value in LEGACY_YEAR_GROUP_MAP.items():
        migrated += Session.query.filter_by(year_group=old_value).update(
            {Session.year_group: new_value},
            synchronize_session=False,
        )
    # Always commit: even a zero-row UPDATE opens a write transaction that would block later bootstrap steps.
    db.session.commit()
    if migrated:
        BAC_LOG.info("step=bootstrap.migration migrated_year_groups=%s", migrated)


def deduplicate_grid_sessions() -> None:
    by_cell: dict[tuple[str, str, str], list[Session]] = defaultdict(list)
    by_teacher_slot: dict[tuple[str, str, int], list[Session]] = defaultdict(list)
    removed = 0
    unassigned_conflicts = 0

    for session in grid_sessions_query().order_by(Session.id.asc()).all():
        by_cell[(session.day, session.slot, session.year_group)].append(session)
        if session.assigned_teacher_id:
            by_teacher_slot[(session.day, session.slot, session.assigned_teacher_id)].append(session)

    for sessions in by_cell.values():
        if len(sessions) <= 1:
            continue
        keep = sessions[0]
        for extra in sessions[1:]:
            # Preserve a filled assignment if the kept row is empty.
            if keep.assigned_teacher_id is None and extra.assigned_teacher_id is not None:
                keep.assigned_teacher_id = extra.assigned_teacher_id
            db.session.delete(extra)
            removed += 1

    for sessions in by_teacher_slot.values():
        if len(sessions) <= 1:
            continue
        for extra in sessions[1:]:
            extra.assigned_teacher_id = None
            unassigned_conflicts += 1

    if removed or unassigned_conflicts:
        db.session.commit()
        BAC_LOG.warning(
            "step=bootstrap.repair_grid removed_duplicate_cells=%s unassigned_teacher_conflicts=%s",
            removed,
            unassigned_conflicts,
        )


def ensure_grid_indexes() -> None:
    if db.engine.url.get_backend_name() != "sqlite":
        return

    max_attempts = 6
    for attempt in range(1, max_attempts + 1):
        try:
            with db.engine.begin() as conn:
                conn.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_grid_cell "
                    "ON sessions(day, slot, year_group) "
                    "WHERE day IS NOT NULL AND slot IS NOT NULL AND year_group IS NOT NULL"
                )
                conn.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_teacher_slot "
                    "ON sessions(day, slot, assigned_teacher_id) "
                    "WHERE assigned_teacher_id IS NOT NULL AND day IS NOT NULL AND slot IS NOT NULL"
                )
            BAC_LOG.info("step=bootstrap.indexes_ready attempt=%s", attempt)
            return
        except OperationalError as exc:
            is_locked = "database is locked" in str(exc).lower()
            if not is_locked or attempt == max_attempts:
                raise
            wait_seconds = attempt * 0.5
            BAC_LOG.warning("step=bootstrap.indexes_retry attempt=%s wait_seconds=%s reason=database_locked", attempt, wait_seconds)
            time.sleep(wait_seconds)


def acquire_app_lock() -> bool:
    global APP_LOCK_HANDLE
    lock_dir = Path(app.instance_path)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "teachers_app.lock"
    handle = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        BAC_LOG.error("step=bootstrap.lock_failed path=%s reason=already_running", lock_path)
        return False

    APP_LOCK_HANDLE = handle
    BAC_LOG.info("step=bootstrap.lock_acquired path=%s", lock_path)
    return True


def release_app_lock() -> None:
    global APP_LOCK_HANDLE
    if not APP_LOCK_HANDLE:
        return

    try:
        if os.name == "nt":
            import msvcrt

            APP_LOCK_HANDLE.seek(0)
            msvcrt.locking(APP_LOCK_HANDLE.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(APP_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        APP_LOCK_HANDLE.close()
        APP_LOCK_HANDLE = None


atexit.register(release_app_lock)


def run_bootstrap_step(step_name: str, fn, *, max_attempts: int = 8, skip_if_locked: bool = True) -> bool:
    for attempt in range(1, max_attempts + 1):
        try:
            fn()
            BAC_LOG.info("step=bootstrap.step_ok name=%s attempt=%s", step_name, attempt)
            return True
        except OperationalError as exc:
            db.session.rollback()
            is_locked = "database is locked" in str(exc).lower()
            if not is_locked:
                raise
            if attempt == max_attempts:
                if skip_if_locked:
                    BAC_LOG.warning("step=bootstrap.step_skipped name=%s reason=database_locked", step_name)
                    return False
                raise
            wait_seconds = attempt * 0.5
            BAC_LOG.warning(
                "step=bootstrap.step_retry name=%s attempt=%s wait_seconds=%s reason=database_locked",
                step_name,
                attempt,
                wait_seconds,
            )
            time.sleep(wait_seconds)


def ensure_session_grid_columns() -> None:
    with db.engine.begin() as conn:
        if db.engine.url.get_backend_name() != "sqlite":
            return

        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(sessions)")}
        if "day" not in columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN day VARCHAR(20)")
            BAC_LOG.info("step=bootstrap.migration added_column=sessions.day")
        if "year_group" not in columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN year_group VARCHAR(20)")
            BAC_LOG.info("step=bootstrap.migration added_column=sessions.year_group")
        if "is_locked" not in columns:
            conn.exec_driver_sql("ALTER TABLE sessions ADD COLUMN is_locked BOOLEAN NOT NULL DEFAULT 0")
            BAC_LOG.info("step=bootstrap.migration added_column=sessions.is_locked")


def split_csv(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def normalize_slot_csv(text: str) -> str:
    return ", ".join(split_multi_value_field(text))


def parse_int_list(values: list[str]) -> list[int]:
    parsed: list[int] = []
    for value in values:
        try:
            parsed.append(int(value))
        except (TypeError, ValueError):
            continue
    return parsed


def split_multi_value_field(value: str) -> list[str]:
    text = (value or "").strip()
    if not text:
        return []
    return [item.strip() for item in re.split(r"[|;,]", text) if item.strip()]


def decode_csv_upload(file_obj) -> tuple[str | None, str | None]:
    if not file_obj or not file_obj.filename:
        return None, "No file selected."

    filename = file_obj.filename
    if not filename.lower().endswith(".csv"):
        return None, "Only .csv files are supported."

    raw = file_obj.read()
    if not raw:
        return None, "Uploaded file is empty."

    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return None, "CSV must be UTF-8 encoded."


def find_row_value(row: dict[str, str], candidates: list[str]) -> str:
    for key in candidates:
        if key in row and row[key] is not None:
            return str(row[key]).strip()
    return ""


def parse_active_day(raw_value: str | None) -> str:
    if raw_value in WEEK_DAYS:
        return raw_value
    return WEEK_DAYS[0]


def default_free_slots() -> str:
    slots: list[str] = []
    for day in WEEK_DAYS:
        for slot in TEACHING_SLOTS:
            slots.append(f"{DAY_SHORT[day]} {slot}")
    return ", ".join(slots)


def slot_aliases(day: str, slot: str) -> set[str]:
    start = slot.split("-")[0]
    short_start = start[1:] if start.startswith("0") else start
    day_norm = normalize(day)
    day_short_norm = normalize(DAY_SHORT[day])
    return {
        normalize(f"{day_norm} {slot}"),
        normalize(f"{day_short_norm} {slot}"),
        normalize(f"{day_norm} {start}"),
        normalize(f"{day_short_norm} {start}"),
        normalize(f"{day_norm} {short_start}"),
        normalize(f"{day_short_norm} {short_start}"),
    }


def teacher_slot_tokens(teacher: Teacher) -> set[str]:
    return {normalize(part) for part in split_multi_value_field(teacher.free_slots)}


def teacher_is_available_for_slot(tokens: set[str], day: str, slot: str) -> bool:
    aliases = slot_aliases(day, slot)
    return any(token in aliases for token in tokens)


def grid_sessions_query():
    return Session.query.filter(Session.day.in_(WEEK_DAYS), Session.slot.in_(TEACHING_SLOTS), Session.year_group.in_(YEAR_GROUPS))


def get_or_create_grid_cell_session(day: str, slot: str, year_group: str, required_skill_id: int) -> tuple[Session, int]:
    matches = Session.query.filter_by(day=day, slot=slot, year_group=year_group).order_by(Session.id.asc()).all()
    if not matches:
        created = Session(day=day, slot=slot, year_group=year_group, required_skill_id=required_skill_id)
        db.session.add(created)
        return created, 0

    primary = matches[0]
    deduped = 0
    for extra in matches[1:]:
        if primary.assigned_teacher_id is None and extra.assigned_teacher_id is not None:
            primary.assigned_teacher_id = extra.assigned_teacher_id
        db.session.delete(extra)
        deduped += 1
    return primary, deduped


def assignment_issue(teacher: Teacher, session: Session, tokens: set[str] | None = None, skill_ids: set[int] | None = None) -> str | None:
    """Return why `teacher` cannot take `session`, or None if the assignment is valid."""
    if tokens is None:
        tokens = teacher_slot_tokens(teacher)
    if skill_ids is None:
        skill_ids = {skill.id for skill in teacher.skills}
    if session.required_skill_id not in skill_ids:
        skill_name = session.required_skill.name if session.required_skill else "the required skill"
        return f"does not teach {skill_name}"
    if not teacher_is_available_for_slot(tokens, session.day, session.slot):
        return "is not free at this time"
    return None


def describe_cell(session: Session) -> str:
    return f"{session.day} {session.slot} {session.year_group}"


def match_teachers_to_sessions(sessions: list[Session], candidates: dict[int, list[int]]) -> dict[int, int]:
    """Maximum bipartite matching (augmenting paths) for one period; returns session_id -> teacher_id.

    Candidate lists are in preference order, so ties go to the teachers listed first.
    """
    holder_of_teacher: dict[int, Session] = {}

    def try_assign(session: Session, visited: set[int]) -> bool:
        for teacher_id in candidates[session.id]:
            if teacher_id in visited:
                continue
            visited.add(teacher_id)
            holder = holder_of_teacher.get(teacher_id)
            if holder is None or try_assign(holder, visited):
                holder_of_teacher[teacher_id] = session
                return True
        return False

    # Hardest-to-fill sessions first so they get first pick of the preferred teachers.
    for session in sorted(sessions, key=lambda item: len(candidates[item.id])):
        try_assign(session, set())

    return {session.id: teacher_id for teacher_id, session in holder_of_teacher.items()}


def allocate_sessions(replace_locked: bool = False) -> dict[str, int]:
    teacher_rows = Teacher.query.order_by(Teacher.name.asc()).all()
    session_rows = grid_sessions_query().all()
    BAC_LOG.info("step=allocate.start teachers=%s sessions=%s replace_locked=%s", len(teacher_rows), len(session_rows), replace_locked)

    teacher_by_id = {teacher.id: teacher for teacher in teacher_rows}
    teacher_tokens = {teacher.id: teacher_slot_tokens(teacher) for teacher in teacher_rows}
    teacher_skill_ids = {teacher.id: {skill.id for skill in teacher.skills} for teacher in teacher_rows}
    assigned_count: dict[int, int] = {teacher.id: 0 for teacher in teacher_rows}

    sessions_by_period: dict[tuple[str, str], list[Session]] = defaultdict(list)
    kept_locked = 0
    dropped_locks = 0

    for session in session_rows:
        sessions_by_period[(session.day, session.slot)].append(session)
        teacher = teacher_by_id.get(session.assigned_teacher_id)
        if session.is_locked and not replace_locked and teacher is not None:
            issue = assignment_issue(teacher, session, teacher_tokens[teacher.id], teacher_skill_ids[teacher.id])
            if issue is None:
                kept_locked += 1
                assigned_count[teacher.id] += 1
                continue
            dropped_locks += 1
            BAC_LOG.warning("step=allocate.lock_dropped session_id=%s teacher_id=%s reason=%r", session.id, teacher.id, issue)
        session.assigned_teacher_id = None
        session.is_locked = False

    # Write the cleared cells first so new assignments cannot trip the one-teacher-per-period index mid-flush.
    db.session.flush()

    assigned = 0
    unassigned = 0
    day_order = {day: idx for idx, day in enumerate(WEEK_DAYS)}
    slot_order = {slot: idx for idx, slot in enumerate(TEACHING_SLOTS)}

    for day, slot in sorted(sessions_by_period, key=lambda key: (day_order[key[0]], slot_order[key[1]])):
        period_sessions = sessions_by_period[(day, slot)]
        busy = {session.assigned_teacher_id for session in period_sessions if session.assigned_teacher_id}
        open_sessions = [session for session in period_sessions if not session.assigned_teacher_id]
        if not open_sessions:
            continue

        # Teachers with lighter loads so far are preferred, which spreads work across the week.
        ranked_teachers = sorted(
            (teacher for teacher in teacher_rows if teacher.id not in busy),
            key=lambda teacher: (assigned_count[teacher.id], teacher.name.lower()),
        )
        candidates = {
            session.id: [
                teacher.id
                for teacher in ranked_teachers
                if assignment_issue(teacher, session, teacher_tokens[teacher.id], teacher_skill_ids[teacher.id]) is None
            ]
            for session in open_sessions
        }

        matching = match_teachers_to_sessions(open_sessions, candidates)
        for session in open_sessions:
            teacher_id = matching.get(session.id)
            if teacher_id is None:
                unassigned += 1
                BAC_LOG.info(
                    "step=allocate.unassigned session_id=%s day=%s slot=%s year_group=%s",
                    session.id,
                    session.day,
                    session.slot,
                    session.year_group,
                )
                continue
            session.assigned_teacher_id = teacher_id
            assigned_count[teacher_id] += 1
            assigned += 1
            BAC_LOG.info(
                "step=allocate.assigned session_id=%s teacher_id=%s day=%s slot=%s year_group=%s",
                session.id,
                teacher_id,
                session.day,
                session.slot,
                session.year_group,
            )

    db.session.commit()
    BAC_LOG.info(
        "step=allocate.complete assigned=%s unassigned=%s kept_locked=%s dropped_locks=%s",
        assigned,
        unassigned,
        kept_locked,
        dropped_locks,
    )
    return {"assigned": assigned, "unassigned": unassigned, "kept_locked": kept_locked, "dropped_locks": dropped_locks}


def build_schedule(teachers: list[Teacher], sessions: list[Session]) -> dict[str, list[dict]]:
    schedule_sessions = [
        session
        for session in sessions
        if session.day in WEEK_DAYS and session.slot in TEACHING_SLOTS and session.year_group in YEAR_GROUPS
    ]
    session_lookup = {(session.day, session.slot, session.year_group): session for session in schedule_sessions}

    busy_at_slot: dict[tuple[str, str], set[int]] = defaultdict(set)
    for session in schedule_sessions:
        if session.assigned_teacher_id:
            busy_at_slot[(session.day, session.slot)].add(session.assigned_teacher_id)

    teacher_tokens = {teacher.id: teacher_slot_tokens(teacher) for teacher in teachers}
    teacher_skill_ids = {teacher.id: {skill.id for skill in teacher.skills} for teacher in teachers}

    schedule: dict[str, list[dict]] = {}
    for day in WEEK_DAYS:
        day_rows: list[dict] = []
        for row in TIME_ROWS:
            slot = row["slot"]
            if row["is_lunch"]:
                day_rows.append({"is_lunch": True, "slot": slot, "cells": []})
                continue

            cells: list[dict] = []
            for year_group in YEAR_GROUPS:
                existing = session_lookup.get((day, slot, year_group))
                current_teacher_id = existing.assigned_teacher_id if existing else None
                required_skill_id = existing.required_skill_id if existing else None
                assignment_problem = None
                if existing and existing.assigned_teacher:
                    assignment_problem = assignment_issue(
                        existing.assigned_teacher,
                        existing,
                        teacher_tokens.get(current_teacher_id),
                        teacher_skill_ids.get(current_teacher_id),
                    )

                teacher_options = []
                for teacher in teachers:
                    available = teacher_is_available_for_slot(teacher_tokens[teacher.id], day, slot)
                    busy_elsewhere = teacher.id in busy_at_slot[(day, slot)] and teacher.id != current_teacher_id
                    has_skill = True
                    if required_skill_id:
                        has_skill = required_skill_id in teacher_skill_ids.get(teacher.id, set())
                    selectable = available and (not busy_elsewhere) and has_skill

                    teacher_options.append(
                        {
                            "id": teacher.id,
                            "name": teacher.name,
                            "skills": ", ".join(skill.name for skill in teacher.skills) or "No skills",
                            "selected": teacher.id == current_teacher_id,
                            "available": available,
                            "busy": busy_elsewhere,
                            "has_skill": has_skill,
                            "selectable": selectable,
                        }
                    )

                cells.append(
                    {
                        "year_group": year_group,
                        "day": day,
                        "slot": slot,
                        "session_id": existing.id if existing else None,
                        "required_skill_id": required_skill_id,
                        "assigned_teacher_id": current_teacher_id,
                        "assigned_teacher_name": existing.assigned_teacher.name if existing and existing.assigned_teacher else None,
                        "is_locked": bool(existing and existing.is_locked and current_teacher_id),
                        "assignment_problem": assignment_problem,
                        "teacher_options": teacher_options,
                    }
                )

            day_rows.append({"is_lunch": False, "slot": slot, "cells": cells})

        schedule[day] = day_rows

    return schedule


@app.get("/")
def index():
    active_day = parse_active_day(request.args.get("active_day"))
    edit_teacher_id = request.args.get("edit_teacher_id", type=int)
    edit_skill_id = request.args.get("edit_skill_id", type=int)

    teachers = Teacher.query.order_by(Teacher.name.asc()).all()
    skills = Skill.query.order_by(Skill.name.asc()).all()
    sessions = Session.query.order_by(Session.id.asc()).all()

    edit_teacher = db.session.get(Teacher, edit_teacher_id) if edit_teacher_id else None
    edit_skill = db.session.get(Skill, edit_skill_id) if edit_skill_id else None

    schedule = build_schedule(teachers, sessions)
    unassigned_count = sum(
        1
        for session in sessions
        if session.day in WEEK_DAYS and session.slot in TEACHING_SLOTS and session.year_group in YEAR_GROUPS and session.assigned_teacher_id is None
    )

    BAC_LOG.info(
        "step=index.render teachers=%s skills=%s sessions=%s active_day=%s",
        len(teachers),
        len(skills),
        len(sessions),
        active_day,
    )

    return render_template(
        "index.html",
        teachers=teachers,
        skills=skills,
        edit_teacher=edit_teacher,
        edit_skill=edit_skill,
        edit_teacher_skill_ids={skill.id for skill in edit_teacher.skills} if edit_teacher else set(),
        week_days=WEEK_DAYS,
        year_groups=YEAR_GROUPS,
        schedule=schedule,
        active_day=active_day,
        unassigned_count=unassigned_count,
    )


@app.post("/import/skills")
def import_skills_csv():
    upload = request.files.get("skills_file")
    BAC_LOG.info("step=skills.import.request filename=%r", upload.filename if upload else None)

    text, error = decode_csv_upload(upload)
    if error:
        BAC_LOG.warning("step=skills.import.validation_failed reason=%s", error)
        flash(error, "error")
        return redirect(url_for("index"))

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        BAC_LOG.warning("step=skills.import.validation_failed reason=missing_header")
        flash("Skills CSV must contain headers.", "error")
        return redirect(url_for("index"))

    existing = {normalize(skill.name): skill for skill in Skill.query.all()}
    inserted_count = 0
    skipped_count = 0

    for line_no, row in enumerate(reader, start=2):
        name_field = find_row_value(row, ["name", "skill", "skill_name"])
        names = split_multi_value_field(name_field)
        if not names:
            skipped_count += 1
            BAC_LOG.warning("step=skills.import.row_skipped line=%s reason=missing_name", line_no)
            continue

        for name in names:
            key = normalize(name)
            if key in existing:
                skipped_count += 1
                BAC_LOG.info("step=skills.import.row_skipped line=%s reason=duplicate name=%r", line_no, name)
                continue

            skill = Skill(name=name)
            db.session.add(skill)
            existing[key] = skill
            inserted_count += 1
            BAC_LOG.info("step=skills.import.row_added line=%s name=%r", line_no, name)

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        BAC_LOG.warning("step=skills.import.failed reason=integrity_error")
        flash("Skills import failed due to duplicate values.", "error")
        return redirect(url_for("index"))

    BAC_LOG.info("step=skills.import.complete inserted=%s skipped=%s", inserted_count, skipped_count)
    flash(f"Skills import complete. Added {inserted_count}, skipped {skipped_count}.", "success")
    return redirect(url_for("index"))


@app.post("/import/teachers")
def import_teachers_csv():
    upload = request.files.get("teachers_file")
    BAC_LOG.info("step=teachers.import.request filename=%r", upload.filename if upload else None)

    text, error = decode_csv_upload(upload)
    if error:
        BAC_LOG.warning("step=teachers.import.validation_failed reason=%s", error)
        flash(error, "error")
        return redirect(url_for("index"))

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        BAC_LOG.warning("step=teachers.import.validation_failed reason=missing_header")
        flash("Teachers CSV must contain headers.", "error")
        return redirect(url_for("index"))

    skill_map = {normalize(skill.name): skill for skill in Skill.query.all()}
    inserted_teachers = 0
    auto_created_skills = 0
    skipped_rows = 0
    defaulted_slot_rows = 0

    for line_no, row in enumerate(reader, start=2):
        name = find_row_value(row, ["name", "teacher", "teacher_name"])
        free_slots_raw = find_row_value(row, ["free_slots", "slots", "availability"])
        skills_raw = find_row_value(row, ["skills", "skill", "skill_names"])

        slots = split_multi_value_field(free_slots_raw)
        skill_names = split_multi_value_field(skills_raw)

        if not name:
            skipped_rows += 1
            BAC_LOG.warning("step=teachers.import.row_skipped line=%s reason=missing_name", line_no)
            continue

        free_slots_value = ", ".join(slots) if slots else default_free_slots()
        if not slots:
            defaulted_slot_rows += 1
            BAC_LOG.info("step=teachers.import.row_defaulted_slots line=%s teacher_name=%r", line_no, name)

        teacher = Teacher(name=name, free_slots=free_slots_value)

        seen_skill_keys: set[str] = set()
        for skill_name in skill_names:
            key = normalize(skill_name)
            if key in seen_skill_keys:
                BAC_LOG.info("step=teachers.import.row_skipped line=%s reason=duplicate_skill_in_row name=%r", line_no, skill_name)
                continue
            seen_skill_keys.add(key)
            skill = skill_map.get(key)
            if skill is None:
                skill = Skill(name=skill_name)
                db.session.add(skill)
                skill_map[key] = skill
                auto_created_skills += 1
                BAC_LOG.info("step=teachers.import.skill_auto_created line=%s name=%r", line_no, skill_name)
            teacher.skills.append(skill)

        db.session.add(teacher)
        inserted_teachers += 1
        BAC_LOG.info("step=teachers.import.row_added line=%s teacher_name=%r", line_no, name)

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        BAC_LOG.warning("step=teachers.import.failed reason=integrity_error")
        flash("Teacher import failed due to duplicate values.", "error")
        return redirect(url_for("index"))

    BAC_LOG.info(
        "step=teachers.import.complete inserted_teachers=%s auto_created_skills=%s skipped_rows=%s defaulted_slot_rows=%s",
        inserted_teachers,
        auto_created_skills,
        skipped_rows,
        defaulted_slot_rows,
    )
    flash(
        (
            "Teacher import complete. "
            f"Added {inserted_teachers} teachers, "
            f"created {auto_created_skills} skills, "
            f"defaulted availability for {defaulted_slot_rows} rows, "
            f"skipped {skipped_rows} rows."
        ),
        "success",
    )
    return redirect(url_for("index"))


@app.post("/skills")
def create_skill():
    name = request.form.get("name", "").strip()
    BAC_LOG.info("step=skills.create.request name=%r", name)
    if not name:
        BAC_LOG.warning("step=skills.create.validation_failed reason=missing_name")
        flash("Skill name is required.", "error")
        return redirect(url_for("index"))

    db.session.add(Skill(name=name))
    try:
        db.session.commit()
        BAC_LOG.info("step=skills.create.success name=%r", name)
        flash("Skill created.", "success")
    except IntegrityError:
        db.session.rollback()
        BAC_LOG.warning("step=skills.create.failed reason=duplicate_name name=%r", name)
        flash("Skill name must be unique.", "error")

    return redirect(url_for("index"))


@app.post("/skills/<int:skill_id>/update")
def update_skill(skill_id: int):
    skill = db.get_or_404(Skill, skill_id)
    name = request.form.get("name", "").strip()
    BAC_LOG.info("step=skills.update.request skill_id=%s new_name=%r", skill_id, name)

    if not name:
        BAC_LOG.warning("step=skills.update.validation_failed skill_id=%s reason=missing_name", skill_id)
        flash("Skill name is required.", "error")
        return redirect(url_for("index", edit_skill_id=skill.id))

    skill.name = name
    try:
        db.session.commit()
        BAC_LOG.info("step=skills.update.success skill_id=%s", skill_id)
        flash("Skill updated.", "success")
    except IntegrityError:
        db.session.rollback()
        BAC_LOG.warning("step=skills.update.failed skill_id=%s reason=duplicate_name", skill_id)
        flash("Skill name must be unique.", "error")
        return redirect(url_for("index", edit_skill_id=skill.id))

    return redirect(url_for("index"))


@app.post("/skills/<int:skill_id>/delete")
def delete_skill(skill_id: int):
    skill = db.get_or_404(Skill, skill_id)
    BAC_LOG.info("step=skills.delete.request skill_id=%s", skill_id)

    if skill.required_sessions:
        BAC_LOG.warning("step=skills.delete.blocked skill_id=%s reason=in_use_by_sessions", skill_id)
        flash("Cannot delete a skill that is used by sessions.", "error")
        return redirect(url_for("index"))

    db.session.delete(skill)
    db.session.commit()
    BAC_LOG.info("step=skills.delete.success skill_id=%s", skill_id)
    flash("Skill deleted.", "success")
    return redirect(url_for("index"))


@app.post("/teachers")
def create_teacher():
    name = request.form.get("name", "").strip()
    free_slots_raw = request.form.get("free_slots", "").strip()
    free_slots = normalize_slot_csv(free_slots_raw) if free_slots_raw else default_free_slots()
    skill_ids = parse_int_list(request.form.getlist("skill_ids"))
    BAC_LOG.info("step=teachers.create.request name=%r skill_ids=%s has_free_slots_input=%s", name, skill_ids, bool(free_slots_raw))

    if not name:
        BAC_LOG.warning("step=teachers.create.validation_failed reason=missing_name")
        flash("Teacher name is required.", "error")
        return redirect(url_for("index"))

    teacher = Teacher(name=name, free_slots=free_slots)
    if skill_ids:
        teacher.skills = Skill.query.filter(Skill.id.in_(skill_ids)).all()

    db.session.add(teacher)
    db.session.commit()
    BAC_LOG.info("step=teachers.create.success teacher_id=%s", teacher.id)
    flash("Teacher created.", "success")
    return redirect(url_for("index"))


@app.post("/teachers/<int:teacher_id>/update")
def update_teacher(teacher_id: int):
    teacher = db.get_or_404(Teacher, teacher_id)
    name = request.form.get("name", "").strip()
    free_slots_raw = request.form.get("free_slots")
    if free_slots_raw is None or not free_slots_raw.strip():
        free_slots = teacher.free_slots or default_free_slots()
    else:
        free_slots = normalize_slot_csv(free_slots_raw)
    skill_ids = parse_int_list(request.form.getlist("skill_ids"))
    BAC_LOG.info("step=teachers.update.request teacher_id=%s name=%r skill_ids=%s has_free_slots_input=%s", teacher_id, name, skill_ids, bool(free_slots_raw and free_slots_raw.strip()))

    if not name:
        BAC_LOG.warning("step=teachers.update.validation_failed teacher_id=%s reason=missing_name", teacher_id)
        flash("Teacher name is required.", "error")
        return redirect(url_for("index", edit_teacher_id=teacher.id))

    teacher.name = name
    teacher.free_slots = free_slots
    teacher.skills = Skill.query.filter(Skill.id.in_(skill_ids)).all() if skill_ids else []

    # Drop any existing assignments the new skills/availability no longer allow.
    released: list[str] = []
    for session in grid_sessions_query().filter(Session.assigned_teacher_id == teacher.id).all():
        issue = assignment_issue(teacher, session)
        if issue is None:
            continue
        session.assigned_teacher_id = None
        session.is_locked = False
        released.append(f"{describe_cell(session)} ({issue})")
        BAC_LOG.warning("step=teachers.update.released_session teacher_id=%s session_id=%s reason=%r", teacher_id, session.id, issue)

    db.session.commit()
    BAC_LOG.info("step=teachers.update.success teacher_id=%s released_sessions=%s", teacher_id, len(released))
    flash("Teacher updated.", "success")
    if released:
        flash(
            f"{teacher.name} was removed from {len(released)} class(es) they can no longer take: " + "; ".join(released),
            "warning",
        )
    return redirect(url_for("index"))


@app.post("/teachers/<int:teacher_id>/delete")
def delete_teacher(teacher_id: int):
    teacher = db.get_or_404(Teacher, teacher_id)
    BAC_LOG.info("step=teachers.delete.request teacher_id=%s", teacher_id)
    Session.query.filter_by(assigned_teacher_id=teacher.id).update({Session.assigned_teacher_id: None})
    db.session.delete(teacher)
    db.session.commit()
    BAC_LOG.info("step=teachers.delete.success teacher_id=%s", teacher_id)
    flash("Teacher deleted.", "success")
    return redirect(url_for("index"))


@app.post("/sessions/grid/save")
def save_grid_session():
    active_day = parse_active_day(request.form.get("active_day"))
    day = (request.form.get("day", "") or "").strip()
    slot = (request.form.get("slot", "") or "").strip()
    year_group = (request.form.get("year_group", "") or "").strip()
    required_skill_id = request.form.get("required_skill_id", type=int)
    assigned_teacher_id = request.form.get("assigned_teacher_id", type=int)
    is_locked = request.form.get("is_locked") == "1"

    BAC_LOG.info(
        "step=sessions.grid.save.request day=%r slot=%r year_group=%r skill_id=%r teacher_id=%r is_locked=%s",
        day,
        slot,
        year_group,
        required_skill_id,
        assigned_teacher_id,
        is_locked,
    )

    if day not in WEEK_DAYS or slot not in TEACHING_SLOTS or year_group not in YEAR_GROUPS:
        BAC_LOG.warning("step=sessions.grid.save.validation_failed reason=invalid_grid_coordinates")
        flash("Invalid day/slot/year cell.", "error")
        return redirect(url_for("index", active_day=active_day))

    skill = db.session.get(Skill, required_skill_id) if required_skill_id else None
    if not skill:
        BAC_LOG.warning("step=sessions.grid.save.validation_failed reason=missing_skill")
        flash("Select a required skill for this cell.", "error")
        return redirect(url_for("index", active_day=active_day))

    session, deduped_cells = get_or_create_grid_cell_session(day, slot, year_group, skill.id)
    if deduped_cells:
        BAC_LOG.warning("step=sessions.grid.save.deduped_cells count=%s day=%s slot=%s year_group=%s", deduped_cells, day, slot, year_group)
    session.required_skill_id = skill.id

    if assigned_teacher_id:
        teacher = db.session.get(Teacher, assigned_teacher_id)
        if not teacher:
            BAC_LOG.warning("step=sessions.grid.save.validation_failed reason=teacher_not_found")
            flash("Selected teacher does not exist.", "error")
            return redirect(url_for("index", active_day=active_day))

        tokens = teacher_slot_tokens(teacher)
        if not teacher_is_available_for_slot(tokens, day, slot):
            BAC_LOG.warning("step=sessions.grid.save.validation_failed reason=teacher_not_available teacher_id=%s", teacher.id)
            flash("Selected teacher is not free in this time slot.", "error")
            return redirect(url_for("index", active_day=active_day))

        teacher_skill_ids = {item.id for item in teacher.skills}
        if skill.id not in teacher_skill_ids:
            BAC_LOG.warning("step=sessions.grid.save.validation_failed reason=teacher_missing_skill teacher_id=%s", teacher.id)
            flash("Selected teacher does not have the selected skill.", "error")
            return redirect(url_for("index", active_day=active_day))

        conflict = Session.query.filter(
            Session.day == day,
            Session.slot == slot,
            Session.assigned_teacher_id == teacher.id,
            Session.id != session.id,
        ).first()
        if conflict:
            BAC_LOG.warning("step=sessions.grid.save.validation_failed reason=teacher_busy teacher_id=%s", teacher.id)
            flash("Selected teacher is already allocated in this period.", "error")
            return redirect(url_for("index", active_day=active_day))

        session.assigned_teacher_id = teacher.id
        session.is_locked = is_locked
    else:
        session.assigned_teacher_id = None
        session.is_locked = False

    db.session.commit()
    BAC_LOG.info("step=sessions.grid.save.success session_id=%s", session.id)
    flash("Session cell saved.", "success")
    return redirect(url_for("index", active_day=active_day))


@app.post("/sessions/grid/clear")
def clear_grid_session():
    active_day = parse_active_day(request.form.get("active_day"))
    day = (request.form.get("day", "") or "").strip()
    slot = (request.form.get("slot", "") or "").strip()
    year_group = (request.form.get("year_group", "") or "").strip()

    BAC_LOG.info("step=sessions.grid.clear.request day=%r slot=%r year_group=%r", day, slot, year_group)

    sessions = Session.query.filter_by(day=day, slot=slot, year_group=year_group).all()
    if not sessions:
        BAC_LOG.warning("step=sessions.grid.clear.skipped reason=not_found")
        flash("No session found for this cell.", "error")
        return redirect(url_for("index", active_day=active_day))

    for session in sessions:
        db.session.delete(session)
    db.session.commit()
    BAC_LOG.info("step=sessions.grid.clear.success removed=%s", len(sessions))
    flash("Session cell cleared.", "success")
    return redirect(url_for("index", active_day=active_day))


@app.post("/allocate")
def run_allocation():
    BAC_LOG.info("step=allocate.request")
    if grid_sessions_query().count() == 0:
        BAC_LOG.warning("step=allocate.skipped reason=no_sessions")
        flash("No sessions to allocate.", "error")
        return redirect(url_for("index"))

    replace_locked = request.form.get("replace_locked") == "1"
    result = allocate_sessions(replace_locked=replace_locked)
    BAC_LOG.info("step=allocate.success")

    message = f"Allocation completed. Assigned {result['assigned']}"
    if result["kept_locked"]:
        message += f", kept {result['kept_locked']} locked"
    message += f", {result['unassigned']} still unassigned."
    flash(message, "success" if result["unassigned"] == 0 else "warning")
    if result["dropped_locks"]:
        flash(
            f"{result['dropped_locks']} locked cell(s) were unlocked and re-allocated because their teacher no longer has the skill or free time.",
            "warning",
        )
    return redirect(url_for("index"))


def initialize_database() -> None:
    with app.app_context():
        run_bootstrap_step("create_all", db.create_all)
        run_bootstrap_step("ensure_session_grid_columns", ensure_session_grid_columns)
        run_bootstrap_step("migrate_legacy_year_groups", migrate_legacy_year_groups)
        run_bootstrap_step("deduplicate_grid_sessions", deduplicate_grid_sessions)
        run_bootstrap_step("ensure_grid_indexes", ensure_grid_indexes)
        BAC_LOG.info("step=bootstrap.db_ready uri=%s", app.config["SQLALCHEMY_DATABASE_URI"])


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    if not acquire_app_lock():
        raise SystemExit(1)
    should_initialize = (not debug) or (os.getenv("WERKZEUG_RUN_MAIN") == "true")
    if should_initialize:
        initialize_database()
    else:
        BAC_LOG.info("step=bootstrap.skip reason=debug_reloader_parent")
    BAC_LOG.info("step=server.start host=%s port=%s debug=%s use_reloader=%s", host, port, debug, False)
    app.run(host=host, port=port, debug=debug, use_reloader=False)
