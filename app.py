from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO, emit, join_room, leave_room
import sqlite3
import os
import datetime
from werkzeug.utils import secure_filename
import csv

app = Flask(__name__)
app.config['SECRET_KEY'] = 'socrates-secret-key-123'
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Initialize SocketIO
socketio = SocketIO(app, cors_allowed_origins="*")

DB_FILE = "socrates.db"

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Employees (Roster)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS employees (
        emp_code TEXT PRIMARY KEY,
        emp_name TEXT,
        branch_name TEXT,
        zone TEXT,
        division TEXT,
        business_unit TEXT,
        role TEXT,
        product_name TEXT,
        status TEXT DEFAULT 'ACTIVE',
        change_detail TEXT DEFAULT 'ADDED MANUALLY'
    )''')
    
    # Run migration to add columns if db was created in older version
    cursor.execute("PRAGMA table_info(employees)")
    cols = [row[1] for row in cursor.fetchall()]
    if 'role' not in cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN role TEXT")
    if 'product_name' not in cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN product_name TEXT")
    if 'status' not in cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN status TEXT DEFAULT 'ACTIVE'")
    if 'change_detail' not in cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN change_detail TEXT DEFAULT 'ADDED MANUALLY'")
    
    # Trainers
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS trainers (
        trainer_id TEXT PRIMARY KEY,
        name TEXT,
        zone TEXT,
        password TEXT,
        status TEXT DEFAULT 'Active',
        role TEXT DEFAULT 'Trainer',
        last_login TEXT,
        zones TEXT DEFAULT 'ALL',
        divisions TEXT DEFAULT 'ALL',
        branches TEXT DEFAULT 'ALL',
        business_units TEXT DEFAULT 'ALL'
    )''')
    
    # Run migration to add trainer scope columns if db was created in older version
    cursor.execute("PRAGMA table_info(trainers)")
    trainer_cols = [row[1] for row in cursor.fetchall()]
    if 'zones' not in trainer_cols:
        cursor.execute("ALTER TABLE trainers ADD COLUMN zones TEXT DEFAULT 'ALL'")
    if 'divisions' not in trainer_cols:
        cursor.execute("ALTER TABLE trainers ADD COLUMN divisions TEXT DEFAULT 'ALL'")
    if 'branches' not in trainer_cols:
        cursor.execute("ALTER TABLE trainers ADD COLUMN branches TEXT DEFAULT 'ALL'")
    if 'business_units' not in trainer_cols:
        cursor.execute("ALTER TABLE trainers ADD COLUMN business_units TEXT DEFAULT 'ALL'")
    
    # Add a default Super Admin if none exists
    cursor.execute("SELECT * FROM trainers WHERE trainer_id='ADMIN'")
    if not cursor.fetchone():
        cursor.execute("INSERT INTO trainers (trainer_id, name, zone, password, role, zones, divisions, branches, business_units) VALUES ('ADMIN', 'Super Admin', 'All', 'admin123', 'SuperAdmin', 'ALL', 'ALL', 'ALL', 'ALL')")
    
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
    
    # Assessment Results (For learning curves)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS assessment_results (
        emp_code TEXT,
        module_id INTEGER,
        assignment_day TEXT,
        pre_test_score REAL,
        post_test_score REAL,
        completed_at TEXT,
        PRIMARY KEY (emp_code, module_id, assignment_day),
        FOREIGN KEY(emp_code) REFERENCES employees(emp_code),
        FOREIGN KEY(module_id) REFERENCES modules(id)
    )''')
    
    conn.commit()
    conn.close()

init_db()

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

# --- HTML TEMPLATE ROUTES ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin')
def admin():
    return render_template('admin.html')

# --- API ROUTES ---

# 1. AUTHENTICATION
@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    data = request.json
    trainer_id = data.get('trainer_id')
    password = data.get('password')
    
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM trainers WHERE trainer_id=? AND password=? AND status='Active'", (trainer_id, password)).fetchone()
    if user:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        conn.execute("UPDATE trainers SET last_login=? WHERE trainer_id=?", (now, trainer_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "role": user['role'], "name": user['name']})
    conn.close()
    return jsonify({"status": "error", "message": "Invalid Credentials or Account Revoked"}), 401

# 2. TRAINER MANAGEMENT (Super Admin Only)
# 2. TRAINER MANAGEMENT (Super Admin Only)
@app.route('/api/trainers', methods=['GET', 'POST'])
def handle_trainers():
    conn = get_db_connection()
    if request.method == 'GET':
        trainers = conn.execute("SELECT trainer_id AS id, name, zone, status, last_login, zones, divisions, branches, business_units, password FROM trainers WHERE role='Trainer'").fetchall()
        conn.close()
        return jsonify([dict(t) for t in trainers])
    
    elif request.method == 'POST':
        data = request.json
        try:
            conn.execute(
                "INSERT INTO trainers (trainer_id, name, zone, password, zones, divisions, branches, business_units) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    data['id'].upper().strip(),
                    data['name'].strip(),
                    data.get('zone', 'ALL'),
                    data['password'].strip(),
                    data.get('zones', 'ALL'),
                    data.get('divisions', 'ALL'),
                    data.get('branches', 'ALL'),
                    data.get('business_units', 'ALL')
                )
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({"status": "error", "message": "Trainer ID already exists. Please choose a different ID or delete the existing account first."}), 400
        conn.close()
        return jsonify({"status": "success"})

@app.route('/api/trainers/<trainer_id>', methods=['PUT', 'DELETE'])
def handle_single_trainer(trainer_id):
    trainer_id = trainer_id.upper().strip()
    conn = get_db_connection()
    
    if request.method == 'DELETE':
        if trainer_id == 'ADMIN':
            return jsonify({"status": "error", "message": "Super Admin cannot be deleted"}), 400
        try:
            conn.execute("DELETE FROM trainers WHERE trainer_id=?", (trainer_id,))
            conn.commit()
        except Exception as e:
            conn.close()
            return jsonify({"status": "error", "message": str(e)}), 500
        conn.close()
        return jsonify({"status": "success", "message": "Trainer deleted successfully"})
        
    elif request.method == 'PUT':
        data = request.json
        name = data.get('name', '').strip()
        password = data.get('password', '').strip()
        zone = data.get('zone', 'ALL')
        zones = data.get('zones', 'ALL')
        divisions = data.get('divisions', 'ALL')
        branches = data.get('branches', 'ALL')
        business_units = data.get('business_units', 'ALL')
        
        if not name or not password:
            conn.close()
            return jsonify({"status": "error", "message": "Name and Password are required."}), 400
            
        try:
            conn.execute(
                "UPDATE trainers SET name=?, password=?, zone=?, zones=?, divisions=?, branches=?, business_units=? WHERE trainer_id=?",
                (name, password, zone, zones, divisions, branches, business_units, trainer_id)
            )
            conn.commit()
        except Exception as e:
            conn.close()
            return jsonify({"status": "error", "message": str(e)}), 500
        conn.close()
        return jsonify({"status": "success", "message": "Trainer updated successfully"})

@app.route('/api/trainers/<trainer_id>/status', methods=['PUT'])
def update_trainer_status(trainer_id):
    data = request.json
    conn = get_db_connection()
    conn.execute("UPDATE trainers SET status=? WHERE trainer_id=?", (data['status'], trainer_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/trainers/upload', methods=['POST'])
def upload_trainers():
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "No selected file"}), 400
    
    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        REQUIRED_HEADERS = ['Trainer ID', 'Trainer Name', 'Password', 'Business Units', 'Zones', 'Divisions', 'Branches']
        
        rows = []
        headers = []
        try:
            try:
                with open(filepath, 'r', encoding='utf-8-sig') as csvfile:
                    reader = csv.reader(csvfile)
                    headers = [h.strip() for h in next(reader)]
                    for row_idx, r in enumerate(reader, start=2):
                        if not r or len(r) < len(headers):
                            continue
                        rows.append((row_idx, r))
            except (UnicodeDecodeError, ValueError):
                rows = []
                with open(filepath, 'r', encoding='latin-1') as csvfile:
                    reader = csv.reader(csvfile)
                    headers = [h.strip() for h in next(reader)]
                    for row_idx, r in enumerate(reader, start=2):
                        if not r or len(r) < len(headers):
                            continue
                        rows.append((row_idx, r))
            
            missing_headers = [req for req in REQUIRED_HEADERS if req not in headers]
            if missing_headers:
                return jsonify({
                    "status": "error", 
                    "message": f"Invalid CSV format. Missing column headers: {', '.join(missing_headers)}"
                }), 400
                
            hdr_indices = {h: headers.index(h) for h in REQUIRED_HEADERS}
            
            final_rows = []
            for row_idx, r in rows:
                row_data = {
                    'id': r[hdr_indices['Trainer ID']].strip().upper(),
                    'name': r[hdr_indices['Trainer Name']].strip(),
                    'password': r[hdr_indices['Password']].strip(),
                    'business_units': r[hdr_indices['Business Units']].strip(),
                    'zones': r[hdr_indices['Zones']].strip(),
                    'divisions': r[hdr_indices['Divisions']].strip(),
                    'branches': r[hdr_indices['Branches']].strip(),
                }
                final_rows.append((row_idx, row_data))
            rows = final_rows

        except Exception as e:
            return jsonify({"status": "error", "message": f"Failed to parse CSV: {str(e)}"}), 400
            
        seen_ids = {}
        duplicates = []
        
        conn = get_db_connection()
        for idx, row in rows:
            tid = row['id']
            if not tid:
                continue
            
            if tid in seen_ids:
                duplicates.append(f"Row {idx}: Trainer ID '{tid}' is duplicated in the file.")
            else:
                seen_ids[tid] = idx
                
            db_match = conn.execute("SELECT name FROM trainers WHERE trainer_id=?", (tid,)).fetchone()
            if db_match:
                duplicates.append(f"Row {idx}: Trainer ID '{tid}' already exists in the database as '{db_match['name']}'.")
        
        if duplicates:
            conn.close()
            return jsonify({
                "status": "error", 
                "message": "This is the duplicacy. You remove that.",
                "details": duplicates
            }), 400
            
        for _, row in rows:
            try:
                conn.execute(
                    "INSERT INTO trainers (trainer_id, name, zone, password, zones, divisions, branches, business_units) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (row['id'], row['name'], row['zones'].split(',')[0].strip().upper() if row['zones'] else 'ALL', row['password'], row['zones'].upper(), row['divisions'].upper(), row['branches'].upper(), row['business_units'].upper())
                )
            except Exception as e:
                conn.rollback()
                conn.close()
                return jsonify({"status": "error", "message": f"Database insertion failed: {str(e)}"}), 500
                
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "Trainers uploaded and registered successfully!"})

@app.route('/api/metadata', methods=['GET'])
def get_metadata():
    conn = get_db_connection()
    zones = conn.execute("SELECT DISTINCT zone FROM employees WHERE zone IS NOT NULL AND zone != '' ORDER BY zone").fetchall()
    divisions = conn.execute("SELECT DISTINCT division FROM employees WHERE division IS NOT NULL AND division != '' ORDER BY division").fetchall()
    branches = conn.execute("SELECT DISTINCT branch_name FROM employees WHERE branch_name IS NOT NULL AND branch_name != '' ORDER BY branch_name").fetchall()
    conn.close()
    
    bus = ["Two-Wheeler", "Personal Loan", "Gold Loan", "Commercial Vehicle", "Retail"]
    
    return jsonify({
        "business_units": bus,
        "zones": [r[0] for r in zones],
        "divisions": [r[0] for r in divisions],
        "branches": [r[0] for r in branches]
    })

@app.route('/api/admin/reset-database', methods=['POST'])
def reset_database():
    conn = get_db_connection()
    try:
        # Clear tables
        conn.execute("DELETE FROM assessment_results")
        conn.execute("DELETE FROM training_sessions")
        conn.execute("DELETE FROM questions")
        conn.execute("DELETE FROM modules")
        conn.execute("DELETE FROM employees")
        conn.execute("DELETE FROM trainers WHERE role != 'SuperAdmin' AND trainer_id != 'ADMIN'")
        
        # Ensure Super Admin remains
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trainers WHERE trainer_id='ADMIN'")
        if not cursor.fetchone():
            cursor.execute("INSERT INTO trainers (trainer_id, name, zone, password, role) VALUES ('ADMIN', 'Super Admin', 'All', 'admin123', 'SuperAdmin')")
        
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500
    conn.close()
    return jsonify({"status": "success", "message": "Database reset successfully"})

# 3. ROSTER MANAGEMENT
@app.route('/api/roster', methods=['GET'])
def get_roster():
    zone = request.args.get('zone', '').strip()
    branch = request.args.get('branch', '').strip()
    division = request.args.get('division', '').strip()
    search = request.args.get('q', '').strip()
    
    query = "SELECT * FROM employees WHERE 1=1"
    params = []
    if zone:
        query += " AND zone = ?"
        params.append(zone)
    if branch:
        query += " AND branch_name = ?"
        params.append(branch)
    if division:
        query += " AND division = ?"
        params.append(division)
    if search:
        query += " AND (emp_code LIKE ? OR emp_name LIKE ? OR product_name LIKE ?)"
        params.append(f"%{search}%")
        params.append(f"%{search}%")
        params.append(f"%{search}%")
        
    query += " ORDER BY emp_code ASC"
    
    conn = get_db_connection()
    emps = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(e) for e in emps])

@app.route('/api/roster/filters', methods=['GET'])
def get_roster_filters():
    conn = get_db_connection()
    zones = conn.execute("SELECT DISTINCT zone FROM employees WHERE zone IS NOT NULL AND zone != '' ORDER BY zone").fetchall()
    divisions = conn.execute("SELECT DISTINCT division, zone FROM employees WHERE division IS NOT NULL AND division != '' ORDER BY division").fetchall()
    branches = conn.execute("SELECT DISTINCT branch_name, division FROM employees WHERE branch_name IS NOT NULL AND branch_name != '' ORDER BY branch_name").fetchall()
    conn.close()
    
    return jsonify({
        "zones": [r[0] for r in zones],
        "divisions": [r[0] for r in divisions],
        "branches": [r[0] for r in branches],
        "divisions_meta": [{"name": r[0], "zone": r[1]} for r in divisions],
        "branches_meta": [{"name": r[0], "division": r[1]} for r in branches]
    })

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
        
        # Read and Validate CSV
        rows = []
        headers = []
        try:
            try:
                with open(filepath, 'r', encoding='utf-8-sig') as csvfile:
                    reader = csv.reader(csvfile)
                    headers = [h.strip() for h in next(reader)]
                    for row_idx, r in enumerate(reader, start=2):
                        if not r or len(r) < len(headers):
                            continue
                        rows.append((row_idx, r))
            except (UnicodeDecodeError, ValueError):
                rows = []
                with open(filepath, 'r', encoding='latin-1') as csvfile:
                    reader = csv.reader(csvfile)
                    headers = [h.strip() for h in next(reader)]
                    for row_idx, r in enumerate(reader, start=2):
                        if not r or len(r) < len(headers):
                            continue
                        rows.append((row_idx, r))
            
            # Check for header format
            missing_headers = [req for req in REQUIRED_HEADERS if req not in headers]
            if missing_headers:
                return jsonify({
                    "status": "error", 
                    "message": f"Invalid CSV format. Missing column headers: {', '.join(missing_headers)}"
                }), 400
                
            # Map columns by index
            hdr_indices = {h: headers.index(h) for h in headers}
            
            # Form final row data
            final_rows = []
            for row_idx, r in rows:
                row_data = {h: r[hdr_indices[h]].strip().upper() if h in hdr_indices else '' for h in REQUIRED_HEADERS}
                row_data['Product Name'] = r[hdr_indices['Product Name']].strip().upper() if 'Product Name' in hdr_indices else 'N/A'
                final_rows.append((row_idx, row_data))
            rows = final_rows

        except Exception as e:
            return jsonify({"status": "error", "message": f"Failed to parse CSV: {str(e)}"}), 400
            
        # Check for duplication within CSV and database
        seen_codes_in_csv = {}
        duplicates = []
        
        conn = get_db_connection()
        for idx, row in rows:
            code = row['Employee Code']
            if not code:
                continue
            
            # Duplication within the CSV itself
            if code in seen_codes_in_csv:
                duplicates.append(f"Row {idx}: Employee Code '{code}' is duplicated in the file.")
            else:
                seen_codes_in_csv[code] = idx
                
            # Duplication check against SQLite database
            db_match = conn.execute("SELECT emp_name FROM employees WHERE emp_code=?", (code,)).fetchone()
            if db_match:
                duplicates.append(f"Row {idx}: Employee Code '{code}' ({row['Employee Name']}) already exists in the database as '{db_match['emp_name']}'.")
        
        if duplicates:
            conn.close()
            return jsonify({
                "status": "error", 
                "message": "This is the duplicacy. You remove that.",
                "details": duplicates
            }), 400
            
        # Insert records if no duplicates found
        now_str = datetime.datetime.now().strftime("%Y-%m-%d")
        for _, row in rows:
            try:
                conn.execute(
                    "INSERT INTO employees (emp_code, emp_name, branch_name, zone, division, business_unit, role, product_name, status, change_detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)",
                    (row['Employee Code'], row['Employee Name'], row['Branch Name'], row['Zone'], row['Division'], row['Business Unit'], row['Role'], row['Product Name'], f"UPLOADED VIA CSV ON {now_str}")
                )
            except Exception as e:
                conn.rollback()
                conn.close()
                return jsonify({"status": "error", "message": f"Database insertion failed: {str(e)}"}), 500
                
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "Roster uploaded and processed successfully!"})

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
    product_name = data.get('product_name', '').strip().upper()
    change_detail = data.get('change_detail', '').strip().upper()
    
    if not change_detail:
        change_detail = "ADDED MANUALLY"
        
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
        
    conn.execute("INSERT INTO employees (emp_code, emp_name, branch_name, zone, division, business_unit, role, product_name, status, change_detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)",
                 (emp_code, emp_name, branch_name, zone, division, business_unit, role, product_name, change_detail))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": f"Employee '{emp_name}' added manually successfully!"})

@app.route('/api/roster/<emp_code>', methods=['PUT', 'DELETE'])
def handle_single_roster_item(emp_code):
    emp_code = emp_code.upper().strip()
    conn = get_db_connection()
    
    if request.method == 'DELETE':
        hard = request.args.get('hard', 'false').lower() == 'true'
        try:
            if hard:
                conn.execute("DELETE FROM employees WHERE emp_code=?", (emp_code,))
                conn.commit()
                conn.close()
                return jsonify({"status": "success", "message": "Employee permanently deleted successfully"})
            else:
                reason = request.args.get('reason', '').strip().upper()
                if not reason:
                    try:
                        data = request.json or {}
                        reason = data.get('reason', '').strip().upper()
                    except Exception:
                        pass
                if not reason:
                    reason = "NO REASON SPECIFIED"
                
                now_str = datetime.datetime.now().strftime("%Y-%m-%d")
                conn.execute(
                    "UPDATE employees SET status='DELETED', change_detail=? WHERE emp_code=?",
                    (f"DELETED ON {now_str}: {reason}", emp_code)
                )
                conn.commit()
                conn.close()
                return jsonify({"status": "success", "message": "Employee status set to DELETED"})
        except Exception as e:
            conn.close()
            return jsonify({"status": "error", "message": str(e)}), 500
        
    elif request.method == 'PUT':
        data = request.json
        emp_name = data.get('emp_name', '').strip().upper()
        branch_name = data.get('branch_name', '').strip().upper()
        zone = data.get('zone', '').strip().upper()
        division = data.get('division', '').strip().upper()
        business_unit = data.get('business_unit', '').strip().upper()
        role = data.get('role', '').strip().upper()
        product_name = data.get('product_name', '').strip().upper()
        status = data.get('status', 'ACTIVE').strip().upper()
        change_detail = data.get('change_detail', '').strip().upper()
        
        if not emp_name:
            conn.close()
            return jsonify({"status": "error", "message": "Employee Name is required."}), 400
            
        try:
            conn.execute(
                "UPDATE employees SET emp_name=?, branch_name=?, zone=?, division=?, business_unit=?, role=?, product_name=?, status=?, change_detail=? WHERE emp_code=?",
                (emp_name, branch_name, zone, division, business_unit, role, product_name, status, change_detail, emp_code)
            )
            conn.commit()
        except Exception as e:
            conn.close()
            return jsonify({"status": "error", "message": str(e)}), 500
        conn.close()
        return jsonify({"status": "success", "message": "Employee updated successfully"})

@app.route('/api/roster/search', methods=['GET'])
def search_roster():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])
        
    conn = get_db_connection()
    results = conn.execute(
        "SELECT * FROM employees WHERE emp_name LIKE ? OR emp_code LIKE ? LIMIT 10",
        (f"%{query}%", f"%{query}%")
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in results])


# 4. MODULE MANAGEMENT (Maker-Checker & Dynamic AI Support)
@app.route('/api/modules', methods=['GET', 'POST'])
def handle_modules():
    conn = get_db_connection()
    if request.method == 'GET':
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

@app.route('/api/modules/generate', methods=['POST'])
def generate_module():
    count = int(request.form.get('count', 15))
    title = request.form.get('title', 'Product Refresher Policy').strip()
    trainer_id = request.form.get('trainer_id', 'ADMIN').strip()
    
    text_content = ""
    
    # 1. Parse uploaded PDF if present
    if 'file' in request.files:
        file = request.files['file']
        if file and file.filename != '':
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            
            try:
                import pypdf
                reader = pypdf.PdfReader(filepath)
                extracted_text = []
                for page in reader.pages:
                    txt = page.extract_text()
                    if txt:
                        extracted_text.append(txt)
                text_content = "\n".join(extracted_text)
            except Exception as e:
                print(f"Failed to parse PDF: {str(e)}")
                text_content = f"Uploaded PDF: {filename}"
                
    if not text_content:
        text_content = request.form.get('text', '').strip()
        
    if not text_content:
        text_content = "Default Two-Wheeler Policy Document"
        
    # 2. Try to call Gemini API
    gemini_success = False
    generated_questions = []
    
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if api_key:
        try:
            import google.generativeai as genai
            import json
            
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel('gemini-1.5-flash')
            
            prompt = f"""
            You are a senior Socratic Trainer with 20 years of experience.
            Perform deep research on this policy content and generate exactly {count} multiple-choice Socratic assessment questions.
            Each question must have exactly 4 choices (labeled Option A, Option B, Option C, Option D) and a correct option index (0 to 3).
            Ensure the questions are challenging, dialogue-oriented, and directly based on the key rules, constraints, numeric thresholds, and exceptions inside the text.
            
            Format your response STRICTLY as a JSON array of objects. Do not wrap in markdown or backticks.
            Example format:
            [
              {{
                "question": "What is the maximum loan ratio allowed under the new policy?",
                "options": ["75%", "85%", "90%", "100%"],
                "correctIndex": 1
              }}
            ]
            
            Policy content:
            {text_content}
            """
            
            response = model.generate_content(prompt)
            res_text = response.text.strip()
            if res_text.startswith("```"):
                res_text = res_text.split("json")[-1].split("```")[0].strip()
                
            generated_questions = json.loads(res_text)
            
            # --- PASS 2: Double Validation & Self-Correction ---
            if len(generated_questions) > 0:
                double_validation_prompt = f"""
                You are a Socratic Policy Auditor. Your task is to perform a two-step validation (Double Validation) on these Socratic questions against the source policy document.
                
                Here is the source policy document:
                \"\"\"
                {text_content}
                \"\"\"
                
                Here are the Socratic questions that were generated:
                {json.dumps(generated_questions, indent=2)}
                
                For EACH question in the array:
                1. **Validation Step 1 (Factual Accuracy & Depth)**: Cross-reference the question and options with the source document. Make sure the Socratic question is factually accurate, deep, and does not misrepresent any policy details. Correct any errors in options or text.
                2. **Validation Step 2 (Correct Index Audit)**: Audit the `correctIndex` (0 to 3) twice. Verify that the option at the `correctIndex` is mathematically and factually the only correct answer based strictly on the document. If it is wrong or misaligned, update the `correctIndex` to the correct option, or rewrite the option.
                
                Return the finalized, audited, and double-corrected questions array STRICTLY as a JSON array of objects. Do not wrap in markdown or backticks. Follow the exact same format as input.
                """
                
                audit_response = model.generate_content(double_validation_prompt)
                audit_res_text = audit_response.text.strip()
                if audit_res_text.startswith("```"):
                    audit_res_text = audit_res_text.split("json")[-1].split("```")[0].strip()
                
                audited_questions = json.loads(audit_res_text)
                if len(audited_questions) > 0:
                    generated_questions = audited_questions
                    print("AI Double-Validation completed successfully!")
            
            if len(generated_questions) > 0:
                gemini_success = True
        except Exception as e:
            print(f"Gemini API call failed, falling back to Socratic Offline Generator: {str(e)}")
            
    # 3. High-Fidelity Socratic Offline Fallback Generator
    if not gemini_success:
        # Standard offline pool of high-quality Socratic questions to serve
        offline_pool = [
            {
                "question": "Under the standard Two-Wheeler policy, what is the maximum Loan-to-Value (LTV) ratio permitted without special credit approvals?",
                "options": ["75%", "85%", "90%", "100%"],
                "correctIndex": 1
            },
            {
                "question": "What is the absolute minimum CIBIL score required for an executive to approve a 90% LTV loan amount?",
                "options": ["650", "700", "750", "800"],
                "correctIndex": 2
            },
            {
                "question": "Which specific verification document is strictly mandatory for any credit disbursement exceeding ₹2 Lakhs?",
                "options": ["Electricity Bill", "Rent Agreement", "ITR / Form 16", "Passport"],
                "correctIndex": 2
            },
            {
                "question": "If an applicant's monthly debt obligation exceeds 50% of net income, what is the maximum loan tenure permitted?",
                "options": ["24 Months", "36 Months", "48 Months", "60 Months"],
                "correctIndex": 1
            },
            {
                "question": "For co-applicants on a standard retail loan, whose CIBIL score is considered as the primary rating for approval?",
                "options": ["Primary applicant only", "Co-applicant only", "The higher score of the two", "The average score of both"],
                "correctIndex": 2
            },
            {
                "question": "What is the maximum age limit of the applicant at the time of loan maturity under the Two-Wheeler policy?",
                "options": ["58 Years", "60 Years", "65 Years", "70 Years"],
                "correctIndex": 2
            },
            {
                "question": "Under what circumstance can a loan be disbursed without a physical address verification report?",
                "options": ["Loan below ₹50,000", "Customer has active banking with us", "Under no circumstance", "Approved by Zone Credit Manager"],
                "correctIndex": 2
            },
            {
                "question": "What is the standard processing fee percentage charged for commercial vehicle loans?",
                "options": ["1.0%", "1.5%", "2.0%", "2.5%"],
                "correctIndex": 2
            },
            {
                "question": "Which of the following is considered an acceptable income proof for a self-employed applicant?",
                "options": ["3-month bank statement", "Declaration on letterhead", "Latest 2 years Audited ITR", "GST registration copy only"],
                "correctIndex": 2
            }
        ]
        
        generated_questions = []
        for i in range(count):
            pool_item = offline_pool[i % len(offline_pool)]
            edited_q = {
                "question": f"({title}) {pool_item['question']}" if i < 3 else pool_item['question'],
                "options": pool_item['options'],
                "correctIndex": pool_item['correctIndex'],
                "approved": 0
            }
            generated_questions.append(edited_q)
            
    return jsonify({
        "status": "success",
        "title": title,
        "count": len(generated_questions),
        "questions": generated_questions
    })

@app.route('/api/modules/save', methods=['POST'])
def save_module():
    data = request.json
    title = data.get('title', 'AI Generated Module').strip()
    trainer_id = data.get('trainer_id', 'ADMIN').strip()
    questions = data.get('questions', [])
    module_id = data.get('module_id') # If editing an existing draft
    
    if not questions:
        return jsonify({"status": "error", "message": "No questions provided to save."}), 400
        
    all_approved = all([int(q.get('approved', 0)) == 1 for q in questions])
    status = 'Ready' if all_approved else 'Pending Audit'
    
    conn = get_db_connection()
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d")
        cursor = conn.cursor()
        
        if module_id:
            # Update existing module
            cursor.execute(
                "UPDATE modules SET title=?, questions_count=?, status=? WHERE id=?",
                (title, len(questions), status, module_id)
            )
            # Delete old questions to replace them with the newly audited ones
            cursor.execute("DELETE FROM questions WHERE module_id=?", (module_id,))
        else:
            # Create new module
            cursor.execute(
                "INSERT INTO modules (title, questions_count, created_at, status, created_by) VALUES (?, ?, ?, ?, ?)",
                (title, len(questions), now, status, trainer_id)
            )
            module_id = cursor.lastrowid
            
        for q in questions:
            opts = q.get('options', ["Option A", "Option B", "Option C", "Option D"])
            cursor.execute(
                "INSERT INTO questions (module_id, question_text, option_a, option_b, option_c, option_d, correct_index, approved) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (module_id, q.get('question_text', q.get('question')), opts[0], opts[1], opts[2], opts[3], q.get('correctIndex', q.get('correct_index', 0)), q.get('approved', 0))
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
    data = request.json
    emp_code = data.get('emp_code', '').upper()
    module_id = data.get('module_id')
    assignment_day = data.get('assignment_day', 'zero day').upper()
    pre_test_score = data.get('pre_test_score')
    post_test_score = data.get('post_test_score')
    
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM assessment_results WHERE emp_code=? AND module_id=? AND assignment_day=?", 
                           (emp_code, module_id, assignment_day)).fetchone()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        if row:
            if pre_test_score is not None:
                conn.execute("UPDATE assessment_results SET pre_test_score=?, completed_at=? WHERE emp_code=? AND module_id=? AND assignment_day=?",
                             (pre_test_score, now_str, emp_code, module_id, assignment_day))
            if post_test_score is not None:
                conn.execute("UPDATE assessment_results SET post_test_score=?, completed_at=? WHERE emp_code=? AND module_id=? AND assignment_day=?",
                             (post_test_score, now_str, emp_code, module_id, assignment_day))
        else:
            p_val = pre_test_score if pre_test_score is not None else 0.0
            post_val = post_test_score if post_test_score is not None else 0.0
            conn.execute("INSERT INTO assessment_results (emp_code, module_id, assignment_day, pre_test_score, post_test_score, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
                         (emp_code, module_id, assignment_day, p_val, post_val, now_str))
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": f"Failed to save score: {str(e)}"}), 500
    conn.close()
    return jsonify({"status": "success", "message": "Assessment score saved successfully!"})

@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    zone_filter = request.args.get('zone', '').strip()
    division_filter = request.args.get('division', '').strip()
    branch_filter = request.args.get('branch', '').strip()
    emp_filter = request.args.get('emp_code', '').strip()
    
    # 1. Base query parts
    where_clauses = []
    query_params = []
    
    if zone_filter:
        where_clauses.append("e.zone = ?")
        query_params.append(zone_filter)
    if division_filter:
        where_clauses.append("e.division = ?")
        query_params.append(division_filter)
    if branch_filter:
        where_clauses.append("e.branch_name = ?")
        query_params.append(branch_filter)
    if emp_filter:
        where_clauses.append("e.emp_code = ?")
        query_params.append(emp_filter)
        
    where_str = ""
    if where_clauses:
        where_str = "WHERE " + " AND ".join(where_clauses)
        
    conn = get_db_connection()
    
    try:
        # A. Query Temporal averages for the current filter scope
        temporal_query = f"""
            SELECT ar.assignment_day, 
                   AVG(ar.pre_test_score) as avg_pre, 
                   AVG(ar.post_test_score) as avg_post,
                   COUNT(DISTINCT e.emp_code) as participants
            FROM assessment_results ar
            JOIN employees e ON ar.emp_code = e.emp_code
            {where_str}
            GROUP BY ar.assignment_day
        """
        results = conn.execute(temporal_query, query_params).fetchall()
        
        # B. Query Breakdown for the child entities in the current scope
        if emp_filter:
            group_field = "e.emp_code"
            display_field = "e.emp_name"
        elif branch_filter:
            group_field = "e.emp_code"
            display_field = "e.emp_name"
        elif division_filter:
            group_field = "e.branch_name"
            display_field = "e.branch_name"
        elif zone_filter:
            group_field = "e.division"
            display_field = "e.division"
        else:
            group_field = "e.zone"
            display_field = "e.zone"
            
        breakdown_query = f"""
            SELECT {group_field} AS entity_id,
                   {display_field} AS entity_name,
                   AVG(ar.pre_test_score) as avg_pre,
                   AVG(ar.post_test_score) as avg_post,
                   (AVG(ar.post_test_score) - AVG(ar.pre_test_score)) as avg_growth,
                   COUNT(DISTINCT e.emp_code) as participants
            FROM assessment_results ar
            JOIN employees e ON ar.emp_code = e.emp_code
            {where_str}
            GROUP BY {group_field}
            ORDER BY avg_growth DESC
        """
        breakdown_results = conn.execute(breakdown_query, query_params).fetchall()
        
        # C. Query lists of active filters to populate dynamic cascading dropdowns
        distinct_zones = conn.execute("SELECT DISTINCT zone FROM employees WHERE zone IS NOT NULL AND zone != ''").fetchall()
        distinct_divs = conn.execute("SELECT DISTINCT division, zone FROM employees WHERE division IS NOT NULL AND division != ''").fetchall()
        distinct_branches = conn.execute("SELECT DISTINCT branch_name, division, zone FROM employees WHERE branch_name IS NOT NULL AND branch_name != ''").fetchall()
        distinct_emps = conn.execute("SELECT DISTINCT emp_code, emp_name, branch_name FROM employees WHERE emp_code IS NOT NULL AND emp_code != ''").fetchall()
        
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500
        
    conn.close()
    
    # 2. Build temporal response payload with high-fidelity default fallback values
    payload = {
        'ZERO DAY': {'pre': 0.0, 'post': 0.0, 'count': 0},
        'SIX DAYS': {'pre': 0.0, 'post': 0.0, 'count': 0},
        'TWENTY DAYS': {'pre': 0.0, 'post': 0.0, 'count': 0}
    }
    
    has_live_data = False
    for r in results:
        day = r['assignment_day'].upper()
        if day in payload:
            payload[day]['pre'] = round(r['avg_pre'], 1)
            payload[day]['post'] = round(r['avg_post'], 1)
            payload[day]['count'] = r['participants']
            has_live_data = True
            
    payload_metadata = {
        "temporal": payload,
        "has_live_data": has_live_data,
        "breakdown": [
            {
                "id": b["entity_id"],
                "name": b["entity_name"],
                "pre": round(b["avg_pre"], 1),
                "post": round(b["avg_post"], 1),
                "growth": round(b["avg_growth"], 1),
                "count": b["participants"]
            } for b in breakdown_results
        ],
        "filter_options": {
            "zones": [z[0] for z in distinct_zones],
            "divisions": [{"name": d[0], "zone": d[1]} for d in distinct_divs],
            "branches": [{"name": br[0], "division": br[1], "zone": br[2]} for br in distinct_branches],
            "executives": [{"code": ec[0], "name": ec[1], "branch": ec[2]} for ec in distinct_emps]
        }
    }
    
    return jsonify(payload_metadata)

@app.route('/api/analytics/export', methods=['GET'])
def export_analytics():
    zone_filter = request.args.get('zone', '').strip()
    division_filter = request.args.get('division', '').strip()
    branch_filter = request.args.get('branch', '').strip()
    emp_filter = request.args.get('emp_code', '').strip()
    
    where_clauses = []
    query_params = []
    
    if zone_filter:
        where_clauses.append("e.zone = ?")
        query_params.append(zone_filter)
    if division_filter:
        where_clauses.append("e.division = ?")
        query_params.append(division_filter)
    if branch_filter:
        where_clauses.append("e.branch_name = ?")
        query_params.append(branch_filter)
    if emp_filter:
        where_clauses.append("e.emp_code = ?")
        query_params.append(emp_filter)
        
    where_str = ""
    if where_clauses:
        where_str = "WHERE " + " AND ".join(where_clauses)
        
    conn = get_db_connection()
    try:
        pivot_query = f"""
            SELECT 
                e.emp_code,
                e.emp_name,
                e.zone,
                e.division,
                e.branch_name,
                e.business_unit,
                e.role,
                MAX(CASE WHEN UPPER(ar.assignment_day) = 'ZERO DAY' THEN ar.pre_test_score END) AS zero_pre,
                MAX(CASE WHEN UPPER(ar.assignment_day) = 'ZERO DAY' THEN ar.post_test_score END) AS zero_post,
                MAX(CASE WHEN UPPER(ar.assignment_day) = 'SIX DAYS' THEN ar.pre_test_score END) AS six_pre,
                MAX(CASE WHEN UPPER(ar.assignment_day) = 'SIX DAYS' THEN ar.post_test_score END) AS six_post,
                MAX(CASE WHEN UPPER(ar.assignment_day) = 'TWENTY DAYS' THEN ar.pre_test_score END) AS twenty_pre,
                MAX(CASE WHEN UPPER(ar.assignment_day) = 'TWENTY DAYS' THEN ar.post_test_score END) AS twenty_post
            FROM employees e
            JOIN assessment_results ar ON e.emp_code = ar.emp_code
            {where_str}
            GROUP BY e.emp_code
        """
        rows = conn.execute(pivot_query, query_params).fetchall()
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500
    conn.close()
    
    # 2. Build CSV response in memory
    import io
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow([
        "Employee Code", "Employee Name", "Zone", "Division", "Branch Name", "BU", "Role",
        "Day 0 Pre-Test (%)", "Day 0 Post-Test (%)", "Day 0 Delta (%)",
        "Day 6 Pre-Test (%)", "Day 6 Post-Test (%)", "Day 6 Delta (%)",
        "Day 20 Pre-Test (%)", "Day 20 Post-Test (%)", "Day 20 Delta (%)",
        "Retention Decay (%)"
    ])
    
    for r in rows:
        z_pre = r["zero_pre"] if r["zero_pre"] is not None else ""
        z_post = r["zero_post"] if r["zero_post"] is not None else ""
        z_delta = round(z_post - z_pre, 1) if (z_post != "" and z_pre != "") else ""
        
        s_pre = r["six_pre"] if r["six_pre"] is not None else ""
        s_post = r["six_post"] if r["six_post"] is not None else ""
        s_delta = round(s_post - s_pre, 1) if (s_post != "" and s_pre != "") else ""
        
        t_pre = r["twenty_pre"] if r["twenty_pre"] is not None else ""
        t_post = r["twenty_post"] if r["twenty_post"] is not None else ""
        t_delta = round(t_post - t_pre, 1) if (t_post != "" and t_pre != "") else ""
        
        decay = round(t_post - z_post, 1) if (t_post != "" and z_post != "") else ""
        
        writer.writerow([
            r["emp_code"], r["emp_name"], r["zone"], r["division"], r["branch_name"], r["business_unit"], r["role"],
            z_pre, z_post, z_delta,
            s_pre, s_post, s_delta,
            t_pre, t_post, t_delta,
            decay
        ])
        
    from flask import Response
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=Socrates_Analytics_Report.csv"}
    )

@app.route('/api/dashboard/stats', methods=['GET'])
def get_dashboard_stats():
    trainer_id = request.args.get('trainer_id', '').strip().upper()
    conn = get_db_connection()
    
    where_clauses = []
    query_params = []
    
    # Extract Trainer Scopes if a trainer is logged in/impersonated
    if trainer_id and trainer_id != 'ADMIN':
        trainer = conn.execute("SELECT * FROM trainers WHERE trainer_id=?", (trainer_id,)).fetchone()
        if trainer:
            zones = trainer['zones'].strip().upper() if trainer['zones'] else 'ALL'
            divisions = trainer['divisions'].strip().upper() if trainer['divisions'] else 'ALL'
            branches = trainer['branches'].strip().upper() if trainer['branches'] else 'ALL'
            business_units = trainer['business_units'].strip().upper() if trainer['business_units'] else 'ALL'
            
            if zones != 'ALL' and zones != '':
                clause = "e.zone IN (" + ",".join(["?"] * len(zones.split(','))) + ")"
                where_clauses.append(clause)
                query_params.extend([z.strip() for z in zones.split(',')])
            if divisions != 'ALL' and divisions != '':
                clause = "e.division IN (" + ",".join(["?"] * len(divisions.split(','))) + ")"
                where_clauses.append(clause)
                query_params.extend([d.strip() for d in divisions.split(',')])
            if branches != 'ALL' and branches != '':
                clause = "e.branch_name IN (" + ",".join(["?"] * len(branches.split(','))) + ")"
                where_clauses.append(clause)
                query_params.extend([b.strip() for b in branches.split(',')])
            if business_units != 'ALL' and business_units != '':
                clause = "e.business_unit IN (" + ",".join(["?"] * len(business_units.split(','))) + ")"
                where_clauses.append(clause)
                query_params.extend([bu.strip() for bu in business_units.split(',')])

    where_str = ""
    if where_clauses:
        where_str = "WHERE " + " AND ".join(where_clauses)
        
    try:
        # 1. Main stats counters
        # Sessions: Count sessions of this trainer specifically, or matching their branches
        if trainer_id and trainer_id != 'ADMIN':
            sessions_res = conn.execute("SELECT COUNT(*) FROM training_sessions WHERE trainer_id=?", (trainer_id,)).fetchone()[0]
        else:
            sessions_res = conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0]
            
        # Visited branches
        if trainer_id and trainer_id != 'ADMIN':
            branches_res = conn.execute("SELECT COUNT(DISTINCT branch_name) FROM training_sessions WHERE trainer_id=?", (trainer_id,)).fetchone()[0]
            if not branches_res:
                # Fallback to scoped roster branches count
                br_query = f"SELECT COUNT(DISTINCT branch_name) FROM employees e {where_str}"
                branches_res = conn.execute(br_query, query_params).fetchone()[0]
        else:
            branches_res = conn.execute("SELECT COUNT(DISTINCT branch_name) FROM training_sessions").fetchone()[0]
            if not branches_res:
                branches_res = conn.execute("SELECT COUNT(DISTINCT branch_name) FROM employees WHERE branch_name IS NOT NULL AND branch_name != ''").fetchone()[0]
            
        # Execs trained (matching scope)
        execs_query = f"""
            SELECT COUNT(DISTINCT ar.emp_code) 
            FROM assessment_results ar
            JOIN employees e ON ar.emp_code = e.emp_code
            {where_str}
        """
        execs_res = conn.execute(execs_query, query_params).fetchone()[0]
        
        # Learning curve growth
        growth_query = f"""
            SELECT AVG(ar.post_test_score) - AVG(ar.pre_test_score) 
            FROM assessment_results ar
            JOIN employees e ON ar.emp_code = e.emp_code
            {where_str}
        """
        growth_res = conn.execute(growth_query, query_params).fetchone()[0]
        
        modules_res = conn.execute("SELECT COUNT(*) FROM modules").fetchone()[0]
        
        # 2. Recent Socratic Sessions
        if trainer_id and trainer_id != 'ADMIN':
            recent_sessions_rows = conn.execute('''
                SELECT ts.session_id, ts.date, ts.branch_name, m.title AS module_title, tr.name AS trainer_name,
                       (SELECT COUNT(DISTINCT emp_code) FROM assessment_results ar WHERE ar.module_id = ts.module_id AND ar.assignment_day = 'Day 0') AS attendee_count
                FROM training_sessions ts
                LEFT JOIN modules m ON ts.module_id = m.id
                LEFT JOIN trainers tr ON ts.trainer_id = tr.trainer_id
                WHERE ts.trainer_id = ?
                ORDER BY ts.date DESC, ts.session_id DESC LIMIT 5
            ''', (trainer_id,)).fetchall()
        else:
            recent_sessions_rows = conn.execute('''
                SELECT ts.session_id, ts.date, ts.branch_name, m.title AS module_title, tr.name AS trainer_name,
                       (SELECT COUNT(DISTINCT emp_code) FROM assessment_results ar WHERE ar.module_id = ts.module_id AND ar.assignment_day = 'Day 0') AS attendee_count
                FROM training_sessions ts
                LEFT JOIN modules m ON ts.module_id = m.id
                LEFT JOIN trainers tr ON ts.trainer_id = tr.trainer_id
                ORDER BY ts.date DESC, ts.session_id DESC LIMIT 5
            ''').fetchall()
        
        # 3. Top Branches by Learning Growth Delta (matching scope)
        top_branches_query = f"""
            SELECT e.branch_name, AVG(ar.post_test_score) - AVG(ar.pre_test_score) AS growth_delta, COUNT(DISTINCT ar.emp_code) AS count
            FROM assessment_results ar
            JOIN employees e ON ar.emp_code = e.emp_code
            {where_str}
            GROUP BY e.branch_name
            ORDER BY growth_delta DESC LIMIT 5
        """
        top_branches_rows = conn.execute(top_branches_query, query_params).fetchall()
        
        # 4. Pending Audits (Maker-Checker drafts awaiting trainer sign-off)
        pending_audits_rows = conn.execute('''
            SELECT m.id, m.title, m.questions_count,
                   (SELECT COUNT(*) FROM questions q WHERE q.module_id = m.id AND q.approved = 1) AS approved_count
            FROM modules m
            WHERE m.status = 'Pending Audit'
            ORDER BY m.id DESC LIMIT 5
        ''').fetchall()
        
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500
    conn.close()
    
    sessions = sessions_res
    branches = branches_res
    execs = execs_res
    growth = round(growth_res, 1) if growth_res is not None else 0.0
    
    recent_sessions = [dict(r) for r in recent_sessions_rows]
    top_branches = [
        {
            "branch_name": r["branch_name"],
            "growth_delta": round(r["growth_delta"], 1) if r["growth_delta"] is not None else 0.0,
            "count": r["count"]
        } for r in top_branches_rows
    ]
    pending_audits = [dict(r) for r in pending_audits_rows]
    
    return jsonify({
        "sessions_count": sessions,
        "branches_visited": branches,
        "execs_trained": execs,
        "avg_growth_delta": growth,
        "modules_count": modules_res,
        "recent_sessions": recent_sessions,
        "top_branches": top_branches,
        "pending_audits": pending_audits
    })

# --- WEBSOCKET EVENT LISTENERS (Flask-SocketIO) & GAMIFICATION STATE ---
import time

SESSION_REGISTRY = {}

@socketio.on('join_session')
def on_join_session(data):
    pin = str(data.get('pin'))
    emp_id = data.get('emp_id')
    join_room(pin)
    print(f"Employee {emp_id} connected to session PIN: {pin}")
    
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
        
    # Broadcast entire dynamic payload (includes questions/options) to trainee screen
    emit('change_view', data, room=pin)

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

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5050, host='0.0.0.0', allow_unsafe_werkzeug=True)
