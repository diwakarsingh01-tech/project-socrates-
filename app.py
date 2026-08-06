import html
import re

def sanitize_llm_text(text):
    if not isinstance(text, str):
        return text
    # Unescape HTML entities
    text = html.unescape(text)
    # Remove raw unicode escapes and latex noise
    text = re.sub(r'\\u[0-9a-fA-F]{4}', '', text)
    text = re.sub(r'[\\$](begin|end|frac|text|sqrt|alpha|beta)\\{[^\\}]*\\}', '', text)
    # Clean control characters
    text = re.sub(r'[\r\t\x00-\x08\x0b\x0c\x0e-\x1f]', ' ', text)
    # Clean double spaces
    text = re.sub(r' +', ' ', text).strip()
    return text

from flask import Flask, request, jsonify, render_template, session, Response
from flask_socketio import SocketIO, emit, join_room, leave_room
import sqlite3
import os
import datetime
from werkzeug.utils import secure_filename
import csv
import io
import re
import urllib.request
import json

app = Flask(__name__)
app.config['SECRET_KEY'] = 'socrates-secret-key-123'
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Initialize SocketIO with threading async_mode for Python 3.12+ compatibility
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

DB_FILE = "socrates.db"

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    def _table_exists(name):
        return cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
    
    # Employees (Roster)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS employees (
        emp_code TEXT PRIMARY KEY,
        emp_name TEXT,
        branch_name TEXT,
        zone TEXT,
        division TEXT,
        business_unit TEXT,
        role TEXT
    )''')
    
    # Run migration to add 'role' column if db was created in older version
    cursor.execute("PRAGMA table_info(employees)")
    cols = [row[1] for row in cursor.fetchall()]
    if 'role' not in cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN role TEXT")
    if 'product_name' not in cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN product_name TEXT")
    if 'status' not in cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN status TEXT DEFAULT 'ACTIVE'")
    if 'change_detail' not in cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN change_detail TEXT")
    if 'extra_data' not in cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN extra_data TEXT")
    
    # Trainers
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS trainers (
        trainer_id TEXT PRIMARY KEY,
        name TEXT,
        zone TEXT,
        password TEXT,
        status TEXT DEFAULT 'Active',
        role TEXT DEFAULT 'Trainer',
        last_login TEXT
    )''')
    
    # Migration: trainer access scope columns (zone/division/branch/BU visibility)
    cursor.execute("PRAGMA table_info(trainers)")
    tr_cols = [row[1] for row in cursor.fetchall()]
    if 'business_units' not in tr_cols:
        cursor.execute("ALTER TABLE trainers ADD COLUMN business_units TEXT DEFAULT 'ALL'")
    if 'divisions' not in tr_cols:
        cursor.execute("ALTER TABLE trainers ADD COLUMN divisions TEXT DEFAULT 'ALL'")
    if 'branches' not in tr_cols:
        cursor.execute("ALTER TABLE trainers ADD COLUMN branches TEXT DEFAULT 'ALL'")
    
    # Add/Ensure default Super Admin account is present and active
    cursor.execute("SELECT * FROM trainers WHERE UPPER(trainer_id)='ADMIN'")
    admin_user = cursor.fetchone()
    if not admin_user:
        cursor.execute("INSERT INTO trainers (trainer_id, name, zone, password, role, status) VALUES ('ADMIN', 'Super Admin', 'All', 'admin123', 'SuperAdmin', 'Active')")
    else:
        cursor.execute("UPDATE trainers SET password='admin123', status='Active', role='SuperAdmin' WHERE UPPER(trainer_id)='ADMIN'")
    
    # Modules
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS modules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        questions_count INTEGER,
        created_at TEXT,
        status TEXT DEFAULT 'Pending Audit',
        created_by TEXT DEFAULT 'ADMIN'
    )''')
    
    # Run migration to add status and created_by columns in modules if db was created in older version
    cursor.execute("PRAGMA table_info(modules)")
    mod_cols = [row[1] for row in cursor.fetchall()]
    if 'status' not in mod_cols:
        cursor.execute("ALTER TABLE modules ADD COLUMN status TEXT DEFAULT 'Pending Audit'")
    if 'created_by' not in mod_cols:
        cursor.execute("ALTER TABLE modules ADD COLUMN created_by TEXT DEFAULT 'ADMIN'")
    if 'difficulty' not in mod_cols:
        cursor.execute("ALTER TABLE modules ADD COLUMN difficulty TEXT DEFAULT 'Medium'")
    if 'audited_by' not in mod_cols:
        cursor.execute("ALTER TABLE modules ADD COLUMN audited_by TEXT DEFAULT 'Super Admin'")
    if 'source_text' not in mod_cols:
        cursor.execute("ALTER TABLE modules ADD COLUMN source_text TEXT")
    if 'time_limit_minutes' not in mod_cols:
        cursor.execute("ALTER TABLE modules ADD COLUMN time_limit_minutes INTEGER DEFAULT 15")
    if 'pass_percentage' not in mod_cols:
        cursor.execute("ALTER TABLE modules ADD COLUMN pass_percentage INTEGER DEFAULT 70")
    if 'enable_anti_cheat' not in mod_cols:
        cursor.execute("ALTER TABLE modules ADD COLUMN enable_anti_cheat INTEGER DEFAULT 1")
    if 'shuffle_questions' not in mod_cols:
        cursor.execute("ALTER TABLE modules ADD COLUMN shuffle_questions INTEGER DEFAULT 1")
    if 'shuffle_options' not in mod_cols:
        cursor.execute("ALTER TABLE modules ADD COLUMN shuffle_options INTEGER DEFAULT 1")

    # Migration for questions table (fresh installs create the full schema below)
    if _table_exists('questions'):
        cursor.execute("PRAGMA table_info(questions)")
        q_cols = [row[1] for row in cursor.fetchall()]
        if 'question_type' not in q_cols:
            cursor.execute("ALTER TABLE questions ADD COLUMN question_type TEXT DEFAULT 'mcq_single'")
        if 'points_weight' not in q_cols:
            cursor.execute("ALTER TABLE questions ADD COLUMN points_weight REAL DEFAULT 1.0")
        if 'negative_points' not in q_cols:
            cursor.execute("ALTER TABLE questions ADD COLUMN negative_points REAL DEFAULT 0.0")
        if 'media_url' not in q_cols:
            cursor.execute("ALTER TABLE questions ADD COLUMN media_url TEXT")
        if 'matching_pairs' not in q_cols:
            cursor.execute("ALTER TABLE questions ADD COLUMN matching_pairs TEXT")

    # Migration for assessment_results table (append-only training history).
    # Legacy schema keyed on (emp_code, module_id, assignment_day) OVERWRITES a trainee's
    # history whenever they attend the same module again (e.g. January vs April training).
    # Rebuild as append-only: autoincrement PK + UNIQUE key that includes session_id (the
    # training occurrence), preserving every existing row with a synthesized 'LEGACY' session.
    if _table_exists('assessment_results'):
        cursor.execute("PRAGMA table_info(assessment_results)")
        res_cols = [row[1] for row in cursor.fetchall()]
        if 'id' not in res_cols:
            legacy_cols = [c for c in ('emp_code', 'module_id', 'assignment_day', 'pre_test_score',
                                       'post_test_score', 'completed_at', 'tab_switch_count',
                                       'time_taken_seconds', 'passed_status', 'certificate_id') if c in res_cols]
            cursor.execute("ALTER TABLE assessment_results RENAME TO assessment_results_legacy")
            cursor.execute('''
            CREATE TABLE assessment_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                emp_code TEXT,
                module_id INTEGER,
                assignment_day TEXT,
                session_id TEXT,
                training_date TEXT,
                trainer_id TEXT,
                zone TEXT,
                division TEXT,
                business_unit TEXT,
                branch_name TEXT,
                pre_test_score REAL,
                post_test_score REAL,
                completed_at TEXT,
                tab_switch_count INTEGER DEFAULT 0,
                time_taken_seconds INTEGER DEFAULT 0,
                passed_status INTEGER DEFAULT 1,
                certificate_id TEXT,
                UNIQUE(emp_code, module_id, session_id, assignment_day),
                FOREIGN KEY(emp_code) REFERENCES employees(emp_code),
                FOREIGN KEY(module_id) REFERENCES modules(id)
            )''')
            legacy_sel = ', '.join(legacy_cols)
            legacy_cols_wc = ", ".join("a." + c for c in legacy_cols)
            cursor.execute(f"""
                INSERT INTO assessment_results ({legacy_sel}, session_id, training_date, zone, division, business_unit, branch_name)
                SELECT {legacy_cols_wc}, 'LEGACY', substr(a.completed_at, 1, 10), e.zone, e.division, e.business_unit, e.branch_name
                FROM assessment_results_legacy a
                LEFT JOIN employees e ON a.emp_code = e.emp_code
            """)
            cursor.execute("DROP TABLE assessment_results_legacy")
            res_cols = [r[1] for r in cursor.execute("PRAGMA table_info(assessment_results)").fetchall()]
        for col, ddl in (('session_id', 'TEXT'), ('training_date', 'TEXT'), ('trainer_id', 'TEXT'),
                         ('zone', 'TEXT'), ('division', 'TEXT'), ('business_unit', 'TEXT'), ('branch_name', 'TEXT')):
            if col not in res_cols:
                cursor.execute(f"ALTER TABLE assessment_results ADD COLUMN {col} {ddl}")
        if 'tab_switch_count' not in res_cols:
            cursor.execute("ALTER TABLE assessment_results ADD COLUMN tab_switch_count INTEGER DEFAULT 0")
        if 'time_taken_seconds' not in res_cols:
            cursor.execute("ALTER TABLE assessment_results ADD COLUMN time_taken_seconds INTEGER DEFAULT 0")
        if 'passed_status' not in res_cols:
            cursor.execute("ALTER TABLE assessment_results ADD COLUMN passed_status INTEGER DEFAULT 1")
        if 'certificate_id' not in res_cols:
            cursor.execute("ALTER TABLE assessment_results ADD COLUMN certificate_id TEXT")
        
    # Questions (Maker-Checker details)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        module_id INTEGER,
        question_text TEXT,
        option_a TEXT,
        option_b TEXT,
        option_c TEXT,
        option_d TEXT,
        correct_index INTEGER,
        approved INTEGER DEFAULT 0,
        FOREIGN KEY(module_id) REFERENCES modules(id) ON DELETE CASCADE
    )''')
    
    # Training Sessions (For Tracking Productivity)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS training_sessions (
        session_id TEXT PRIMARY KEY,
        date TEXT,
        trainer_id TEXT,
        module_id INTEGER,
        branch_name TEXT,
        FOREIGN KEY(trainer_id) REFERENCES trainers(trainer_id)
    )''')
    
    # Assessment Results (For learning curves) — append-only training history.
    # A new training occurrence (session_id) always INSERTs a new row; the UNIQUE key
    # (emp_code, module_id, session_id, assignment_day) only de-dupes retries of the
    # SAME session, so January vs April trainings are both preserved.
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS assessment_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT,
        module_id INTEGER,
        assignment_day TEXT,
        session_id TEXT,
        training_date TEXT,
        trainer_id TEXT,
        zone TEXT,
        division TEXT,
        business_unit TEXT,
        branch_name TEXT,
        pre_test_score REAL,
        post_test_score REAL,
        completed_at TEXT,
        tab_switch_count INTEGER DEFAULT 0,
        time_taken_seconds INTEGER DEFAULT 0,
        passed_status INTEGER DEFAULT 1,
        certificate_id TEXT,
        UNIQUE(emp_code, module_id, session_id, assignment_day),
        FOREIGN KEY(emp_code) REFERENCES employees(emp_code),
        FOREIGN KEY(module_id) REFERENCES modules(id)
    )''')
    
    # Session Feedback (Post-Test survey)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS session_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT,
        session_id TEXT,
        rating INTEGER DEFAULT 5,
        understanding TEXT,
        manpower_saved TEXT,
        comments TEXT,
        created_at TEXT
    )''')
    
    # AI Refresher Campaigns (trainees flagged below 60% for mandatory retraining)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS refresher_campaigns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT,
        module_id INTEGER,
        campaign_date TEXT,
        status TEXT DEFAULT 'PENDING'
    )''')
    
    # Field Visits / Travel Hub (Planner, GPS check-in, Manager sign-off)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS visits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trainer_id TEXT,
        trainer_name TEXT,
        zone TEXT,
        division TEXT,
        branch_name TEXT,
        branch_code TEXT,
        business_unit TEXT,
        planned_date TEXT,
        end_date TEXT,
        meeting_agenda TEXT,
        meeting_with TEXT,
        purpose TEXT,
        key_contacts TEXT,
        details TEXT,
        status TEXT DEFAULT 'PLANNED',
        checkin_time TEXT,
        geo_lat REAL,
        geo_lng REAL,
        co_presence_count INTEGER DEFAULT 0,
        mom_notes TEXT,
        travel_mode TEXT,
        travel_from TEXT,
        travel_to TEXT,
        overnight_stay TEXT,
        created_at TEXT,
        updated_at TEXT
    )''')
    
    conn.commit()
    conn.close()

init_db()

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def _session_user():
    """Return the logged-in admin/trainer session user dict, or None."""
    user = session.get('user')
    return user if user else None


def _trainer_scope(trainer_id):
    """Load a trainer's access scope from the trainers table.
    Returns dict with keys: role, zones, divisions, branches, business_units.
    Each list is None when the scope is unrestricted ('ALL')."""
    if not trainer_id:
        return None
    conn = get_db_connection()
    row = conn.execute(
        "SELECT zone, business_units, divisions, branches, role FROM trainers WHERE UPPER(trainer_id)=UPPER(?)",
        (trainer_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None

    def parse(v):
        if not v:
            return None
        v = str(v).strip()
        if not v or v.upper() == 'ALL':
            return None
        return [x.strip().upper() for x in re.split(r'[|,;]', v) if x.strip()]

    return {
        'role': str(row['role'] or 'Trainer'),
        'zones': parse(row['zone']),
        'divisions': parse(row['divisions']),
        'branches': parse(row['branches']),
        'business_units': parse(row['business_units']),
    }


def _is_global_role(role):
    """SuperAdmin / Leader see the full dataset; Trainers are scope-restricted."""
    r = (role or '').lower().replace(' ', '')
    return r in ('superadmin', 'leader')


def _apply_trainer_scope(query_parts, params, scope):
    """Append scope WHERE clauses for a trainer. Returns (query_string, params)."""
    if scope:
        if scope.get('zones'):
            q = " AND UPPER(TRIM(zone)) IN ({})".format(','.join('?' * len(scope['zones'])))
            query_parts.append(q)
            params.extend(scope['zones'])
        if scope.get('divisions'):
            q = " AND UPPER(TRIM(division)) IN ({})".format(','.join('?' * len(scope['divisions'])))
            query_parts.append(q)
            params.extend(scope['divisions'])
        if scope.get('branches'):
            q = " AND UPPER(TRIM(branch_name)) IN ({})".format(','.join('?' * len(scope['branches'])))
            query_parts.append(q)
            params.extend(scope['branches'])
        if scope.get('business_units'):
            q = " AND UPPER(TRIM(business_unit)) IN ({})".format(','.join('?' * len(scope['business_units'])))
            query_parts.append(q)
            params.extend(scope['business_units'])
    return "".join(query_parts), params

# --- HTML TEMPLATE ROUTES ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin')
def admin():
    return render_template('admin.html')

# --- API ROUTES ---

# 1. AUTHENTICATION & DIAGNOSTICS
@app.route('/api/admin/me', methods=['GET'])
def admin_me():
    if 'user' in session:
        return jsonify({
            "status": "success",
            "user": session['user']
        })
    return jsonify({"status": "error", "message": "No active session"}), 401

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    data = request.json or {}
    raw_id = str(data.get('trainer_id', '')).strip()
    password = str(data.get('password', '')).strip()
    
    if not raw_id or not password:
        return jsonify({"status": "error", "message": "Please enter both Trainer ID and Password."}), 400
        
    conn = get_db_connection()
    user = conn.execute(
        "SELECT * FROM trainers WHERE (UPPER(trainer_id)=UPPER(?) OR UPPER(name)=UPPER(?)) AND password=?",
        (raw_id, raw_id, password)
    ).fetchone()
    
    if user:
        if user['status'] and str(user['status']).lower() == 'inactive':
            conn.close()
            return jsonify({"status": "error", "message": "Account is inactive. Access revoked by Super Admin."}), 403
            
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        conn.execute("UPDATE trainers SET last_login=? WHERE trainer_id=?", (now, user['trainer_id']))
        conn.commit()
        
        user_data = {
            "trainer_id": user['trainer_id'],
            "name": user['name'],
            "role": user['role']
        }
        session['user'] = user_data
        conn.close()
        
        return jsonify({
            "status": "success",
            "role": user['role'],
            "name": user['name'],
            "user": {
                "trainer_id": user['trainer_id'],
                "name": user['name'],
                "role": user['role']
            }
        })
        
    conn.close()
    return jsonify({"status": "error", "message": "Invalid Credentials or Access Revoked"}), 401

@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('user', None)
    return jsonify({"status": "success", "message": "Logged out successfully"})

@app.route('/api/admin/diagnostics', methods=['GET'])
def admin_diagnostics():
    return jsonify({
        "status": "success",
        "database_type": "SQLite",
        "is_ephemeral": False,
        "connection_status": "Connected",
        "database_url": DB_FILE,
        "connection_error": None
    })

@app.route('/api/gdrive/status', methods=['GET'])
def gdrive_status():
    gd_folder = os.environ.get('GD_FOLDER_ID', '')
    return jsonify({
        "status": "success",
        "configured": bool(gd_folder),
        "folder_id": gd_folder,
        "service_account": "socrates-sync@skillful-octane-494413-a5.iam.gserviceaccount.com"
    })

# 2. TRAINER & ACCESS CONTROL MANAGEMENT
@app.route('/api/trainers', methods=['GET', 'POST'])
def handle_trainers():
    conn = get_db_connection()
    if request.method == 'GET':
        trainers = conn.execute("SELECT trainer_id as id, name, zone, status, role, last_login, password as plain_password, business_units, divisions, branches FROM trainers ORDER BY trainer_id ASC").fetchall()
        conn.close()
        return jsonify([dict(t) for t in trainers])
    
    elif request.method == 'POST':
        data = request.json or {}
        t_id = str(data.get('id', '')).upper().strip()
        name = str(data.get('name', '')).strip()
        password = str(data.get('password', 'password123')).strip()
        role = str(data.get('role', 'Trainer')).strip()
        zone = str(data.get('zone', 'ALL')).strip()
        business_units = str(data.get('business_units', 'ALL')).strip() or 'ALL'
        divisions = str(data.get('divisions', 'ALL')).strip() or 'ALL'
        branches = str(data.get('branches', 'ALL')).strip() or 'ALL'
        
        if not t_id or not name:
            conn.close()
            return jsonify({"status": "error", "message": "Trainer ID and Name are required."}), 400
            
        conn.execute(
            "INSERT INTO trainers (trainer_id, name, zone, password, role, status, business_units, divisions, branches) VALUES (?, ?, ?, ?, ?, 'Active', ?, ?, ?) ON CONFLICT(trainer_id) DO UPDATE SET name=excluded.name, password=excluded.password, role=excluded.role, status='Active', business_units=excluded.business_units, divisions=excluded.divisions, branches=excluded.branches",
            (t_id, name, zone, password, role, business_units, divisions, branches)
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": f"Trainer '{t_id}' created successfully!"})

@app.route('/api/trainers/<trainer_id>/status', methods=['PUT'])
def update_trainer_status(trainer_id):
    data = request.json or {}
    conn = get_db_connection()
    conn.execute("UPDATE trainers SET status=? WHERE UPPER(trainer_id)=?", (data.get('status', 'Active'), trainer_id.upper()))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/trainers/<trainer_id>', methods=['PUT', 'DELETE'])
def manage_single_trainer(trainer_id):
    trainer_id = trainer_id.upper().strip()
    
    if trainer_id == 'ADMIN':
        return jsonify({"status": "error", "message": "The primary Super Admin account 'ADMIN' cannot be modified or deleted."}), 403
        
    conn = get_db_connection()
    if request.method == 'DELETE':
        conn.execute("DELETE FROM trainers WHERE UPPER(trainer_id)=?", (trainer_id,))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": f"Trainer '{trainer_id}' deleted successfully."})
        
    elif request.method == 'PUT':
        data = request.json or {}
        name = str(data.get('name', '')).strip()
        password = str(data.get('password', '')).strip()
        role = str(data.get('role', 'Trainer')).strip()
        zone = str(data.get('zone', 'ALL')).strip()
        business_units = str(data.get('business_units', 'ALL')).strip() or 'ALL'
        divisions = str(data.get('divisions', 'ALL')).strip() or 'ALL'
        branches = str(data.get('branches', 'ALL')).strip() or 'ALL'
        
        conn.execute("""
            UPDATE trainers SET
                name=COALESCE(NULLIF(?, ''), name),
                password=COALESCE(NULLIF(?, ''), password),
                role=COALESCE(NULLIF(?, ''), role),
                zone=COALESCE(NULLIF(?, ''), zone),
                business_units=?,
                divisions=?,
                branches=?
            WHERE UPPER(trainer_id)=?
        """, (name, password, role, zone, business_units, divisions, branches, trainer_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": f"Trainer profile '{trainer_id}' updated successfully."})

@app.route('/api/trainers/upload', methods=['POST'])
def bulk_upload_trainers():
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file part provided."}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "No file selected."}), 400
        
    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        try:
            conn = get_db_connection()
            rows_processed = 0
            
            with open(filepath, 'r', encoding='utf-8-sig') as csvfile:
                reader = csv.reader(csvfile)
                raw_headers = [h.strip().lower() for h in next(reader)]
                
                def find_idx(keywords):
                    for idx, h in enumerate(raw_headers):
                        if any(k in h for k in keywords):
                            return idx
                    return -1
                    
                id_idx = find_idx(['trainer id', 'id', 'trainer_id', 'code'])
                name_idx = find_idx(['name', 'trainer name', 'trainer_name'])
                pwd_idx = find_idx(['password', 'pass', 'pwd'])
                role_idx = find_idx(['role', 'designation'])
                zone_idx = find_idx(['zone', 'region'])
                bu_idx = find_idx(['business unit', 'bu', 'business_unit'])
                div_idx = find_idx(['division', 'div'])
                br_idx = find_idx(['branch', 'branches'])
                
                if id_idx == -1:
                    conn.close()
                    return jsonify({"status": "error", "message": "Invalid CSV. Missing 'Trainer ID' column."}), 400
                    
                for r in reader:
                    if not r or len(r) <= id_idx:
                        continue
                    t_id = r[id_idx].strip().upper()
                    if not t_id or t_id == 'ADMIN':
                        continue
                        
                    t_name = r[name_idx].strip().upper() if name_idx != -1 and len(r) > name_idx else f"TRAINER {t_id}"
                    t_pwd = r[pwd_idx].strip() if pwd_idx != -1 and len(r) > pwd_idx else 'password123'
                    t_role = r[role_idx].strip() if role_idx != -1 and len(r) > role_idx else 'Trainer'
                    t_zone = r[zone_idx].strip() if zone_idx != -1 and len(r) > zone_idx else 'ALL'
                    t_bu = r[bu_idx].strip() if bu_idx != -1 and len(r) > bu_idx else 'ALL'
                    t_div = r[div_idx].strip() if div_idx != -1 and len(r) > div_idx else 'ALL'
                    t_br = r[br_idx].strip() if br_idx != -1 and len(r) > br_idx else 'ALL'
                    
                    conn.execute("""
                        INSERT INTO trainers (trainer_id, name, zone, password, role, status, business_units, divisions, branches)
                        VALUES (?, ?, ?, ?, ?, 'Active', ?, ?, ?)
                        ON CONFLICT(trainer_id) DO UPDATE SET
                            name=excluded.name,
                            zone=excluded.zone,
                            password=excluded.password,
                            role=excluded.role,
                            status='Active',
                            business_units=excluded.business_units,
                            divisions=excluded.divisions,
                            branches=excluded.branches
                    """, (t_id, t_name, t_zone, t_pwd, t_role, t_bu, t_div, t_br))
                    rows_processed += 1
                    
            conn.commit()
            conn.close()
            return jsonify({
                "status": "success",
                "message": f"Successfully processed {rows_processed} trainer accounts from CSV!"
            })
        except Exception as e:
            return jsonify({"status": "error", "message": f"Bulk upload failed: {str(e)}"}), 500

@app.route('/api/admin/reset-database', methods=['POST'])
def reset_database():
    data = request.json or {}
    mode = data.get('mode', 'full')
    conn = get_db_connection()
    try:
        if mode == 'demo_only':
            conn.execute("DELETE FROM employees WHERE emp_code LIKE 'SF-100%' OR emp_code LIKE 'SF-99%' OR emp_code LIKE 'UNL-%' OR emp_code LIKE 'DIV-%'")
            conn.execute("DELETE FROM trainers WHERE (trainer_id LIKE 'TR-100%' OR trainer_id LIKE 'TR-99%') AND UPPER(trainer_id) != 'ADMIN'")
            conn.execute("DELETE FROM assessment_results WHERE emp_code LIKE 'SF-100%' OR emp_code LIKE 'SF-99%'")
            msg = "Demo sample records cleared successfully!"
        else:
            conn.execute("DELETE FROM employees")
            conn.execute("DELETE FROM assessment_results")
            conn.execute("DELETE FROM modules")
            conn.execute("DELETE FROM questions")
            conn.execute("DELETE FROM trainers WHERE UPPER(trainer_id) != 'ADMIN'")
            msg = "System database reset completed successfully to pristine clean slate!"
            
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": msg})
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": f"Reset failed: {str(e)}"}), 500

# 3. ROSTER MANAGEMENT
@app.route('/api/roster', methods=['GET'])
def get_roster():
    conn = get_db_connection()
    search = request.args.get('search', '').strip() or request.args.get('q', '').strip()
    zone = request.args.get('zone', '').strip()
    division = request.args.get('division', '').strip()
    branch = request.args.get('branch', '').strip() or request.args.get('branch_name', '').strip()
    bu = request.args.get('bu', '').strip() or request.args.get('business_unit', '').strip()
    role = request.args.get('role', '').strip()
    product = request.args.get('product', '').strip() or request.args.get('product_name', '').strip()
    status = request.args.get('status', '').strip()
    
    query = "SELECT * FROM employees WHERE 1=1"
    params = []
    
    # Server-side access control: trainers only see their assigned zone/division/
    # branch/business-unit scope. SuperAdmin/Leader see everything.
    user = _session_user()
    if user and not _is_global_role(user.get('role', '')):
        scope = _trainer_scope(user.get('trainer_id'))
        if scope:
            query, params = _apply_trainer_scope([query], params, scope)
    
    if search:
        query += " AND (UPPER(emp_name) LIKE ? OR UPPER(emp_code) LIKE ? OR UPPER(branch_name) LIKE ? OR UPPER(role) LIKE ? OR UPPER(division) LIKE ? OR UPPER(zone) LIKE ?)"
        term = f"%{search.upper()}%"
        params.extend([term, term, term, term, term, term])
    if zone:
        query += " AND UPPER(TRIM(zone)) = UPPER(TRIM(?))"
        params.append(zone)
    if division:
        query += " AND UPPER(TRIM(division)) = UPPER(TRIM(?))"
        params.append(division)
    if branch:
        query += " AND UPPER(TRIM(branch_name)) = UPPER(TRIM(?))"
        params.append(branch)
    if bu:
        query += " AND UPPER(TRIM(business_unit)) = UPPER(TRIM(?))"
        params.append(bu)
    if role:
        query += " AND UPPER(TRIM(role)) = UPPER(TRIM(?))"
        params.append(role)
    if product:
        query += " AND UPPER(TRIM(product_name)) = UPPER(TRIM(?))"
        params.append(product)
    if status:
        query += " AND UPPER(TRIM(status)) = UPPER(TRIM(?))"
        params.append(status)
        
    query += " ORDER BY emp_code ASC"
    
    limit_arg = request.args.get('limit')
    if limit_arg and limit_arg.isdigit():
        query += f" LIMIT {int(limit_arg)}"
        
    emps = conn.execute(query, params).fetchall()
    conn.close()
    
    results = []
    for e in emps:
        d = dict(e)
        if d.get('extra_data'):
            try:
                import json
                d['extra'] = json.loads(d['extra_data'])
            except:
                pass
        results.append(d)
        
    return jsonify(results)


# --- DYNAMIC ROSTER FILTERS & EXPORT ---
@app.route('/api/roster/filters', methods=['GET'])
def get_roster_filters():
    conn = get_db_connection()
    try:
        zones = [r[0].strip() for r in conn.execute("SELECT DISTINCT TRIM(zone) FROM employees WHERE zone IS NOT NULL AND TRIM(zone) != '' ORDER BY zone").fetchall()]
        divisions = [r[0].strip() for r in conn.execute("SELECT DISTINCT TRIM(division) FROM employees WHERE division IS NOT NULL AND TRIM(division) != '' ORDER BY division").fetchall()]
        branches = [r[0].strip() for r in conn.execute("SELECT DISTINCT TRIM(branch_name) FROM employees WHERE branch_name IS NOT NULL AND TRIM(branch_name) != '' ORDER BY branch_name").fetchall()]
        business_units = [r[0].strip() for r in conn.execute("SELECT DISTINCT TRIM(business_unit) FROM employees WHERE business_unit IS NOT NULL AND TRIM(business_unit) != '' ORDER BY business_unit").fetchall()]
        roles = [r[0].strip() for r in conn.execute("SELECT DISTINCT TRIM(role) FROM employees WHERE role IS NOT NULL AND TRIM(role) != '' ORDER BY role").fetchall()]
        products = [r[0].strip() for r in conn.execute("SELECT DISTINCT TRIM(product_name) FROM employees WHERE product_name IS NOT NULL AND TRIM(product_name) != '' ORDER BY product_name").fetchall()]
        statuses = [r[0].strip() for r in conn.execute("SELECT DISTINCT TRIM(status) FROM employees WHERE status IS NOT NULL AND TRIM(status) != '' ORDER BY status").fetchall()]
        
        divisions_meta = [
            {"name": row[0].strip(), "zone": (row[1] or '').strip()}
            for row in conn.execute("SELECT DISTINCT TRIM(division), TRIM(zone) FROM employees WHERE division IS NOT NULL AND TRIM(division) != '' ORDER BY division").fetchall()
        ]

        branches_meta = [
            {"name": row[0].strip(), "division": (row[1] or '').strip(), "zone": (row[2] or '').strip()}
            for row in conn.execute("SELECT DISTINCT TRIM(branch_name), TRIM(division), TRIM(zone) FROM employees WHERE branch_name IS NOT NULL AND TRIM(branch_name) != '' ORDER BY branch_name").fetchall()
        ]

        conn.close()
        return jsonify({
            "status": "success",
            "zones": zones,
            "divisions": divisions,
            "divisions_meta": divisions_meta,
            "branches": branches,
            "branches_meta": branches_meta,
            "business_units": business_units,
            "roles": roles,
            "products": products,
            "statuses": statuses
        })
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/roster/export', methods=['GET'])
def export_roster():
    conn = get_db_connection()
    employees = conn.execute("SELECT * FROM employees ORDER BY emp_code ASC").fetchall()
    conn.close()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    headers = ['Employee Code', 'Employee Name', 'Branch Name', 'Zone', 'Division', 'Business Unit', 'Role', 'Product Name', 'Status', 'Change Detail']
    writer.writerow(headers)
    
    for e in employees:
        writer.writerow([
            e['emp_code'] or '',
            e['emp_name'] or '',
            e['branch_name'] or '',
            e['zone'] or '',
            e['division'] or '',
            e['business_unit'] or '',
            e['role'] or '',
            e['product_name'] or '',
            e['status'] or 'ACTIVE',
            e['change_detail'] or ''
        ])
        
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=Socrates_Roster_Export.csv"}
    )

@app.route('/api/roster/upload', methods=['POST'])
def upload_roster():
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "No selected file"}), 400
    
    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        REQUIRED_HEADERS = ['Employee Code', 'Employee Name', 'Branch Name', 'Zone', 'Division', 'Business Unit', 'Role']
        
        rows = []
        try:
            with open(filepath, 'r', encoding='utf-8-sig') as csvfile:
                reader = csv.reader(csvfile)
                raw_headers = [h.strip() for h in next(reader)]
                
                # Check for required headers flexibly
                def find_hdr_idx(req):
                    req_norm = req.lower().replace('_', ' ').replace('-', ' ').strip()
                    req_compact = re.sub(r'\s+', '', req_norm)
                    for idx, h in enumerate(raw_headers):
                        h_norm = h.lower().replace('_', ' ').replace('-', ' ').strip()
                        h_compact = re.sub(r'\s+', '', h_norm)
                        if req_norm == h_norm or h_compact == req_compact:
                            return idx
                    # Small synonym dictionary for common variants
                    SYNONYMS = {
                        'employee code': ['emp code', 'empcode', 'code', 'employee id', 'emp id', 'empid'],
                        'employee name': ['emp name', 'empname', 'name', 'employee'],
                        'branch name': ['branch', 'branchname'],
                        'business unit': ['bu', 'businessunit', 'bunit'],
                        'product name': ['product', 'productname'],
                        'zone': ['zone name'],
                        'division': ['division name', 'div'],
                        'role': ['designation', 'job title', 'position']
                    }
                    for syn in SYNONYMS.get(req_norm, []):
                        syn_compact = re.sub(r'\s+', '', syn.lower())
                        for idx, h in enumerate(raw_headers):
                            h_compact = re.sub(r'\s+', '', h.lower())
                            if h_compact == syn_compact:
                                return idx
                    return -1
                    
                missing_headers = [req for req in REQUIRED_HEADERS if find_hdr_idx(req) == -1]
                if missing_headers:
                    return jsonify({
                        "status": "error",
                        "message": f"Invalid CSV format. Missing column headers: {', '.join(missing_headers)}"
                    }), 400
                    
                hdr_indices = {req: find_hdr_idx(req) for req in REQUIRED_HEADERS}
                prod_idx = find_hdr_idx('Product Name')
                
                standard_indices = set(hdr_indices.values())
                if prod_idx != -1:
                    standard_indices.add(prod_idx)
                    
                for row_idx, r in enumerate(reader, start=2):
                    if not r or len(r) < 1:
                        continue
                        
                    emp_code = r[hdr_indices['Employee Code']].strip().upper() if hdr_indices['Employee Code'] < len(r) else ''
                    if not emp_code:
                        continue
                        
                    emp_name = r[hdr_indices['Employee Name']].strip().upper() if hdr_indices['Employee Name'] < len(r) else f"EMP {emp_code}"
                    branch = r[hdr_indices['Branch Name']].strip().upper() if hdr_indices['Branch Name'] < len(r) else 'HEAD OFFICE'
                    zone = r[hdr_indices['Zone']].strip().upper() if hdr_indices['Zone'] < len(r) else 'GENERAL'
                    division = r[hdr_indices['Division']].strip().upper() if hdr_indices['Division'] < len(r) else 'GENERAL'
                    bu = r[hdr_indices['Business Unit']].strip().upper() if hdr_indices['Business Unit'] < len(r) else 'TWO-WHEELER'
                    role = r[hdr_indices['Role']].strip().upper() if hdr_indices['Role'] < len(r) else 'PL EXE'
                    prod = r[prod_idx].strip().upper() if prod_idx != -1 and prod_idx < len(r) else ''
                    
                    # Dynamically collect all custom non-standard extra columns into JSON
                    extra_data = {}
                    for idx, h in enumerate(raw_headers):
                        if idx not in standard_indices and idx < len(r):
                            val = r[idx].strip()
                            if val:
                                extra_data[h] = val
                                
                    row_data = {
                        'Employee Code': emp_code,
                        'Employee Name': emp_name,
                        'Branch Name': branch,
                        'Zone': zone,
                        'Division': division,
                        'Business Unit': bu,
                        'Role': role,
                        'Product Name': prod,
                        'Extra Data': json.dumps(extra_data) if extra_data else ''
                    }
                    rows.append((row_idx, row_data))
        except Exception as e:
            return jsonify({"status": "error", "message": f"Failed to parse CSV: {str(e)}"}), 400

        # Check for duplication within CSV (in-file duplicate codes are hard errors)
        seen_codes_in_csv = {}
        duplicates = []
        
        conn = get_db_connection()
        for idx, row in rows:
            code = row['Employee Code']
            if not code:
                continue
            
            if code in seen_codes_in_csv:
                duplicates.append(f"Row {idx}: Employee Code '{code}' is duplicated in the file.")
            else:
                seen_codes_in_csv[code] = idx
        
        if duplicates:
            conn.close()
            return jsonify({
                "status": "error", 
                "message": "Duplicate employee codes found inside the uploaded file. Please remove them and re-upload.",
                "details": duplicates
            }), 400
            
        # Upsert: insert new employees, update existing ones (matched by emp_code)
        added_count = 0
        updated_count = 0
        for _, row in rows:
            code = row['Employee Code']
            try:
                db_match = conn.execute("SELECT emp_code FROM employees WHERE emp_code=?", (code,)).fetchone()
                if db_match:
                    conn.execute(
                        "UPDATE employees SET emp_name=?, branch_name=?, zone=?, division=?, business_unit=?, role=?, product_name=?, extra_data=?, status='ACTIVE' WHERE emp_code=?",
                        (row['Employee Name'], row['Branch Name'], row['Zone'], row['Division'], row['Business Unit'], row['Role'], row['Product Name'], row['Extra Data'], code)
                    )
                    updated_count += 1
                else:
                    conn.execute(
                        "INSERT INTO employees (emp_code, emp_name, branch_name, zone, division, business_unit, role, product_name, extra_data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (row['Employee Code'], row['Employee Name'], row['Branch Name'], row['Zone'], row['Division'], row['Business Unit'], row['Role'], row['Product Name'], row['Extra Data'])
                    )
                    added_count += 1
            except Exception as e:
                conn.rollback()
                conn.close()
                return jsonify({"status": "error", "message": f"Database insertion failed: {str(e)}"}), 500
                
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": f"Roster uploaded and processed successfully! Added {added_count} new, updated {updated_count} existing."})

@app.route('/api/roster/manual', methods=['POST'])
def add_roster_manual():
    data = request.json
    emp_code = data.get('emp_code', '').strip().upper()
    emp_name = data.get('emp_name', '').strip().upper()
    branch_name = data.get('branch_name', '').strip().upper()
    zone = data.get('zone', '').strip().upper()
    division = data.get('division', '').strip().upper()
    business_unit = data.get('business_unit', '').strip().upper()
    role = data.get('role', '').strip().upper()
    
    if not emp_code or not emp_name:
        return jsonify({"status": "error", "message": "Employee Code and Name are required."}), 400
        
    conn = get_db_connection()
    existing = conn.execute("SELECT * FROM employees WHERE emp_code = ?", (emp_code,)).fetchone()
    if existing:
        conn.close()
        return jsonify({
            "status": "error", 
            "message": "This is the duplicacy. You remove that.",
            "details": [f"Employee Code '{emp_code}' already exists in the database as '{existing['emp_name']}'."]
        }), 400
        
    conn.execute("INSERT INTO employees (emp_code, emp_name, branch_name, zone, division, business_unit, role) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (emp_code, emp_name, branch_name, zone, division, business_unit, role))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": f"Employee '{emp_name}' added manually successfully!"})

@app.route('/api/roster/search', methods=['GET'])
def search_roster():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])
        
    conn = get_db_connection()
    # Case-insensitive query matches emp_name or emp_code
    results = conn.execute(
        "SELECT * FROM employees WHERE emp_name LIKE ? OR emp_code LIKE ? LIMIT 10",
        (f"%{query}%", f"%{query}%")
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in results])



# --- HISTORICAL ASSESSMENT CSV IMPORT ROUTE ---
@app.route('/api/assessments/upload-historical', methods=['POST'])
@app.route('/api/assessments/upload', methods=['POST'])
def upload_historical_assessments():
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file part provided."}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "No file selected."}), 400
        
    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        try:
            conn = get_db_connection()
            rows_processed = 0
            
            with open(filepath, 'r', encoding='utf-8-sig') as csvfile:
                reader = csv.reader(csvfile)
                headers = [h.strip() for h in next(reader)]
                
                if 'Employee Code' not in headers:
                    conn.close()
                    return jsonify({
                        "status": "error",
                        "message": "Invalid CSV format. Missing required column 'Employee Code'."
                    }), 400
                    
                hdr_idx = {h: headers.index(h) for h in headers}
                
                for row_idx, r in enumerate(reader, start=2):
                    if not r or len(r) < len(headers):
                        continue
                    emp_code = r[hdr_idx['Employee Code']].strip().upper()
                    if not emp_code:
                        continue
                        
                    emp_name = r[hdr_idx['Employee Name']].strip().upper() if 'Employee Name' in hdr_idx else f"EMP {emp_code}"
                    branch = r[hdr_idx['Branch Name']].strip().upper() if 'Branch Name' in hdr_idx else 'HEAD OFFICE'
                    zone = r[hdr_idx['Zone']].strip().upper() if 'Zone' in hdr_idx else 'GENERAL'
                    division = r[hdr_idx['Division']].strip().upper() if 'Division' in hdr_idx else 'GENERAL'
                    bu = r[hdr_idx['Business Unit']].strip().upper() if 'Business Unit' in hdr_idx else 'TWO-WHEELER'
                    role = r[hdr_idx['Role']].strip().upper() if 'Role' in hdr_idx else 'PL EXE'
                    
                    conn.execute("""
                        INSERT INTO employees (emp_code, emp_name, branch_name, zone, division, business_unit, role)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(emp_code) DO UPDATE SET
                            emp_name=excluded.emp_name,
                            branch_name=excluded.branch_name,
                            zone=excluded.zone,
                            division=excluded.division,
                            business_unit=excluded.business_unit,
                            role=excluded.role
                    """, (emp_code, emp_name, branch, zone, division, bu, role))
                    
                    session_date = r[hdr_idx['Date of Visit']].strip() if 'Date of Visit' in hdr_idx else datetime.datetime.now().strftime("%Y-%m-%d")
                    module_id = int(r[hdr_idx['Module ID']].strip()) if 'Module ID' in hdr_idx and r[hdr_idx['Module ID']].strip().isdigit() else 1
                    
                    day_mappings = [
                        ('ZERO DAY', 'Zero Day Pre-Test', 'Zero Day Post-Test'),
                        ('SIX DAYS', 'Six Days Pre-Test', 'Six Days Post-Test'),
                        ('TWENTY DAYS', 'Twenty Days Pre-Test', 'Twenty Days Post-Test')
                    ]
                    
                    for day_key, pre_col, post_col in day_mappings:
                        pre_str = r[hdr_idx[pre_col]].strip() if pre_col in hdr_idx else None
                        post_str = r[hdr_idx[post_col]].strip() if post_col in hdr_idx else None
                        
                        pre_val = None
                        post_val = None
                        
                        if pre_str and pre_str.upper() != 'N/A':
                            try: pre_val = float(pre_str)
                            except: pass
                        if post_str and post_str.upper() != 'N/A':
                            try: post_val = float(post_str)
                            except: pass
                            
                        if pre_val is not None or post_val is not None:
                            p_score = pre_val if pre_val is not None else 0.0
                            post_score = post_val if post_val is not None else 0.0

                            # History-safe key: one session per training DATE, so the same
                            # trainee/module attending again on a later date (Jan vs Apr)
                            # creates a NEW row instead of overwriting the earlier visit.
                            session_key = f"CSV-{session_date}"
                            conn.execute("""
                                INSERT INTO assessment_results (emp_code, module_id, assignment_day, session_id, training_date, zone, division, business_unit, branch_name, pre_test_score, post_test_score, completed_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(emp_code, module_id, session_id, assignment_day) DO UPDATE SET
                                    pre_test_score=excluded.pre_test_score,
                                    post_test_score=excluded.post_test_score,
                                    completed_at=excluded.completed_at
                            """, (emp_code, module_id, day_key, session_key, session_date, zone, division, bu, branch, p_score, post_score, session_date))
                            
                    rows_processed += 1
                    
            conn.commit()
            conn.close()
            return jsonify({
                "status": "success",
                "message": f"Processed {rows_processed} historical assessment records successfully!"
            })
        except Exception as e:
            return jsonify({"status": "error", "message": f"Historical import failed: {str(e)}"}), 500



# --- SINGLE & BULK ROSTER ACTIONS (EDIT & DELETE) ---
@app.route('/api/roster/<emp_code>', methods=['PUT', 'DELETE'])
def handle_single_roster_action(emp_code):
    emp_code = emp_code.upper().strip()
    conn = get_db_connection()
    
    if request.method == 'DELETE':
        reason = request.args.get('reason', 'Individual Deletion').strip()
        conn.execute("DELETE FROM employees WHERE UPPER(emp_code)=?", (emp_code,))
        conn.execute("DELETE FROM assessment_results WHERE UPPER(emp_code)=?", (emp_code,))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": f"Employee '{emp_code}' deleted successfully."})
        
    elif request.method == 'PUT':
        data = request.json or {}
        emp_name = data.get('emp_name', '').strip().upper()
        branch_name = data.get('branch_name', '').strip().upper()
        zone = data.get('zone', '').strip().upper()
        division = data.get('division', '').strip().upper()
        business_unit = data.get('business_unit', '').strip().upper()
        role = data.get('role', '').strip().upper()
        product_name = data.get('product_name', '').strip().upper()
        status = data.get('status', 'ACTIVE').strip().upper()
        
        conn.execute("""
            UPDATE employees SET
                emp_name=COALESCE(NULLIF(?, ''), emp_name),
                branch_name=COALESCE(NULLIF(?, ''), branch_name),
                zone=COALESCE(NULLIF(?, ''), zone),
                division=COALESCE(NULLIF(?, ''), division),
                business_unit=COALESCE(NULLIF(?, ''), business_unit),
                role=COALESCE(NULLIF(?, ''), role),
                product_name=COALESCE(NULLIF(?, ''), product_name),
                status=COALESCE(NULLIF(?, ''), status)
            WHERE UPPER(emp_code)=?
        """, (emp_name, branch_name, zone, division, business_unit, role, product_name, status, emp_code))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": f"Employee '{emp_code}' updated successfully."})

@app.route('/api/roster/bulk-action', methods=['POST'])
def handle_bulk_roster_action():
    data = request.json or {}
    action = data.get('action', '').lower()
    emp_codes = [c.upper().strip() for c in data.get('emp_codes', []) if c]
    reason = data.get('reason', 'Bulk Operation').strip()
    
    conn = get_db_connection()
    
    if action == 'delete':
        if not emp_codes:
            conn.execute("DELETE FROM employees")
            conn.execute("DELETE FROM assessment_results")
            count = "All"
        else:
            placeholders = ','.join(['?'] * len(emp_codes))
            conn.execute(f"DELETE FROM employees WHERE UPPER(emp_code) IN ({placeholders})", emp_codes)
            conn.execute(f"DELETE FROM assessment_results WHERE UPPER(emp_code) IN ({placeholders})", emp_codes)
            count = len(emp_codes)
            
        conn.commit()
        conn.close()
        return jsonify({
            "status": "success",
            "message": f"Bulk deletion completed successfully! ({count} records deleted)"
        })
        
    elif action == 'edit':
        if not emp_codes:
            conn.close()
            return jsonify({"status": "error", "message": "No employees selected for bulk edit."}), 400
            
        placeholders = ','.join(['?'] * len(emp_codes))
        updates = []
        params = []
        
        for field in ['zone', 'division', 'branch_name', 'business_unit', 'role', 'product_name', 'status', 'change_detail']:
            if field in data and data[field]:
                updates.append(f"{field}=?")
                params.append(str(data[field]).strip().upper())
                
        if not updates:
            conn.close()
            return jsonify({"status": "error", "message": "No fields provided to update."}), 400
            
        sql = f"UPDATE employees SET {','.join(updates)} WHERE UPPER(emp_code) IN ({placeholders})"
        params.extend(emp_codes)
        
        conn.execute(sql, params)
        conn.commit()
        conn.close()
        return jsonify({
            "status": "success",
            "message": f"Bulk edit applied successfully to {len(emp_codes)} employees!"
        })
        
    conn.close()
    return jsonify({"status": "error", "message": "Invalid bulk action requested."}), 400


# 4. MODULE MANAGEMENT (Maker-Checker & Dynamic AI Support)
@app.route('/api/modules', methods=['GET', 'POST'])
def handle_modules():
    conn = get_db_connection()
    if request.method == 'GET':
        trainer_id = request.args.get('trainer_id')
        if trainer_id:
            # Private Draft Isolation: Load trainer's own drafts, plus any 'Ready' shared modules
            modules = conn.execute(
                "SELECT * FROM modules WHERE created_by = ? OR status = 'Ready' ORDER BY id DESC",
                (trainer_id,)
            ).fetchall()
        else:
            modules = conn.execute("SELECT * FROM modules ORDER BY id DESC").fetchall()
            
        res_list = []
        for m in modules:
            m_dict = dict(m)
            q_rows = conn.execute("SELECT * FROM questions WHERE module_id=?", (m['id'],)).fetchall()
            m_dict['questions'] = [dict(q) for q in q_rows]
            res_list.append(m_dict)
            
        conn.close()
        return jsonify(res_list)
    
    elif request.method == 'POST':
        data = request.json
        now = datetime.datetime.now().strftime("%Y-%m-%d")
        trainer_id = data.get('created_by', 'ADMIN')
        conn.execute("INSERT INTO modules (title, questions_count, created_at, status, created_by) VALUES (?, ?, ?, ?, ?)",
                     (data['title'], 15, now, 'Ready', trainer_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})

@app.route('/api/modules/<int:module_id>', methods=['DELETE'])
def delete_module(module_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM modules WHERE id=?", (module_id,))
    conn.execute("DELETE FROM questions WHERE module_id=?", (module_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

# --- FIELD VISITS / TRAVEL HUB (Planner, GPS check-in, Manager sign-off) ---
# Default manager sign-off PIN for visit verification (override via MANAGER_PIN env).
MANAGER_PIN = os.environ.get('MANAGER_PIN', '2468')


def _visit_row_to_dict(row):
    d = dict(row)
    return d


def _resolve_branch_info(branch_name, branch_code=None):
    """Resolve zone/division/branch_code/business_unit from the roster for a branch."""
    conn = get_db_connection()
    row = None
    if branch_code:
        row = conn.execute(
            "SELECT branch_name, zone, division, business_unit FROM employees WHERE UPPER(TRIM(branch_name))=UPPER(TRIM(?)) LIMIT 1",
            (branch_code,)
        ).fetchone()
    if not row and branch_name:
        row = conn.execute(
            "SELECT branch_name, zone, division, business_unit FROM employees WHERE UPPER(TRIM(branch_name))=UPPER(TRIM(?)) LIMIT 1",
            (branch_name,)
        ).fetchone()
    conn.close()
    if not row:
        return {
            "zone": None, "division": None, "branch_name": (branch_name or branch_code or '').upper(),
            "branch_code": (branch_code or branch_name or '').upper(), "business_unit": None
        }
    return {
        "zone": row['zone'],
        "division": row['division'],
        "branch_name": row['branch_name'],
        "branch_code": row['branch_name'],
        "business_unit": row['business_unit'],
    }


def _trainer_id_and_name(trainer_id_param=None):
    """Resolve trainer identity: explicit param wins (admin use), else session."""
    user = _session_user()
    if trainer_id_param and trainer_id_param.strip().upper() != 'ADMIN':
        tid = trainer_id_param.strip().upper()
    elif user:
        tid = user.get('trainer_id', 'ADMIN')
    else:
        tid = 'ADMIN'
    conn = get_db_connection()
    row = conn.execute("SELECT name FROM trainers WHERE UPPER(trainer_id)=UPPER(?)", (tid,)).fetchone()
    conn.close()
    return tid, (row['name'] if row else tid)


@app.route('/api/visits', methods=['GET'])
def list_visits():
    conn = get_db_connection()
    query = "SELECT * FROM visits WHERE 1=1"
    params = []
    user = _session_user()

    # Access control: trainers only see their own planned visits.
    req_trainer = request.args.get('trainer_id', '').strip()
    if req_trainer:
        query += " AND UPPER(TRIM(trainer_id))=UPPER(TRIM(?))"
        params.append(req_trainer)
    elif user and not _is_global_role(user.get('role', '')):
        query += " AND UPPER(TRIM(trainer_id))=UPPER(TRIM(?))"
        params.append(user.get('trainer_id', ''))

    zone = request.args.get('zone', '').strip()
    division = request.args.get('division', '').strip()
    branch = request.args.get('branch', '').strip()
    status = request.args.get('status', '').strip()
    month = request.args.get('month', '').strip()
    if zone:
        query += " AND UPPER(TRIM(zone))=UPPER(TRIM(?))"
        params.append(zone)
    if division:
        query += " AND UPPER(TRIM(division))=UPPER(TRIM(?))"
        params.append(division)
    if branch:
        query += " AND UPPER(TRIM(branch_name))=UPPER(TRIM(?))"
        params.append(branch)
    if status:
        query += " AND UPPER(TRIM(status))=UPPER(TRIM(?))"
        params.append(status)
    if month and re.match(r'^\d{4}-\d{2}$', month):
        query += " AND (substr(planned_date,1,7)=? OR substr(end_date,1,7)=?)"
        params.extend([month, month])

    query += " ORDER BY planned_date DESC, id DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([_visit_row_to_dict(r) for r in rows])


@app.route('/api/visits/plan', methods=['POST'])
def plan_visit():
    data = request.json or {}
    branch_name = str(data.get('branch_name', '')).strip()
    planned_date = str(data.get('planned_date', '')).strip()
    if not branch_name or not planned_date:
        return jsonify({"status": "error", "message": "Branch and planned date are required."}), 400

    trainer_id, trainer_name = _trainer_id_and_name(str(data.get('trainer_id', '')).strip())
    info = _resolve_branch_info(branch_name)
    purpose = str(data.get('purpose', '')).strip()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = get_db_connection()
    cur = conn.execute("""
        INSERT INTO visits (trainer_id, trainer_name, zone, division, branch_name, branch_code, business_unit,
                            planned_date, end_date, meeting_agenda, meeting_with, purpose, key_contacts, details,
                            status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PLANNED', ?, ?)
    """, (
        trainer_id, trainer_name, info['zone'], info['division'], info['branch_name'], info['branch_code'],
        info['business_unit'], planned_date, str(data.get('end_date', '')).strip() or planned_date,
        purpose, '', purpose, str(data.get('key_contacts', '')).strip(), str(data.get('details', '')).strip(),
        now, now
    ))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": "Visit planned successfully!", "visit_id": cur.lastrowid})


@app.route('/api/visits/upload', methods=['POST'])
def upload_visits():
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "No selected file"}), 400

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    errors = []
    added = 0
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = get_db_connection()
    try:
        with open(filepath, 'r', encoding='utf-8-sig') as csvfile:
            reader = csv.reader(csvfile)
            raw_headers = [h.strip().lower() for h in next(reader)]

            def hdr_idx(keywords):
                for idx, h in enumerate(raw_headers):
                    if any(k in h for k in keywords):
                        return idx
                return -1

            tname_idx = hdr_idx(['trainer name', 'trainer_name', 'name'])
            tid_idx = hdr_idx(['trainer id', 'trainer_id', 'emp code', 'code'])
            bu_idx = hdr_idx(['business unit', 'bu'])
            date_from_idx = hdr_idx(['date of visit from', 'visit from', 'from date', 'planned date'])
            date_to_idx = hdr_idx(['date of visit to', 'visit to', 'to date', 'end date'])
            br_idx = hdr_idx(['branch code', 'branch'])
            agenda_idx = hdr_idx(['meeting agenda', 'agenda'])
            meet_idx = hdr_idx(['meeting with', 'meeting with role'])
            overnight_idx = hdr_idx(['overnight stay'])
            travel_from_idx = hdr_idx(['travel from'])
            travel_to_idx = hdr_idx(['travel to'])
            travel_mode_idx = hdr_idx(['travel mode'])

            if date_from_idx == -1 or (tname_idx == -1 and tid_idx == -1):
                conn.close()
                os.remove(filepath)
                return jsonify({
                    "status": "error",
                    "message": "Invalid CSV. Required columns: 'Trainer Name' (or 'Trainer ID') and 'Date of Visit From'."
                }), 400

            for row_idx, r in enumerate(reader, start=2):
                if not r or len(r) < 1:
                    continue
                # Resolve trainer
                if tid_idx != -1 and len(r) > tid_idx and r[tid_idx].strip():
                    tid = r[tid_idx].strip().upper()
                    tname = ''
                else:
                    tname = r[tname_idx].strip().upper() if tname_idx != -1 and len(r) > tname_idx else ''
                    tid = ''
                if not tid and not tname:
                    errors.append(f"Row {row_idx}: missing trainer name/id — skipped.")
                    continue

                # Resolve trainer id by name if needed
                if not tid:
                    trow = conn.execute("SELECT trainer_id, name FROM trainers WHERE UPPER(TRIM(name))=UPPER(TRIM(?)) LIMIT 1", (tname,)).fetchone()
                    if trow:
                        tid, tname = trow['trainer_id'], trow['name']
                    else:
                        tid = f"CSV:{tname[:20]}" if tname else "CSV"

                planned_date = r[date_from_idx].strip() if date_from_idx != -1 and len(r) > date_from_idx else ''
                if not planned_date:
                    errors.append(f"Row {row_idx}: missing visit date — skipped.")
                    continue
                # Normalise date to YYYY-MM-DD
                if re.match(r'^\d{2}/\d{2}/\d{4}$', planned_date):
                    try:
                        planned_date = datetime.datetime.strptime(planned_date, "%d/%m/%Y").strftime("%Y-%m-%d")
                    except ValueError:
                        pass
                elif re.match(r'^\d{4}-\d{2}-\d{2}$', planned_date):
                    pass
                elif re.match(r'^\d{4}-\d{2}$', planned_date):
                    planned_date = planned_date + "-01"
                else:
                    errors.append(f"Row {row_idx}: unparseable date '{planned_date}' — skipped.")
                    continue

                end_date = ''
                if date_to_idx != -1 and len(r) > date_to_idx:
                    end_date = r[date_to_idx].strip()
                    if re.match(r'^\d{2}/\d{2}/\d{4}$', end_date):
                        try:
                            end_date = datetime.datetime.strptime(end_date, "%d/%m/%Y").strftime("%Y-%m-%d")
                        except ValueError:
                            end_date = planned_date
                if not end_date:
                    end_date = planned_date

                branch_code = r[br_idx].strip() if br_idx != -1 and len(r) > br_idx else ''
                agenda = r[agenda_idx].strip() if agenda_idx != -1 and len(r) > agenda_idx else ''
                meeting_with = r[meet_idx].strip() if meet_idx != -1 and len(r) > meet_idx else ''
                bu = r[bu_idx].strip() if bu_idx != -1 and len(r) > bu_idx else ''
                overnight = r[overnight_idx].strip() if overnight_idx != -1 and len(r) > overnight_idx else ''
                travel_from = r[travel_from_idx].strip() if travel_from_idx != -1 and len(r) > travel_from_idx else ''
                travel_to = r[travel_to_idx].strip() if travel_to_idx != -1 and len(r) > travel_to_idx else ''
                travel_mode = r[travel_mode_idx].strip() if travel_mode_idx != -1 and len(r) > travel_mode_idx else ''

                info = _resolve_branch_info(branch_code or '')
                if not info['zone'] and branch_code:
                    errors.append(f"Row {row_idx}: branch '{branch_code}' not found in roster — mapped as raw branch (no zone/division).")

                conn.execute("""
                    INSERT INTO visits (trainer_id, trainer_name, zone, division, branch_name, branch_code, business_unit,
                                        planned_date, end_date, meeting_agenda, meeting_with, purpose, status,
                                        travel_mode, travel_from, travel_to, overnight_stay, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PLANNED', ?, ?, ?, ?, ?, ?)
                """, (
                    tid, tname or tid, info['zone'], info['division'], info['branch_name'] or branch_code,
                    info['branch_code'] or branch_code, bu or info['business_unit'], planned_date, end_date,
                    agenda or 'Field Visit', meeting_with, agenda or 'Field Visit',
                    travel_mode, travel_from, travel_to, overnight, now, now
                ))
                added += 1

        conn.commit()
        conn.close()
        os.remove(filepath)
        msg = f"Bulk upload complete: {added} visit(s) added."
        if errors:
            msg += f" {len(errors)} row(s) skipped."
        return jsonify({"status": "success", "message": msg, "details": errors})
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        try:
            os.remove(filepath)
        except Exception:
            pass
        return jsonify({"status": "error", "message": f"Failed to parse CSV: {str(e)}"}), 400


@app.route('/api/visits/checkin', methods=['POST'])
def visit_checkin():
    data = request.json or {}
    visit_id = data.get('visit_id')
    if not visit_id:
        return jsonify({"status": "error", "message": "Missing visit id."}), 400
    try:
        lat = float(data.get('latitude'))
        lng = float(data.get('longitude'))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid GPS coordinates."}), 400

    conn = get_db_connection()
    visit = conn.execute("SELECT * FROM visits WHERE id=?", (visit_id,)).fetchone()
    if not visit:
        conn.close()
        return jsonify({"status": "error", "message": "Visit not found."}), 404
    if visit['status'] not in ('PLANNED', 'GEOFENCED'):
        conn.close()
        return jsonify({"status": "error", "message": "Visit already verified."}), 400

    # Count co-present training sessions at the same branch on the visit date.
    co_presence = conn.execute(
        "SELECT COUNT(*) AS c FROM training_sessions WHERE UPPER(TRIM(branch_name))=UPPER(TRIM(?)) AND date=?",
        (visit['branch_name'], visit['planned_date'])
    ).fetchone()['c']

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute(
        "UPDATE visits SET status='GEOFENCED', geo_lat=?, geo_lng=?, checkin_time=?, co_presence_count=?, updated_at=? WHERE id=?",
        (lat, lng, now, co_presence, now, visit_id)
    )
    conn.commit()
    conn.close()
    return jsonify({
        "status": "success",
        "message": f"GPS check-in recorded for {visit['branch_name']}.",
        "co_presence": co_presence
    })


@app.route('/api/visits/verify', methods=['POST'])
def visit_verify():
    data = request.json or {}
    visit_id = data.get('visit_id')
    manager_pin = str(data.get('manager_pin', '')).strip()
    if not visit_id:
        return jsonify({"status": "error", "message": "Missing visit id."}), 400

    user = _session_user()
    is_admin_force = user and _is_global_role(user.get('role', ''))

    if not is_admin_force and manager_pin != MANAGER_PIN:
        return jsonify({"status": "error", "message": "Invalid Manager PIN. Please verify with the Branch Manager."}), 401

    conn = get_db_connection()
    visit = conn.execute("SELECT * FROM visits WHERE id=?", (visit_id,)).fetchone()
    if not visit:
        conn.close()
        return jsonify({"status": "error", "message": "Visit not found."}), 404

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute("UPDATE visits SET status='VERIFIED', updated_at=? WHERE id=?", (now, visit_id))
    conn.commit()
    conn.close()
    actor = "Admin force sign-off" if is_admin_force else "Branch Manager sign-off"
    return jsonify({"status": "success", "message": f"Visit {visit['branch_name']} verified via {actor}."})


@app.route('/api/visits/<int:visit_id>/mom', methods=['POST'])
def save_visit_mom(visit_id):
    data = request.json or {}
    mom_notes = str(data.get('mom_notes', '')).strip()
    if not mom_notes:
        return jsonify({"status": "error", "message": "MoM notes are required."}), 400
    conn = get_db_connection()
    visit = conn.execute("SELECT * FROM visits WHERE id=?", (visit_id,)).fetchone()
    if not visit:
        conn.close()
        return jsonify({"status": "error", "message": "Visit not found."}), 404
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute("UPDATE visits SET mom_notes=?, updated_at=? WHERE id=?", (mom_notes, now, visit_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": "Minutes of Meeting saved & formatted for management mail!"})


@app.route('/api/visits/compliance-stats', methods=['GET'])
def visits_compliance_stats():
    month = request.args.get('month', datetime.datetime.now().strftime("%Y-%m"))
    if not re.match(r'^\d{4}-\d{2}$', month):
        return jsonify({"status": "error", "message": "Invalid month format (use YYYY-MM)."}), 400
    conn = get_db_connection()
    total_active = conn.execute("SELECT COUNT(*) AS c FROM trainers WHERE status='Active'").fetchone()['c']
    updated = conn.execute(
        "SELECT COUNT(DISTINCT trainer_id) AS c FROM visits WHERE substr(planned_date,1,7)=?",
        (month,)
    ).fetchone()['c']
    conn.close()
    return jsonify({
        "status": "success",
        "month": month,
        "total_active_trainers": total_active,
        "updated_count": updated,
        "not_updated_count": max(0, total_active - updated)
    })


@app.route('/api/visits/export', methods=['GET'])
def export_visits():
    conn = get_db_connection()
    query = "SELECT * FROM visits WHERE 1=1"
    params = []
    period = request.args.get('period', 'ALL').strip().upper()
    today = datetime.date.today()
    month = request.args.get('month', today.strftime("%Y-%m")).strip()
    year = request.args.get('year', str(today.year)).strip()
    zone = request.args.get('zone', '').strip()
    division = request.args.get('division', '').strip()
    branch = request.args.get('branch', '').strip()
    trainer = request.args.get('trainer', '').strip()
    status = request.args.get('status', '').strip()

    if period == 'MTD':
        query += " AND substr(planned_date,1,7)=?"
        params.append(today.strftime("%Y-%m"))
    elif period == 'YTD':
        query += " AND substr(planned_date,1,4)=?"
        params.append(str(today.year))
    elif period == 'MONTH':
        query += " AND (substr(planned_date,1,7)=? OR substr(end_date,1,7)=?)"
        params.extend([month, month])
    elif period == 'YEAR':
        query += " AND substr(planned_date,1,4)=?"
        params.append(year)
    if zone:
        query += " AND UPPER(TRIM(zone))=UPPER(TRIM(?))"
        params.append(zone)
    if division:
        query += " AND UPPER(TRIM(division))=UPPER(TRIM(?))"
        params.append(division)
    if branch:
        query += " AND UPPER(TRIM(branch_name))=UPPER(TRIM(?))"
        params.append(branch)
    if trainer:
        query += " AND UPPER(TRIM(trainer_name))=UPPER(TRIM(?))"
        params.append(trainer)
    if status:
        query += " AND UPPER(TRIM(status))=UPPER(TRIM(?))"
        params.append(status)

    query += " ORDER BY planned_date DESC, id DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Visit ID', 'Trainer ID', 'Trainer Name', 'Zone', 'Division', 'Branch', 'Branch Code',
                     'Business Unit', 'Planned Date', 'End Date', 'Agenda', 'Meeting With', 'Status',
                     'Check-in Time', 'Co-Presence', 'Travel Mode', 'Travel From', 'Travel To', 'Overnight Stay'])
    for v in rows:
        writer.writerow([v['id'], v['trainer_id'], v['trainer_name'], v['zone'], v['division'], v['branch_name'],
                         v['branch_code'], v['business_unit'], v['planned_date'], v['end_date'],
                         v['meeting_agenda'], v['meeting_with'], v['status'], v['checkin_time'],
                         v['co_presence_count'], v['travel_mode'], v['travel_from'], v['travel_to'], v['overnight_stay']])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=Socrates_Field_Visits_Report.csv"}
    )


@app.route('/api/visits/<int:visit_id>', methods=['DELETE'])
def delete_visit(visit_id):
    user = _session_user()
    if not user or not _is_global_role(user.get('role', '')):
        return jsonify({"status": "error", "message": "Only Super Admin or Leader can cancel itineraries."}), 403
    conn = get_db_connection()
    conn.execute("DELETE FROM visits WHERE id=?", (visit_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": "Itinerary visit deleted successfully!"})


# --- ADMIN DASHBOARD STATS (live data from the actual database) ---
@app.route('/api/dashboard/stats', methods=['GET'])
def dashboard_stats():
    conn = get_db_connection()
    trainer_id = request.args.get('trainer_id', '').strip()
    today = datetime.date.today()
    month_start = today.replace(day=1).strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")

    # Server-side visibility scope (zone/division/branch/BU) for non-global roles.
    emp_scope_sql, emp_scope_params = "", []
    sess_scope_sql, sess_scope_params = "", []
    _user = _session_user()
    if _user and not _is_global_role(_user.get('role', '')):
        _scope = _trainer_scope(_user.get('trainer_id'))
        if _scope:
            sp_e, sp_s = [], []
            if _scope.get('zones'):
                sp_e.append("UPPER(TRIM(e.zone)) IN ({})".format(','.join('?' * len(_scope['zones']))))
                emp_scope_params.extend(_scope['zones'])
            if _scope.get('divisions'):
                sp_e.append("UPPER(TRIM(e.division)) IN ({})".format(','.join('?' * len(_scope['divisions']))))
                emp_scope_params.extend(_scope['divisions'])
            if _scope.get('branches'):
                sp_e.append("UPPER(TRIM(e.branch_name)) IN ({})".format(','.join('?' * len(_scope['branches']))))
                emp_scope_params.extend(_scope['branches'])
                sp_s.append("UPPER(TRIM(branch_name)) IN ({})".format(','.join('?' * len(_scope['branches']))))
                sess_scope_params.extend(_scope['branches'])
            if _scope.get('business_units'):
                sp_e.append("UPPER(TRIM(e.business_unit)) IN ({})".format(','.join('?' * len(_scope['business_units']))))
                emp_scope_params.extend(_scope['business_units'])
            if sp_e:
                emp_scope_sql = " AND " + " AND ".join(sp_e)
            if sp_s:
                sess_scope_sql = " AND " + " AND ".join(sp_s)

    # Live sessions logged by the Live Session module (month-to-date)
    sess_q = "SELECT * FROM training_sessions WHERE date >= ?"
    sess_p = [month_start]
    if trainer_id and trainer_id.upper() != 'ADMIN':
        sess_q += " AND UPPER(TRIM(trainer_id))=UPPER(TRIM(?))"
        sess_p.append(trainer_id)
    if sess_scope_sql:
        sess_q += sess_scope_sql
        sess_p.extend(sess_scope_params)
    sessions = conn.execute(sess_q, sess_p).fetchall()

    # Legacy/offline campaigns never wrote training_sessions; when the log is
    # empty, derive session cohorts from assessment results instead of showing 0.
    total_ts = conn.execute("SELECT COUNT(*) AS c FROM training_sessions").fetchone()['c']
    use_fallback = total_ts == 0
    sessions_count = len(sessions)
    recent_sessions = []

    if use_fallback:
        fb_scope = emp_scope_sql.replace("e.zone", "e.zone").replace("UPPER(TRIM(e.branch_name))", "UPPER(TRIM(e.branch_name))")
        fbq = """
            SELECT a.module_id, TRIM(COALESCE(e.branch_name,'')) AS branch_name,
                   COUNT(DISTINCT a.emp_code) AS attendee_count,
                   MAX(a.completed_at) AS latest
            FROM assessment_results a
            LEFT JOIN employees e ON a.emp_code = e.emp_code
            WHERE a.completed_at >= ? {scope}
            GROUP BY a.module_id, TRIM(COALESCE(e.branch_name,''))
        """
        fbp = [month_start]
        if emp_scope_sql:
            fbq = fbq.format(scope=emp_scope_sql)
            fbp.extend(emp_scope_params)
        else:
            fbq = fbq.format(scope="")
        cohort_rows = conn.execute(fbq, fbp).fetchall()
        sessions_count = len(cohort_rows)
        for c in cohort_rows[:8]:
            title = ''
            trow = conn.execute("SELECT title FROM modules WHERE id=?", (c['module_id'],)).fetchone()
            if trow:
                title = trow['title']
            recent_sessions.append({
                "date": (c['latest'] or '')[:10],
                "module_title": title or f"Module #{c['module_id']}",
                "branch_name": c['branch_name'] or '—',
                "trainer_name": '—',
                "attendee_count": c['attendee_count']
            })
    else:
        recent = conn.execute(
            "SELECT * FROM training_sessions ORDER BY date DESC, session_id DESC LIMIT 8"
        ).fetchall()
        for s in recent:
            title = ''
            trow = conn.execute("SELECT title FROM modules WHERE id=?", (s['module_id'],)).fetchone()
            if trow:
                title = trow['title']
            tr_name = ''
            trow2 = conn.execute("SELECT name FROM trainers WHERE trainer_id=?", (s['trainer_id'],)).fetchone()
            if trow2:
                tr_name = trow2['name']
            attendee = 0
            if s['module_id']:
                attendee = conn.execute(
                    "SELECT COUNT(DISTINCT emp_code) AS c FROM assessment_results WHERE module_id=? AND DATE(completed_at)=?",
                    (s['module_id'], s['date'])
                ).fetchone()['c']
            recent_sessions.append({
                "date": s['date'],
                "module_title": title,
                "branch_name": s['branch_name'],
                "trainer_name": tr_name or s['trainer_id'],
                "attendee_count": attendee
            })

    # Branches visited: same scope as the session numbers
    if use_fallback:
        fb_bq = """
            SELECT COUNT(DISTINCT TRIM(COALESCE(e.branch_name,''))) AS c
            FROM assessment_results a
            LEFT JOIN employees e ON a.emp_code = e.emp_code
            WHERE a.completed_at >= ? {scope} AND TRIM(COALESCE(e.branch_name,'')) != ''
        """
        bbp = [month_start]
        if emp_scope_sql:
            fb_bq = fb_bq.format(scope=emp_scope_sql)
            bbp.extend(emp_scope_params)
        else:
            fb_bq = fb_bq.format(scope="")
        branches_visited = conn.execute(fb_bq, bbp).fetchone()['c']
    else:
        bv_q = "SELECT COUNT(DISTINCT branch_name) AS c FROM training_sessions WHERE branch_name IS NOT NULL AND TRIM(branch_name)!='' AND date >= ?"
        bv_p = [month_start]
        if trainer_id and trainer_id.upper() != 'ADMIN':
            bv_q += " AND UPPER(TRIM(trainer_id))=UPPER(TRIM(?))"
            bv_p.append(trainer_id)
        if sess_scope_sql:
            bv_q += sess_scope_sql
            bv_p.extend(sess_scope_params)
        branches_visited = conn.execute(bv_q, bv_p).fetchone()['c']

    # Execs trained / growth: scoped to the viewer's visibility + current month
    ar_scope = emp_scope_sql
    execs_trained = conn.execute(
        "SELECT COUNT(DISTINCT a.emp_code) AS c FROM assessment_results a LEFT JOIN employees e ON a.emp_code=e.emp_code WHERE a.completed_at >= ?" + ar_scope,
        [month_start] + emp_scope_params
    ).fetchone()['c']
    growth = conn.execute(
        "SELECT AVG(a.post_test_score - a.pre_test_score) AS g FROM assessment_results a LEFT JOIN employees e ON a.emp_code=e.emp_code WHERE a.post_test_score IS NOT NULL AND a.pre_test_score IS NOT NULL AND a.completed_at >= ?" + ar_scope,
        [month_start] + emp_scope_params
    ).fetchone()['g']
    modules_count = conn.execute("SELECT COUNT(*) AS c FROM modules").fetchone()['c']

    # Branch leaderboard from real assessment results (scoped)
    top_branches = []
    br_rows = conn.execute("""
        SELECT e.branch_name, COUNT(DISTINCT ar.emp_code) AS cnt,
               AVG(ar.post_test_score - ar.pre_test_score) AS delta
        FROM assessment_results ar
        LEFT JOIN employees e ON e.emp_code = ar.emp_code
        WHERE e.branch_name IS NOT NULL AND TRIM(e.branch_name)!='' AND ar.completed_at >= ?
        {scope}
        GROUP BY e.branch_name
        ORDER BY cnt DESC LIMIT 5
    """.format(scope=emp_scope_sql), [month_start] + emp_scope_params).fetchall()
    for b in br_rows:
        top_branches.append({
            "branch_name": b['branch_name'],
            "count": b['cnt'],
            "growth_delta": round((b['delta'] or 0), 1)
        })

    # Maker-Checker pending audits
    pending_audits = []
    pend = conn.execute("SELECT * FROM modules WHERE status='Pending Audit' ORDER BY id DESC").fetchall()
    for m in pend:
        approved = conn.execute("SELECT COUNT(*) AS c FROM questions WHERE module_id=? AND approved=1", (m['id'],)).fetchone()['c']
        total = conn.execute("SELECT COUNT(*) AS c FROM questions WHERE module_id=?", (m['id'],)).fetchone()['c']
        pending_audits.append({
            "id": m['id'],
            "title": m['title'],
            "creator_name": m['audited_by'],
            "created_by": m['created_by'],
            "difficulty": m['difficulty'],
            "approved_count": approved,
            "questions_count": total or (m['questions_count'] or 0)
        })

    # Today's field visits (travel hub live movement)
    todays_visits = []
    vis_q = "SELECT * FROM visits WHERE planned_date <= ? AND (end_date IS NULL OR end_date >= ?) AND status != 'CANCELLED'"
    vis_p = [today_str, today_str]
    if trainer_id and trainer_id.upper() != 'ADMIN':
        vis_q += " AND UPPER(TRIM(trainer_id))=UPPER(TRIM(?))"
        vis_p.append(trainer_id)
    vis_q += " ORDER BY planned_date ASC"
    vis = conn.execute(vis_q, vis_p).fetchall()
    for v in vis:
        todays_visits.append({
            "id": v['id'],
            "branch_name": v['branch_name'],
            "trainer_name": v['trainer_name'] or v['trainer_id'],
            "status": v['status'],
            "checkin_time": v['checkin_time']
        })

    conn.close()
    return jsonify({
        "status": "success",
        "sessions_count": sessions_count,
        "branches_visited": branches_visited,
        "execs_trained": execs_trained,
        "avg_growth_delta": round(growth or 0, 1),
        "modules_count": modules_count,
        "recent_sessions": recent_sessions,
        "top_branches": top_branches,
        "pending_audits": pending_audits,
        "todays_visits": todays_visits
    })


# --- Document-grounded question synthesis helpers (no external AI required) ---

def _balanced_sample(text, limit=6000):
    """Sample text from the start, middle, and end so multi-section documents are all represented."""
    if len(text) <= limit:
        return text
    third = limit // 3
    mid = len(text) // 2
    return f"{text[:third]}\n[...]\n{text[mid:mid+third]}\n[...]\n{text[-third:]}"


def _normalize_key(text):
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


_MONTH_NAMES = frozenset({'january','february','march','april','may','june','july','august','september','october','november','december'})
# Month names are intentionally NOT in _NUM_WORDS: 'may'/'march' as verbs appear far
# more often in training text than standalone month mentions, and matching them produced
# nonsense cloze questions like 'employees __________ work overtime'.
_NUM_WORDS = r'one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand'
_NUM_UNITS = r'%|percent|hours?|days?|months?|years?|minutes?|seconds?|meters?|metres?|rupees?|lakhs?|crores?|times?|working days?'
_NUM_FREQ = r'annually|monthly|weekly|daily|quarterly|yearly|semi-annually|bi-monthly|once|twice|thrice'
# Compound word-numbers are supported: 'twenty-four', 'five hundred', 'five thousand'
_NUM_TOKEN_RE = re.compile(rf'\b((?:\d+(?:\.\d+)?|\d{{1,2}}(?:st|nd|rd|th))|(?:{_NUM_WORDS})(?:[ -](?:{_NUM_WORDS})){{0,2}})\s*({_NUM_UNITS})?\b|\b({_NUM_FREQ})\b', re.IGNORECASE)


def _find_numeric_token(chunk):
    """Extract a numeric/unit/frequency/date token from a clause
    (e.g. 'six months', '50%', '1 April 2026')."""
    m = _NUM_TOKEN_RE.search(chunk)
    if not m:
        return None
    end = m.end()
    if m.group(3):
        token = m.group(3)
    elif m.group(2) == '%':
        token = m.group(1) + '%'
    elif m.group(2):
        token = f"{m.group(1)} {m.group(2)}"
    else:
        token = m.group(1)
        # The trailing \b in the regex pushes the percent sign out of the match
        # ('50%' matches as '50'); reattach it so the cloze reads '__________ %'.
        if end < len(chunk) and chunk[end] == '%':
            token += '%'
            end += 1
    # Absorb a following month name so dates like '1 April 2026' become '1 April'
    # and the cloze reads 'Effective from __________ 2026'.
    if m.group(3) is None:
        mm = re.match(r"^\s*(\d{1,2}(?:st|nd|rd|th)?)?\s*([A-Za-z]+)", chunk[end:])
        if mm and mm.group(2).lower() in _MONTH_NAMES:
            token = ' '.join(p for p in (token, mm.group(1), mm.group(2)) if p)
    return token if len(token) >= 2 else None


_STOP_WORDS = {'the','a','an','and','or','but','of','in','on','at','to','for','with','by','from','as','is','are','was','were','must','be','been','any','all','every','each','that','which','who','whose','when','where','why','how','into','during','after','before','under','over','within','without','following','among','including','per','their','its','his','her','our','your','if','not','no','never','only','also','then','so','whenever','unless','than','may','can','will','shall'}


def _topic_of(chunk):
    """Short distinctive subject phrase of a clause (e.g. 'gold loan disbursement').
    Pure numbers, month names and date fragments are skipped so topics stay meaningful."""
    picked = []
    for w in re.split(r'\s+', chunk):
        wc = w.strip('.,;:()')
        if not wc:
            continue
        wl = wc.lower()
        if wl in _STOP_WORDS or wl in _MONTH_NAMES or _NUM_TOKEN_RE.match(wc):
            continue
        picked.append(wc)
        if len(picked) >= 3:
            break
    if len(picked) < 2:
        picked = re.split(r'\s+', chunk.strip(' .'))[:4]
    return ' '.join(picked)


_FRAGMENT_STOPS = frozenset("""
a i
am an as at be by do go he hi id if in is it me my no of ok on or so to up us we
ad ex mr dr st sr jr vs eg ie co inc ltd kg km cm mm mg ml hr min sec rs quo hoc
the and for are but not you all can had her was one our out day has him his how
its may new off old own per she two use who why yet any ago did few get got let
put say see set try way yes etc via now end key top low run red due net too lot
""".split())


def _is_fragment(token):
    """True when `token` looks like the broken head/tail of a word split by a PDF
    soft line-wrap (e.g. 'er' in 'custom'+'er', 't' in 'withou'+'t'). Short real
    words, abbreviations and numbers are never fragments, so genuine line
    boundaries are preserved."""
    t = token.strip(' .,;:()"\'')
    if not t:
        return False
    tl = t.lower()
    if any(ch.isdigit() for ch in tl) or '%' in tl or '.' in t or ',' in t:
        return False
    return len(tl) <= 3 and tl not in _FRAGMENT_STOPS


def _load_wordlist():
    """Load a system English word list for mid-word break repair (falls back to an
    empty set on systems without one, where the shorter fragment heuristic applies)."""
    for p in ('/usr/share/dict/words', '/usr/local/share/dict/words', '/opt/homebrew/share/dict/words'):
        try:
            with open(p, 'r', encoding='utf-8', errors='ignore') as f:
                words = {w.strip().lower() for w in f if w.strip().isalpha()}
            if len(words) > 50000:
                return words
        except OSError:
            continue
    return set()


_EN_WORDS = _load_wordlist()


def _wrap_breaks(prev, nxt):
    """True when the boundary between two wrapped lines is a mid-word break that
    word-list verification confirms (e.g. 'verific'+'ation' -> 'verification',
    'the a'+'pplication' -> 'application'). Word boundaries like 'book'+'ends'
    are left untouched because both halves are real words."""
    if not _EN_WORDS:
        return False
    w1 = prev.rsplit(' ', 1)[-1].strip(' .,;:()"\'')
    w2 = nxt.split(' ', 1)[0].strip(' .,;:()"\'')
    if not w1.isalpha() or not w2.isalpha():
        return False
    w1l, w2l = w1.lower(), w2.lower()
    if w1l + w2l not in _EN_WORDS:
        return False
    return w1l not in _EN_WORDS or w2l not in _EN_WORDS


def _merge_wrapped_lines(text_content):
    """Merge PDF soft line-wraps into real paragraphs (lines that do not end in
    sentence punctuation continue onto the next line). Mid-word wrap breaks
    ('custom' + 'er', 'withou' + 't') are repaired so text never renders as
    'custom er' / 'withou t'. All-caps heading lines (e.g. 'FIRE SAFETY TRAINING
    MANUAL') are dropped — they are document titles, not content, and must never
    be used as question/option text."""
    paragraphs = []
    cur = []
    for line in re.split(r'\r?\n', text_content):
        line = line.strip()
        if not line:
            if cur:
                paragraphs.append(' '.join(cur))
                cur = []
            continue
        if len(line) < 50 and line.isupper():
            if cur:
                paragraphs.append(' '.join(cur))
                cur = []
            continue
        if cur and cur[-1].endswith('-'):
            # hyphenated line-wrap: 'twenty-' + 'four hours' -> 'twenty-four hours'
            cur[-1] = cur[-1][:-1] + '-' + line
        elif cur and (_is_fragment(line.split(' ', 1)[0]) or _is_fragment(cur[-1].rsplit(' ', 1)[-1]) or _wrap_breaks(cur[-1], line)):
            # mid-word wrap: 'custom' + 'er must' -> 'customer must'
            cur[-1] = cur[-1] + line
        else:
            cur.append(line)
        if re.search(r'[.!?]["\')\]]?\s*$', line):
            paragraphs.append(' '.join(cur))
            cur = []
    if cur:
        paragraphs.append(' '.join(cur))
    return paragraphs


def _split_doc_chunks(text_content):
    """Split document text into unique, meaningful clause-level chunks (all real document text)."""
    chunks = []
    for para in _merge_wrapped_lines(text_content):
        for sent in re.split(r'(?<=[.!?])\s+', para):
            sent = sent.strip()
            if not sent:
                continue
            # Split on commas/semicolons/colons ONLY — never inside parentheses,
            # which produced broken fragments like 'KYC) verification' before.
            parts = [p.strip() for p in re.split(r'[,;:]\s*', sent)]
            merged = []
            for p in parts:
                if len(p) < 14 and merged:
                    merged[-1] = f"{merged[-1]} {p}"
                else:
                    merged.append(p)
            for m in merged:
                m = sanitize_llm_text(m).strip(' .()')
                m = re.sub(r'\s{2,}', ' ', m)
                # Drop document titles / heading lines (e.g. "FIRE SAFETY TRAINING MANUAL")
                if len(m) >= 20 and not (len(m) < 40 and m.isupper()):
                    chunks.append(m)
    seen, uniq = set(), []
    for c in chunks:
        key = _normalize_key(c)
        if key and key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq


def _clean_option(o):
    """Trim/normalise one option string so options never render as raw fragments."""
    o = sanitize_llm_text(str(o)).strip()
    o = re.sub(r'\s{2,}', ' ', o)
    o = o.strip(' \t.,;:()[]')
    return o


def _mcq_from_chunk(chunk, all_chunks, idx):
    """Build one MCQ grounded in `chunk`; every option is real text from the document."""
    # Pattern 1 — numeric/unit cloze: blank the number/unit; distractors are other
    # real numbers/units found elsewhere in the same document.
    token = _find_numeric_token(chunk)
    if token:
        stem = chunk.replace(token, '__________', 1)
        distractors = []
        for c in all_chunks:
            if c == chunk:
                continue
            d = _find_numeric_token(c)
            if d and d.lower() != token.lower() and all(d.lower() != x.lower() for x in distractors):
                distractors.append(d)
            if len(distractors) >= 3:
                break
        if len(distractors) >= 3:
            pos = idx % 4
            opts = (distractors[:pos] + [token] + distractors[pos:])[:4]
            return {
                "question": f"Select the option that correctly completes the statement: '{stem}'",
                "options": [_clean_option(o) for o in opts],
                "correctIndex": pos,
            }

    # Pattern 2 — statement selection: correct clause + 3 other real clauses with a
    # topic-specific stem (e.g. "...about 'gold loan disbursement' is correct?") so
    # every question is unique instead of repeating one generic stem.
    topic = _topic_of(chunk)
    n = len(all_chunks)
    others = []
    seen_keys = {_normalize_key(chunk)}
    seen_topics = set()
    # Pass 1: prefer distractors from DIFFERENT topics (best option variety).
    offset = 1
    while len(others) < 3 and offset < n:
        cand = all_chunks[(idx + offset * 7) % n]
        offset += 1
        ck = _normalize_key(cand)
        if ck in seen_keys:
            continue
        t = _topic_of(cand)
        if t and t.lower() in seen_topics:
            continue
        if t:
            seen_topics.add(t.lower())
        seen_keys.add(ck)
        others.append(cand)
    # Pass 2: if the document is homogeneous (repeated topics), fill the remaining
    # slots with any other DISTINCT statement so full question sets are still
    # produced instead of silently returning 3-4 questions.
    offset = 1
    while len(others) < 3 and offset < n:
        cand = all_chunks[(idx + offset * 13) % n]
        offset += 1
        ck = _normalize_key(cand)
        if ck in seen_keys:
            continue
        seen_keys.add(ck)
        others.append(cand)
    if len(others) >= 3:
        pos = idx % 4
        opts = (others[:pos] + [chunk] + others[pos:])[:4]
        # Clean every option; drop the question if any option is a fragment (< 15 chars)
        # or if options are not 4 unique statements.
        clean_opts = [_clean_option(o) for o in opts]
        clean_opts = [o for o in clean_opts if o]
        if len(clean_opts) < 4 or len(set(_normalize_key(o) for o in clean_opts)) < 4:
            return None
        if any(len(o) < 15 for o in clean_opts):
            return None
        stems = [
            f"Which of the following statements about '{topic}' is correct?",
            f"Identify the statement that is true regarding '{topic}':",
            f"Which statement about '{topic}' is accurate?",
            f"Select the correct statement related to '{topic}':",
        ]
        return {
            "question": stems[idx % len(stems)],
            "options": clean_opts,
            "correctIndex": pos,
        }
    return None


def _finalize_questions(questions):
    """Post-process a question set: trim options, enforce 4 unique options,
    drop empty/duplicate stems, and never emit a question whose correct option
    is not one of its own options."""
    out = []
    seen_stems = set()
    for q in questions:
        stem = _clean_option(q.get('question', ''))
        if len(stem) < 10:
            continue
        key = _normalize_key(stem)
        if key in seen_stems:
            continue
        opts = []
        for o in (q.get('options') or [])[:4]:
            oc = _clean_option(o)
            if oc and all(oc.lower() != x.lower() for x in opts):
                opts.append(oc)
        if len(opts) < 4:
            continue
        seen_stems.add(key)
        out.append({
            "question": stem,
            "options": opts,
            "correctIndex": min(max(0, int(q.get('correctIndex', 0))), 3),
            "approved": 0,
            "explanation": q.get('explanation', ''),
        })
    return out


def _synthesize_doc_questions(text_content, count, title):
    """Generate up to `count` document-grounded MCQs from the actual uploaded text content."""
    chunks = _split_doc_chunks(text_content)
    if len(chunks) < 4:
        sentences = []
        for para in _merge_wrapped_lines(text_content):
            sentences.extend(s.strip() for s in re.split(r'(?<=[.!?])\s+', para) if len(s.strip()) >= 15)
        seen, chunks = set(), []
        for s in sentences:
            key = _normalize_key(s)
            if key and key not in seen:
                seen.add(key)
                chunks.append(s)
    # Bound the chunk set with a balanced sample (start/middle/end). A multi-MB PDF
    # can yield 30k+ clauses; scanning all of them for distractors per question is
    # O(n^2) and stalls the generator (57s+ observed live). Sampling ~400 chunks
    # keeps every section represented while keeping generation instant.
    if len(chunks) > 400:
        step = max(1, len(chunks) // 400)
        chunks = chunks[::step][:400]
    questions = []
    seen_keys = set()
    for i in range(len(chunks)):
        if len(questions) >= count:
            break
        q = _mcq_from_chunk(chunks[i], chunks, i)
        if not q:
            continue
        # Dedupe on the question STEM only — two questions that ask the same thing
        # are duplicates even if their option lists differ.
        key = _normalize_key(q['question'])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        q['approved'] = 0
        questions.append(q)
    return _finalize_questions(questions)


def _pad_to_count(questions, count, text_content, title):
    """Pad a short question list with unique document-grounded questions."""
    result = list(questions)
    seen = {_normalize_key(q['question']) for q in result}
    for eq in _synthesize_doc_questions(text_content, count, title):
        if len(result) >= count:
            break
        key = _normalize_key(eq['question'])
        if key not in seen:
            seen.add(key)
            result.append(eq)
    return result


@app.route('/api/modules/generate', methods=['POST'])
def generate_module():
    try:
        count = int(request.form.get('count', 15))
        title = request.form.get('title', 'Product Refresher Policy').strip()
        trainer_id = request.form.get('trainer_id', 'ADMIN').strip()
        difficulty = request.form.get('difficulty', 'Medium').strip()
        gen_language = request.form.get('language', 'English').strip()
        
        text_content = ""
        
        # 1. Parse uploaded PDF safely
        if 'file' in request.files:
            file = request.files['file']
            if file and file.filename != '':
                filename = secure_filename(file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)
                
                try:
                    # Robust pypdf text extraction engine
                    from pypdf import PdfReader
                    reader = PdfReader(filepath)
                    pages = list(reader.pages)
                    # Bound extraction: sample pages evenly across the document so
                    # very large PDFs (1000+ pages) don't stall generation for tens
                    # of seconds, while every section stays represented.
                    MAX_EXTRACT_PAGES = 60
                    if len(pages) > MAX_EXTRACT_PAGES:
                        step = len(pages) / MAX_EXTRACT_PAGES
                        indices = sorted({int(i * step) for i in range(MAX_EXTRACT_PAGES)})
                        pages = [pages[i] for i in indices]
                    extracted_pages = []
                    for page in pages:
                        txt = page.extract_text()
                        if txt:
                            extracted_pages.append(txt)
                    if extracted_pages:
                        text_content = "\n".join(extracted_pages).strip()
                        print(f"Successfully extracted {len(text_content)} chars across {len(pages)}/{len(reader.pages)} PDF pages!")
                except Exception as e_pdf:
                    print(f"pypdf extraction warning: {e_pdf}")
                    try:
                        # Fallback simple text read if UTF-8 plain text file uploaded as PDF
                        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f_txt:
                            text_content = f_txt.read().strip()
                    except Exception as e_txt:
                        print(f"Fallback text read warning: {e_txt}")
                finally:
                    # Never leave uploaded files on disk (privacy + prevents stale-file reuse)
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
                    
        file_provided = 'file' in request.files and request.files['file'].filename != ''
        text_from_form = request.form.get('text', '').strip()
        
        # The uploaded file is the single source of truth when one is provided.
        # Pasted text from an earlier session must NEVER leak into a new file-based
        # generation (stale-state fix).
        if file_provided:
            if len(text_content) < 100:
                return jsonify({"status": "error", "message": "Could not extract readable text from the PDF (it may be scanned or image-based). Please paste the training text instead."}), 400
        else:
            if text_from_form:
                text_content = text_from_form
            else:
                return jsonify({"status": "error", "message": "Please upload a PDF or paste training text."}), 400
            
        extracted_chars = len(text_content)
            
        # 2. Try Gemini REST API (Standard Library urllib - Zero extra packages needed)
        api_key = os.environ.get("GEMINI_API_KEY", "")
        generated_questions = []
        gemini_success = False
        
        if api_key:
            try:
                import urllib.request
                clean_text = sanitize_llm_text(_balanced_sample(text_content, 6000))
                prompt = f"""
                You are an expert assessment engine and senior Socratic Trainer building formal, exam-grade certification questions.
                Analyze the provided training document and generate exactly {count} high-quality Multiple Choice Questions (MCQs) in {gen_language} language at {difficulty} difficulty.

                QUALITY RULES (mandatory):
                1. Every question must test a specific fact, procedure, requirement, or number from the provided text. Never invent facts, figures, dates, or names that are not in the document.
                2. Write professional, realistic stems that a manager or assessor would set in a real exam. Avoid trivial, childish, or "first-class student" phrasing. Do not start stems with "According to the document..." — vary the wording naturally.
                3. Options must be plausible and concise (under 20 words). Distractors must be realistic but clearly incorrect. No option may repeat or paraphrase another option.
                4. Exactly 4 distinct options per question, and exactly one correct answer marked via correct_answer_index (0 to 3).
                5. Mix question types: definitions, procedures, numeric requirements, do's/don'ts, consequences, and scenario-based judgment.
                6. Include a 1-sentence explanation per question that cites the fact from the document.
                7. Respond with clean plain text: no markdown fences, no LaTeX, no HTML entities, no unescaped special characters.

                Format your response STRICTLY as a JSON object matching this schema:
                {{
                  "module_title": "{title}",
                  "questions": [
                    {{
                      "id": 1,
                      "question_text": "What is the primary compliance requirement before approval?",
                      "options": ["Mandatory Document Verification and KYC", "Oral confirmation from the customer", "Post-disbursement review only", "Waived for repeat customers"],
                      "correct_answer_index": 0,
                      "explanation": "Document verification and KYC is required prior to credit approval."
                    }}
                  ]
                }}

                TRAINING DOCUMENT TEXT:
                {clean_text}
                """
                
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
                headers = {"Content-Type": "application/json"}
                body_bytes = json.dumps({
                    "contents": [{"parts": [{"text": prompt}]}]
                }).encode('utf-8')
                
                req = urllib.request.Request(url, data=body_bytes, headers=headers, method='POST')
                with urllib.request.urlopen(req, timeout=15) as response:
                    res_json = json.loads(response.read().decode('utf-8'))
                    res_text = res_json['candidates'][0]['content']['parts'][0]['text'].strip()
                    
                    res_text = re.sub(r'^```(?:json)?\s*|\s*```$', '', res_text.strip())
                        
                    parsed_obj = json.loads(res_text)
                    raw_qs = parsed_obj.get('questions', []) if isinstance(parsed_obj, dict) else (parsed_obj if isinstance(parsed_obj, list) else [])
                    
                    if raw_qs and len(raw_qs) > 0:
                        cleaned_qs = []
                        for idx, q in enumerate(raw_qs):
                            if not isinstance(q, dict):
                                continue
                            q_txt = sanitize_llm_text(str(q.get('question_text') or q.get('question', '')).strip())
                            raw_opts = q.get('options', [])
                            opts = [sanitize_llm_text(str(opt)).strip() for opt in raw_opts] if isinstance(raw_opts, list) and len(raw_opts) >= 4 else ['Option A', 'Option B', 'Option C', 'Option D']
                            try:
                                corr_idx = int(q.get('correct_answer_index') if q.get('correct_answer_index') is not None else q.get('correctIndex', 0))
                            except (ValueError, TypeError):
                                corr_idx = 0
                            expl = sanitize_llm_text(str(q.get('explanation', '')).strip())
                            
                            cleaned_qs.append({
                                "id": idx + 1,
                                "question": q_txt,
                                "options": opts[:4],
                                "correctIndex": min(max(0, corr_idx), 3),
                                "explanation": expl,
                                "approved": 0
                            })
                        # Enforce the full requested count: short AI output is padded
                        # with additional document-grounded questions instead of
                        # silently returning 3-4 questions.
                        if len(cleaned_qs) < count:
                            cleaned_qs = _pad_to_count(cleaned_qs, count, text_content, title)
                        generated_questions = cleaned_qs
                        gemini_success = True
            except Exception as e_gemini:
                print(f"Gemini REST API notice: {e_gemini}")
                
        # 3. Document-grounded Socratic Question Synthesizer (no external AI needed)
        # Every question and every option is derived from the actual uploaded text
        # content — never from a static pool, the file name, or metadata.
        if not gemini_success:
            generated_questions = _synthesize_doc_questions(text_content, count, title)
            if not generated_questions:
                return jsonify({"status": "error", "message": "Could not generate questions from the provided content. The text may be too short or contain no usable facts — please upload a document with at least 3-4 distinct statements."}), 400

        # Final QA pass (both paths): trim/normalise options, enforce 4 unique
        # options per question, drop duplicate or empty stems.
        generated_questions = _finalize_questions(generated_questions)
        if len(generated_questions) < count:
            generated_questions = _pad_to_count(generated_questions, count, text_content, title)
            generated_questions = _finalize_questions(generated_questions)
        if not generated_questions:
            return jsonify({"status": "error", "message": "Could not generate usable questions from the provided content. Please upload a document with more factual detail."}), 400

        return jsonify({
            "status": "success",
            "title": title,
            "difficulty": difficulty,
            "language": gen_language,
            "count": len(generated_questions),
            "extracted_chars": extracted_chars,
            "questions": generated_questions
        })
    except Exception as err_main:
        print(f"Error generating module: {str(err_main)}")
        return jsonify({"status": "error", "message": f"Generation error: {str(err_main)}"}), 500

@app.route('/api/modules/save', methods=['POST'])
def save_module():
    data = request.json or {}
    title = str(data.get('title', 'AI Generated Module')).strip()
    trainer_id = str(data.get('trainer_id', 'ADMIN')).strip()
    difficulty = str(data.get('difficulty', 'Medium')).strip()
    audited_by = str(data.get('audited_by', 'Super Admin')).strip()
    source_text = str(data.get('source_text', '')).strip()
    time_limit = int(data.get('time_limit_minutes', 15))
    pass_pct = int(data.get('pass_percentage', 70))
    anti_cheat = int(data.get('enable_anti_cheat', 1))
    shuffle_q = int(data.get('shuffle_questions', 1))
    shuffle_opt = int(data.get('shuffle_options', 1))
    questions = data.get('questions', [])
    module_id = data.get('module_id')
    
    if not questions:
        return jsonify({"status": "error", "message": "No questions provided to save."}), 400
        
    # Pre-validation: every question must have a stem, 4 non-empty and 4 DISTINCT
    # options, and the module must not contain duplicate question stems.
    for qi, q in enumerate(questions):
        if not isinstance(q, dict):
            return jsonify({"status": "error", "message": f"Question {qi + 1} is not a valid question object."}), 400
        q_txt = str(q.get('question_text') or q.get('question', '')).strip()
        if not q_txt:
            return jsonify({"status": "error", "message": f"Question {qi + 1} has an empty question text."}), 400
        opts_arr = q.get('options') if isinstance(q.get('options'), list) and len(q.get('options')) >= 4 else None
        opts = [
            str(q.get('option_a') or (opts_arr[0] if opts_arr else '')).strip(),
            str(q.get('option_b') or (opts_arr[1] if opts_arr else '')).strip(),
            str(q.get('option_c') or (opts_arr[2] if opts_arr else '')).strip(),
            str(q.get('option_d') or (opts_arr[3] if opts_arr else '')).strip(),
        ]
        if any(not o for o in opts):
            return jsonify({"status": "error", "message": f"Question {qi + 1} has an empty option — all 4 options are required."}), 400
        if len(set(o.lower() for o in opts)) < 4:
            return jsonify({"status": "error", "message": f"Question {qi + 1} has duplicate options — all 4 options must be distinct."}), 400
    stems_seen = set()
    for qi, q in enumerate(questions):
        stem_key = _normalize_key(str(q.get('question_text') or q.get('question', '')).strip())
        if stem_key in stems_seen:
            return jsonify({"status": "error", "message": f"Duplicate question detected (Question {qi + 1}) — every question must be unique."}), 400
        stems_seen.add(stem_key)
        
    all_approved = all([int(q.get('approved', 0)) == 1 for q in questions])
    status = 'Ready' if all_approved else 'Pending Audit'
    
    conn = get_db_connection()
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d")
        cursor = conn.cursor()
        
        if module_id:
            cursor.execute(
                "UPDATE modules SET title=?, questions_count=?, status=?, difficulty=?, audited_by=?, source_text=?, time_limit_minutes=?, pass_percentage=?, enable_anti_cheat=?, shuffle_questions=?, shuffle_options=? WHERE id=?",
                (title, len(questions), status, difficulty, audited_by, source_text, time_limit, pass_pct, anti_cheat, shuffle_q, shuffle_opt, module_id)
            )
            cursor.execute("DELETE FROM questions WHERE module_id=?", (module_id,))
        else:
            cursor.execute(
                "INSERT INTO modules (title, questions_count, created_at, status, created_by, difficulty, audited_by, source_text, time_limit_minutes, pass_percentage, enable_anti_cheat, shuffle_questions, shuffle_options) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, len(questions), now, status, trainer_id, difficulty, audited_by, source_text, time_limit, pass_pct, anti_cheat, shuffle_q, shuffle_opt)
            )
            module_id = cursor.lastrowid
            
        for q in questions:
            q_text = str(q.get('question_text') or q.get('question', '')).strip()
            q_type = str(q.get('question_type', 'mcq_single')).strip()
            pts_wt = float(q.get('points_weight', 1.0))
            neg_pts = float(q.get('negative_points', 0.0))
            media = str(q.get('media_url', '')).strip()
            match_json = json.dumps(q.get('matching_pairs', [])) if isinstance(q.get('matching_pairs'), list) else str(q.get('matching_pairs', ''))
            
            opts_arr = q.get('options') if isinstance(q.get('options'), list) and len(q.get('options')) >= 4 else None
            opt_a = str(q.get('option_a') or (opts_arr[0] if opts_arr else 'Option A')).strip()
            opt_b = str(q.get('option_b') or (opts_arr[1] if opts_arr else 'Option B')).strip()
            opt_c = str(q.get('option_c') or (opts_arr[2] if opts_arr else 'Option C')).strip()
            opt_d = str(q.get('option_d') or (opts_arr[3] if opts_arr else 'Option D')).strip()
            
            corr_idx = int(q.get('correct_index') if q.get('correct_index') is not None else q.get('correctIndex', 0))
            appr_val = int(q.get('approved', 0))
            
            cursor.execute(
                "INSERT INTO questions (module_id, question_text, option_a, option_b, option_c, option_d, correct_index, approved, question_type, points_weight, negative_points, media_url, matching_pairs) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (module_id, q_text, opt_a, opt_b, opt_c, opt_d, corr_idx, appr_val, q_type, pts_wt, neg_pts, media, match_json)
            )
            
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"status": "error", "message": f"Failed to save module: {str(e)}"}), 500
        
    conn.close()
    return jsonify({
        "status": "success", 
        "module_id": module_id, 
        "module_status": status,
        "message": f"Module '{title}' saved successfully as {status}!"
    })

# 5. ASSESSMENT SUBMISSION & DYNAMIC ANALYTICS
@app.route('/api/assessments/submit', methods=['POST'])
def submit_assessment():
    data = request.json or {}
    emp_code = str(data.get('emp_code', '')).upper()
    module_id = data.get('module_id')
    assignment_day = str(data.get('assignment_day', 'zero day')).upper()
    
    conn = get_db_connection()
    try:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        
        # Authoritative per-answer counts sent by the examinee client
        correct_count = data.get('correct_count')
        wrong_count = data.get('wrong_count')
        test_type = str(data.get('test_type', '')).lower()
        tab_switch_count = int(data.get('tab_switch_count', 0) or 0)
        time_taken_seconds = int(data.get('time_taken_seconds', 0) or 0)
        
        # Compute score from correct/wrong counts when provided
        computed_score = None
        if correct_count is not None or wrong_count is not None:
            answered = int(correct_count or 0) + int(wrong_count or 0)
            computed_score = round((int(correct_count or 0) / answered) * 100, 1) if answered > 0 else 0.0
        
        # Backward-compat: accept direct pre_test_score / post_test_score values when present
        pre_test_score = data.get('pre_test_score')
        post_test_score = data.get('post_test_score')
        if test_type == 'pre':
            if pre_test_score is None:
                pre_test_score = computed_score if computed_score is not None else 0.0
        elif test_type == 'post':
            if post_test_score is None:
                post_test_score = computed_score if computed_score is not None else 0.0
        elif pre_test_score is None and post_test_score is None:
            pre_test_score = computed_score if computed_score is not None else 0.0
        
        # Passing threshold from module config (default 70)
        pass_pct = 70
        if module_id is not None:
            mod_row = conn.execute("SELECT pass_percentage FROM modules WHERE id=?", (module_id,)).fetchone()
            if mod_row and mod_row['pass_percentage'] is not None:
                pass_pct = int(mod_row['pass_percentage'])
        
        score_ref = post_test_score if post_test_score is not None else (pre_test_score if pre_test_score is not None else 0.0)
        passed_status = 1 if (score_ref is not None and float(score_ref) >= pass_pct) else 0
        
        # --- History-safe persistence -----------------------------------------
        # Key results on the training OCCURRENCE (session_id), not a bare
        # (emp, module, day) triple, so a January training and an April training
        # for the same trainee/module each get their own append-only row.
        session_id = str(data.get('session_id') or '').strip() or None
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        if not session_id:
            session_id = f"WEB-{today_str}"
        training_date = today_str
        trainer_id = None
        ts_row = conn.execute("SELECT trainer_id, date FROM training_sessions WHERE session_id=?", (session_id,)).fetchone()
        if ts_row:
            trainer_id = ts_row['trainer_id']
            if ts_row['date']:
                training_date = ts_row['date']
        # Snapshot the employee's org context at save time so historical reports
        # keep the branch/zone/division the trainee belonged to for that training.
        emp_ctx = conn.execute("SELECT zone, division, business_unit, branch_name FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
        snap = {
            'zone': emp_ctx['zone'] if emp_ctx else None,
            'division': emp_ctx['division'] if emp_ctx else None,
            'business_unit': emp_ctx['business_unit'] if emp_ctx else None,
            'branch_name': emp_ctx['branch_name'] if emp_ctx else None,
        }

        result_id = None
        row = conn.execute("SELECT * FROM assessment_results WHERE emp_code=? AND module_id=? AND session_id=? AND assignment_day=?", 
                           (emp_code, module_id, session_id, assignment_day)).fetchone()
        if row:
            updates = ["completed_at=?"]
            params = [now_str]
            if pre_test_score is not None:
                updates.append("pre_test_score=?")
                params.append(pre_test_score)
            if post_test_score is not None:
                updates.append("post_test_score=?")
                params.append(post_test_score)
            updates.append("tab_switch_count=?")
            params.append(tab_switch_count)
            updates.append("time_taken_seconds=?")
            params.append(time_taken_seconds)
            if post_test_score is not None:
                updates.append("passed_status=?")
                params.append(passed_status)
                cert_id = data.get('certificate_id') or row['certificate_id']
                if cert_id:
                    updates.append("certificate_id=?")
                    params.append(cert_id)
            params.append(row['id'])
            conn.execute(f"UPDATE assessment_results SET {', '.join(updates)} WHERE id=?", params)
            result_id = row['id']
        else:
            p_val = pre_test_score if pre_test_score is not None else 0.0
            post_val = post_test_score if post_test_score is not None else 0.0
            cert_id = data.get('certificate_id')
            cur = conn.execute("""INSERT INTO assessment_results
                (emp_code, module_id, assignment_day, session_id, training_date, trainer_id,
                 zone, division, business_unit, branch_name,
                 pre_test_score, post_test_score, completed_at, tab_switch_count, time_taken_seconds, passed_status, certificate_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                               (emp_code, module_id, assignment_day, session_id, training_date, trainer_id,
                                snap['zone'], snap['division'], snap['business_unit'], snap['branch_name'],
                                p_val, post_val, now_str, tab_switch_count, time_taken_seconds, passed_status, cert_id))
            result_id = cur.lastrowid
        conn.commit()
        
        # Generate a certificate id for passed post-tests that don't have one yet
        final_cert = data.get('certificate_id')
        if final_cert is None and test_type == 'post' and passed_status == 1 and result_id:
            r2 = conn.execute("SELECT certificate_id FROM assessment_results WHERE id=?", (result_id,)).fetchone()
            if r2 and r2['certificate_id']:
                final_cert = r2['certificate_id']
            else:
                from uuid import uuid4
                final_cert = f"SRC-{emp_code}-{module_id}-{assignment_day}-{uuid4().hex[:8]}"
                conn.execute("UPDATE assessment_results SET certificate_id=? WHERE id=?", (final_cert, result_id))
                conn.commit()
        
        conn.close()
        return jsonify({
            "status": "success",
            "message": "Assessment score saved successfully!",
            "score": score_ref,
            "passed_status": passed_status,
            "certificate_id": final_cert
        })
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": f"Failed to save score: {str(e)}"}), 500

@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    conn = get_db_connection()
    try:
        where_sql, params = _analytics_where(request.args)
        # Always keep a non-empty WHERE so later scope clauses can append with
        # plain "AND ..." — otherwise an empty where_sql would push the scope
        # conditions into the LEFT JOIN's ON clause and silently bypass filtering.
        if not where_sql:
            where_sql = " WHERE 1=1"

        # Server-side access control: non-global roles only see their assigned
        # zone/division/branch/business-unit scope.
        e_scope_sql = ""       # conditions for queries aliased on `e`
        emp_scope_sql = ""     # conditions for bare `employees` queries
        e_scope_params = []
        emp_scope_params = []
        _user = _session_user()
        if _user and not _is_global_role(_user.get('role', '')):
            _scope = _trainer_scope(_user.get('trainer_id'))
            if _scope:
                parts_e, parts_emp = [], []
                if _scope.get('zones'):
                    parts_e.append("UPPER(TRIM(e.zone)) IN ({})".format(','.join('?' * len(_scope['zones']))))
                    e_scope_params = _scope['zones']
                    parts_emp.append("UPPER(TRIM(zone)) IN ({})".format(','.join('?' * len(_scope['zones']))))
                    emp_scope_params = _scope['zones']
                if _scope.get('divisions'):
                    parts_e.append("UPPER(TRIM(e.division)) IN ({})".format(','.join('?' * len(_scope['divisions']))))
                    e_scope_params = e_scope_params + _scope['divisions']
                    parts_emp.append("UPPER(TRIM(division)) IN ({})".format(','.join('?' * len(_scope['divisions']))))
                    emp_scope_params = emp_scope_params + _scope['divisions']
                if _scope.get('branches'):
                    parts_e.append("UPPER(TRIM(e.branch_name)) IN ({})".format(','.join('?' * len(_scope['branches']))))
                    e_scope_params = e_scope_params + _scope['branches']
                    parts_emp.append("UPPER(TRIM(branch_name)) IN ({})".format(','.join('?' * len(_scope['branches']))))
                    emp_scope_params = emp_scope_params + _scope['branches']
                if _scope.get('business_units'):
                    parts_e.append("UPPER(TRIM(e.business_unit)) IN ({})".format(','.join('?' * len(_scope['business_units']))))
                    e_scope_params = e_scope_params + _scope['business_units']
                    parts_emp.append("UPPER(TRIM(business_unit)) IN ({})".format(','.join('?' * len(_scope['business_units']))))
                    emp_scope_params = emp_scope_params + _scope['business_units']
                if parts_e:
                    where_sql += " AND " + " AND ".join(parts_e)
                    params.extend(e_scope_params)
                if parts_emp:
                    emp_scope_sql = " AND " + " AND ".join(parts_emp)

        base = "FROM assessment_results a LEFT JOIN employees e ON a.emp_code = e.emp_code"

        # 1) Temporal learning progression (avg pre/post per assignment milestone)
        results = conn.execute(f"""
            SELECT a.assignment_day,
                   AVG(a.pre_test_score) as avg_pre,
                   AVG(a.post_test_score) as avg_post,
                   COUNT(DISTINCT a.emp_code) as participants
            {base}
            {where_sql}
            GROUP BY a.assignment_day
        """, params).fetchall()

        # 2) Summary metrics (org penetration + role-wise distribution)
        summary_row = conn.execute(f"""
            SELECT COUNT(DISTINCT e.branch_name) as branches_count,
                   COUNT(DISTINCT a.emp_code) as employees_count,
                   COUNT(*) as records_count,
                   AVG(a.post_test_score) as avg_post,
                   AVG(a.post_test_score - a.pre_test_score) as growth
            {base}
            {where_sql}
        """, params).fetchone()
        role_rows = conn.execute(f"""
            SELECT e.role as role, COUNT(DISTINCT a.emp_code) as cnt
            {base}
            {where_sql}
            GROUP BY e.role ORDER BY cnt DESC
        """, params).fetchall()

        # 3) Score distribution buckets (latest assessment per employee by completion time)
        dist_where = where_sql + ((" AND " if where_sql else " WHERE ") + "a.post_test_score IS NOT NULL")
        dist_rows = conn.execute(f"""
            SELECT emp_code, emp_name, branch_name, division, zone, business_unit, post_test_score
            FROM (
                SELECT a.emp_code, e.emp_name, e.branch_name, e.division, e.zone, e.business_unit,
                       a.post_test_score,
                       ROW_NUMBER() OVER (
                           PARTITION BY a.emp_code
                           ORDER BY a.completed_at DESC, a.id DESC
                       ) rn
                {base}
                {dist_where}
            )
            WHERE rn = 1
            ORDER BY post_test_score DESC
        """, params).fetchall()

        score_distribution = {'below_60': [], '60_80': [], 'above_80': []}
        for d in dist_rows:
            emp = {
                "emp_code": d['emp_code'],
                "emp_name": d['emp_name'] or d['emp_code'],
                "business_unit": d['business_unit'] or '',
                "branch_name": d['branch_name'] or '',
                "post_test_score": round(d['post_test_score'] or 0, 1)
            }
            s = d['post_test_score'] or 0
            if s < 60:
                score_distribution['below_60'].append(emp)
            elif s < 80:
                score_distribution['60_80'].append(emp)
            else:
                score_distribution['above_80'].append(emp)

        # 4) Hierarchical drill-down breakdown (zone -> division -> branch -> executive)
        sel_zone = request.args.get('zone', '').strip()
        sel_div = request.args.get('division', '').strip()
        sel_branch = request.args.get('branch', '').strip()
        if sel_branch:
            dim, name_col, label = "a.emp_code", "e.emp_name", "executive"
        elif sel_div:
            dim, name_col, label = "TRIM(e.branch_name)", "TRIM(e.branch_name)", "branch"
        elif sel_zone:
            dim, name_col, label = "TRIM(e.division)", "TRIM(e.division)", "division"
        else:
            dim, name_col, label = "TRIM(e.zone)", "TRIM(e.zone)", "zone"
        if label == "executive":
            dim_where = where_sql + ((" AND " if where_sql else " WHERE ") + "a.emp_code IS NOT NULL")
        else:
            dim_where = where_sql + ((" AND " if where_sql else " WHERE ") + f"{dim} IS NOT NULL AND {dim} != ''")
        breakdown_rows = conn.execute(f"""
            SELECT {dim} as id, {name_col} as name,
                   AVG(a.pre_test_score) as pre,
                   AVG(a.post_test_score) as post,
                   AVG(a.post_test_score - a.pre_test_score) as growth,
                   COUNT(DISTINCT a.emp_code) as count
            {base}
            {dim_where}
            GROUP BY {dim}, {name_col}
            ORDER BY growth DESC, count DESC
        """, params).fetchall()
        breakdown = [
            {
                "id": b['id'], "name": b['name'] or b['id'],
                "pre": round(b['pre'] or 0, 1), "post": round(b['post'] or 0, 1),
                "growth": round(b['growth'] or 0, 1), "count": b['count']
            }
            for b in breakdown_rows
        ]

        # 5) Critical branch pain areas (avg post < 60% OR learning growth < 15%)
        pain_rows = conn.execute(f"""
            SELECT TRIM(e.branch_name) as branch_name,
                   AVG(a.pre_test_score) as pre,
                   AVG(a.post_test_score) as post,
                   AVG(a.post_test_score - a.pre_test_score) as growth,
                   COUNT(DISTINCT a.emp_code) as count
            {base}
            {where_sql + ((" AND " if where_sql else " WHERE ") + "TRIM(e.branch_name) != ''")}
            GROUP BY TRIM(e.branch_name)
            HAVING AVG(a.post_test_score) < 60 OR AVG(a.post_test_score - a.pre_test_score) < 15
            ORDER BY AVG(a.post_test_score) ASC
        """, params).fetchall()
        critical_pain_areas = [
            {
                "branch_name": p['branch_name'],
                "pre": round(p['pre'] or 0, 1), "post": round(p['post'] or 0, 1),
                "growth": round(p['growth'] or 0, 1), "count": p['count']
            }
            for p in pain_rows
        ]

        # 6) AI module usage & effectiveness
        module_rows = conn.execute(f"""
            SELECT a.module_id, m.title,
                   COUNT(DISTINCT a.emp_code) as participants,
                   AVG(a.pre_test_score) as avg_pre,
                   AVG(a.post_test_score) as avg_post,
                   AVG(a.time_taken_seconds) as avg_time,
                   ROUND(100.0 * SUM(CASE WHEN a.post_test_score >= COALESCE(m.pass_percentage, 60) THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) as pass_rate
            {base}
            LEFT JOIN modules m ON a.module_id = m.id
            {where_sql}
            GROUP BY a.module_id
            ORDER BY participants DESC
        """, params).fetchall()
        module_usage = [
            {
                "module_id": m['module_id'],
                "title": m['title'] or f"Module #{m['module_id']}",
                "participants": m['participants'],
                "avg_pre": round(m['avg_pre'] or 0, 1),
                "avg_post": round(m['avg_post'] or 0, 1),
                "avg_time_seconds": round(m['avg_time'] or 0, 0),
                "pass_rate": m['pass_rate'] or 0.0
            }
            for m in module_rows
        ]

        # Filter options for cascading dropdowns (full roster, independent of active filters)
        opt_where = " WHERE 1=1" + emp_scope_sql
        opt_params = list(emp_scope_params)
        zones = [r[0].strip() for r in conn.execute("SELECT DISTINCT TRIM(zone) FROM employees" + opt_where + " AND zone IS NOT NULL AND TRIM(zone) != '' ORDER BY zone", opt_params).fetchall()]
        divisions = [
            {"name": row[0].strip(), "zone": (row[1] or '').strip()}
            for row in conn.execute("SELECT DISTINCT TRIM(division), TRIM(zone) FROM employees" + opt_where + " AND division IS NOT NULL AND TRIM(division) != '' ORDER BY division", opt_params).fetchall()
        ]
        branches = [
            {"name": row[0].strip(), "division": (row[1] or '').strip(), "zone": (row[2] or '').strip()}
            for row in conn.execute("SELECT DISTINCT TRIM(branch_name), TRIM(division), TRIM(zone) FROM employees" + opt_where + " AND branch_name IS NOT NULL AND TRIM(branch_name) != '' ORDER BY branch_name", opt_params).fetchall()
        ]
        executives = [
            {"code": row[0], "name": row[1], "branch": (row[2] or ''), "division": (row[3] or ''), "zone": (row[4] or '')}
            for row in conn.execute("SELECT emp_code, emp_name, branch_name, division, zone FROM employees" + opt_where + " ORDER BY emp_name", opt_params).fetchall()
        ]
        business_units = [r[0].strip() for r in conn.execute("SELECT DISTINCT TRIM(business_unit) FROM employees" + opt_where + " AND business_unit IS NOT NULL AND TRIM(business_unit) != '' ORDER BY business_unit", opt_params).fetchall()]
        products = [r[0].strip() for r in conn.execute("SELECT DISTINCT TRIM(product_name) FROM employees" + opt_where + " AND product_name IS NOT NULL AND TRIM(product_name) != '' ORDER BY product_name", opt_params).fetchall()]
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500
    conn.close()

    # Normalize assignment_day labels to canonical milestones so charts render correctly.
    def _norm_day(day):
        token = re.sub(r'[^0-9A-Z]', '', str(day).upper())
        if 'TWENTY' in token or '20' in token:
            return 'TWENTY DAYS'
        if 'SIX' in token or '6' in token:
            return 'SIX DAYS'
        if 'ZERO' in token or '0' in token:
            return 'ZERO DAY'
        return str(day).upper()

    defaults = {'pre': 0.0, 'post': 0.0, 'count': 0}
    payload = {
        'ZERO DAY': dict(defaults),
        'SIX DAYS': dict(defaults),
        'TWENTY DAYS': dict(defaults)
    }
    for r in results:
        day = _norm_day(r['assignment_day'])
        entry = payload.setdefault(day, dict(defaults))
        entry['pre'] = round(r['avg_pre'] or 0.0, 1)
        entry['post'] = round(r['avg_post'] or 0.0, 1)
        entry['count'] = r['participants']

    summary_metrics = {
        "branches_count": summary_row['branches_count'] or 0,
        "employees_count": summary_row['employees_count'] or 0,
        "records_count": summary_row['records_count'] or 0,
        "avg_post": round(summary_row['avg_post'] or 0.0, 1),
        "growth": round(summary_row['growth'] or 0.0, 1),
        "role_wise": {r['role'] or 'UNASSIGNED': r['cnt'] for r in role_rows}
    }
    has_live_data = (summary_row['records_count'] or 0) > 0

    return jsonify({
        "status": "success",
        "temporal": payload,
        "summary_metrics": summary_metrics,
        "score_distribution": score_distribution,
        "breakdown": breakdown,
        "critical_pain_areas": critical_pain_areas,
        "topic_knowledge_gaps": [],
        "module_usage": module_usage,
        "filter_options": {
            "zones": zones,
            "divisions": divisions,
            "branches": branches,
            "executives": executives,
            "business_units": business_units,
            "products": products
        },
        "has_live_data": has_live_data
    })

def _analytics_where(args):
    conditions = []
    params = []
    zone = args.get('zone', '').strip()
    division = args.get('division', '').strip()
    branch = args.get('branch', '').strip()
    emp_code = args.get('emp_code', '').strip()
    business_unit = args.get('business_unit', '').strip()
    product_name = args.get('product_name', '').strip()
    start_date = args.get('start_date', '').strip()
    end_date = args.get('end_date', '').strip()
    
    if zone:
        conditions.append("UPPER(TRIM(e.zone)) = UPPER(TRIM(?))")
        params.append(zone)
    if division:
        conditions.append("UPPER(TRIM(e.division)) = UPPER(TRIM(?))")
        params.append(division)
    if branch:
        conditions.append("UPPER(TRIM(e.branch_name)) = UPPER(TRIM(?))")
        params.append(branch)
    if emp_code:
        conditions.append("UPPER(TRIM(a.emp_code)) = UPPER(TRIM(?))")
        params.append(emp_code)
    if business_unit:
        conditions.append("UPPER(TRIM(e.business_unit)) = UPPER(TRIM(?))")
        params.append(business_unit)
    if product_name:
        conditions.append("UPPER(TRIM(e.product_name)) = UPPER(TRIM(?))")
        params.append(product_name)
    if start_date:
        conditions.append("a.completed_at >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("a.completed_at <= ?")
        params.append(end_date + " 23:59")
    
    where_sql = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    return where_sql, params

@app.route('/api/analytics/history', methods=['GET'])
def analytics_history():
    """Historical training + test tracking (append-only).

    Every training occurrence is its own row in assessment_results (keyed by
    session_id), so a trainee's January and April trainings are both preserved.
    This endpoint exposes:
      - agent_history:  full chronological history for one trainee
      - agent_growth:   first vs latest post-test per trainee (growth delta)
      - period_aggregates: avg pre/post/growth by period (month/quarter) x dimension
      - module_trends:  avg post-test across repeated exposures to the same module
    Filters: emp_code, module_id, trainer_id, zone, division, branch,
             business_unit, product_name, start_date, end_date,
             group_by (zone|division|business_unit|branch_name|module), period (month|quarter).
    """
    conn = get_db_connection()
    try:
        args = request.args
        group_by = (args.get('group_by') or 'branch_name').strip()
        period = (args.get('period') or 'month').strip()
        if group_by not in ('zone', 'division', 'business_unit', 'branch_name', 'module'):
            group_by = 'branch_name'
        if period not in ('month', 'quarter'):
            period = 'month'

        where_sql, base_params = _analytics_where(args)
        if not where_sql:
            where_sql = " WHERE 1=1"

        # Access control: non-global roles only see their assigned scope.
        scope_conds, scope_params = [], []
        _user = _session_user()
        if _user and not _is_global_role(_user.get('role', '')):
            _scope = _trainer_scope(_user.get('trainer_id'))
            if _scope:
                if _scope.get('zones'):
                    scope_conds.append("UPPER(TRIM(e.zone)) IN ({})".format(','.join('?' * len(_scope['zones']))))
                    scope_params.extend(_scope['zones'])
                if _scope.get('divisions'):
                    scope_conds.append("UPPER(TRIM(e.division)) IN ({})".format(','.join('?' * len(_scope['divisions']))))
                    scope_params.extend(_scope['divisions'])
                if _scope.get('branches'):
                    scope_conds.append("UPPER(TRIM(e.branch_name)) IN ({})".format(','.join('?' * len(_scope['branches']))))
                    scope_params.extend(_scope['branches'])
                if _scope.get('business_units'):
                    scope_conds.append("UPPER(TRIM(e.business_unit)) IN ({})".format(','.join('?' * len(_scope['business_units']))))
                    scope_params.extend(_scope['business_units'])

        # Extra dimension filters (bound AFTER scope params, matching SQL order).
        extra_conds, extra_params = [], []
        emp_code = args.get('emp_code', '').strip()
        module_id = args.get('module_id', '').strip()
        trainer_id = args.get('trainer_id', '').strip()
        if emp_code:
            extra_conds.append("UPPER(TRIM(a.emp_code)) = UPPER(TRIM(?))")
            extra_params.append(emp_code)
        if module_id:
            extra_conds.append("a.module_id = ?")
            extra_params.append(module_id)
        if trainer_id:
            extra_conds.append("UPPER(TRIM(a.trainer_id)) = UPPER(TRIM(?))")
            extra_params.append(trainer_id)

        conds = scope_conds + extra_conds
        if conds:
            where_sql += " AND " + " AND ".join(conds)
        params = base_params + scope_params + extra_params

        base = ("FROM assessment_results a "
                "LEFT JOIN employees e ON a.emp_code = e.emp_code "
                "LEFT JOIN modules m ON a.module_id = m.id "
                "LEFT JOIN trainers t ON a.trainer_id = t.trainer_id")

        # 1) Full chronological history for the selected trainee.
        agent_history = []
        if emp_code:
            rows = conn.execute(f"""
                SELECT a.id, a.emp_code, e.emp_name, a.module_id, m.title AS module_title,
                       a.training_date, a.session_id, a.assignment_day,
                       a.pre_test_score, a.post_test_score,
                       (a.post_test_score - a.pre_test_score) AS score_delta,
                       a.trainer_id, t.name AS trainer_name, a.completed_at,
                       a.passed_status, a.certificate_id
                {base}
                WHERE UPPER(TRIM(a.emp_code)) = UPPER(TRIM(?))
                ORDER BY a.training_date, a.id
            """, (emp_code,)).fetchall()
            agent_history = [dict(r) for r in rows]

        # 2) Per-trainee growth: first vs latest post-test.
        agent_growth = conn.execute(f"""
            SELECT x.emp_code, x.emp_name, x.first_date, x.latest_date,
                   (SELECT ar.post_test_score FROM assessment_results ar
                    WHERE ar.emp_code = x.emp_code AND ar.post_test_score IS NOT NULL
                    ORDER BY ar.training_date ASC, ar.id ASC LIMIT 1) AS first_post,
                   (SELECT ar.post_test_score FROM assessment_results ar
                    WHERE ar.emp_code = x.emp_code AND ar.post_test_score IS NOT NULL
                    ORDER BY ar.training_date DESC, ar.id DESC LIMIT 1) AS latest_post
            FROM (
                SELECT a.emp_code, e.emp_name, MIN(a.training_date) AS first_date, MAX(a.training_date) AS latest_date
                FROM assessment_results a
                LEFT JOIN employees e ON a.emp_code = e.emp_code
                {where_sql}
                AND a.post_test_score IS NOT NULL
                GROUP BY a.emp_code, e.emp_name
            ) x
            ORDER BY (x.emp_code) ASC
        """, params).fetchall()
        growth_out = []
        for g in agent_growth:
            fp = g['first_post']
            lp = g['latest_post']
            growth_out.append({
                "emp_code": g['emp_code'],
                "emp_name": g['emp_name'] or g['emp_code'],
                "first_date": g['first_date'],
                "latest_date": g['latest_date'],
                "first_post": round(fp, 1) if fp is not None else None,
                "latest_post": round(lp, 1) if lp is not None else None,
                "delta": round(lp - fp, 1) if (fp is not None and lp is not None) else None,
            })

        # 3) Period aggregation: avg pre/post/growth by month/quarter x dimension.
        if period == 'quarter':
            period_expr = ("printf('%04d-Q%01d', CAST(substr(a.training_date,1,4) AS INTEGER), "
                           "(CAST(substr(a.training_date,6,2) AS INTEGER)+2)/3)")
        else:
            period_expr = "substr(a.training_date,1,7)"
        dim_expr = {
            'zone': "COALESCE(NULLIF(TRIM(a.zone), ''), e.zone)",
            'division': "COALESCE(NULLIF(TRIM(a.division), ''), e.division)",
            'business_unit': "COALESCE(NULLIF(TRIM(a.business_unit), ''), e.business_unit)",
            'branch_name': "COALESCE(NULLIF(TRIM(a.branch_name), ''), e.branch_name)",
            'module': "COALESCE(m.title, CAST(a.module_id AS TEXT))",
        }[group_by]
        period_aggregates = conn.execute(f"""
            SELECT {dim_expr} AS dimension,
                   {period_expr} AS period,
                   COUNT(*) AS records,
                   COUNT(DISTINCT a.emp_code) AS employees,
                   AVG(a.pre_test_score) AS avg_pre,
                   AVG(a.post_test_score) AS avg_post,
                   AVG(CASE WHEN a.pre_test_score IS NOT NULL AND a.post_test_score IS NOT NULL
                            THEN a.post_test_score - a.pre_test_score END) AS avg_growth
            {base}
            {where_sql}
            AND a.training_date IS NOT NULL AND TRIM(a.training_date) != ''
            GROUP BY dimension, period
            ORDER BY period, dimension
        """, params).fetchall()
        period_out = [{
            "dimension": r['dimension'],
            "period": r['period'],
            "records": r['records'],
            "employees": r['employees'],
            "avg_pre": round(r['avg_pre'], 1) if r['avg_pre'] is not None else None,
            "avg_post": round(r['avg_post'], 1) if r['avg_post'] is not None else None,
            "avg_growth": round(r['avg_growth'], 1) if r['avg_growth'] is not None else None,
        } for r in period_aggregates]

        # 4) Module-wise trends across repeated exposures (attempt 1, 2, ...).
        module_trends = conn.execute(f"""
            SELECT module_id, title, exposure, ROUND(AVG(post_test_score), 1) AS avg_post, COUNT(*) AS attempts
            FROM (
                SELECT a.emp_code, a.module_id, m.title, a.post_test_score,
                       ROW_NUMBER() OVER (
                           PARTITION BY a.emp_code, a.module_id
                           ORDER BY a.training_date ASC, a.id ASC
                       ) AS exposure
                FROM assessment_results a
                LEFT JOIN employees e ON a.emp_code = e.emp_code
                LEFT JOIN modules m ON a.module_id = m.id
                {where_sql}
                AND a.post_test_score IS NOT NULL
            )
            WHERE exposure IS NOT NULL
            GROUP BY module_id, title, exposure
            ORDER BY module_id, exposure
        """, params).fetchall()

        # Filter dropdowns (scope-aware agent + module lists).
        agents = conn.execute(f"""
            SELECT e.emp_code, e.emp_name
            FROM employees e
            {("WHERE " + " AND ".join(scope_conds)) if scope_conds else ""}
            ORDER BY e.emp_name ASC LIMIT 2000
        """, scope_params).fetchall()
        modules = conn.execute("SELECT id, title FROM modules ORDER BY title ASC").fetchall()

        conn.close()
        return jsonify({
            "status": "success",
            "group_by": group_by,
            "period": period,
            "agent_history": agent_history,
            "agent_growth": growth_out,
            "period_aggregates": period_out,
            "module_trends": [dict(r) for r in module_trends],
            "agents": [{"emp_code": r['emp_code'], "emp_name": r['emp_name'] or r['emp_code']} for r in agents],
            "modules": [{"id": r['id'], "title": r['title']} for r in modules],
        })
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/analytics/growth', methods=['GET'])
def analytics_growth():
    """Product/module-wise Pre vs Post growth (dashboard widget + drill-down modal).

    SUMMARY (no module_id):
      - one row per module: avg pre, avg post, growth = avg_post - avg_pre,
        paired growth (records where BOTH scores exist), trainees, sessions.
    DETAIL (module_id + page):
      - paginated per-record rows for one module with per-row growth + stats.

    Filters: zone, division, branch, emp_code, business_unit, product_name,
             start_date, end_date, module_id, trainer_id (+ trainer scope).
    """
    conn = get_db_connection()
    try:
        args = request.args
        module_id = args.get('module_id', '').strip()
        trainer_id = args.get('trainer_id', '').strip()

        where_sql, base_params = _analytics_where(args)
        if not where_sql:
            where_sql = " WHERE 1=1"

        # Access control: non-global roles only see their assigned scope.
        scope_conds, scope_params = [], []
        _user = _session_user()
        if _user and not _is_global_role(_user.get('role', '')):
            _scope = _trainer_scope(_user.get('trainer_id'))
            if _scope:
                if _scope.get('zones'):
                    scope_conds.append("UPPER(TRIM(e.zone)) IN ({})".format(','.join('?' * len(_scope['zones']))))
                    scope_params.extend(_scope['zones'])
                if _scope.get('divisions'):
                    scope_conds.append("UPPER(TRIM(e.division)) IN ({})".format(','.join('?' * len(_scope['divisions']))))
                    scope_params.extend(_scope['divisions'])
                if _scope.get('branches'):
                    scope_conds.append("UPPER(TRIM(e.branch_name)) IN ({})".format(','.join('?' * len(_scope['branches']))))
                    scope_params.extend(_scope['branches'])
                if _scope.get('business_units'):
                    scope_conds.append("UPPER(TRIM(e.business_unit)) IN ({})".format(','.join('?' * len(_scope['business_units']))))
                    scope_params.extend(_scope['business_units'])

        # Extra filters (bound AFTER scope params, matching SQL order).
        extra_conds, extra_params = [], []
        if module_id:
            extra_conds.append("a.module_id = ?")
            extra_params.append(module_id)
        if trainer_id:
            extra_conds.append("UPPER(TRIM(a.trainer_id)) = UPPER(TRIM(?))")
            extra_params.append(trainer_id)

        conds = scope_conds + extra_conds
        if conds:
            where_sql += " AND " + " AND ".join(conds)
        params = base_params + scope_params + extra_params

        base = ("FROM assessment_results a "
                "LEFT JOIN employees e ON a.emp_code = e.emp_code "
                "LEFT JOIN modules m ON a.module_id = m.id")

        modules = conn.execute("SELECT id, title FROM modules ORDER BY title ASC").fetchall()

        def _fmt(v):
            return round(v, 1) if v is not None else None

        if module_id:
            # ---- DETAIL mode: per-record rows for one module (pagination) ----
            try:
                page = max(1, int(args.get('page', 1) or 1))
            except ValueError:
                page = 1
            try:
                page_size = max(5, min(50, int(args.get('page_size', 10) or 10)))
            except ValueError:
                page_size = 10
            offset = (page - 1) * page_size

            total = conn.execute(
                "SELECT COUNT(*) AS c {base} {where}".format(base=base, where=where_sql),
                params
            ).fetchone()['c']
            pages = max(1, (total + page_size - 1) // page_size) if total else 1
            page = min(page, pages)
            offset = (page - 1) * page_size

            rows = conn.execute("""
                SELECT a.id, a.emp_code, e.emp_name, a.module_id, m.title AS module_title,
                       a.training_date, a.assignment_day, a.session_id,
                       a.trainer_id, t.name AS trainer_name,
                       a.pre_test_score, a.post_test_score,
                       (a.post_test_score - a.pre_test_score) AS growth,
                       a.zone, a.division, a.branch_name, a.business_unit
                {base}
                LEFT JOIN trainers t ON a.trainer_id = t.trainer_id
                {where}
                ORDER BY a.training_date DESC, a.id DESC
                LIMIT ? OFFSET ?
            """.format(base=base, where=where_sql), params + [page_size, offset]).fetchall()

            stats = conn.execute("""
                SELECT COUNT(DISTINCT a.emp_code) AS trainees,
                       COUNT(DISTINCT a.session_id) AS sessions,
                       COUNT(*) AS records,
                       AVG(a.pre_test_score) AS avg_pre,
                       AVG(a.post_test_score) AS avg_post,
                       AVG(CASE WHEN a.pre_test_score IS NOT NULL AND a.post_test_score IS NOT NULL
                                THEN a.post_test_score - a.pre_test_score END) AS paired_growth
                {base}
                {where}
            """.format(base=base, where=where_sql), params).fetchone()

            detail = [{
                "id": r['id'],
                "emp_code": r['emp_code'],
                "emp_name": r['emp_name'] or r['emp_code'],
                "training_date": r['training_date'],
                "assignment_day": r['assignment_day'],
                "session_id": r['session_id'],
                "trainer_name": r['trainer_name'],
                "zone": r['zone'],
                "division": r['division'],
                "branch_name": r['branch_name'],
                "business_unit": r['business_unit'],
                "pre_test_score": _fmt(r['pre_test_score']),
                "post_test_score": _fmt(r['post_test_score']),
                "growth": _fmt(r['growth']),
            } for r in rows]

            conn.close()
            return jsonify({
                "status": "success",
                "mode": "detail",
                "module_id": module_id,
                "page": page,
                "page_size": page_size,
                "total": total,
                "pages": pages,
                "detail": detail,
                "stats": {
                    "avg_pre": _fmt(stats['avg_pre']),
                    "avg_post": _fmt(stats['avg_post']),
                    "growth": _fmt(stats['avg_post'] - stats['avg_pre']) if stats['avg_pre'] is not None and stats['avg_post'] is not None else None,
                    "paired_growth": _fmt(stats['paired_growth']),
                    "trainees": stats['trainees'],
                    "sessions": stats['sessions'],
                    "records": stats['records'],
                },
            })

        # ---- SUMMARY mode: one row per module with real averages ----
        summary = conn.execute("""
            SELECT a.module_id, m.title AS module_title,
                   COUNT(*) AS records,
                   COUNT(DISTINCT a.emp_code) AS trainees,
                   COUNT(DISTINCT a.session_id) AS sessions,
                   AVG(a.pre_test_score) AS avg_pre,
                   AVG(a.post_test_score) AS avg_post,
                   AVG(CASE WHEN a.pre_test_score IS NOT NULL AND a.post_test_score IS NOT NULL
                            THEN a.post_test_score - a.pre_test_score END) AS paired_growth
            {base}
            {where}
            GROUP BY a.module_id, m.title
            HAVING COUNT(a.pre_test_score) + COUNT(a.post_test_score) > 0
            ORDER BY avg_post IS NULL, avg_post DESC, m.title ASC
        """.format(base=base, where=where_sql), params).fetchall()

        summary_out = [{
            "module_id": r['module_id'],
            "module_title": r['module_title'] or "Module {0}".format(r['module_id']),
            "records": r['records'],
            "trainees": r['trainees'],
            "sessions": r['sessions'],
            "avg_pre": _fmt(r['avg_pre']),
            "avg_post": _fmt(r['avg_post']),
            "growth": _fmt(r['avg_post'] - r['avg_pre']) if r['avg_pre'] is not None and r['avg_post'] is not None else None,
            "paired_growth": _fmt(r['paired_growth']),
        } for r in summary]

        conn.close()
        return jsonify({
            "status": "success",
            "mode": "summary",
            "summary": summary_out,
            "modules": [{"id": r['id'], "title": r['title']} for r in modules],
            "total": len(summary_out),
        })
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/analytics/export', methods=['GET'])
def export_analytics():
    conn = get_db_connection()
    try:
        where_sql, params = _analytics_where(request.args)
        rows = conn.execute(f"""
            SELECT e.emp_code, e.emp_name, e.branch_name, e.zone, e.division, e.business_unit, e.product_name,
                   a.assignment_day, a.pre_test_score, a.post_test_score, a.tab_switch_count, a.time_taken_seconds,
                   a.passed_status, a.certificate_id, a.completed_at
            FROM assessment_results a
            LEFT JOIN employees e ON a.emp_code = e.emp_code
            {where_sql}
            ORDER BY a.completed_at DESC
        """, params).fetchall()
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500
    conn.close()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Employee Code', 'Employee Name', 'Branch', 'Zone', 'Division', 'Business Unit', 'Product', 'Assignment Day', 'Pre Score', 'Post Score', 'Tab Switches', 'Time Taken (s)', 'Passed', 'Certificate ID', 'Completed At'])
    for r in rows:
        writer.writerow([r[k] if r[k] is not None else '' for k in (
            'emp_code', 'emp_name', 'branch_name', 'zone', 'division', 'business_unit', 'product_name',
            'assignment_day', 'pre_test_score', 'post_test_score', 'tab_switch_count', 'time_taken_seconds',
            'passed_status', 'certificate_id', 'completed_at'
        )])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=Socrates_Analytics_Report.csv"}
    )

@app.route('/api/trainers/performance', methods=['GET'])
def trainers_performance():
    """Trainer Productivity & Quality comparison matrix.
    Live sessions are logged in training_sessions; when no session rows exist
    yet (legacy offline campaigns), trainers fall back to platform-wide
    averages so the report is never misleading/empty."""
    conn = get_db_connection()
    try:
        start_date = request.args.get('start_date', '').strip()
        end_date = request.args.get('end_date', '').strip()
        trainers = conn.execute(
            "SELECT trainer_id, name, zone FROM trainers ORDER BY name ASC"
        ).fetchall()
        total_sessions = conn.execute("SELECT COUNT(*) AS c FROM training_sessions").fetchone()['c']
        platform_growth = conn.execute(
            "SELECT AVG(post_test_score - pre_test_score) AS g FROM assessment_results WHERE post_test_score IS NOT NULL AND pre_test_score IS NOT NULL"
        ).fetchone()['g'] or 0

        fb = conn.execute("""
            SELECT AVG(rating) AS avg_rating,
                   AVG(CASE UPPER(TRIM(understanding))
                           WHEN 'FULLY CLEAR' THEN 100 WHEN 'PARTIALLY' THEN 60 WHEN 'NEED HELP' THEN 30 ELSE NULL END) AS clarity,
                   AVG(CASE WHEN UPPER(TRIM(manpower_saved)) LIKE 'YES%' OR UPPER(TRIM(manpower_saved)) LIKE '%FASTER%'
                                 OR UPPER(TRIM(manpower_saved)) LIKE '%SAVES%' OR UPPER(TRIM(manpower_saved)) LIKE '%SAVED%' THEN 100
                            WHEN UPPER(TRIM(manpower_saved)) LIKE 'SOMEWHAT%' OR UPPER(TRIM(manpower_saved)) LIKE '%PARTIALLY%' THEN 60
                            WHEN UPPER(TRIM(manpower_saved)) LIKE 'NO%' OR UPPER(TRIM(manpower_saved)) LIKE '%NOT%' THEN 30
                            ELSE NULL END) AS nps
            FROM session_feedback
        """).fetchone()

        result = []
        for t in trainers:
            tid = t['trainer_id']
            if total_sessions > 0:
                sq = "SELECT COUNT(*) AS c FROM training_sessions WHERE UPPER(TRIM(trainer_id))=UPPER(TRIM(?))"
                sp = [tid]
                if start_date:
                    sq += " AND date >= ?"; sp.append(start_date)
                if end_date:
                    sq += " AND date <= ?"; sp.append(end_date)
                s_count = conn.execute(sq, sp).fetchone()['c']
                gq = "SELECT AVG(post_test_score - pre_test_score) AS g FROM assessment_results WHERE module_id IN (SELECT DISTINCT module_id FROM training_sessions WHERE UPPER(TRIM(trainer_id))=UPPER(TRIM(?)))"
                gp = [tid]
                if start_date:
                    gq += " AND completed_at >= ?"; gp.append(start_date)
                if end_date:
                    gq += " AND completed_at <= ?"; gp.append(end_date + " 23:59")
                growth = conn.execute(gq, gp).fetchone()['g'] or platform_growth
                fq = """SELECT AVG(f.rating) AS avg_rating,
                                AVG(CASE UPPER(TRIM(f.understanding))
                                        WHEN 'FULLY CLEAR' THEN 100 WHEN 'PARTIALLY' THEN 60 WHEN 'NEED HELP' THEN 30 ELSE NULL END) AS clarity,
                                AVG(CASE WHEN UPPER(TRIM(f.manpower_saved)) LIKE 'YES%' OR UPPER(TRIM(f.manpower_saved)) LIKE '%FASTER%'
                                              OR UPPER(TRIM(f.manpower_saved)) LIKE '%SAVES%' OR UPPER(TRIM(f.manpower_saved)) LIKE '%SAVED%' THEN 100
                                         WHEN UPPER(TRIM(f.manpower_saved)) LIKE 'SOMEWHAT%' OR UPPER(TRIM(f.manpower_saved)) LIKE '%PARTIALLY%' THEN 60
                                         WHEN UPPER(TRIM(f.manpower_saved)) LIKE 'NO%' OR UPPER(TRIM(f.manpower_saved)) LIKE '%NOT%' THEN 30
                                         ELSE NULL END) AS nps
                         FROM session_feedback f
                         WHERE f.session_id IN (SELECT session_id FROM training_sessions WHERE UPPER(TRIM(trainer_id))=UPPER(TRIM(?)))"""
                fb_row = conn.execute(fq, [tid]).fetchone()
            else:
                s_count = 0
                growth = platform_growth
                fb_row = fb

            result.append({
                "trainer_id": tid,
                "name": t['name'] or tid,
                "sessions_count": s_count,
                "avg_rating": round(fb_row['avg_rating'] or 0, 1) if fb_row else 0,
                "clarity_index": round(fb_row['clarity'] or 0, 0) if fb_row else 0,
                "nps": round(fb_row['nps'] or 0, 0) if fb_row else 0,
                "growth_delta": round(growth or 0, 1)
            })
        conn.close()
        return jsonify(result)
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/refresher/campaign', methods=['POST'])
def push_refresher_campaign():
    """Flag trainees (below 60%) for a mandatory AI Socratic refresher campaign."""
    data = request.json or {}
    emp_codes = data.get('emp_codes') or []
    if not isinstance(emp_codes, list) or len(emp_codes) == 0:
        return jsonify({"status": "error", "message": "No employees selected for the refresher campaign."}), 400
    emp_codes = [str(x).strip().upper() for x in emp_codes if str(x).strip()]
    module_id = data.get('module_id')
    try:
        module_id = int(module_id) if module_id else None
    except (TypeError, ValueError):
        module_id = None
    conn = get_db_connection()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    pushed = 0
    try:
        for code in emp_codes:
            exists = conn.execute(
                "SELECT 1 FROM refresher_campaigns WHERE emp_code=? AND status='PENDING'",
                (code,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO refresher_campaigns (emp_code, module_id, campaign_date, status) VALUES (?, ?, ?, 'PENDING')",
                    (code, module_id, now)
                )
                pushed += 1
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": f"Failed to push campaign: {str(e)}"}), 500
    conn.close()
    msg = f"🚀 Refresher campaign pushed to {pushed} trainee(s)." if pushed else "All selected trainees are already in a pending refresher campaign."
    return jsonify({"status": "success", "message": msg, "pushed": pushed})

@app.route('/api/feedback/submit', methods=['POST'])
def submit_feedback():
    data = request.json or {}
    emp_code = str(data.get('emp_code', '')).upper()
    session_id = str(data.get('session_id', ''))
    rating = int(data.get('rating', 5) or 0)
    understanding = str(data.get('understanding', ''))
    manpower_saved = str(data.get('manpower_saved', ''))
    comments = str(data.get('comments', ''))
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO session_feedback (emp_code, session_id, rating, understanding, manpower_saved, comments, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (emp_code, session_id, rating, understanding, manpower_saved, comments, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        )
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": f"Failed to save feedback: {str(e)}"}), 500
    conn.close()
    return jsonify({"status": "success", "message": "Feedback submitted successfully!"})

# --- WEBSOCKET EVENT LISTENERS (Flask-SocketIO) & GAMIFICATION STATE ---
import time

SESSION_REGISTRY = {}

@socketio.on('join_session')
def on_join_session(data):
    pin = str(data.get('pin'))
    emp_id = data.get('emp_id')
    join_room(pin)
    print(f"Employee {emp_id} connected to session PIN: {pin}")
    
    # Persist a live session row when the trainer opens the room (feeds Analytics Hub)
    if emp_id == 'TRAINER':
        try:
            trainer_id = session.get('user', {}).get('trainer_id')
            if trainer_id:
                conn = sqlite3.connect(DB_FILE)
                conn.execute(
                    "INSERT INTO training_sessions (session_id, date, trainer_id) VALUES (?, ?, ?) "
                    "ON CONFLICT(session_id) DO NOTHING",
                    (pin, datetime.datetime.now().strftime("%Y-%m-%d"), trainer_id)
                )
                conn.commit()
                conn.close()
        except Exception as e:
            print(f"trainer session persist failed: {e}")

    # Initialize session registry if trainer starts a new session room
    if pin not in SESSION_REGISTRY:
        SESSION_REGISTRY[pin] = {
            "push_time": 0.0,
            "correct_index": -1,
            "leaderboard": {}
        }
        
    # Register trainee in current session leaderboard
    if emp_id and emp_id != 'TRAINER':
        if emp_id not in SESSION_REGISTRY[pin]["leaderboard"]:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT emp_name FROM employees WHERE emp_code=?", (emp_id,))
            row = cursor.fetchone()
            conn.close()
            emp_name = row[0] if row else emp_id
            
            SESSION_REGISTRY[pin]["leaderboard"][emp_id] = {
                "name": emp_name,
                "score": 0,
                "last_speed": 0.0,
                "last_correct": False
            }
            
    emit('user_connected', {'emp_id': emp_id}, room=pin)

@socketio.on('trainer_broadcast')
def on_trainer_broadcast(data):
    pin = str(data.get('pin'))
    view = data.get('view')
    
    # If pushing a live assessment quiz, capture start timing for speed bonus
    if view in ['pretest', 'posttest']:
        if pin not in SESSION_REGISTRY:
            SESSION_REGISTRY[pin] = {
                "push_time": 0.0,
                "correct_index": -1,
                "leaderboard": {}
            }
        SESSION_REGISTRY[pin]["push_time"] = time.time()
        SESSION_REGISTRY[pin]["correct_index"] = int(data.get('correctIndex', -1))
        
        # Attach the module to this live session row so Analytics Hub reports
        # show the real module title and attendee counts.
        try:
            mod_id = data.get('module_id')
            trainer_id = session.get('user', {}).get('trainer_id')
            if mod_id and trainer_id:
                conn = sqlite3.connect(DB_FILE)
                conn.execute(
                    "INSERT INTO training_sessions (session_id, date, trainer_id, module_id) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET module_id=excluded.module_id",
                    (pin, datetime.datetime.now().strftime("%Y-%m-%d"), trainer_id, mod_id)
                )
                conn.commit()
                conn.close()
        except Exception as e:
            print(f"session module persist failed: {e}")
        
    # Broadcast payload to trainee screens, but strip the answer key (correctIndex)
    # and the full module object (contains correct answers) from the client-visible payload.
    relay = dict(data)
    relay.pop('correctIndex', None)
    relay.pop('activeModule', None)
    emit('change_view', relay, room=pin)

@socketio.on('submit_vote')
def on_submit_vote(data):
    pin = str(data.get('pin'))
    emp_id = data.get('emp_id')
    answer_idx = int(data.get('answer_idx', 0))
    
    points_earned = 0
    speed_bonus = 0
    is_correct = False
    response_time = 0.0
    
    if pin in SESSION_REGISTRY:
        session = SESSION_REGISTRY[pin]
        push_time = session.get("push_time", 0.0)
        correct_index = session.get("correct_index", -1)
        
        if push_time > 0.0:
            response_time = time.time() - push_time
            
        if answer_idx == correct_index:
            is_correct = True
            base_points = 1000
            # Answering within 20 seconds yields a speed bonus
            speed_bonus = max(0, int(1000 - (response_time * 50)))
            points_earned = base_points + speed_bonus
            
        # Ensure student is registered
        if emp_id not in session["leaderboard"]:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT emp_name FROM employees WHERE emp_code=?", (emp_id,))
            row = cursor.fetchone()
            conn.close()
            emp_name = row[0] if row else emp_id
            
            session["leaderboard"][emp_id] = {
                "name": emp_name,
                "score": 0,
                "last_speed": 0.0,
                "last_correct": False
            }
            
        # Update session points
        session["leaderboard"][emp_id]["score"] += points_earned
        session["leaderboard"][emp_id]["last_speed"] = round(response_time, 2)
        session["leaderboard"][emp_id]["last_correct"] = is_correct
        
    # Broadcast standard vote updates for presenter chart
    emit('vote_update', {'emp_id': emp_id, 'answer_idx': answer_idx}, room=pin)
    
    # Emit score confirmation details back to student tab for immediate screen celebrations
    emit('score_confirmation', {
        'points': points_earned,
        'speed_bonus': speed_bonus,
        'is_correct': is_correct,
        'total_score': SESSION_REGISTRY[pin]["leaderboard"][emp_id]["score"] if pin in SESSION_REGISTRY else points_earned,
        'response_time': round(response_time, 2)
    }, room=request.sid)
    
    # Broadcast updated sorted leaderboard list to presenter control drawer
    if pin in SESSION_REGISTRY:
        leaderboard_sorted = []
        for code, player in SESSION_REGISTRY[pin]["leaderboard"].items():
            leaderboard_sorted.append({
                'emp_code': code,
                'emp_name': player['name'],
                'score': player['score'],
                'last_speed': player['last_speed'],
                'last_correct': player['last_correct']
            })
        leaderboard_sorted.sort(key=lambda x: x['score'], reverse=True)
        emit('leaderboard_update', {'leaderboard': leaderboard_sorted}, room=pin)

@socketio.on('trainer_command')
def on_trainer_command(data):
    pin = str(data.get('pin'))
    command = data.get('command')
    
    if command == 'reset_scores':
        if pin in SESSION_REGISTRY:
            for code in SESSION_REGISTRY[pin]["leaderboard"]:
                SESSION_REGISTRY[pin]["leaderboard"][code]["score"] = 0
            emit('leaderboard_update', {'leaderboard': []}, room=pin)
            
    # Forward general custom commands (e.g. final confetti podium) to all clients
    emit('client_command', data, room=pin)

# 6. DYNAMIC PDF CERTIFICATE GENERATOR & VERIFIER
@app.route('/api/assessments/certificate/<cert_id>', methods=['GET'])
def get_certificate(cert_id):
    conn = get_db_connection()
    row = conn.execute('''
        SELECT a.*, e.emp_name, e.branch_name, e.division, m.title as module_title
        FROM assessment_results a
        LEFT JOIN employees e ON a.emp_code = e.emp_code
        LEFT JOIN modules m ON a.module_id = m.id
        WHERE a.certificate_id = ? OR a.emp_code = ?
    ''', (cert_id, cert_id.upper())).fetchone()
    conn.close()
    
    if row:
        res = dict(row)
        if res.get('passed_status') == 0:
            candidate_name = res.get('emp_name') or res.get('emp_code') or 'Trainee Candidate'
            return f'''<!DOCTYPE html><html><head><meta charset="utf-8"/><title>Certificate Not Awarded</title>
            <style>body{{background:#090D16;color:#F8FAFC;font-family:'Outfit',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;}}
            .box{{text-align:center;max-width:480px;padding:40px;border:2px solid rgba(225,29,72,0.4);border-radius:24px;background:#0F172A;}}
            .big{{font-size:56px;}} h1{{font-size:22px;margin:16px 0 8px;}} p{{color:#94A3B8;font-size:14px;line-height:1.6;}}</style></head>
            <body><div class="box"><div class="big">🚫</div><h1>Certificate Not Awarded</h1>
            <p>{candidate_name}, your post-test score did not reach the passing threshold required for certification. Please contact your trainer for a re-assessment.</p></div></body></html>'''
        candidate_name = res.get('emp_name') or res.get('emp_code') or 'Trainee Candidate'
        module_title = res.get('module_title') or 'Socrates AI Enterprise Knowledge Assessment'
        score = res.get('post_test_score') if res.get('post_test_score') is not None else (res.get('pre_test_score') or 85)
        date_str = res.get('completed_at') or datetime.datetime.now().strftime("%B %d, %Y")
        verification_code = res.get('certificate_id') or cert_id
    else:
        return f'''<!DOCTYPE html><html><head><meta charset="utf-8"/><title>Certificate Not Found</title>
        <style>body{{background:#090D16;color:#F8FAFC;font-family:'Outfit',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;}}
        .box{{text-align:center;max-width:480px;padding:40px;border:2px solid rgba(148,163,184,0.3);border-radius:24px;background:#0F172A;}}
        .big{{font-size:56px;}} h1{{font-size:22px;margin:16px 0 8px;}} p{{color:#94A3B8;font-size:14px;line-height:1.6;}}</style></head>
        <body><div class="box"><div class="big">🔍</div><h1>Certificate Not Found</h1>
        <p>No assessment record was found for verification ID <strong>{cert_id}</strong>. Complete the assessment first, then download your certificate.</p></div></body></html>'''
    
    html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8"/>
    <title>Socrates AI Certificate - {candidate_name}</title>
    <link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@700;900&family=Outfit:wght@400;600;800&display=swap" rel="stylesheet"/>
    <style>
        body {{ margin: 0; padding: 40px; background: #090D16; font-family: 'Outfit', sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
        .cert-card {{ width: 900px; padding: 60px; background: #0F172A; border: 4px solid #F59E0B; border-radius: 24px; box-shadow: 0 25px 50px rgba(0,0,0,0.8); position: relative; text-align: center; color: white; box-sizing: border-box; overflow: hidden; }}
        .watermark {{ position: absolute; inset: 0; opacity: 0.03; background: url('/static/socrates_mind_logo.jpg') center/cover no-repeat; pointer-events: none; }}
        .gold-badge {{ width: 80px; h: 80px; margin: 0 auto 20px; border-radius: 50%; background: linear-gradient(135deg, #F59E0B, #D97706); display: flex; align-items: center; justify-content: center; font-size: 36px; box-shadow: 0 10px 25px rgba(245,158,11,0.4); }}
        .title {{ font-family: 'Cinzel', serif; font-size: 38px; font-weight: 900; letter-spacing: 4px; color: #F59E0B; text-transform: uppercase; margin: 0 0 10px; }}
        .subtitle {{ font-size: 14px; text-transform: uppercase; letter-spacing: 3px; color: #94A3B8; margin-bottom: 40px; font-weight: 600; }}
        .name {{ font-size: 42px; font-weight: 800; color: #FFFFFF; border-bottom: 2px solid rgba(245,158,11,0.4); display: inline-block; padding-bottom: 10px; margin-bottom: 25px; font-family: 'Cinzel', serif; }}
        .reason {{ font-size: 16px; color: #CBD5E1; max-width: 650px; margin: 0 auto 40px; line-height: 1.6; }}
        .reason strong {{ color: #F59E0B; }}
        .meta-grid {{ display: flex; justify-content: space-between; align-items: flex-end; margin-top: 40px; padding-top: 30px; border-t: 1px solid rgba(255,255,255,0.1); }}
        .meta-box {{ text-align: left; }}
        .meta-box.right {{ text-align: right; }}
        .meta-label {{ font-size: 10px; text-transform: uppercase; letter-spacing: 2px; color: #64748B; font-weight: 800; margin-bottom: 4px; }}
        .meta-val {{ font-size: 14px; font-weight: 700; color: #E2E8F0; }}
        .print-btn {{ position: fixed; top: 20px; right: 20px; padding: 12px 24px; background: #F59E0B; color: #090D16; font-weight: 800; border: none; border-radius: 12px; cursor: pointer; font-size: 14px; shadow: 0 4px 12px rgba(245,158,11,0.3); }}
    </style>
</head>
<body>
    <button class="print-btn" onclick="window.print()">🖨️ Print / Save PDF</button>
    <div class="cert-card">
        <div class="watermark"></div>
        <div class="gold-badge">🏆</div>
        <div class="title">Certificate of Excellence</div>
        <div class="subtitle">Socrates AI Enterprise Knowledge Assessment</div>
        
        <div>This is officially awarded to</div>
        <div class="name">{candidate_name}</div>
        
        <div class="reason">
            For successfully completing and demonstrating mastery in <strong>{module_title}</strong> with an audited evaluation score of <strong>{score}%</strong>.
        </div>
        
        <div class="meta-grid">
            <div class="meta-box">
                <div class="meta-label">Date Awarded</div>
                <div class="meta-val">{date_str}</div>
            </div>
            <div class="meta-box">
                <div class="meta-label">Issuing Authority</div>
                <div class="meta-val">Socrates AI Proctoring Engine</div>
            </div>
            <div class="meta-box right">
                <div class="meta-label">Verification ID</div>
                <div class="meta-val" style="font-family:monospace;color:#F59E0B;">{verification_code}</div>
            </div>
        </div>
    </div>
</body>
</html>'''
    return html

if __name__ == '__main__':
    socketio.run(app, debug=False, use_reloader=False, port=5050, host='0.0.0.0', allow_unsafe_werkzeug=True)
