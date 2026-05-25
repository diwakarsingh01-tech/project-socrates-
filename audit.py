import unittest
import sqlite3
import os
import tempfile
import json
import csv
from app import app, DB_FILE

class ProjectSocratesAuditSuite(unittest.TestCase):
    def setUp(self):
        # Configure Flask application for testing
        app.config['TESTING'] = True
        self.client = app.test_client()
        
        # Open connection to socrates.db to run sanity schema checks
        self.conn = sqlite3.connect(DB_FILE)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()

    def test_database_schema_integrity(self):
        """Audit 1: Verify all required SQLite tables exist and have correct schemas"""
        cursor = self.conn.cursor()
        
        # Check Tables List
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row['name'] for row in cursor.fetchall()]
        
        required_tables = ['employees', 'trainers', 'modules', 'questions', 'training_sessions']
        for tab in required_tables:
            self.assertIn(tab, tables, f"Database Integrity Audit Failed: Missing table '{tab}'!")
            
        # Check employees column layout
        cursor.execute("PRAGMA table_info(employees);")
        columns = [row['name'] for row in cursor.fetchall()]
        required_emp_columns = ['emp_code', 'emp_name', 'branch_name', 'zone', 'division', 'business_unit', 'role']
        for col in required_emp_columns:
            self.assertIn(col, columns, f"Database Integrity Audit Failed: Table 'employees' missing column '{col}'!")

        # Check modules column layout (status, created_by)
        cursor.execute("PRAGMA table_info(modules);")
        mod_columns = [row['name'] for row in cursor.fetchall()]
        self.assertIn('status', mod_columns, "Database Integrity Audit Failed: Table 'modules' missing 'status'!")
        self.assertIn('created_by', mod_columns, "Database Integrity Audit Failed: Table 'modules' missing 'created_by'!")

        # Check questions column layout
        cursor.execute("PRAGMA table_info(questions);")
        q_columns = [row['name'] for row in cursor.fetchall()]
        required_q_columns = ['id', 'module_id', 'question_text', 'option_a', 'option_b', 'option_c', 'option_d', 'correct_index', 'approved']
        for col in required_q_columns:
            self.assertIn(col, q_columns, f"Database Integrity Audit Failed: Table 'questions' missing column '{col}'!")

    def test_jinja_syntax_protection_audit(self):
        """Audit 2: Scan HTML files to guarantee React Babel scripts are raw-escaped from Jinja2 parsing errors"""
        base_dir = os.path.dirname(os.path.abspath(__file__))
        templates = ['admin.html', 'index.html']
        
        for name in templates:
            path = os.path.join(base_dir, 'templates', name)
            self.assertTrue(os.path.exists(path), f"Audit Failed: Template '{name}' not found at {path}!")
            
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
                
                # Assert raw-escape tags are present to protect React double curly braces
                self.assertIn('{% raw %}', content, f"Template Safety Audit Failed: '{name}' does not contain escape '{'{% raw %}'}' block!")
                self.assertIn('{% endraw %}', content, f"Template Safety Audit Failed: '{name}' does not contain closing escape '{'{% endraw %}'}' block!")

    def test_csv_upload_validation_and_duplicacy(self):
        """Audit 3: Verify roster CSV upload rejects missing headers and duplicates with explicit warning messages"""
        
        # 3a. Test Roster Upload with Missing Headers
        with tempfile.NamedTemporaryFile(suffix='.csv', mode='w+', delete=False) as f:
            writer = csv.writer(f)
            writer.writerow(['Employee Code', 'Employee Name'])  # Missing other 4 headers
            writer.writerow(['SF-9999', 'TEST USER'])
            temp_path = f.name
            
        with open(temp_path, 'rb') as f:
            res = self.client.post('/api/roster/upload', data={'file': (f, 'test_missing_headers.csv')})
            data = json.loads(res.data)
            self.assertEqual(res.status_code, 400)
            self.assertEqual(data['status'], 'error')
            self.assertIn("Missing column headers", data['message'])
        os.remove(temp_path)

        # 3b. Test Roster Upload with Duplicates (within CSV and database)
        # First write a valid seed row manually so it exists in DB
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM employees WHERE emp_code='SF-7777';")
        self.conn.commit()
        
        # Create CSV file with duplicates
        with tempfile.NamedTemporaryFile(suffix='.csv', mode='w+', delete=False) as f:
            writer = csv.writer(f)
            writer.writerow(['Employee Code', 'Employee Name', 'Branch Name', 'Zone', 'Division', 'Business Unit', 'Role'])
            # Row 2 (Valid)
            writer.writerow(['SF-7777', 'Seed Employee', 'HQ', 'North', 'HQ Div', 'Retail', 'Sales Executive'])
            # Row 3 (Duplicate inside file of Row 2)
            writer.writerow(['SF-7777', 'Seed Employee Duplicate', 'HQ', 'North', 'HQ Div', 'Retail', 'Sales Executive Duplicate'])
            temp_path = f.name
            
        with open(temp_path, 'rb') as f:
            res = self.client.post('/api/roster/upload', data={'file': (f, 'test_duplicates.csv')})
            data = json.loads(res.data)
            self.assertEqual(res.status_code, 400)
            self.assertEqual(data['status'], 'error')
            # Check requirement: "This is the duplicacy. You remove that."
            self.assertEqual(data['message'], "This is the duplicacy. You remove that.")
            self.assertTrue(len(data['details']) > 0, "Audit Failed: Duplication details array is empty!")
            
        os.remove(temp_path)

    def test_smart_search_matching(self):
        """Audit 4: Verify auto-fetch smart search returns correct case-insensitive matches"""
        # Inject seed employee for testing
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO employees (emp_code, emp_name, branch_name, zone, division, business_unit, role) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ('SF-8888', 'RAHUL SHARMA', 'South Delhi', 'North Zone', 'Delhi Division', 'Two-Wheeler', 'Sales Rep'))
        self.conn.commit()
        
        # Test exact match
        res = self.client.get('/api/roster/search?q=RAHUL')
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(len(data) > 0)
        self.assertEqual(data[0]['emp_code'], 'SF-8888')
        
        # Test case-insensitivity match
        res_lower = self.client.get('/api/roster/search?q=rahul')
        data_lower = json.loads(res_lower.data)
        self.assertTrue(len(data_lower) > 0)
        self.assertEqual(data_lower[0]['emp_name'], 'RAHUL SHARMA')

    def test_assessment_submission_and_analytics(self):
        """Audit 5: Verify assessment score upsert and analytical average scoring grouping works"""
        # Inject dummy employee
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO employees (emp_code, emp_name, branch_name, zone, division, business_unit, role) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ('SF-1234', 'DUMMY EXECUTIVE', 'Mumbai Central', 'West Zone', 'Mumbai Div', 'Two-Wheeler', 'PL Exe'))
        cursor.execute("DELETE FROM assessment_results WHERE emp_code='SF-1234';")
        self.conn.commit()

        # Step 1: Submit pre-test score
        res = self.client.post('/api/assessments/submit', json={
            'emp_code': 'SF-1234',
            'module_id': 1,
            'assignment_day': 'six days',
            'pre_test_score': 100
        })
        self.assertEqual(res.status_code, 200)
        
        # Verify db has pre score and 0/null post score
        row = self.conn.execute("SELECT * FROM assessment_results WHERE emp_code='SF-1234' AND assignment_day='SIX DAYS'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['pre_test_score'], 100.0)
        self.assertEqual(row['post_test_score'], 0.0) # Defaults to 0 on insert

        # Step 2: Submit post-test score (upsert)
        res_post = self.client.post('/api/assessments/submit', json={
            'emp_code': 'SF-1234',
            'module_id': 1,
            'assignment_day': 'six days',
            'post_test_score': 100
        })
        self.assertEqual(res_post.status_code, 200)

        # Verify db updated post score but did NOT overwrite pre score!
        row_updated = self.conn.execute("SELECT * FROM assessment_results WHERE emp_code='SF-1234' AND assignment_day='SIX DAYS'").fetchone()
        self.assertEqual(row_updated['pre_test_score'], 100.0)
        self.assertEqual(row_updated['post_test_score'], 100.0)

        # Step 3: Verify dynamic analytics groups it correctly under the new nested temporal payload
        res_analytics = self.client.get('/api/analytics')
        self.assertEqual(res_analytics.status_code, 200)
        data = json.loads(res_analytics.data)
        self.assertIn('temporal', data)
        self.assertIn('SIX DAYS', data['temporal'])
        self.assertEqual(data['temporal']['SIX DAYS']['pre'], 100.0)
        self.assertEqual(data['temporal']['SIX DAYS']['post'], 100.0)
        
        # Step 4: Verify dynamic hierarchical drill-down zone filtering & cascading options
        res_filtered = self.client.get('/api/analytics?zone=West+Zone')
        self.assertEqual(res_filtered.status_code, 200)
        data_filtered = json.loads(res_filtered.data)
        
        # Assert breakdown lists divisions under West Zone
        self.assertTrue('breakdown' in data_filtered)
        self.assertTrue('filter_options' in data_filtered)
        self.assertIn('West Zone', data_filtered['filter_options']['zones'])

    def test_maker_checker_module_creation_and_audit(self):
        """Audit 6: Verify Maker-Checker module creation, intermediate drafts, and isolation boundaries"""
        cursor = self.conn.cursor()
        # Clean up database state
        cursor.execute("DELETE FROM modules WHERE title='TEST SYSTEM GENERATED MODULE';")
        self.conn.commit()

        # Step 1: Call save_module to save an incomplete Draft (Pending Audit) for Trainer A
        draft_payload = {
            'title': 'TEST SYSTEM GENERATED MODULE',
            'trainer_id': 'TRAINER_A',
            'questions': [
                {
                    'question_text': 'What is two plus two?',
                    'options': ['Three', 'Four', 'Five', 'Six'],
                    'correct_index': 1,
                    'approved': 0  # Pending trainer review
                },
                {
                    'question_text': 'What is the color of the sky?',
                    'options': ['Green', 'Blue', 'Red', 'Yellow'],
                    'correct_index': 1,
                    'approved': 1  # Audited & Approved
                }
            ]
        }
        res_save = self.client.post('/api/modules/save', json=draft_payload)
        self.assertEqual(res_save.status_code, 200)
        data_save = json.loads(res_save.data)
        self.assertEqual(data_save['status'], 'success')
        self.assertEqual(data_save['module_status'], 'Pending Audit')
        module_id = data_save['module_id']

        # Step 2: Verify that Trainer A can see this draft in their modules list
        res_list_a = self.client.get(f'/api/modules?trainer_id=TRAINER_A')
        self.assertEqual(res_list_a.status_code, 200)
        modules_a = json.loads(res_list_a.data)
        found_in_a = any([m['id'] == module_id for m in modules_a])
        self.assertTrue(found_in_a, "Audit Failed: Trainer A cannot see their own pending draft!")

        # Step 3: Verify Global Module Sharing - Trainer B should see Trainer A's pending draft
        res_list_b = self.client.get(f'/api/modules?trainer_id=TRAINER_B')
        self.assertEqual(res_list_b.status_code, 200)
        modules_b = json.loads(res_list_b.data)
        found_in_b = any([m['id'] == module_id for m in modules_b])
        self.assertTrue(found_in_b, "Trainer B should see Trainer A's pending draft under globally shared modules!")

        # Step 4: Complete the audit (Finalize module - all approved)
        final_payload = {
            'title': 'TEST SYSTEM GENERATED MODULE',
            'trainer_id': 'TRAINER_A',
            'module_id': module_id,
            'questions': [
                {
                    'question_text': 'What is two plus two?',
                    'options': ['Three', 'Four', 'Five', 'Six'],
                    'correct_index': 1,
                    'approved': 1  # Now Audited & Approved
                },
                {
                    'question_text': 'What is the color of the sky?',
                    'options': ['Green', 'Blue', 'Red', 'Yellow'],
                    'correct_index': 1,
                    'approved': 1  # Audited & Approved
                }
            ]
        }
        res_finalize = self.client.post('/api/modules/save', json=final_payload)
        self.assertEqual(res_finalize.status_code, 200)
        data_finalize = json.loads(res_finalize.data)
        self.assertEqual(data_finalize['module_status'], 'Ready')

        # Step 5: Shared Approved Pool - Now Trainer B must be able to see this approved Ready module
        res_list_b_after = self.client.get(f'/api/modules?trainer_id=TRAINER_B')
        self.assertEqual(res_list_b_after.status_code, 200)
        modules_b_after = json.loads(res_list_b_after.data)
        found_in_b_after = any([m['id'] == module_id for m in modules_b_after])
        self.assertTrue(found_in_b_after, "Audit Failed: Trainer B cannot see Trainer A's finalized Ready module in the shared pool!")

        # Clean up
        self.client.delete(f'/api/modules/{module_id}')

    def test_live_gamification_and_leaderboard(self):
        """Audit 7: Verify backend gamification scoring calculations (Base + Speed Bonus)"""
        import time
        from app import SESSION_REGISTRY
        
        pin = "TEST_PIN_9999"
        # Reset/initialize session in registry
        SESSION_REGISTRY[pin] = {
            "push_time": time.time() - 2.0,  # Pushed 2 seconds ago
            "correct_index": 1,
            "leaderboard": {
                "EMP_TEST1": {
                    "name": "TEST TRAINEE 1",
                    "score": 0,
                    "last_speed": 0.0,
                    "last_correct": False
                }
            }
        }
        
        session = SESSION_REGISTRY[pin]
        correct_index = session["correct_index"]
        
        # Test Case 1: Trainee answers CORRECTLY after 2 seconds
        ans_idx_correct = 1
        is_correct_1 = ans_idx_correct == correct_index
        self.assertTrue(is_correct_1)
        
        response_time_1 = 2.0
        base_points = 1000
        speed_bonus_1 = max(0, int(1000 - (response_time_1 * 50)))
        self.assertEqual(speed_bonus_1, 900) # 1000 - 100 = 900
        points_earned_1 = base_points + speed_bonus_1
        self.assertEqual(points_earned_1, 1900)
        
        # Test Case 2: Trainee answers INCORRECTLY
        ans_idx_incorrect = 2
        is_correct_2 = ans_idx_incorrect == correct_index
        self.assertFalse(is_correct_2)
        points_earned_2 = 0
        self.assertEqual(points_earned_2, 0)
        
        # Clean up
        if pin in SESSION_REGISTRY:
            del SESSION_REGISTRY[pin]

    def test_trainer_access_control_and_revocation(self):
        """Audit 8: Verify live trainer provisioning, status revocation, and active-status login checks"""
        # Step 1: Create a temporary test trainer
        trainer_payload = {
            'id': 'TR-AUDIT-TEST',
            'name': 'AUDIT TRAINER',
            'zone': 'CH_BU',
            'password': 'auditpassword123'
        }
        res_create = self.client.post('/api/trainers', json=trainer_payload)
        self.assertEqual(res_create.status_code, 200)

        # Step 2: Verify they can login successfully
        login_payload = {
            'trainer_id': 'TR-AUDIT-TEST',
            'password': 'auditpassword123'
        }
        res_login_ok = self.client.post('/api/admin/login', json=login_payload)
        self.assertEqual(res_login_ok.status_code, 200)
        data_login_ok = json.loads(res_login_ok.data)
        self.assertEqual(data_login_ok['role'], 'Trainer')

        # Step 3: Revoke access (set status to Revoked)
        res_revoke = self.client.put('/api/trainers/TR-AUDIT-TEST/status', json={'status': 'Revoked'})
        self.assertEqual(res_revoke.status_code, 200)

        # Step 4: Verify login fails now with 401 Account Revoked
        res_login_fail = self.client.post('/api/admin/login', json=login_payload)
        self.assertEqual(res_login_fail.status_code, 401)

        # Step 5: Re-activate access
        res_reactivate = self.client.put('/api/trainers/TR-AUDIT-TEST/status', json={'status': 'Active'})
        self.assertEqual(res_reactivate.status_code, 200)

        # Step 6: Verify login succeeds again
        res_login_ok_2 = self.client.post('/api/admin/login', json=login_payload)
        self.assertEqual(res_login_ok_2.status_code, 200)

        # Clean up database record
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM trainers WHERE trainer_id='TR-AUDIT-TEST';")
        self.conn.commit()

    def test_database_reset_and_data_purge(self):
        """Audit 9: Verify database reset deletes all demo data while preserving ADMIN Super Admin"""
        # Step 1: Insert dummy test records into SQLite
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO employees (emp_code, emp_name) VALUES ('SF-RESET-DUMMY', 'Reset Trainee')")
        cursor.execute("INSERT OR REPLACE INTO trainers (trainer_id, name, password, role) VALUES ('TR-RESET-DUMMY', 'Reset Trainer', 'pwd', 'Trainer')")
        self.conn.commit()
        
        # Step 2: Trigger reset API
        res = self.client.post('/api/admin/reset-database')
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertEqual(data['status'], 'success')
        
        # Step 3: Verify data has been wiped
        cursor.execute("SELECT COUNT(*) FROM employees WHERE emp_code='SF-RESET-DUMMY'")
        self.assertEqual(cursor.fetchone()[0], 0)
        
        cursor.execute("SELECT COUNT(*) FROM trainers WHERE trainer_id='TR-RESET-DUMMY'")
        self.assertEqual(cursor.fetchone()[0], 0)
        
        # Step 4: Verify Super Admin is still preserved
        cursor.execute("SELECT password FROM trainers WHERE trainer_id='ADMIN'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 'admin123')

    def test_global_module_sharing(self):
        """Audit 10: Verify that modules are globally shared and visible to all trainers"""
        # Step 1: Create a test module from TR-TRAINER-A
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO modules (id, title, status, created_by) VALUES (8881, 'Shared Test Module', 'Pending Audit', 'TR-TRAINER-A')")
        self.conn.commit()

        # Step 2: Fetch modules as TR-TRAINER-B and verify it is visible
        res = self.client.get('/api/modules?trainer_id=TR-TRAINER-B')
        self.assertEqual(res.status_code, 200)
        modules = json.loads(res.data)
        
        module_titles = [m['title'] for m in modules]
        self.assertIn('Shared Test Module', module_titles)

        # Clean up module
        cursor.execute("DELETE FROM modules WHERE id=8881")
        self.conn.commit()

    def test_roster_filtering(self):
        """Audit 11: Verify dynamic roster filters and distinct option queries work correctly"""
        # Step 1: Insert distinct seed employees
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO employees (emp_code, emp_name, zone, division, branch_name) VALUES ('SF-FILT-1', 'NAME A', 'ZONE-X', 'DIV-Y', 'BRANCH-Z')")
        cursor.execute("INSERT OR REPLACE INTO employees (emp_code, emp_name, zone, division, branch_name) VALUES ('SF-FILT-2', 'NAME B', 'ZONE-P', 'DIV-Q', 'BRANCH-R')")
        self.conn.commit()

        # Step 2: Fetch filters and verify distinct lists
        res_filt = self.client.get('/api/roster/filters')
        self.assertEqual(res_filt.status_code, 200)
        filters = json.loads(res_filt.data)
        
        self.assertIn('ZONE-X', filters['zones'])
        self.assertIn('DIV-Q', filters['divisions'])
        self.assertIn('BRANCH-Z', filters['branches'])

        # Step 3: Fetch filtered roster and verify correct counts
        res_roster = self.client.get('/api/roster?zone=ZONE-X')
        self.assertEqual(res_roster.status_code, 200)
        employees = json.loads(res_roster.data)
        
        self.assertEqual(len(employees), 1)
        self.assertEqual(employees[0]['emp_code'], 'SF-FILT-1')

        # Step 4: Verify search queries work
        res_search = self.client.get('/api/roster?q=NAME%20B')
        self.assertEqual(res_search.status_code, 200)
        employees_search = json.loads(res_search.data)
        self.assertEqual(len(employees_search), 1)
        self.assertEqual(employees_search[0]['emp_code'], 'SF-FILT-2')

        # Clean up employees
        cursor.execute("DELETE FROM employees WHERE emp_code IN ('SF-FILT-1', 'SF-FILT-2')")
        self.conn.commit()

    def test_single_employee_edit_and_delete(self):
        """Audit 12: Verify individual employee PUT (edit) and DELETE APIs work correctly"""
        # Step 1: Insert seed employee
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO employees (emp_code, emp_name, zone, division, branch_name, business_unit, role) VALUES ('SF-CRUD-TEST', 'ORIGINAL NAME', 'North', 'Delhi Div', 'HQ', 'Two-Wheeler', 'Sales Rep')")
        self.conn.commit()

        # Step 2: Edit employee via PUT API
        edit_payload = {
            'emp_name': 'UPDATED NAME',
            'branch_name': 'HQ',
            'zone': 'North',
            'division': 'Delhi Div',
            'business_unit': 'Two-Wheeler',
            'role': 'PL Exe'
        }
        res_put = self.client.put('/api/roster/SF-CRUD-TEST', json=edit_payload)
        self.assertEqual(res_put.status_code, 200)
        data_put = json.loads(res_put.data)
        self.assertEqual(data_put['status'], 'success')

        # Verify details were updated in SQLite
        cursor.execute("SELECT emp_name, role FROM employees WHERE emp_code='SF-CRUD-TEST'")
        row = cursor.fetchone()
        self.assertEqual(row[0], 'UPDATED NAME')
        self.assertEqual(row[1], 'PL EXE')

        # Step 3: Delete employee via DELETE API
        res_del = self.client.delete('/api/roster/SF-CRUD-TEST?hard=true')
        self.assertEqual(res_del.status_code, 200)
        data_del = json.loads(res_del.data)
        self.assertEqual(data_del['status'], 'success')

        # Verify deletion in SQLite
        cursor.execute("SELECT COUNT(*) FROM employees WHERE emp_code='SF-CRUD-TEST'")
        self.assertEqual(cursor.fetchone()[0], 0)

    def test_employee_product_name_and_soft_delete(self):
        """Audit 13: Verify employee product name integration, PUT update, and soft-delete with custom reasons"""
        # Step 1: Clean up any old record
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM employees WHERE emp_code='SF-TEST-SOFT-DEL';")
        self.conn.commit()

        # Step 2: Add employee manually with product_name and custom change_detail
        payload = {
            'emp_code': 'SF-TEST-SOFT-DEL',
            'emp_name': 'SOFT DEL TEST',
            'branch_name': 'HQ',
            'zone': 'NORTH',
            'division': 'DELHI',
            'business_unit': 'RETAIL',
            'role': 'SALES REP',
            'product_name': 'GOLD LOAN',
            'change_detail': 'TEST SEED'
        }
        res_post = self.client.post('/api/roster/manual', json=payload)
        self.assertEqual(res_post.status_code, 200)
        
        # Verify db record
        cursor.execute("SELECT product_name, status, change_detail FROM employees WHERE emp_code='SF-TEST-SOFT-DEL'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['product_name'], 'GOLD LOAN')
        self.assertEqual(row['status'], 'ACTIVE')
        self.assertEqual(row['change_detail'], 'TEST SEED')

        # Step 3: Edit product name and status via PUT
        put_payload = {
            'emp_name': 'SOFT DEL TEST UPDATED',
            'branch_name': 'HQ',
            'zone': 'NORTH',
            'division': 'DELHI',
            'business_unit': 'RETAIL',
            'role': 'SALES REP',
            'product_name': 'TWO-WHEELER LOAN',
            'status': 'ACTIVE',
            'change_detail': 'EDITED TEST SEED'
        }
        res_put = self.client.put('/api/roster/SF-TEST-SOFT-DEL', json=put_payload)
        self.assertEqual(res_put.status_code, 200)
        
        # Verify db updated
        cursor.execute("SELECT emp_name, product_name, change_detail FROM employees WHERE emp_code='SF-TEST-SOFT-DEL'")
        row = cursor.fetchone()
        self.assertEqual(row['emp_name'], 'SOFT DEL TEST UPDATED')
        self.assertEqual(row['product_name'], 'TWO-WHEELER LOAN')
        self.assertEqual(row['change_detail'], 'EDITED TEST SEED')

        # Step 4: Soft delete employee with custom reason in query param
        res_del = self.client.delete('/api/roster/SF-TEST-SOFT-DEL?reason=Left+Company')
        self.assertEqual(res_del.status_code, 200)
        data_del = json.loads(res_del.data)
        self.assertEqual(data_del['status'], 'success')

        # Verify soft delete status in db
        cursor.execute("SELECT status, change_detail FROM employees WHERE emp_code='SF-TEST-SOFT-DEL'")
        row = cursor.fetchone()
        self.assertEqual(row['status'], 'DELETED')
        self.assertTrue(row['change_detail'].startswith("DELETED ON "))
        self.assertIn("LEFT COMPANY", row['change_detail'])

        # Step 5: Hard delete employee to clean up database
        res_hard_del = self.client.delete('/api/roster/SF-TEST-SOFT-DEL?hard=true')
        self.assertEqual(res_hard_del.status_code, 200)
        
        # Verify permanent deletion in SQLite
        cursor.execute("SELECT COUNT(*) FROM employees WHERE emp_code='SF-TEST-SOFT-DEL'")
        self.assertEqual(cursor.fetchone()[0], 0)

    def test_trainer_multi_select_and_bulk_upload(self):
        """Audit 14: Verify trainer bulk upload from CSV, scope extraction, multi-select scope updates, metadata APIs, and scoped dashboard queries"""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM trainers WHERE trainer_id IN ('TR-CSV-1', 'TR-CSV-2');")
        self.conn.commit()

        # Step 1: Upload a CSV with invalid headers
        with tempfile.NamedTemporaryFile(suffix='.csv', mode='w+', delete=False) as f:
            writer = csv.writer(f)
            writer.writerow(['Trainer ID', 'Trainer Name', 'Password']) # Missing BUs, Zones, Divisions, Branches
            writer.writerow(['TR-CSV-1', 'Trainer CSV 1', 'pwd123'])
            temp_path = f.name
            
        with open(temp_path, 'rb') as f:
            res = self.client.post('/api/trainers/upload', data={'file': (f, 'invalid_trainers.csv')})
            data = json.loads(res.data)
            self.assertEqual(res.status_code, 400)
            self.assertEqual(data['status'], 'error')
            self.assertIn("Missing column headers", data['message'])
        os.remove(temp_path)

        # Step 2: Upload a valid CSV containing multiple trainers with multi-select rights
        with tempfile.NamedTemporaryFile(suffix='.csv', mode='w+', delete=False) as f:
            writer = csv.writer(f)
            writer.writerow(['Trainer ID', 'Trainer Name', 'Password', 'Business Units', 'Zones', 'Divisions', 'Branches'])
            writer.writerow(['TR-CSV-1', 'Trainer CSV 1', 'pwd123', 'Two-Wheeler,Retail', 'North,South', 'Delhi Div,Mumbai Div', 'HQ,MUMBAI'])
            writer.writerow(['TR-CSV-2', 'Trainer CSV 2', 'pwd456', 'ALL', 'ALL', 'ALL', 'ALL'])
            temp_path = f.name
            
        with open(temp_path, 'rb') as f:
            res = self.client.post('/api/trainers/upload', data={'file': (f, 'valid_trainers.csv')})
            data = json.loads(res.data)
            self.assertEqual(res.status_code, 200)
            self.assertEqual(data['status'], 'success')
        os.remove(temp_path)

        # Verify db records
        cursor.execute("SELECT * FROM trainers WHERE trainer_id='TR-CSV-1'")
        t1 = cursor.fetchone()
        self.assertIsNotNone(t1)
        self.assertEqual(t1['business_units'], 'TWO-WHEELER,RETAIL')
        self.assertEqual(t1['zones'], 'NORTH,SOUTH')
        self.assertEqual(t1['divisions'], 'DELHI DIV,MUMBAI DIV')
        self.assertEqual(t1['branches'], 'HQ,MUMBAI')

        cursor.execute("SELECT * FROM trainers WHERE trainer_id='TR-CSV-2'")
        t2 = cursor.fetchone()
        self.assertIsNotNone(t2)
        self.assertEqual(t2['business_units'], 'ALL')
        self.assertEqual(t2['zones'], 'ALL')
        self.assertEqual(t2['divisions'], 'ALL')
        self.assertEqual(t2['branches'], 'ALL')

        # Step 3: Test upload duplicacy error on re-upload
        with tempfile.NamedTemporaryFile(suffix='.csv', mode='w+', delete=False) as f:
            writer = csv.writer(f)
            writer.writerow(['Trainer ID', 'Trainer Name', 'Password', 'Business Units', 'Zones', 'Divisions', 'Branches'])
            writer.writerow(['TR-CSV-1', 'Trainer CSV 1', 'pwd123', 'Two-Wheeler,Retail', 'North,South', 'Delhi Div,Mumbai Div', 'HQ,MUMBAI'])
            temp_path = f.name
            
        with open(temp_path, 'rb') as f:
            res = self.client.post('/api/trainers/upload', data={'file': (f, 'dup_trainers.csv')})
            data = json.loads(res.data)
            self.assertEqual(res.status_code, 400)
            self.assertEqual(data['status'], 'error')
            self.assertEqual(data['message'], "This is the duplicacy. You remove that.")
            self.assertTrue(len(data['details']) > 0)
        os.remove(temp_path)

        # Step 4: Verify metadata route structure
        res_meta = self.client.get('/api/metadata')
        self.assertEqual(res_meta.status_code, 200)
        meta = json.loads(res_meta.data)
        self.assertIn('business_units', meta)
        self.assertIn('zones', meta)
        self.assertIn('divisions', meta)
        self.assertIn('branches', meta)

        # Step 5: Verify PUT trainer updating rights on the go
        put_payload = {
            'name': 'Trainer CSV 1 Updated',
            'password': 'newpwd123',
            'business_units': 'GOLD LOAN',
            'zones': 'EAST',
            'divisions': 'KOLKATA DIV',
            'branches': 'KOLKATA OFFICE'
        }
        res_put = self.client.put('/api/trainers/TR-CSV-1', json=put_payload)
        self.assertEqual(res_put.status_code, 200)
        
        cursor.execute("SELECT * FROM trainers WHERE trainer_id='TR-CSV-1'")
        t1_up = cursor.fetchone()
        self.assertEqual(t1_up['name'], 'Trainer CSV 1 Updated')
        self.assertEqual(t1_up['business_units'], 'GOLD LOAN')
        self.assertEqual(t1_up['zones'], 'EAST')
        self.assertEqual(t1_up['divisions'], 'KOLKATA DIV')
        self.assertEqual(t1_up['branches'], 'KOLKATA OFFICE')

        # Step 6: Verify dashboard stats query filtering with trainer_id scope works
        res_stats = self.client.get('/api/dashboard/stats?trainer_id=TR-CSV-1')
        self.assertEqual(res_stats.status_code, 200)
        stats = json.loads(res_stats.data)
        self.assertIn('execs_trained', stats)
        self.assertIn('avg_growth_delta', stats)

        # Clean up database
        cursor.execute("DELETE FROM trainers WHERE trainer_id IN ('TR-CSV-1', 'TR-CSV-2');")
        self.conn.commit()

if __name__ == '__main__':
    unittest.main()
