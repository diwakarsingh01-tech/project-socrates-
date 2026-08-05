from flask import Flask, request, jsonify, render_template, session, Response
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

# Initialize SocketIO with threading async_mode for Python 3.12+ compatibility
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

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
        trainers = conn.execute("SELECT trainer_id as id, name, zone, status, role, last_login, password as plain_password FROM trainers ORDER BY trainer_id ASC").fetchall()
        conn.close()
        return jsonify([dict(t) for t in trainers])
    
    elif request.method == 'POST':
        data = request.json or {}
        t_id = str(data.get('id', '')).upper().strip()
        name = str(data.get('name', '')).strip()
        password = str(data.get('password', 'password123')).strip()
        role = str(data.get('role', 'Trainer')).strip()
        zone = str(data.get('zone', 'ALL')).strip()
        
        if not t_id or not name:
            conn.close()
            return jsonify({"status": "error", "message": "Trainer ID and Name are required."}), 400
            
        conn.execute(
            "INSERT INTO trainers (trainer_id, name, zone, password, role, status) VALUES (?, ?, ?, ?, ?, 'Active') ON CONFLICT(trainer_id) DO UPDATE SET name=excluded.name, password=excluded.password, role=excluded.role, status='Active'",
            (t_id, name, zone, password, role)
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
        
        conn.execute("""
            UPDATE trainers SET
                name=COALESCE(NULLIF(?, ''), name),
                password=COALESCE(NULLIF(?, ''), password),
                role=COALESCE(NULLIF(?, ''), role),
                zone=COALESCE(NULLIF(?, ''), zone)
            WHERE UPPER(trainer_id)=?
        """, (name, password, role, zone, trainer_id))
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
                    
                    conn.execute("""
                        INSERT INTO trainers (trainer_id, name, zone, password, role, status)
                        VALUES (?, ?, ?, ?, ?, 'Active')
                        ON CONFLICT(trainer_id) DO UPDATE SET
                            name=excluded.name,
                            zone=excluded.zone,
                            password=excluded.password,
                            role=excluded.role,
                            status='Active'
                    """, (t_id, t_name, t_zone, t_pwd, t_role))
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
        
        conn.close()
        return jsonify({
            "status": "success",
            "zones": zones,
            "divisions": divisions,
            "branches": branches,
            "business_units": business_units,
            "roles": roles,
            "products": products
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
                    for idx, h in enumerate(raw_headers):
                        h_norm = h.lower().replace('_', ' ').replace('-', ' ').strip()
                        if req_norm in h_norm or h_norm in req_norm:
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

        # Check for duplication within CSV and database
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
        for _, row in rows:
            try:
                conn.execute(
                    "INSERT INTO employees (emp_code, emp_name, branch_name, zone, division, business_unit, role, product_name, extra_data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (row['Employee Code'], row['Employee Name'], row['Branch Name'], row['Zone'], row['Division'], row['Business Unit'], row['Role'], row['Product Name'], row['Extra Data'])
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
                            
                            conn.execute("""
                                INSERT INTO assessment_results (emp_code, module_id, assignment_day, pre_test_score, post_test_score, completed_at)
                                VALUES (?, ?, ?, ?, ?, ?)
                                ON CONFLICT(emp_code, module_id, assignment_day) DO UPDATE SET
                                    pre_test_score=excluded.pre_test_score,
                                    post_test_score=excluded.post_test_score,
                                    completed_at=excluded.completed_at
                            """, (emp_code, module_id, day_key, p_score, post_score, session_date))
                            
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
            Analyze this policy content and generate exactly {count} multiple-choice Socratic assessment questions.
            Each question must have exactly 4 choices (labeled Option A, Option B, Option C, Option D) and a correct option index (0 to 3).
            Ensure the questions are challenging, dialogue-oriented, and directly based on the key rules inside the text.
            
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
    conn = get_db_connection()
    # Query average scores grouped by assignment_day
    try:
        results = conn.execute("""
            SELECT assignment_day, 
                   AVG(pre_test_score) as avg_pre, 
                   AVG(post_test_score) as avg_post,
                   COUNT(emp_code) as participants
            FROM assessment_results 
            GROUP BY assignment_day
        """).fetchall()
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500
    conn.close()
    
    # Prepare dynamic payload
    payload = {
        'ZERO DAY': {'pre': 40.0, 'post': 65.0, 'count': 0},
        'SIX DAYS': {'pre': 50.0, 'post': 80.0, 'count': 0},
        'TWENTY DAYS': {'pre': 60.0, 'post': 90.0, 'count': 0}
    }
    
    for r in results:
        day = r['assignment_day'].upper()
        if day in payload:
            payload[day]['pre'] = round(r['avg_pre'], 1)
            payload[day]['post'] = round(r['avg_post'], 1)
            payload[day]['count'] = r['participants']
            
    return jsonify(payload)

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
    socketio.run(app, debug=False, use_reloader=False, port=5050, host='0.0.0.0', allow_unsafe_werkzeug=True)
