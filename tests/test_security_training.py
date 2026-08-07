"""Security & training regression tests for the Socrates platform.

Run from the project root:
    SOCRATES_DB=/tmp/socrates_test.db ./venv/bin/python -m unittest tests.test_security_training -v

The tests use an isolated SQLite database (never the real socrates.db) and the
Flask test client, so nothing touches the live server.
"""
import io
import os
import sqlite3
import unittest

# Point the app at an isolated database BEFORE importing it.
TEST_DB = os.path.join('/tmp', 'socrates_test_{}.db'.format(os.getpid()))
os.environ['SOCRATES_DB'] = TEST_DB
for stale in (TEST_DB + '-wal', TEST_DB + '-shm'):
    if os.path.exists(stale):
        os.remove(stale)
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

import app as app_module  # noqa: E402


class SecurityTrainingTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_module.app.config['TESTING'] = True
        with app_module.app.app_context():
            app_module.init_db()
        cls.client = app_module.app.test_client()

    def _db(self):
        conn = sqlite3.connect(TEST_DB)
        conn.row_factory = sqlite3.Row
        return conn

    def _login(self, tid='ADMIN', pwd='admin123'):
        return self.client.post('/api/admin/login', json={
            'trainer_id': tid, 'password': pwd})

    # ---- 1. Password hashing ----

    def test_01_login_with_hashed_password(self):
        r = self._login('ADMIN', 'admin123')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(d['status'], 'success')
        self.assertIn('must_change', d)

    def test_02_legacy_plaintext_login_rehashes(self):
        conn = self._db()
        conn.execute(
            "INSERT INTO trainers (trainer_id, name, zone, password, role, status, must_change) "
            "VALUES ('LEGACY1', 'Legacy Trainer', 'All', 'plainpass1', 'Trainer', 'Active', 0)")
        conn.commit()
        conn.close()
        r = self._login('LEGACY1', 'plainpass1')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        conn = self._db()
        row = conn.execute(
            "SELECT password FROM trainers WHERE trainer_id='LEGACY1'").fetchone()
        conn.close()
        self.assertFalse(row['password'].startswith('pbkdf2:') is False and row['password'] == 'plainpass1',
                         'legacy password should have been upgraded')
        self.assertTrue(row['password'].startswith('pbkdf2:'))

    def test_03_wrong_password_rejected(self):
        r = self._login('ADMIN', 'wrongpassword')
        self.assertEqual(r.status_code, 401)

    # ---- 2. Change password ----

    def test_04_change_password_wrong_old_rejected(self):
        self._login('ADMIN', 'admin123')
        r = self.client.post('/api/admin/change-password', json={
            'old_password': 'not-the-password', 'new_password': 'brandnew1'})
        self.assertEqual(r.status_code, 401, r.get_data(as_text=True))

    def test_05_change_password_works_then_login_with_new(self):
        self._login('ADMIN', 'admin123')
        r = self.client.post('/api/admin/change-password', json={
            'old_password': 'admin123', 'new_password': 'newpwd456'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # Old password must no longer work.
        self.assertEqual(self._login('ADMIN', 'admin123').status_code, 401)
        self.assertEqual(self._login('ADMIN', 'newpwd456').status_code, 200)
        # Restore original for later tests.
        self._login('ADMIN', 'newpwd456')
        self.client.post('/api/admin/change-password', json={
            'old_password': 'newpwd456', 'new_password': 'admin123'})

    # ---- 3. Forgot-password rate limiting ----

    def test_06_forgot_password_rate_limit(self):
        # 5 allowed attempts per 15 min; the 6th must be throttled with 429.
        # 'NOPE' is unregistered, so each attempt returns 404 (and still counts
        # toward the rate limit via _fp_note_attempt).
        for _ in range(5):
            r = self.client.post('/api/admin/forgot-password', json={
                'trainer_id': 'NOPE', 'name': 'Nobody', 'new_password': 'whatever1'})
            self.assertIn(r.status_code, (400, 404, 429), r.get_data(as_text=True))
        r = self.client.post('/api/admin/forgot-password', json={
            'trainer_id': 'NOPE', 'name': 'Nobody', 'new_password': 'whatever1'})
        self.assertEqual(r.status_code, 429, r.get_data(as_text=True))

    # ---- 4. Bulk CSV training assignment ----

    def test_07_bulk_assign_creates_cycles_and_reports_failures(self):
        conn = self._db()
        conn.execute(
            "INSERT OR REPLACE INTO employees (emp_code, emp_name, branch_name, zone, division, business_unit, role, status) "
            "VALUES ('SF-TEST-1', 'Test Agent', 'TEST BRANCH', 'TEST ZONE', 'TEST DIV', 'TW', 'PL EXE', 'ACTIVE')")
        conn.commit()
        conn.close()
        self._login('ADMIN', 'admin123')
        csv_data = (
            "emp_code,module_id,mode,start_date,notes\n"
            "SF-TEST-1,,FULL,2026-08-10,First cycle\n"
            "SF-TEST-1,,REFRESHER,2026-08-12,Refresher\n"
            "NOT-IN-ROSTER,,FULL,2026-08-10,bad row\n"
        )
        r = self.client.post('/api/training/bulk-assign',
                             data={'file': (io.BytesIO(csv_data.encode()), 'bulk.csv')},
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(d['status'], 'success')
        self.assertEqual(len(d['assigned']), 2)
        self.assertEqual(len(d['failed']), 1)
        self.assertIn('NOT-IN-ROSTER', d['failed'][0]['emp_code'])
        conn = self._db()
        cycles = conn.execute(
            "SELECT * FROM training_cycles WHERE emp_code='SF-TEST-1'").fetchall()
        self.assertEqual(len(cycles), 2, 'history must be preserved, not overwritten')
        stages = conn.execute(
            "SELECT COUNT(*) AS c FROM training_cycle_stages WHERE cycle_id IN "
            "(SELECT id FROM training_cycles WHERE emp_code='SF-TEST-1')").fetchone()['c']
        conn.close()
        # FULL cycle = 3 stages (DAY 0/6/21) + REFRESHER = 1 stage.
        self.assertEqual(stages, 4)
        # The trainee should have received notifications.
        rn = self.client.get('/api/training/notifications?emp_code=SF-TEST-1')
        self.assertEqual(rn.status_code, 200)
        self.assertEqual(len(rn.get_json()['notifications']), 2)

    # ---- 5. Audit log ----

    def test_08_audit_log_records_and_access_control(self):
        conn = self._db()
        n = conn.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()['c']
        conn.close()
        self.assertGreater(n, 0, 'sensitive actions must be recorded')

        # Non-logged-in user cannot read the audit log.
        self.client.post('/api/admin/logout')
        r = self.client.get('/api/admin/audit-log')
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))

        # Super Admin can read it.
        self._login('ADMIN', 'admin123')
        r = self.client.get('/api/admin/audit-log?limit=50')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        logs = r.get_json()['logs']
        actions = {l['action'] for l in logs}
        self.assertIn('login', actions)
        self.assertIn('login_failed', actions)
        self.assertIn('training_bulk_assign', actions)

    # ---- 6. Training overview analytics ----

    def test_09_overview_returns_analytics(self):
        self._login('ADMIN', 'admin123')
        r = self.client.get('/api/training/overview')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertIn('analytics', d)
        self.assertIn('completion_rate', d['analytics'])
        self.assertIn('by_status', d['analytics'])


if __name__ == '__main__':
    unittest.main()
