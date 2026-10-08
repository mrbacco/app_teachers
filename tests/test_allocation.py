"""
Tests for timetable allocation, locked cells, and assignment revalidation.

Run with: python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

_DB_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_DB_DIR.name) / 'test.db'}"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module  # noqa: E402
from app import Session, Skill, Teacher, allocate_sessions, app, db  # noqa: E402

DAY = "Monday"
SLOT = "09:00-10:00"
FREE_MONDAY_9 = "Mon 09:00-10:00"


def tearDownModule():
    # Windows cannot delete the temp database while pooled connections still hold it open.
    with app.app_context():
        db.engine.dispose()
    _DB_DIR.cleanup()


class AllocationTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        app_module.initialize_database()
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def make_skill(self, name):
        skill = Skill(name=name)
        db.session.add(skill)
        db.session.commit()
        return skill

    def make_teacher(self, name, skills, free_slots=FREE_MONDAY_9):
        teacher = Teacher(name=name, free_slots=free_slots, skills=list(skills))
        db.session.add(teacher)
        db.session.commit()
        return teacher

    def make_session(self, year_group, skill, teacher=None, locked=False, day=DAY, slot=SLOT):
        session = Session(
            day=day,
            slot=slot,
            year_group=year_group,
            required_skill_id=skill.id,
            assigned_teacher_id=teacher.id if teacher else None,
            is_locked=locked,
        )
        db.session.add(session)
        db.session.commit()
        return session

    def assigned_name(self, session):
        db.session.refresh(session)
        return session.assigned_teacher.name if session.assigned_teacher else None


class MatchingTests(AllocationTestCase):
    def test_flexible_teacher_is_saved_for_the_class_only_they_can_take(self):
        math, science = self.make_skill("Math"), self.make_skill("Science")
        self.make_teacher("Alice", [math, science])
        self.make_teacher("Bob", [math])
        grade1 = self.make_session("Grade 1", math)
        grade2 = self.make_session("Grade 2", science)

        result = allocate_sessions()

        self.assertEqual(result["unassigned"], 0)
        self.assertEqual(self.assigned_name(grade1), "Bob")
        self.assertEqual(self.assigned_name(grade2), "Alice")

    def test_longer_augmenting_chain_fills_every_class(self):
        a, b, c = self.make_skill("A"), self.make_skill("B"), self.make_skill("C")
        self.make_teacher("T1", [a, b])
        self.make_teacher("T2", [b, c])
        self.make_teacher("T3", [c])
        sessions = [
            self.make_session("Grade 1", a),
            self.make_session("Grade 2", b),
            self.make_session("Grade 3", c),
        ]

        result = allocate_sessions()

        self.assertEqual(result["unassigned"], 0)
        self.assertEqual([self.assigned_name(s) for s in sessions], ["T1", "T2", "T3"])

    def test_class_with_no_valid_teacher_stays_unassigned(self):
        math, art = self.make_skill("Math"), self.make_skill("Art")
        self.make_teacher("Alice", [math])
        session = self.make_session("Grade 1", art)

        result = allocate_sessions()

        self.assertEqual(result["unassigned"], 1)
        self.assertIsNone(self.assigned_name(session))

    def test_teacher_never_double_booked_in_one_period(self):
        math = self.make_skill("Math")
        self.make_teacher("Alice", [math])
        self.make_session("Grade 1", math)
        self.make_session("Grade 2", math)

        result = allocate_sessions()

        self.assertEqual((result["assigned"], result["unassigned"]), (1, 1))

    def test_work_is_spread_across_teachers(self):
        math = self.make_skill("Math")
        both_slots = "Mon 09:00-10:00, Mon 10:00-11:00"
        self.make_teacher("Alice", [math], both_slots)
        self.make_teacher("Bob", [math], both_slots)
        first = self.make_session("Grade 1", math, slot="09:00-10:00")
        second = self.make_session("Grade 1", math, slot="10:00-11:00")

        allocate_sessions()

        self.assertNotEqual(self.assigned_name(first), self.assigned_name(second))


class LockTests(AllocationTestCase):
    def test_locked_cell_is_kept(self):
        math = self.make_skill("Math")
        self.make_teacher("Alice", [math])
        bob = self.make_teacher("Bob", [math])
        session = self.make_session("Grade 1", math, teacher=bob, locked=True)

        result = allocate_sessions()

        self.assertEqual(result["kept_locked"], 1)
        self.assertEqual(self.assigned_name(session), "Bob")
        self.assertTrue(session.is_locked)

    def test_locked_teacher_is_busy_for_other_classes_that_period(self):
        math = self.make_skill("Math")
        alice = self.make_teacher("Alice", [math])
        self.make_session("Grade 1", math, teacher=alice, locked=True)
        other = self.make_session("Grade 2", math)

        allocate_sessions()

        self.assertIsNone(self.assigned_name(other))

    def test_unlocked_cell_is_reallocated(self):
        math = self.make_skill("Math")
        self.make_teacher("Alice", [math])
        bob = self.make_teacher("Bob", [math])
        session = self.make_session("Grade 1", math, teacher=bob, locked=False)

        allocate_sessions()

        self.assertEqual(self.assigned_name(session), "Alice")

    def test_replace_locked_reallocates_and_unlocks(self):
        math = self.make_skill("Math")
        self.make_teacher("Alice", [math])
        bob = self.make_teacher("Bob", [math])
        session = self.make_session("Grade 1", math, teacher=bob, locked=True)

        allocate_sessions(replace_locked=True)

        self.assertEqual(self.assigned_name(session), "Alice")
        self.assertFalse(session.is_locked)

    def test_invalid_locked_cell_is_dropped_and_refilled(self):
        math, art = self.make_skill("Math"), self.make_skill("Art")
        self.make_teacher("Alice", [art])
        bob = self.make_teacher("Bob", [math])
        session = self.make_session("Grade 1", art, teacher=bob, locked=True)

        result = allocate_sessions()

        self.assertEqual(result["dropped_locks"], 1)
        self.assertEqual(self.assigned_name(session), "Alice")
        self.assertFalse(session.is_locked)

    def test_grid_save_stores_lock_flag(self):
        math = self.make_skill("Math")
        alice = self.make_teacher("Alice", [math])
        form = {
            "day": DAY,
            "slot": SLOT,
            "year_group": "Grade 1",
            "active_day": DAY,
            "required_skill_id": str(math.id),
            "assigned_teacher_id": str(alice.id),
            "is_locked": "1",
        }

        self.client.post("/sessions/grid/save", data=form)
        session = Session.query.filter_by(day=DAY, slot=SLOT, year_group="Grade 1").one()
        self.assertTrue(session.is_locked)

        form["assigned_teacher_id"] = ""
        self.client.post("/sessions/grid/save", data=form)
        db.session.refresh(session)
        self.assertIsNone(session.assigned_teacher_id)
        self.assertFalse(session.is_locked)


class TeacherUpdateTests(AllocationTestCase):
    def update_teacher(self, teacher, skills, free_slots):
        return self.client.post(
            f"/teachers/{teacher.id}/update",
            data={"name": teacher.name, "free_slots": free_slots, "skill_ids": [str(s.id) for s in skills]},
            follow_redirects=True,
        )

    def test_removing_a_skill_releases_classes_needing_it(self):
        math, art = self.make_skill("Math"), self.make_skill("Art")
        alice = self.make_teacher("Alice", [math, art])
        math_class = self.make_session("Grade 1", math, teacher=alice, locked=True)
        art_class = self.make_session("Grade 2", art, teacher=alice, slot="10:00-11:00")

        response = self.update_teacher(alice, [art], "Mon 09:00-10:00, Mon 10:00-11:00")

        self.assertIsNone(self.assigned_name(math_class))
        self.assertFalse(math_class.is_locked)
        self.assertEqual(self.assigned_name(art_class), "Alice")
        self.assertIn(b"no longer take", response.data)
        self.assertIn(b"does not teach Math", response.data)

    def test_removing_free_time_releases_classes_in_that_slot(self):
        math = self.make_skill("Math")
        alice = self.make_teacher("Alice", [math])
        session = self.make_session("Grade 1", math, teacher=alice)

        response = self.update_teacher(alice, [math], "Tue 09:00-10:00")

        self.assertIsNone(self.assigned_name(session))
        self.assertIn(b"is not free at this time", response.data)

    def test_valid_update_keeps_assignments_and_shows_no_warning(self):
        math = self.make_skill("Math")
        alice = self.make_teacher("Alice", [math])
        session = self.make_session("Grade 1", math, teacher=alice)

        response = self.update_teacher(alice, [math], FREE_MONDAY_9)

        self.assertEqual(self.assigned_name(session), "Alice")
        self.assertNotIn(b"no longer take", response.data)

    def test_grid_flags_invalid_assignment_left_over_in_data(self):
        math, art = self.make_skill("Math"), self.make_skill("Art")
        alice = self.make_teacher("Alice", [art])
        # Simulates data saved before revalidation existed.
        self.make_session("Grade 1", math, teacher=alice)

        response = self.client.get("/")

        self.assertIn(b"Alice does not teach Math.", response.data)


if __name__ == "__main__":
    unittest.main()
