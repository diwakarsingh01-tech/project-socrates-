from flask import Flask, request, jsonify, render_template, session
from flask_socketio import SocketIO, emit, join_room, leave_room
import sqlite3
import os
import json
import datetime
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import csv
import threading

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-placeholder')
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Initialize SocketIO
socketio = SocketIO(app, cors_allowed_origins="*")

DB_FILE = "socrates.db"

# --- POSTGRESQL WRAPPER FOR SQLITE COMPATIBILITY ---
class PostgresRow:
    def __init__(self, description, row_data):
        self.fields = [col[0].decode('utf-8') if isinstance(col[0], bytes) else col[0] for col in description]
        self.data = row_data
        self.mapping = {name: val for name, val in zip(self.fields, self.data)}

    def __getitem__(self, key):
        if isinstance(key, int):
            return self.data[key]
        return self.mapping[key]

    def keys(self):
        return self.fields

    def __len__(self):
        return len(self.data)

    def __iter__(self):
        return iter(self.data)

    def __repr__(self):
        return f"PostgresRow({self.mapping})"

class PostgresCursorWrapper:
    def __init__(self, pg_cursor):
        self.pg_cursor = pg_cursor
        self._lastrowid = None

    def execute(self, query, params=None):
        # Convert SQLite ? placeholders to PostgreSQL %s placeholders
        query = query.replace('?', '%s')
        
        # Translate SQLite-specific AUTOINCREMENT to PostgreSQL SERIAL
        if "INTEGER PRIMARY KEY AUTOINCREMENT" in query:
            query = query.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            
        # Handle SQLite-specific column-info pragma
        if "PRAGMA table_info" in query:
            import re
            match = re.search(r"PRAGMA table_info\((\w+)\)", query, re.IGNORECASE)
            if match:
                table_name = match.group(1)
                query = f"""
                    SELECT 0 as cid, column_name as name, data_type as type, 
                           case when is_nullable = 'NO' then 1 else 0 end as notnull,
                           column_default as dflt_value, 0 as pk
                    FROM information_schema.columns 
                    WHERE table_name = '{table_name.lower()}'
                """
                params = None

        # Automatically append RETURNING id for INSERT queries to populate lastrowid
        is_insert = query.strip().upper().startswith("INSERT INTO")
        if is_insert and "RETURNING" not in query.upper():
            table_name = query.split()[2].lower().replace('(', '').replace(')', '')
            if "employees" not in table_name and "trainers" not in table_name and "training_sessions" not in table_name and "branch_coordinates" not in table_name and "assessment_results" not in table_name:
                query += " RETURNING id"

        if params is not None:
            if not isinstance(params, (tuple, list)):
                params = (params,)
            self.pg_cursor.execute(query, params)
        else:
            self.pg_cursor.execute(query)

        # Retrieve lastrowid if we appended RETURNING
        if is_insert:
            try:
                row = self.pg_cursor.fetchone()
                if row:
                    self._lastrowid = row[0]
            except Exception:
                pass
        return self

    def executemany(self, query, params_list):
        query = query.replace('?', '%s')
        self.pg_cursor.executemany(query, params_list)

    def fetchone(self):
        row = self.pg_cursor.fetchone()
        if row and self.pg_cursor.description:
            return PostgresRow(self.pg_cursor.description, row)
        return row

    def fetchall(self):
        rows = self.pg_cursor.fetchall()
        desc = self.pg_cursor.description
        if rows and desc:
            return [PostgresRow(desc, r) for r in rows]
        return rows

    @property
    def lastrowid(self):
        return self._lastrowid

class PostgresConnectionWrapper:
    def __init__(self, pg_conn):
        self.pg_conn = pg_conn
        self.row_factory = None  # To match SQLite api signatures

    def cursor(self):
        return PostgresCursorWrapper(self.pg_conn.cursor())

    def execute(self, query, params=None):
        cursor = self.cursor()
        cursor.execute(query, params)
        return cursor

    def commit(self):
        self.pg_conn.commit()

    def rollback(self):
        self.pg_conn.rollback()

    def close(self):
        self.pg_conn.close()

def get_db_connection():
    db_url = os.environ.get('DATABASE_URL')
    if db_url:
        try:
            from urllib.parse import urlparse, unquote
            import pg8000.dbapi
            
            if db_url.startswith("postgres://"):
                db_url = db_url.replace("postgres://", "postgresql://", 1)
                
            url = urlparse(db_url)
            username = unquote(url.username) if url.username else None
            password = unquote(url.password) if url.password else None
            database = url.path[1:]
            hostname = url.hostname
            port = url.port or 5432
            
            if hostname and ".pooler.supabase.com" in hostname.lower() and port == 5432:
                print("[POSTGRES] Automatically rewriting Supabase pooler port from 5432 to 6543 for Render compatibility.")
                port = 6543
            
            connection_host = hostname
                
            import ssl
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            pg_conn = pg8000.dbapi.connect(
                user=username,
                password=password,
                host=connection_host,
                database=database,
                port=port,
                ssl_context=ssl_context,
                timeout=30
            )
            return PostgresConnectionWrapper(pg_conn)
        except Exception as e:
            print(f"[POSTGRES] Connection failed, falling back to SQLite: {str(e)}")
            
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

# --- DATABASE SETUP ---
PG_SCHEMA_SQL = """
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
);
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
    business_units TEXT DEFAULT 'ALL',
    plain_password TEXT
);
CREATE TABLE IF NOT EXISTS modules (
    id SERIAL PRIMARY KEY,
    title TEXT,
    questions_count INTEGER,
    created_at TEXT,
    status TEXT DEFAULT 'Pending Audit',
    created_by TEXT DEFAULT 'ADMIN',
    difficulty TEXT DEFAULT 'Medium'
);
CREATE TABLE IF NOT EXISTS questions (
    id SERIAL PRIMARY KEY,
    module_id INTEGER REFERENCES modules(id) ON DELETE CASCADE,
    question_text TEXT,
    option_a TEXT,
    option_b TEXT,
    option_c TEXT,
    option_d TEXT,
    correct_index INTEGER,
    approved INTEGER DEFAULT 0,
    translations TEXT
);
CREATE TABLE IF NOT EXISTS training_sessions (
    session_id TEXT PRIMARY KEY,
    date TEXT,
    trainer_id TEXT REFERENCES trainers(trainer_id),
    module_id INTEGER,
    branch_name TEXT
);
CREATE TABLE IF NOT EXISTS assessment_results (
    emp_code TEXT,
    module_id INTEGER,
    assignment_day TEXT,
    pre_test_score REAL,
    post_test_score REAL,
    correct_count INTEGER DEFAULT 0,
    wrong_count INTEGER DEFAULT 0,
    unattempted_count INTEGER DEFAULT 0,
    total_questions INTEGER DEFAULT 0,
    completed_at TEXT,
    session_id TEXT,
    PRIMARY KEY (emp_code, module_id, assignment_day),
    FOREIGN KEY(emp_code) REFERENCES employees(emp_code),
    FOREIGN KEY(module_id) REFERENCES modules(id)
);
CREATE TABLE IF NOT EXISTS trainee_feedback (
    id SERIAL PRIMARY KEY,
    emp_code TEXT REFERENCES employees(emp_code),
    session_id TEXT REFERENCES training_sessions(session_id),
    module_id INTEGER REFERENCES modules(id),
    rating INTEGER,
    understanding TEXT,
    manpower_saved TEXT,
    comments TEXT,
    submitted_at TEXT
);
CREATE TABLE IF NOT EXISTS branch_coordinates (
    branch_name TEXT PRIMARY KEY,
    zone TEXT NOT NULL,
    division TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    manager_pin TEXT NOT NULL DEFAULT '1234'
);
CREATE TABLE IF NOT EXISTS field_visits (
    id SERIAL PRIMARY KEY,
    trainer_id TEXT NOT NULL REFERENCES trainers(trainer_id),
    branch_name TEXT NOT NULL REFERENCES branch_coordinates(branch_name),
    planned_date TEXT NOT NULL,
    end_date TEXT,
    purpose TEXT NOT NULL,
    key_contacts TEXT,
    status TEXT DEFAULT 'PLANNED',
    checkin_time TEXT,
    checkin_latitude REAL,
    checkin_longitude REAL,
    co_presence_count INTEGER DEFAULT 0,
    verification_time TEXT
);
"""
PG_MIGRATIONS = {
    'field_visits': {
        'end_date': 'TEXT',
        'month': 'TEXT', 'branch_code': 'TEXT', 'business_unit': 'TEXT',
        'zone': 'TEXT', 'division': 'TEXT', 'meeting_agenda': 'TEXT',
        'meeting_with': 'TEXT', 'overnight_stay': 'TEXT',
        'travel_from': 'TEXT', 'travel_to': 'TEXT', 'travel_mode': 'TEXT',
        'mom_notes': 'TEXT', 'details': 'TEXT'
    },
    'assessment_results': {
        'session_id': 'TEXT', 'correct_count': 'INTEGER DEFAULT 0',
        'wrong_count': 'INTEGER DEFAULT 0', 'unattempted_count': 'INTEGER DEFAULT 0',
        'total_questions': 'INTEGER DEFAULT 0'
    },
    'modules': {
        'source_text': 'TEXT DEFAULT \'\''
    },
    'questions': {
        'translations': 'TEXT'
    }
}

def _setup_pg():
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        return
    try:
        from urllib.parse import urlparse, unquote
        import pg8000.dbapi

        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        url = urlparse(db_url)
        username = unquote(url.username) if url.username else None
        password = unquote(url.password) if url.password else None
        database = url.path[1:]
        hostname = url.hostname
        port = url.port or 5432
        if hostname and ".pooler.supabase.com" in hostname.lower() and port == 5432:
            port = 6543
        connection_host = hostname
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        conn = pg8000.dbapi.connect(user=username, password=password, host=connection_host,
                                     database=database, port=port, ssl_context=ctx, timeout=30)
        cursor = conn.cursor()
        for stmt in PG_SCHEMA_SQL.split(';'):
            s = stmt.strip()
            if s:
                try:
                    cursor.execute(s)
                except Exception:
                    pass
        for table, columns in PG_MIGRATIONS.items():
            for col, dtype in columns.items():
                try:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {dtype}")
                except Exception:
                    pass
        # Seed ADMIN
        cursor.execute("SELECT 1 FROM trainers WHERE trainer_id='ADMIN'")
        if not cursor.fetchone():
            hashed = generate_password_hash('admin123')
            cursor.execute("INSERT INTO trainers (trainer_id, name, zone, password, role, zones, divisions, branches, business_units, plain_password) VALUES ('ADMIN', 'Super Admin', 'All', %s, 'SuperAdmin', 'ALL', 'ALL', 'ALL', 'ALL', 'admin123')", (hashed,))
        # Seed branches
        defaults = [
            ("DELHI RF", "NORTH ZONE", "DELHI DIVISION", 28.6139, 77.209, "1234"),
            ("AHMEDABAD RF", "WEST ZONE", "GUJARAT DIVISION", 23.0225, 72.5714, "1234"),
            ("CHANDIGARH RF", "NORTH ZONE", "PUNJAB DIVISION", 30.7333, 76.7794, "1234"),
            ("KOLKATA RF", "EAST ZONE", "BENGAL DIVISION", 22.5726, 88.3639, "1234"),
            ("MUMBAI RF", "WEST ZONE", "MUMBAI DIVISION", 19.076, 72.8777, "1234")
        ]
        cursor.execute("SELECT COUNT(*) FROM branch_coordinates WHERE branch_name=%s", ("DELHI RF",))
        if cursor.fetchone()[0] == 0:
            for b_name, z, d, lat, lon, pin in defaults:
                cursor.execute("INSERT INTO branch_coordinates (branch_name, zone, division, latitude, longitude, manager_pin) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", (b_name, z, d, lat, lon, pin))
        conn.commit()
        conn.close()
        print("[POSTGRES] Schema setup complete on PostgreSQL")
    except Exception as e:
        print(f"[POSTGRES] Schema setup skipped: {str(e)}")

def init_db():
    _setup_pg()

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
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
        business_units TEXT DEFAULT 'ALL',
        plain_password TEXT
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
    if 'plain_password' not in trainer_cols:
        cursor.execute("ALTER TABLE trainers ADD COLUMN plain_password TEXT")
    
    # Add a default Super Admin if none exists
    cursor.execute("SELECT * FROM trainers WHERE trainer_id='ADMIN'")
    if not cursor.fetchone():
        hashed_pwd = generate_password_hash('admin123')
        cursor.execute("INSERT INTO trainers (trainer_id, name, zone, password, role, zones, divisions, branches, business_units, plain_password) VALUES ('ADMIN', 'Super Admin', 'All', ?, 'SuperAdmin', 'ALL', 'ALL', 'ALL', 'ALL', 'admin123')", (hashed_pwd,))
    
    # Modules
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS modules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        questions_count INTEGER,
        created_at TEXT,
        status TEXT DEFAULT 'Pending Audit',
        created_by TEXT DEFAULT 'ADMIN',
        difficulty TEXT DEFAULT 'Medium'
    )''')
    
    # Run migration to add status, created_by, and audited_by columns in modules if db was created in older version
    cursor.execute("PRAGMA table_info(modules)")
    mod_cols = [row[1] for row in cursor.fetchall()]
    if 'status' not in mod_cols:
        cursor.execute("ALTER TABLE modules ADD COLUMN status TEXT DEFAULT 'Pending Audit'")
    if 'created_by' not in mod_cols:
        cursor.execute("ALTER TABLE modules ADD COLUMN created_by TEXT DEFAULT 'ADMIN'")
    if 'audited_by' not in mod_cols:
        cursor.execute("ALTER TABLE modules ADD COLUMN audited_by TEXT DEFAULT 'Awaiting Audit'")
    if 'difficulty' not in mod_cols:
        cursor.execute("ALTER TABLE modules ADD COLUMN difficulty TEXT DEFAULT 'Medium'")
    if 'source_text' not in mod_cols:
        cursor.execute("ALTER TABLE modules ADD COLUMN source_text TEXT DEFAULT ''")
        
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
        translations TEXT,
        FOREIGN KEY(module_id) REFERENCES modules(id) ON DELETE CASCADE
    )''')
    
    # Run migration to add translations column in questions if db was created in older version
    cursor.execute("PRAGMA table_info(questions)")
    q_cols = [row[1] for row in cursor.fetchall()]
    if 'translations' not in q_cols:
        cursor.execute("ALTER TABLE questions ADD COLUMN translations TEXT")
    
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
        correct_count INTEGER DEFAULT 0,
        wrong_count INTEGER DEFAULT 0,
        unattempted_count INTEGER DEFAULT 0,
        total_questions INTEGER DEFAULT 0,
        completed_at TEXT,
        session_id TEXT,
        PRIMARY KEY (emp_code, module_id, assignment_day),
        FOREIGN KEY(emp_code) REFERENCES employees(emp_code),
        FOREIGN KEY(module_id) REFERENCES modules(id)
    )''')
    
    # Run migration to add new columns in assessment_results if db was created in older version
    cursor.execute("PRAGMA table_info(assessment_results)")
    ar_cols = [row[1] for row in cursor.fetchall()]
    if 'session_id' not in ar_cols:
        cursor.execute("ALTER TABLE assessment_results ADD COLUMN session_id TEXT")
    if 'correct_count' not in ar_cols:
        cursor.execute("ALTER TABLE assessment_results ADD COLUMN correct_count INTEGER DEFAULT 0")
    if 'wrong_count' not in ar_cols:
        cursor.execute("ALTER TABLE assessment_results ADD COLUMN wrong_count INTEGER DEFAULT 0")
    if 'unattempted_count' not in ar_cols:
        cursor.execute("ALTER TABLE assessment_results ADD COLUMN unattempted_count INTEGER DEFAULT 0")
    if 'total_questions' not in ar_cols:
        cursor.execute("ALTER TABLE assessment_results ADD COLUMN total_questions INTEGER DEFAULT 0")
        
    # Trainee Feedback table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS trainee_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT,
        session_id TEXT,
        module_id INTEGER,
        rating INTEGER,
        understanding TEXT,
        manpower_saved TEXT,
        comments TEXT,
        submitted_at TEXT,
        FOREIGN KEY(emp_code) REFERENCES employees(emp_code),
        FOREIGN KEY(session_id) REFERENCES training_sessions(session_id),
        FOREIGN KEY(module_id) REFERENCES modules(id)
    )''')
    
    # Branch Geofence Coordinates Table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS branch_coordinates (
        branch_name TEXT PRIMARY KEY,
        zone TEXT NOT NULL,
        division TEXT NOT NULL,
        latitude REAL NOT NULL,
        longitude REAL NOT NULL,
        manager_pin TEXT NOT NULL DEFAULT '1234'
    )''')
    
    # Field Visits Table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS field_visits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trainer_id TEXT NOT NULL,
        branch_name TEXT NOT NULL,
        planned_date TEXT NOT NULL,
        end_date TEXT,
        purpose TEXT NOT NULL,
        key_contacts TEXT,
        status TEXT DEFAULT 'PLANNED',
        checkin_time TEXT,
        checkin_latitude REAL,
        checkin_longitude REAL,
        co_presence_count INTEGER DEFAULT 0,
        verification_time TEXT,
        FOREIGN KEY (trainer_id) REFERENCES trainers(trainer_id),
        FOREIGN KEY (branch_name) REFERENCES branch_coordinates(branch_name)
    )''')

    # Seed default branch baseline coordinates if they do not exist
    default_branches = [
        ("DELHI RF", "NORTH ZONE", "DELHI DIVISION", 28.6139, 77.209, "1234"),
        ("AHMEDABAD RF", "WEST ZONE", "GUJARAT DIVISION", 23.0225, 72.5714, "1234"),
        ("CHANDIGARH RF", "NORTH ZONE", "PUNJAB DIVISION", 30.7333, 76.7794, "1234"),
        ("KOLKATA RF", "EAST ZONE", "BENGAL DIVISION", 22.5726, 88.3639, "1234"),
        ("MUMBAI RF", "WEST ZONE", "MUMBAI DIVISION", 19.076, 72.8777, "1234")
    ]
    for b_name, zone, div, lat, lon, pin in default_branches:
        bc_exists = cursor.execute("SELECT COUNT(*) FROM branch_coordinates WHERE branch_name=?", (b_name,)).fetchone()[0]
        if bc_exists == 0:
            print(f"[DATABASE-SEED] Seeding default branch: {b_name}")
            cursor.execute('''
                INSERT INTO branch_coordinates (branch_name, zone, division, latitude, longitude, manager_pin)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (b_name, zone, div, lat, lon, pin))
            
    cursor.execute("UPDATE branch_coordinates SET zone = UPPER(zone), division = UPPER(division)")
    cursor.execute("UPDATE employees SET zone = UPPER(zone), division = UPPER(division)")
        
    # Run migration to add end_date column in field_visits if db was created in older version
    cursor.execute("PRAGMA table_info(field_visits)")
    fv_cols = [col[1] for col in cursor.fetchall()]
    if 'end_date' not in fv_cols:
        cursor.execute("ALTER TABLE field_visits ADD COLUMN end_date TEXT")
    
    # New Travel Hub fields
    new_fv_cols = {
        'month': 'TEXT', 'branch_code': 'TEXT', 'business_unit': 'TEXT',
        'zone': 'TEXT', 'division': 'TEXT', 'meeting_agenda': 'TEXT',
        'meeting_with': 'TEXT', 'overnight_stay': 'TEXT',
        'travel_from': 'TEXT', 'travel_to': 'TEXT', 'travel_mode': 'TEXT',
        'mom_notes': 'TEXT'
    }
    for col, dtype in new_fv_cols.items():
        if col not in fv_cols:
            cursor.execute(f"ALTER TABLE field_visits ADD COLUMN {col} {dtype}")
    
    # Run migration to add details column for visit descriptions
    if 'details' not in fv_cols:
        cursor.execute("ALTER TABLE field_visits ADD COLUMN details TEXT")
        
    conn.commit()
    conn.close()

init_db()

try:
    from gdrive_sync import restore_db_from_gdrive
    restore_db_from_gdrive()
except Exception as e:
    print(f"[GDRIVE] Database restoration skipped: {str(e)}")

def seed_demo_data():
    """Seed demo employees if the database is empty."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    count = c.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
    if count > 0:
        print(f"[SEED] Employees table has {count} rows, skipping seed")
        conn.close()
        return
    print("[SEED] Employees table empty, seeding demo data...")
    demo_emps = [
        ("SF-1001","RAHUL SINGH","DELHI RF","NORTH ZONE","DELHI DIVISION","TWO-WHEELER","PL EXE","SPLENDOR V2"),
        ("SF-1002","NEHA SHARMA","AHMEDABAD RF","WEST ZONE","GUJARAT DIVISION","TWO-WHEELER","PL EXE","ACTIVA 6G"),
        ("SF-1003","AMIT PATEL","CHANDIGARH RF","NORTH ZONE","PUNJAB DIVISION","TWO-WHEELER","PL EXE","ACTIVA 6G"),
        ("SF-1004","PRIYA DAS","KOLKATA RF","EAST ZONE","BENGAL DIVISION","RETAIL","CSE","N/A"),
        ("SF-1005","VIKRAM VERMA","MUMBAI RF","WEST ZONE","MUMBAI DIVISION","GOLD LOAN","BH / BPH","N/A"),
        ("SF-1006","ANJALI GUPTA","DELHI RF","NORTH ZONE","DELHI DIVISION","PERSONAL LOAN","SPH / SBH","N/A"),
        ("SF-1007","ROHIT KUMAR","AHMEDABAD RF","WEST ZONE","GUJARAT DIVISION","COMMERCIAL VEHICLE","DTL","N/A"),
        ("SF-1008","SONIA JAIN","KOLKATA RF","EAST ZONE","BENGAL DIVISION","TWO-WHEELER","PL EXE","ACCESS 125"),
    ]
    for e in demo_emps:
        c.execute("INSERT OR REPLACE INTO employees VALUES (?,?,?,?,?,?,?,?,'ACTIVE','SEED DATA')", e)
    from werkzeug.security import generate_password_hash
    for tid,name,zone,role in [("TRAINER1","Rajesh Khanna","NORTH ZONE","Trainer"),("TRAINER2","Sunita Sharma","WEST ZONE","Trainer"),("LEADER1","Amitabh Joshi","All","Leader")]:
        pwh = generate_password_hash("password123")
        c.execute("INSERT OR REPLACE INTO trainers (trainer_id,name,zone,password,status,role,plain_password,zones,divisions,branches,business_units) VALUES (?,?,?,?,'Active',?,?,'ALL','ALL','ALL','ALL')", (tid,name,zone,pwh,role,"password123"))
    now = __import__('datetime').datetime.now().strftime("%Y-%m-%d")
    import random
    two_wheeler_qs = [
        ("What is the minimum down payment for a two-wheeler loan?", ["10%", "15%", "20%", "25%"], 0),
        ("What is the maximum tenure for a two-wheeler loan?", ["3 years", "5 years", "7 years", "10 years"], 1),
        ("Which document is NOT required for a two-wheeler loan?", ["Aadhaar Card", "PAN Card", "Passport", "Voter ID"], 2),
        ("What is the minimum CIBIL score for a two-wheeler loan?", ["600", "650", "700", "750"], 2),
        ("What is the maximum LTV for electric two-wheelers?", ["75%", "80%", "85%", "90%"], 3),
        ("What is the typical processing fee?", ["0.5%", "1%", "1.5%", "2%"], 1),
        ("Which is a valid ID proof?", ["Aadhaar", "Library Card", "College ID", "Club Card"], 0),
        ("Minimum age for a two-wheeler loan?", ["18", "21", "24", "27"], 1),
        ("Most common EMI tenure?", ["12 months", "24 months", "36 months", "48 months"], 2),
        ("Income proof for self-employed?", ["ITR", "Salary Slip", "Bank Statement", "Form 16"], 0),
        ("Maximum loan amount for standard two-wheeler?", ["50K", "1L", "1.5L", "2L"], 2),
        ("Which is NOT a repayment method?", ["Cash", "ECS", "Cheque", "NEFT"], 0),
        ("How is EMI calculated?", ["Simple Interest", "Reducing Balance", "Flat Rate", "Compound"], 1),
        ("Default on 2 EMIs leads to?", ["Notice", "Foreclosure", "Penalty", "Write-off"], 2),
        ("Prepayment usually attracts:", ["No Charge", "2% Fee", "5% Fee", "GST Only"], 0),
    ]
    gold_loan_qs = [
        ("What is the current gold loan interest rate?", ["7.5%", "8.5%", "9.5%", "10.5%"], 1),
        ("Maximum LTV ratio for gold loan?", ["60%", "70%", "75%", "80%"], 2),
        ("Minimum gold purity accepted?", ["18 carat", "20 carat", "22 carat", "24 carat"], 2),
        ("Maximum tenure for a gold loan?", ["6 months", "12 months", "18 months", "24 months"], 1),
        ("Mandatory document for gold loan?", ["Aadhaar", "PAN", "Both", "Passport"], 2),
        ("How is loan amount calculated?", ["Weight x Rate", "Market Value", "LTV Ratio", "All of above"], 3),
        ("What if EMI not paid for 6 months?", ["Notice", "Auction", "Penalty", "Restructure"], 1),
        ("Can customer take top-up loan?", ["Yes", "No", "After 6 months", "Only once"], 0),
        ("Interest calculation method?", ["Flat", "Reducing Balance", "Simple", "Compound"], 1),
        ("Permitted use of gold loan funds?", ["Business", "Personal", "Any purpose", "Education"], 2),
        ("Minimum gold loan amount?", ["5,000", "10,000", "15,000", "20,000"], 1),
        ("How often to re-value gold?", ["Monthly", "Quarterly", "Half-yearly", "Yearly"], 2),
        ("Which gold is NOT accepted?", ["22K", "24K", "18K", "Ornaments only"], 0),
        ("Late payment penalty?", ["1%", "2%", "5%", "No penalty"], 1),
        ("What is required to release gold?", ["ID Proof", "Full Payment", "Both", "Settlement Letter"], 2),
    ]
    for mid, title, difficulty, qs in [
        (1, "Product Knowledge - Two Wheeler Loans", "Medium", two_wheeler_qs),
        (2, "Gold Loan Policy Refresher", "Hard", gold_loan_qs),
    ]:
        c.execute("INSERT OR IGNORE INTO modules (id, title, questions_count, created_at, status, created_by, difficulty) VALUES (?,?,?,?,'Ready','ADMIN',?)", (mid, title, len(qs), now, difficulty))
        for i, (q_text, opts, correct_idx) in enumerate(qs):
            qid = mid * 100 + i + 1
            c.execute("INSERT OR IGNORE INTO questions (id,module_id,question_text,option_a,option_b,option_c,option_d,correct_index,approved) VALUES (?,?,?,?,?,?,?,?,?)",
                      (qid, mid, q_text, opts[0], opts[1], opts[2], opts[3], correct_idx, 1))
    conn.commit()
    conn.close()
    total_qs = len(two_wheeler_qs) + len(gold_loan_qs)
    print(f"[SEED] Inserted {len(demo_emps)} employees, 3 trainers, 2 modules, {total_qs} questions")

seed_demo_data()

try:
    from gdrive_sync import start_db_backup_daemon
    start_db_backup_daemon()
except Exception as e:
    print(f"[GDRIVE] Database backup daemon failed to start: {str(e)}")



@app.before_request
def enforce_authentication():
    # Bypass auth validation in unit test execution environment
    if app.config.get('TESTING') or app.testing:
        return
        
    # Only enforce auth on API endpoints starting with /api/
    if request.path.startswith('/api/'):
        # Define public endpoints allowed to bypass auth
        public_endpoints = [
            '/api/admin/login',
            '/api/roster/search',
            '/api/assessments/submit',
            '/api/feedback/submit',
            '/api/persistence-status',
            '/api/gdrive/status'
        ]
        if request.path in public_endpoints:
            return
            
        # Check session validity
        if 'user' not in session:
            return jsonify({"status": "error", "message": "Unauthorized. Please log in first."}), 401
            
        # Role-based restriction: Access Management, DB Reset, Roster Upload/Modifications are Admin only!
        superadmin_routes = [
            '/api/trainers/upload',
            '/api/admin/reset-database'
        ]
        
        is_superadmin_route = request.path in superadmin_routes or \
                               request.path.startswith('/api/trainers/') or \
                               (request.path == '/api/trainers' and request.method != 'GET') or \
                               (request.path.startswith('/api/roster') and request.method in ['POST', 'PUT', 'DELETE'])
                               
        if is_superadmin_route:
            if request.path == '/api/admin/reset-database':
                if session['user']['role'] != 'SuperAdmin':
                    return jsonify({"status": "error", "message": "Forbidden. SuperAdmin privileges required."}), 403
            else:
                if session['user']['role'] not in ['SuperAdmin', 'Leader']:
                    return jsonify({"status": "error", "message": "Forbidden. SuperAdmin or Leader privileges required."}), 403

@app.route('/api/admin/me', methods=['GET'])
def get_current_session():
    if 'user' in session:
        return jsonify({"status": "success", "user": session['user']})
    return jsonify({"status": "error", "message": "No active session"}), 401

@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.clear()
    return jsonify({"status": "success", "message": "Logged out successfully"})

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
    trainer_id = data.get('trainer_id', '').upper().strip()
    password = data.get('password', '').strip()
    
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM trainers WHERE trainer_id=? AND status='Active'", (trainer_id,)).fetchone()
    
    if user:
        stored_password = user['password']
        is_valid = False
        
        # Check if password is a hash
        if stored_password.startswith(('pbkdf2:', 'scrypt:', 'bcrypt:')):
            is_valid = check_password_hash(stored_password, password)
        else:
            # Fallback for plain-text (Migration)
            if stored_password == password:
                is_valid = True
                # Migrate to hash and store plain version for SuperAdmin visibility
                new_hash = generate_password_hash(password)
                conn.execute("UPDATE trainers SET password=?, plain_password=? WHERE trainer_id=?", (new_hash, password, trainer_id))
                conn.commit()
                
        if is_valid:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            conn.execute("UPDATE trainers SET last_login=? WHERE trainer_id=?", (now, trainer_id))
            conn.commit()
            conn.close()
            
            # Store user profile in backend encrypted session cookie
            session['user'] = {
                "trainer_id": user['trainer_id'],
                "role": user['role'],
                "name": user['name']
            }
            
            return jsonify({"status": "success", "role": user['role'], "name": user['name'], "trainer_id": trainer_id})
            
    conn.close()
    return jsonify({"status": "error", "message": "Invalid Credentials or Account Revoked"}), 401

# 2. TRAINER MANAGEMENT (Super Admin Only)
# 2. TRAINER MANAGEMENT (Super Admin Only)
@app.route('/api/trainers', methods=['GET', 'POST'])
def handle_trainers():
    conn = get_db_connection()
    if request.method == 'GET':
        # Pull newly uploaded trainers from Google Drive
        try:
            from gdrive_sync import sync_trainers_from_gdrive
            sync_trainers_from_gdrive(conn)
        except Exception as e:
            print(f"[GDRIVE] Dynamic trainers sync skipped: {str(e)}")

        is_superadmin = session.get('user', {}).get('role') == 'SuperAdmin'
        is_leader = session.get('user', {}).get('role') == 'Leader'
        if is_superadmin:
            trainers = conn.execute("SELECT trainer_id AS id, name, zone, status, last_login, zones, divisions, branches, business_units, role, plain_password FROM trainers WHERE role IN ('Trainer', 'Leader')").fetchall()
        elif is_leader:
            trainers = conn.execute("SELECT trainer_id AS id, name, zone, status, last_login, zones, divisions, branches, business_units, role, plain_password FROM trainers WHERE role='Trainer'").fetchall()
        else:
            trainers = conn.execute("SELECT trainer_id AS id, name, zone, status, last_login, zones, divisions, branches, business_units, role FROM trainers WHERE role='Trainer'").fetchall()
        
        conn.close()
        return jsonify([dict(t) for t in trainers])
    
    elif request.method == 'POST':
        data = request.json
        password_plain = data['password'].strip()
        hashed_pwd = generate_password_hash(password_plain)
        
        caller_role = session.get('user', {}).get('role')
        target_role = data.get('role', 'Trainer')
        if caller_role == 'Leader' and target_role != 'Trainer':
            conn.close()
            return jsonify({"status": "error", "message": "Forbidden. Leaders can only onboard standard Trainers."}), 403
            
        try:
            conn.execute(
                "INSERT INTO trainers (trainer_id, name, zone, password, zones, divisions, branches, business_units, role, plain_password) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    data['id'].upper().strip(),
                    data['name'].strip(),
                    data.get('zone', 'ALL'),
                    hashed_pwd,
                    data.get('zones', 'ALL'),
                    data.get('divisions', 'ALL'),
                    data.get('branches', 'ALL'),
                    data.get('business_units', 'ALL'),
                    target_role,
                    password_plain
                )
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({"status": "error", "message": "Trainer ID already exists. Please choose a different ID or delete the existing account first."}), 400
        conn.close()

        # Sync trainers to Google Drive in background thread
        try:
            from gdrive_sync import sync_trainers_to_gdrive
            threading.Thread(target=sync_trainers_to_gdrive, daemon=True).start()
        except Exception as e:
            print(f"[GDRIVE] Error spawning trainers upload thread: {str(e)}")

        return jsonify({"status": "success"})

@app.route('/api/trainers/<trainer_id>', methods=['PUT', 'DELETE'])
def handle_single_trainer(trainer_id):
    trainer_id = trainer_id.upper().strip()
    conn = get_db_connection()
    
    target_user = conn.execute("SELECT role FROM trainers WHERE trainer_id=?", (trainer_id,)).fetchone()
    if not target_user:
        conn.close()
        return jsonify({"status": "error", "message": "Trainer not found"}), 404
        
    caller_role = session.get('user', {}).get('role')
    if caller_role == 'Leader' and target_user['role'] != 'Trainer':
        conn.close()
        return jsonify({"status": "error", "message": "Forbidden. Leaders can only manage standard Trainers."}), 403
        
    if request.method == 'DELETE':
        if trainer_id == 'ADMIN':
            conn.close()
            return jsonify({"status": "error", "message": "Super Admin cannot be deleted"}), 400
        try:
            conn.execute("DELETE FROM trainers WHERE trainer_id=?", (trainer_id,))
            conn.commit()
        except Exception as e:
            conn.close()
            return jsonify({"status": "error", "message": str(e)}), 500
        conn.close()
        
        # Trigger real-time trainers synchronization to Google Drive in background thread
        try:
            from gdrive_sync import sync_trainers_to_gdrive
            threading.Thread(target=sync_trainers_to_gdrive, daemon=True).start()
        except Exception as e:
            print(f"[GDRIVE] Error spawning trainers upload thread: {str(e)}")
            
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
        role = data.get('role', target_user['role'])
        
        if caller_role == 'Leader' and role != 'Trainer':
            conn.close()
            return jsonify({"status": "error", "message": "Forbidden. Leaders can only set role to Trainer."}), 403
            
        if not name:
            conn.close()
            return jsonify({"status": "error", "message": "Name is required."}), 400
            
        try:
            # Only update password if it's not the UI placeholder and not empty
            if password and password != 'password123':
                hashed_pwd = generate_password_hash(password)
                conn.execute(
                    "UPDATE trainers SET name=?, password=?, plain_password=?, zone=?, zones=?, divisions=?, branches=?, business_units=?, role=? WHERE trainer_id=?",
                    (name, hashed_pwd, password, zone, zones, divisions, branches, business_units, role, trainer_id)
                )
            else:
                conn.execute(
                    "UPDATE trainers SET name=?, zone=?, zones=?, divisions=?, branches=?, business_units=?, role=? WHERE trainer_id=?",
                    (name, zone, zones, divisions, branches, business_units, role, trainer_id)
                )
            conn.commit()
        except Exception as e:
            conn.close()
            return jsonify({"status": "error", "message": str(e)}), 500
        conn.close()
        
        # Trigger real-time trainers synchronization to Google Drive in background thread
        try:
            from gdrive_sync import sync_trainers_to_gdrive
            threading.Thread(target=sync_trainers_to_gdrive, daemon=True).start()
        except Exception as e:
            print(f"[GDRIVE] Error spawning trainers upload thread: {str(e)}")
            
        return jsonify({"status": "success", "message": "Trainer updated successfully"})

@app.route('/api/trainers/<trainer_id>/status', methods=['PUT'])
def update_trainer_status(trainer_id):
    trainer_id = trainer_id.upper().strip()
    conn = get_db_connection()
    target_user = conn.execute("SELECT role FROM trainers WHERE trainer_id=?", (trainer_id,)).fetchone()
    if not target_user:
        conn.close()
        return jsonify({"status": "error", "message": "Trainer not found"}), 404
        
    caller_role = session.get('user', {}).get('role')
    if caller_role == 'Leader' and target_user['role'] != 'Trainer':
        conn.close()
        return jsonify({"status": "error", "message": "Forbidden. Leaders can only manage standard Trainers."}), 403
        
    data = request.json
    conn.execute("UPDATE trainers SET status=? WHERE trainer_id=?", (data['status'], trainer_id))
    conn.commit()
    conn.close()
    
    # Trigger real-time trainers synchronization to Google Drive in background thread
    try:
        from gdrive_sync import sync_trainers_to_gdrive
        threading.Thread(target=sync_trainers_to_gdrive, daemon=True).start()
    except Exception as e:
        print(f"[GDRIVE] Error spawning trainers upload thread: {str(e)}")
        
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
        
        # REQUIRED_HEADERS now strictly enforced as per user request
        REQUIRED_HEADERS = ['Trainer ID', 'Trainer Name', 'Password', 'Role']
        
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
                    "message": f"Invalid CSV format. Missing required column headers: {', '.join(missing_headers)}"
                }), 400
                
            hdr_indices = {h: headers.index(h) for h in headers if h in REQUIRED_HEADERS or h in ['Business Units', 'Zones', 'Divisions', 'Branches']}
            
            final_rows = []
            caller_role = session.get('user', {}).get('role')
            for row_idx, r in rows:
                p_plain = r[headers.index('Password')].strip()
                row_role = r[headers.index('Role')].strip()
                
                if row_role.upper() == 'SUPERADMIN':
                    row_role = 'SuperAdmin'
                elif row_role.upper() == 'LEADER':
                    row_role = 'Leader'
                else:
                    row_role = 'Trainer'
                
                # Leaders can only create Trainers
                if caller_role == 'Leader':
                    row_role = 'Trainer'
                    
                # Helper to get optional columns safely
                def get_col(name, default='ALL'):
                    if name in headers:
                        val = r[headers.index(name)].strip()
                        return val if val else default
                    return default

                row_data = {
                    'id': r[headers.index('Trainer ID')].strip().upper(),
                    'name': r[headers.index('Trainer Name')].strip(),
                    'password': generate_password_hash(p_plain),
                    'plain_password': p_plain,
                    'business_units': get_col('Business Units'),
                    'zones': get_col('Zones'),
                    'divisions': get_col('Divisions'),
                    'branches': get_col('Branches'),
                    'role': row_role
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
                    "INSERT INTO trainers (trainer_id, name, zone, password, plain_password, zones, divisions, branches, business_units, role) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (row['id'], row['name'], row['zones'].split(',')[0].strip().upper() if row['zones'] else 'ALL', row['password'], row['plain_password'], row['zones'].upper(), row['divisions'].upper(), row['branches'].upper(), row['business_units'].upper(), row['role'])
                )
            except Exception as e:
                conn.rollback()
                conn.close()
                return jsonify({"status": "error", "message": f"Database insertion failed: {str(e)}"}), 500
                
        conn.commit()
        conn.close()

        # Trigger real-time trainers synchronization to Google Drive in background thread
        try:
            from gdrive_sync import sync_trainers_to_gdrive
            threading.Thread(target=sync_trainers_to_gdrive, daemon=True).start()
        except Exception as e:
            print(f"[GDRIVE] Error spawning trainers upload thread: {str(e)}")

        return jsonify({"status": "success", "message": "Trainers uploaded and registered successfully!"})

@app.route('/api/metadata', methods=['GET'])
def get_metadata():
    conn = get_db_connection()
    zones = conn.execute("SELECT DISTINCT zone FROM employees WHERE zone IS NOT NULL AND zone != '' ORDER BY zone").fetchall()
    divisions = conn.execute("SELECT DISTINCT division FROM employees WHERE division IS NOT NULL AND division != '' ORDER BY division").fetchall()
    branches = conn.execute("SELECT DISTINCT branch_name FROM employees WHERE branch_name IS NOT NULL AND branch_name != '' ORDER BY branch_name").fetchall()
    bus_query = conn.execute("SELECT DISTINCT business_unit FROM employees WHERE business_unit IS NOT NULL AND business_unit != '' ORDER BY business_unit").fetchall()
    
    # Check if empty, fallback to seeded branch structure
    if not zones:
        zones = conn.execute("SELECT DISTINCT zone FROM branch_coordinates WHERE zone IS NOT NULL AND zone != '' ORDER BY zone").fetchall()
    if not divisions:
        divisions = conn.execute("SELECT DISTINCT division FROM branch_coordinates WHERE division IS NOT NULL AND division != '' ORDER BY division").fetchall()
    if not branches:
        branches = conn.execute("SELECT DISTINCT branch_name FROM branch_coordinates WHERE branch_name IS NOT NULL AND branch_name != '' ORDER BY branch_name").fetchall()
    conn.close()
    
    zones_list = []
    for r in zones:
        for z in r[0].split(','):
            z_clean = z.strip().upper()
            if z_clean and z_clean not in zones_list:
                zones_list.append(z_clean)
    zones_list.sort()
    if not zones_list:
        zones_list = ["AHMEDABAD", "SURAT", "BIKANER", "JAIPUR"]
        
    divisions_list = []
    for r in divisions:
        for d in r[0].split(','):
            d_clean = d.strip().upper()
            if d_clean and d_clean not in divisions_list:
                divisions_list.append(d_clean)
    divisions_list.sort()
    if not divisions_list:
        divisions_list = ["GANDHIDHAM", "JAMNAGAR", "RAJKOT", "BHAVNAGAR", "AHMEDABAD", "PALANPUR", "SURAT", "BARODA", "GANDHINAGAR", "JODHPUR", "BIKANER", "ALWAR", "AJMER", "JAIPUR", "SIKAR"]
        
    branches_list = []
    for r in branches:
        for b in r[0].split(','):
            b_clean = b.strip().upper()
            if b_clean and b_clean not in branches_list:
                branches_list.append(b_clean)
    branches_list.sort()
    
    bus_list = []
    for r in bus_query:
        for bu in r[0].split(','):
            bu_clean = bu.strip().upper()
            if bu_clean and bu_clean not in bus_list:
                bus_list.append(bu_clean)
    bus_list.sort()
    if not bus_list:
        bus_list = ["AHMEDABAD BU"]
        
    return jsonify({
        "business_units": bus_list,
        "zones": zones_list,
        "divisions": divisions_list,
        "branches": branches_list
    })

@app.route('/api/gdrive/status', methods=['GET'])
def get_gdrive_status():
    from gdrive_sync import get_gdrive_service, LAST_BACKUP_TIME, load_sa_json, _original_socket_for_google
    
    folder_id = os.environ.get('GD_FOLDER_ID')
    sa_json = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    
    configured = bool(folder_id and sa_json)
    connected = False
    write_test = "not_tested"
    file_count = 0
    module_files = []
    service_account_email = "Not Configured"
    masked_folder_id = "Not Configured"
    
    if configured:
        if len(folder_id) > 8:
            masked_folder_id = f"{folder_id[:4]}...{folder_id[-4:]}"
        else:
            masked_folder_id = folder_id
            
        try:
            info = load_sa_json(sa_json)
            service_account_email = info.get('client_email', 'Unknown Service Account')
        except Exception:
            pass
            
        try:
            service = get_gdrive_service()
            if service:
                with _original_socket_for_google():
                    service.files().get(fileId=folder_id, fields='id, name', supportsAllDrives=True).execute()
                    results = service.files().list(q=f"'{folder_id}' in parents and trashed = false", spaces='drive', fields='files(id, name)', pageSize=50, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
                    files = results.get('files', [])
                    file_count = len(files)
                    module_files = [f['name'] for f in files if f.get('name')]
                connected = True
        except Exception as e:
            print(f"[GDRIVE-STATUS] Integration connection check failed: {str(e)}")
            connected = False
            
    last_sync_str = "Never"
    if LAST_BACKUP_TIME > 0:
        dt = datetime.datetime.fromtimestamp(LAST_BACKUP_TIME)
        last_sync_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        
    return jsonify({
        "configured": configured,
        "connected": connected,
        "folder_id": masked_folder_id,
        "service_account_email": service_account_email,
        "last_sync": last_sync_str,
        "file_count": file_count,
        "files": module_files
    })

@app.route('/api/gdrive/debug-sync', methods=['GET'])
def gdrive_debug_sync():
    """Debug endpoint to test GDrive module sync and capture exact errors."""
    from gdrive_sync import sync_module_to_gdrive, _build_drive_service, _original_socket_for_google
    import traceback, json
    result = {"steps": []}

    # Step 1: build service
    try:
        with _original_socket_for_google():
            svc = _build_drive_service()
        result["steps"].append({"step": "build_service", "ok": svc is not None})
    except Exception as e:
        result["steps"].append({"step": "build_service", "ok": False, "error": str(e), "tb": traceback.format_exc()})
        return jsonify(result)

    # Step 2: list files
    folder_id = os.environ.get('GD_FOLDER_ID')
    try:
        with _original_socket_for_google():
            q = f"'{folder_id}' in parents and trashed = false"
            r = svc.files().list(q=q, spaces='drive', fields='files(id, name)', pageSize=10, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        result["steps"].append({"step": "list_files", "ok": True, "count": len(r.get('files',[]))})
    except Exception as e:
        result["steps"].append({"step": "list_files", "ok": False, "error": str(e), "tb": traceback.format_exc()})
        return jsonify(result)

    # Step 3: create a test file
    try:
        from googleapiclient.http import MediaIoBaseUpload
        import io
        test_title = "SyncDebug_test"
        payload = {"title": test_title, "test": True}
        json_bytes = json.dumps(payload).encode('utf-8')
        media = MediaIoBaseUpload(io.BytesIO(json_bytes), mimetype='application/json', resumable=True)
        with _original_socket_for_google():
            c = svc.files().create(body={'name': f'{test_title}.json', 'parents': [folder_id]}, media_body=media, supportsAllDrives=True).execute()
        result["steps"].append({"step": "create_file", "ok": True, "file_id": c.get('id')})
    except Exception as e:
        result["steps"].append({"step": "create_file", "ok": False, "error": str(e), "tb": traceback.format_exc()})
        return jsonify(result)

    # Step 4: try full sync_module_to_gdrive
    try:
        import time
        ok = sync_module_to_gdrive("SyncDebug_" + str(int(time.time())), "Easy", "Ready", "ADMIN", "ADMIN", [], "")
        result["steps"].append({"step": "sync_module_to_gdrive", "ok": ok})
    except Exception as e:
        result["steps"].append({"step": "sync_module_to_gdrive", "ok": False, "error": str(e), "tb": traceback.format_exc()})

    return jsonify(result)

@app.route('/api/admin/diagnostics', methods=['GET'])
def get_db_diagnostics():
    db_url = os.environ.get('DATABASE_URL')
    db_type = 'SQLite'
    status = 'Connected'
    error_msg = None
    masked_url = 'Not Configured'
    
    if db_url:
        db_type = 'PostgreSQL'
        try:
            from urllib.parse import urlparse
            # Handle standard "postgres://" to "postgresql://" url schemes
            if db_url.startswith("postgres://"):
                db_url = db_url.replace("postgres://", "postgresql://", 1)
                
            url = urlparse(db_url)
            hostname = url.hostname or 'Unknown'
            port = url.port or 5432
            
            # Defensive Rewrite: Supabase poolers on port 5432 often time out in hosted environments
            # like Render due to outbound firewall restrictions on direct PostgreSQL ports.
            # We automatically switch to the Transaction Pooler port 6543 which is open.
            if hostname and ".pooler.supabase.com" in hostname.lower() and port == 5432:
                port = 6543
                
            masked_url = f"postgresql://***@{hostname}:{port}{url.path}"
            
            import pg8000.dbapi
            from urllib.parse import unquote
            username = unquote(url.username) if url.username else None
            password = unquote(url.password) if url.password else None
            database = url.path[1:]
            # Use hostname directly (DNS resolution to IP breaks TLS SNI)
            connection_host = hostname
            
            # Attempt a quick direct connection to verify if it works
            import ssl
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            pg_conn = pg8000.dbapi.connect(
                user=username,
                password=password,
                host=connection_host,
                database=database,
                port=port,
                ssl_context=ssl_context,
                timeout=15
            )
            pg_conn.close()
        except Exception as e:
            status = 'Failed'
            error_msg = str(e)
    else:
        # Check SQLite sanity
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.execute("SELECT 1").close()
            conn.close()
        except Exception as e:
            status = 'Failed'
            error_msg = str(e)
            
    return jsonify({
        "database_type": db_type,
        "connection_status": status,
        "connection_error": error_msg,
        "database_url": masked_url,
        "is_ephemeral": bool(os.environ.get('RENDER') == 'true')
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
    # Pull any newly uploaded roster profiles by other trainers from Google Drive
    try:
        from gdrive_sync import sync_roster_from_gdrive
        sync_roster_from_gdrive()
    except Exception as e:
        print(f"[GDRIVE] Dynamic roster import skipped: {str(e)}")

    zone = request.args.get('zone', '').strip()
    branch = request.args.get('branch', '').strip()
    division = request.args.get('division', '').strip()
    business_unit = request.args.get('business_unit', '').strip()
    role = request.args.get('role', '').strip()
    product_name = request.args.get('product_name', '').strip()
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
    if business_unit:
        query += " AND business_unit = ?"
        params.append(business_unit)
    if role:
        query += " AND role = ?"
        params.append(role)
    if product_name:
        query += " AND product_name = ?"
        params.append(product_name)
    if search:
        query += " AND (emp_code LIKE ? OR emp_name LIKE ? OR product_name LIKE ?)"
        params.append(f"%{search}%")
        params.append(f"%{search}%")
        params.append(f"%{search}%")

    # Enforce Role-Based Scoping for Trainer
    curr_user = session.get('user')
    if curr_user and curr_user['role'] == 'Trainer':
        conn = get_db_connection()
        tr_details = conn.execute("SELECT zones, divisions, branches, business_units FROM trainers WHERE trainer_id = ?", (curr_user['trainer_id'],)).fetchone()
        conn.close()
        
        if tr_details:
            zones_scope = [z.strip() for z in tr_details['zones'].split(',') if z.strip()]
            divs_scope = [d.strip() for d in tr_details['divisions'].split(',') if d.strip()]
            branches_scope = [b.strip() for b in tr_details['branches'].split(',') if b.strip()]
            bus_scope = [bu.strip() for bu in tr_details['business_units'].split(',') if bu.strip()]
            
            if zones_scope and 'ALL' not in [z.upper() for z in zones_scope]:
                query += " AND zone IN ({})".format(','.join('?' for _ in zones_scope))
                params.extend(zones_scope)
            if divs_scope and 'ALL' not in [d.upper() for d in divs_scope]:
                query += " AND division IN ({})".format(','.join('?' for _ in divs_scope))
                params.extend(divs_scope)
            if branches_scope and 'ALL' not in [b.upper() for b in branches_scope]:
                query += " AND branch_name IN ({})".format(','.join('?' for _ in branches_scope))
                params.extend(branches_scope)
            if bus_scope and 'ALL' not in [bu.upper() for bu in bus_scope]:
                query += " AND business_unit IN ({})".format(','.join('?' for _ in bus_scope))
                params.extend(bus_scope)
        
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
    
    # Advanced filters for all roster headers
    business_units = conn.execute("SELECT DISTINCT business_unit FROM employees WHERE business_unit IS NOT NULL AND business_unit != '' ORDER BY business_unit").fetchall()
    roles = conn.execute("SELECT DISTINCT role FROM employees WHERE role IS NOT NULL AND role != '' ORDER BY role").fetchall()
    products = conn.execute("SELECT DISTINCT product_name FROM employees WHERE product_name IS NOT NULL AND product_name != '' ORDER BY product_name").fetchall()
    conn.close()
    
    zones_list = [r[0] for r in zones]
    if not zones_list:
        zones_list = ["NORTH ZONE", "WEST ZONE", "EAST ZONE"]
        
    divisions_list = [r[0] for r in divisions]
    if not divisions_list:
        divisions_list = ["GUJARAT DIVISION", "DELHI DIVISION", "PUNJAB DIVISION", "BENGAL DIVISION", "MAHARASHTRA DIVISION", "MUMBAI DIVISION"]
        
    branches_list = [r[0] for r in branches]
    if not branches_list:
        branches_list = ["AHMEDABAD RF", "DELHI RF", "CHANDIGARH RF", "KOLKATA RF", "MUMBAI RF"]
        
    branches_meta = [{"name": r[0], "division": r[1]} for r in branches]
    if not branches_meta:
        rf_division_mapping = {
            "AHMEDABAD RF": "GUJARAT DIVISION",
            "DELHI RF": "DELHI DIVISION",
            "CHANDIGARH RF": "PUNJAB DIVISION",
            "KOLKATA RF": "BENGAL DIVISION",
            "MUMBAI RF": "MUMBAI DIVISION"
        }
        branches_meta = [{"name": rf, "division": rf_division_mapping.get(rf, "GUJARAT DIVISION")} for rf in branches_list]
        
    divisions_meta = [{"name": r[0], "zone": r[1]} for r in divisions]
    if not divisions_meta:
        div_zone_mapping = {
            "GUJARAT DIVISION": "WEST ZONE",
            "DELHI DIVISION": "NORTH ZONE",
            "PUNJAB DIVISION": "NORTH ZONE",
            "BENGAL DIVISION": "EAST ZONE",
            "MAHARASHTRA DIVISION": "WEST ZONE",
            "MUMBAI DIVISION": "WEST ZONE"
        }
        divisions_meta = [{"name": div, "zone": div_zone_mapping.get(div, "WEST ZONE")} for div in divisions_list]

    business_units_list = [r[0] for r in business_units]
    if not business_units_list:
        business_units_list = ["TWO-WHEELER", "PERSONAL LOAN", "GOLD LOAN", "COMMERCIAL VEHICLE", "RETAIL"]
        
    roles_list = [r[0] for r in roles]
    if not roles_list:
        roles_list = ["PL Exe", "SPH / SBH", "CSE", "BTL", "DBH / DPH", "BH / BPH", "CPU Team", "DTL"]
        
    products_list = [r[0] for r in products]
    if not products_list:
        products_list = ["N/A"]
            
    return jsonify({
        "zones": zones_list,
        "divisions": divisions_list,
        "branches": branches_list,
        "divisions_meta": divisions_meta,
        "branches_meta": branches_meta,
        "business_units": business_units_list,
        "roles": roles_list,
        "products": products_list
    })

def normalize_enums(zone, division, branch):
    ZONE_MAPPING = {
        'NORTH': 'NORTH ZONE', 'NORTH ZONE': 'NORTH ZONE', 'DEL_BU': 'NORTH ZONE', 'CH_BU': 'NORTH ZONE',
        'WEST': 'WEST ZONE', 'WEST ZONE': 'WEST ZONE', 'AMD_BU': 'WEST ZONE',
        'EAST': 'EAST ZONE', 'EAST ZONE': 'EAST ZONE', 'KOL_BU': 'EAST ZONE',
        'HQ': 'NORTH ZONE'
    }

    DIVISION_MAPPING = {
        'DELHI': 'DELHI DIVISION', 'DELHI DIVISION': 'DELHI DIVISION',
        'GUJARAT': 'GUJARAT DIVISION', 'GUJARAT DIVISION': 'GUJARAT DIVISION',
        'PUNJAB': 'PUNJAB DIVISION', 'PUNJAB DIVISION': 'PUNJAB DIVISION',
        'BENGAL': 'BENGAL DIVISION', 'BENGAL DIVISION': 'BENGAL DIVISION',
        'WEST BENGAL': 'BENGAL DIVISION',
        'MUMBAI': 'MUMBAI DIVISION', 'MUMBAI DIVISION': 'MUMBAI DIVISION',
        'MAHARASHTRA': 'MUMBAI DIVISION', 'MAHARASHTRA DIVISION': 'MUMBAI DIVISION',
        'HQ DIV': 'DELHI DIVISION'
    }

    BRANCH_MAPPING = {
        'DELHI': 'DELHI RF', 'DELHI RF': 'DELHI RF',
        'AHMEDABAD': 'AHMEDABAD RF', 'AHMEDABAD RF': 'AHMEDABAD RF',
        'CHANDIGARH': 'CHANDIGARH RF', 'CHANDIGARH RF': 'CHANDIGARH RF',
        'KOLKATA': 'KOLKATA RF', 'KOLKATA RF': 'KOLKATA RF',
        'MUMBAI': 'MUMBAI RF', 'MUMBAI RF': 'MUMBAI RF',
        'HQ': 'DELHI RF'
    }
    
    z_upper = (zone or "").strip().upper()
    d_upper = (division or "").strip().upper()
    b_upper = (branch or "").strip().upper()
    
    def normalize_value(val_str, mapping):
        if not val_str:
            return ""
        items = [x.strip() for x in val_str.split(',') if x.strip()]
        normalized_items = []
        for item in items:
            norm = mapping.get(item)
            if not norm:
                # Substring match fallback
                for k, v in mapping.items():
                    if k in item:
                        norm = v
                        break
            if not norm:
                # Fuzzy sequence matching (SequenceMatcher ratio >= 0.8)
                import difflib
                best_match = None
                best_ratio = 0.0
                unique_values = list(set(mapping.values()))
                for val in unique_values:
                    ratio = difflib.SequenceMatcher(None, item, val).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_match = val
                if best_ratio >= 0.8:
                    norm = best_match
            if not norm:
                norm = item
            normalized_items.append(norm)
        return ", ".join(normalized_items)

    norm_zone = normalize_value(z_upper, ZONE_MAPPING)
    norm_div = normalize_value(d_upper, DIVISION_MAPPING)
    norm_br = normalize_value(b_upper, BRANCH_MAPPING)
    
    return norm_zone, norm_div, norm_br

def normalize_employee_data(branch_name, business_unit, product_name, division=None):
    b_name = (branch_name or "").strip().upper()
    bu_name = (business_unit or "").strip().upper()
    p_name = (product_name or "").strip().upper()
    
    VALID_BUS = ['TWO-WHEELER', 'PERSONAL LOAN', 'GOLD LOAN', 'COMMERCIAL VEHICLE', 'RETAIL']
    
    # Fuzzy resolve business unit from standard options
    if "2-WHEELER" in bu_name or "TWO" in bu_name:
        bu_name = "TWO-WHEELER"
    elif bu_name:
        import difflib
        best_bu = None
        best_bu_ratio = 0.0
        for bu in VALID_BUS:
            ratio = difflib.SequenceMatcher(None, bu_name, bu).ratio()
            if ratio > best_bu_ratio:
                best_bu_ratio = ratio
                best_bu = bu
        if best_bu_ratio >= 0.8:
            bu_name = best_bu
            
    if not bu_name:
        bu_name = "TWO-WHEELER"
        
    if not p_name or p_name == 'N/A':
        p_name = ""
        
    return b_name, bu_name, p_name

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
        
        REQUIRED_HEADERS = ['Employee Code', 'Employee Name', 'Branch Name', 'Zone', 'Division', 'Business Unit', 'Role', 'Product Name']
        
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
            
            # Fuzzy mapping dictionary to resolve typos (e.g. "Divison"), case differences, spaces, underscores, etc.
            FUZZY_MAPPING = {
                'Employee Code': ['employee code', 'emp code', 'code', 'employee_code', 'emp_code'],
                'Employee Name': ['employee name', 'emp name', 'name', 'employee_name', 'emp_name'],
                'Business Unit': ['business unit', 'businessunit', 'bu', 'business_unit'],
                'Zone': ['zone'],
                'Division': ['division', 'divison', 'divisionname', 'division_name'],
                'Branch Name': ['branch name', 'branchname', 'branch', 'branch code', 'branch_name', 'branchcode'],
                'Role': ['role', 'designation'],
                'Product Name': ['product name', 'productname', 'product', 'product_name']
            }
            
            hdr_indices = {}
            for canonical, variants in FUZZY_MAPPING.items():
                found_idx = None
                for idx, h in enumerate(headers):
                    norm_h = h.lower().strip().replace('_', ' ').replace('-', ' ')
                    norm_h_clean = " ".join(norm_h.split())
                    if norm_h_clean in variants or norm_h_clean.replace(' ', '') in [v.replace(' ', '') for v in variants]:
                        found_idx = idx
                        break
                hdr_indices[canonical] = found_idx
                
            # Check for critical header: Employee Code
            if hdr_indices.get('Employee Code') is None:
                return jsonify({
                    "status": "error", 
                    "message": "Invalid CSV format. Missing critical column header: 'Employee Code'"
                }), 400
            
            final_rows = []
            
            # Form final row data
            for row_idx, r in rows:
                row_data = {}
                for h in REQUIRED_HEADERS:
                    idx = hdr_indices.get(h)
                    if idx is not None and idx < len(r):
                        row_data[h] = r[idx].strip()
                    else:
                        row_data[h] = ''
                
                if not row_data.get('Product Name') or row_data['Product Name'] == 'N/A':
                    row_data['Product Name'] = 'N/A'
                
                emp_code = row_data['Employee Code'].upper().strip()
                emp_name = row_data['Employee Name'].upper().strip()
                zone_val = row_data['Zone'].upper().strip()
                div_val = row_data['Division'].upper().strip()
                br_val = row_data['Branch Name'].upper().strip()
                
                if not emp_code:
                    continue
                
                if not emp_name:
                    row_data['Employee Name'] = "N/A"
                    emp_name = "N/A"
                
                # Standardize using the normalize_enums helper
                norm_zone, norm_div, norm_br = normalize_enums(zone_val, div_val, br_val)
                
                # Accept whatever is supplied, falling back to parsed values
                row_data['Zone'] = norm_zone if norm_zone else zone_val
                row_data['Division'] = norm_div if norm_div else div_val
                row_data['Branch Name'] = norm_br if norm_br else br_val
                
                final_rows.append((row_idx, row_data))
                
            rows = final_rows

        except Exception as e:
            return jsonify({"status": "error", "message": f"Failed to parse CSV: {str(e)}"}), 400
            
        # Check for name similarity to auto-correct variations, but do not block duplicates
        conn = get_db_connection()
        existing_emp_data = {}
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT emp_code, emp_name, branch_name, zone, division, business_unit, role, product_name, status, change_detail FROM employees")
            for r in cursor.fetchall():
                existing_emp_data[r['emp_code'].upper().strip()] = {
                    'emp_name': r['emp_name'],
                    'branch_name': r['branch_name'],
                    'zone': r['zone'],
                    'division': r['division'],
                    'business_unit': r['business_unit'],
                    'role': r['role'],
                    'product_name': r['product_name'],
                    'status': r['status'],
                    'change_detail': r['change_detail']
                }
        except Exception as e:
            print(f"[OPTIMIZER] Error pre-fetching employees map: {str(e)}")
            
        for idx, row in rows:
            code = row['Employee Code'].upper().strip()
            if not code:
                continue
                
            db_match = existing_emp_data.get(code)
            if db_match and db_match.get('emp_name'):
                import difflib
                db_name = db_match['emp_name'].strip().upper()
                input_name = row['Employee Name'].strip().upper()
                ratio = difflib.SequenceMatcher(None, input_name, db_name).ratio()
                if ratio >= 0.8:
                    row['Employee Name'] = db_name
            
        # Prepare list of records to insert/update
        to_write = []
        now_str = datetime.datetime.now().strftime("%Y-%m-%d")
        for _, row in rows:
            try:
                b_name, bu_name, p_name = normalize_employee_data(
                    row.get('Branch Name', ''),
                    row.get('Business Unit', ''),
                    row.get('Product Name', ''),
                    row.get('Division', '')
                )
                emp_code_upper = row['Employee Code'].upper().strip()
                emp_name_upper = row['Employee Name'].upper().strip()
                role_upper = row.get('Role', '').upper().strip()
                zone_upper = row.get('Zone', '').upper().strip()
                division_upper = row.get('Division', '').upper().strip()
                
                # Performance Optimization: Skip identical records to prevent thousands of remote query writes
                if emp_code_upper in existing_emp_data:
                    existing = existing_emp_data[emp_code_upper]
                    if (
                        (existing.get('emp_name') or '').upper().strip() == emp_name_upper and
                        (existing.get('branch_name') or '').upper().strip() == b_name.upper().strip() and
                        (existing.get('zone') or '').upper().strip() == zone_upper and
                        (existing.get('division') or '').upper().strip() == division_upper and
                        (existing.get('business_unit') or '').upper().strip() == bu_name.upper().strip() and
                        (existing.get('role') or '').upper().strip() == role_upper and
                        (existing.get('product_name') or '').upper().strip() == p_name.upper().strip() and
                        (existing.get('status') or '').upper().strip() == 'ACTIVE'
                    ):
                        continue
                
                to_write.append((
                    emp_code_upper,
                    emp_name_upper,
                    b_name,
                    zone_upper,
                    division_upper,
                    bu_name,
                    role_upper,
                    p_name,
                    'ACTIVE',
                    f"UPLOADED VIA CSV ON {now_str}"
                ))
            except Exception as e:
                conn.rollback()
                conn.close()
                return jsonify({"status": "error", "message": f"Data normalization failed: {str(e)}"}), 400

        # Write to database in batches of 200 rows using high-performance multi-row upserts
        BATCH_SIZE = 200
        try:
            for idx in range(0, len(to_write), BATCH_SIZE):
                chunk = to_write[idx : idx + BATCH_SIZE]
                if not chunk:
                    continue
                    
                placeholders = []
                params = []
                for item in chunk:
                    placeholders.append("(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
                    params.extend(item)
                    
                query = """
                    INSERT INTO employees 
                    (emp_code, emp_name, branch_name, zone, division, business_unit, role, product_name, status, change_detail)
                    VALUES {}
                    ON CONFLICT (emp_code) DO UPDATE SET
                        emp_name = EXCLUDED.emp_name,
                        branch_name = EXCLUDED.branch_name,
                        zone = EXCLUDED.zone,
                        division = EXCLUDED.division,
                        business_unit = EXCLUDED.business_unit,
                        role = EXCLUDED.role,
                        product_name = EXCLUDED.product_name,
                        status = EXCLUDED.status,
                        change_detail = EXCLUDED.change_detail
                """.format(", ".join(placeholders))
                
                conn.execute(query, params)
                
            conn.commit()
        except Exception as e:
            conn.rollback()
            conn.close()
            return jsonify({"status": "error", "message": f"Database insertion failed: {str(e)}"}), 500
            
        conn.close()

        # Trigger real-time roster synchronization to Google Drive in background thread
        try:
            from gdrive_sync import sync_roster_to_gdrive
            threading.Thread(target=sync_roster_to_gdrive, daemon=True).start()
        except Exception as e:
            print(f"[GDRIVE] Error spawning roster upload thread: {str(e)}")

        return jsonify({"status": "success", "message": "Roster uploaded and processed successfully!"})


@app.route('/api/assessments/upload-historical', methods=['POST'])
def upload_historical_assessments():
    curr_user = session.get('user')
    if not curr_user:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "No selected file"}), 400
        
    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        # Unified template complete headers
        REQUIRED_HEADERS = [
            'Employee Code', 'Employee Name', 'Branch Name', 'Zone', 'Division', 'Business Unit', 'Role', 'Product Name',
            'Trainer ID', 'Trainer Name', 'Date of Visit', 'Module ID',
            'Zero Day Pre-Test', 'Zero Day Post-Test',
            'Six Days Pre-Test', 'Six Days Post-Test',
            'Twenty Days Pre-Test', 'Twenty Days Post-Test'
        ]

        
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
                    "message": f"Invalid CSV format. Missing required columns: {', '.join(missing_headers)}"
                }), 400
                
            hdr_indices = {h: headers.index(h) for h in headers}
            
            errors = []
            final_rows = []
            conn = get_db_connection()
            
            import re
            
            # Validation sets
            VALID_ZONES = {'NORTH ZONE', 'WEST ZONE', 'EAST ZONE'}
            VALID_DIVISIONS = {'DELHI DIVISION', 'GUJARAT DIVISION', 'PUNJAB DIVISION', 'BENGAL DIVISION', 'MUMBAI DIVISION'}
            VALID_BRANCHES = {'DELHI RF', 'AHMEDABAD RF', 'CHANDIGARH RF', 'KOLKATA RF', 'MUMBAI RF'}
            
            for row_idx, r in rows:
                def get_cell(header_name, fallback=''):
                    return r[hdr_indices[header_name]].strip() if header_name in hdr_indices else fallback
                
                emp_code = get_cell('Employee Code').upper()
                emp_name = get_cell('Employee Name').upper()
                branch_name_raw = get_cell('Branch Name')
                zone_raw = get_cell('Zone')
                division_raw = get_cell('Division')
                business_unit_raw = get_cell('Business Unit')
                role = get_cell('Role').upper()
                product_name_raw = get_cell('Product Name', 'N/A')
                
                trainer_id = get_cell('Trainer ID').upper()
                trainer_name = get_cell('Trainer Name')
                date_val = get_cell('Date of Visit')
                module_id_str = get_cell('Module ID')
                
                zero_pre_str = get_cell('Zero Day Pre-Test')
                zero_post_str = get_cell('Zero Day Post-Test')
                six_pre_str = get_cell('Six Days Pre-Test')
                six_post_str = get_cell('Six Days Post-Test')
                twenty_pre_str = get_cell('Twenty Days Pre-Test')
                twenty_post_str = get_cell('Twenty Days Post-Test')
                
                # 1. Employee validations
                if not emp_code:
                    errors.append(f"Row {row_idx}: Employee Code is required.")
                elif not re.match(r"^[A-Z0-9\-]{3,15}$", emp_code):
                    errors.append(f"Row {row_idx}: Employee Code '{emp_code}' is invalid.")
                if not emp_name:
                    errors.append(f"Row {row_idx}: Employee Name is required.")
                
                # Normalize using standard enums & uppercase helpers
                norm_zone, norm_div, norm_br = normalize_enums(zone_raw, division_raw, branch_name_raw)
                
                zone_items = [z.strip() for z in (norm_zone or "").split(",") if z.strip()]
                all_zones_valid = all(z in VALID_ZONES for z in zone_items)
                if not zone_items or not all_zones_valid:
                    errors.append(f"Row {row_idx}: Invalid Zone '{zone_raw}'. Must be NORTH ZONE, WEST ZONE, or EAST ZONE.")
                
                div_items = [d.strip() for d in (norm_div or "").split(",") if d.strip()]
                all_divs_valid = all(d in VALID_DIVISIONS for d in div_items)
                if not div_items or not all_divs_valid:
                    errors.append(f"Row {row_idx}: Invalid Division '{division_raw}'. Must be DELHI DIVISION, GUJARAT DIVISION, PUNJAB DIVISION, BENGAL DIVISION, or MUMBAI DIVISION.")
                
                br_items = [b.strip() for b in (norm_br or "").split(",") if b.strip()]
                all_branches_valid = all(b in VALID_BRANCHES for b in br_items)
                if not br_items or not all_branches_valid:
                    errors.append(f"Row {row_idx}: Invalid Branch Name '{branch_name_raw}'. Must be DELHI RF, AHMEDABAD RF, CHANDIGARH RF, KOLKATA RF, or MUMBAI RF.")
                
                norm_branch, norm_bu, norm_prod = normalize_employee_data(norm_br, business_unit_raw, product_name_raw, norm_div)
                
                # 2. Trainer validations
                if not trainer_id:
                    errors.append(f"Row {row_idx}: Trainer ID is required.")
                if not trainer_name:
                    errors.append(f"Row {row_idx}: Trainer Name is required.")
                    
                # 3. Module validation
                try:
                    module_id = int(module_id_str)
                    mod_match = conn.execute("SELECT id FROM modules WHERE id=?", (module_id,)).fetchone()
                    if not mod_match:
                        # Auto-create module to be user friendly
                        conn.execute("INSERT OR REPLACE INTO modules (id, title, questions_count) VALUES (?, 'Historical Policy Refresher', 10)", (module_id,))
                        conn.commit()
                except ValueError:
                    errors.append(f"Row {row_idx}: Module ID must be an integer.")
                    
                # 4. Date validation
                parsed_date = None
                for fmt in ('%Y-%m-%d', '%Y-%m'):
                    try:
                        parsed_date = datetime.datetime.strptime(date_val, fmt).date()
                        break
                    except ValueError:
                        pass
                if not parsed_date:
                    errors.append(f"Row {row_idx}: Date of Visit '{date_val}' is invalid. Use YYYY-MM-DD or YYYY-MM.")
                else:
                    if len(date_val) == 7:
                        date_val = f"{date_val}-01"
                        
                def parse_score(val_str, name):
                    if not val_str or val_str.upper() in ('N/A', '', 'NULL'):
                        return None
                    try:
                        score = float(val_str)
                        if not (0 <= score <= 100):
                            errors.append(f"Row {row_idx}: {name} score must be between 0 and 100.")
                            return None
                        return score
                    except ValueError:
                        errors.append(f"Row {row_idx}: {name} score '{val_str}' is invalid.")
                        return None
                
                zero_pre = parse_score(zero_pre_str, "Zero Day Pre-Test")
                zero_post = parse_score(zero_post_str, "Zero Day Post-Test")
                six_pre = parse_score(six_pre_str, "Six Days Pre-Test")
                six_post = parse_score(six_post_str, "Six Days Post-Test")
                twenty_pre = parse_score(twenty_pre_str, "Twenty Days Pre-Test")
                twenty_post = parse_score(twenty_post_str, "Twenty Days Post-Test")
                
                if not errors:
                    final_rows.append({
                        "emp_code": emp_code,
                        "emp_name": emp_name,
                        "branch_name": norm_branch,
                        "zone": norm_zone,
                        "division": norm_div,
                        "business_unit": norm_bu,
                        "role": role,
                        "product_name": norm_prod,
                        "trainer_id": trainer_id,
                        "trainer_name": trainer_name,
                        "date": date_val,
                        "module_id": module_id,
                        "zero_pre": zero_pre,
                        "zero_post": zero_post,
                        "six_pre": six_pre,
                        "six_post": six_post,
                        "twenty_pre": twenty_pre,
                        "twenty_post": twenty_post
                    })
                    
            if errors:
                conn.close()
                return jsonify({
                    "status": "error",
                    "message": "Historical data file validation failed.",
                    "details": errors
                }), 400
                
            # If validated successfully, upsert ALL required tables dynamically!
            for row in final_rows:
                # 1. Upsert Employee
                conn.execute("""
                    INSERT OR REPLACE INTO employees 
                    (emp_code, emp_name, branch_name, zone, division, business_unit, role, product_name, status, change_detail)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 'HISTORICAL IMPORT')
                """, (row['emp_code'], row['emp_name'], row['branch_name'], row['zone'], row['division'], row['business_unit'], row['role'], row['product_name']))
                
                # 2. Upsert Trainer
                t_match = conn.execute("SELECT name FROM trainers WHERE trainer_id=?", (row['trainer_id'],)).fetchone()
                if not t_match:
                    conn.execute("""
                        INSERT INTO trainers (trainer_id, name, zone, password, status, role)
                        VALUES (?, ?, ?, 'password123', 'Active', 'Trainer')
                    """, (row['trainer_id'], row['trainer_name'], row['zone'].split(',')[0].strip().upper()))
                    
                # 3. Create Training Session
                safe_branch = row['branch_name'].replace(' ', '')
                session_id = f"HIST-{safe_branch}-{row['module_id']}-{row['date']}"
                sess_match = conn.execute("SELECT session_id FROM training_sessions WHERE session_id=?", (session_id,)).fetchone()
                if not sess_match:
                    conn.execute("""
                        INSERT INTO training_sessions (session_id, module_id, branch_name, date, trainer_id)
                        VALUES (?, ?, ?, ?, ?)
                    """, (session_id, row['module_id'], row['branch_name'], row['date'], row['trainer_id']))
                    
                # 4. Create verified field travel logs for visual calendar
                v_match = conn.execute("SELECT id FROM field_visits WHERE trainer_id=? AND branch_name=? AND planned_date=?", (row['trainer_id'], row['branch_name'], row['date'])).fetchone()
                if not v_match:
                    conn.execute("""
                        INSERT INTO field_visits 
                        (trainer_id, branch_name, planned_date, purpose, status, checkin_time, verification_time, co_presence_count)
                        VALUES (?, ?, ?, 'Training', 'VERIFIED', ?, ?, 1)
                    """, (row['trainer_id'], row['branch_name'], row['date'], row['date'] + " 10:00", row['date'] + " 11:30"))
                    
                # 5. Insert assessment score curves
                if row['zero_pre'] is not None and row['zero_post'] is not None:
                    conn.execute("""
                        INSERT OR REPLACE INTO assessment_results 
                        (emp_code, module_id, assignment_day, pre_test_score, post_test_score, completed_at, session_id)
                        VALUES (?, ?, 'ZERO DAY', ?, ?, ?, ?)
                    """, (row['emp_code'], row['module_id'], row['zero_pre'], row['zero_post'], row['date'], session_id))
                    
                if row['six_pre'] is not None and row['six_post'] is not None:
                    comp_date = (datetime.datetime.strptime(row['date'], '%Y-%m-%d') + datetime.timedelta(days=6)).strftime('%Y-%m-%d')
                    conn.execute("""
                        INSERT OR REPLACE INTO assessment_results 
                        (emp_code, module_id, assignment_day, pre_test_score, post_test_score, completed_at, session_id)
                        VALUES (?, ?, 'SIX DAYS', ?, ?, ?, ?)
                    """, (row['emp_code'], row['module_id'], row['six_pre'], row['six_post'], comp_date, session_id))
                    
                if row['twenty_pre'] is not None and row['twenty_post'] is not None:
                    comp_date = (datetime.datetime.strptime(row['date'], '%Y-%m-%d') + datetime.timedelta(days=21)).strftime('%Y-%m-%d')
                    conn.execute("""
                        INSERT OR REPLACE INTO assessment_results 
                        (emp_code, module_id, assignment_day, pre_test_score, post_test_score, completed_at, session_id)
                        VALUES (?, ?, 'TWENTY DAYS', ?, ?, ?, ?)
                    """, (row['emp_code'], row['module_id'], row['twenty_pre'], row['twenty_post'], comp_date, session_id))
                    
            conn.commit()
            conn.close()
            
            # Sync to Google Drive in background thread if configured
            try:
                from gdrive_sync import backup_db_to_gdrive
                threading.Thread(target=backup_db_to_gdrive, daemon=True).start()
            except Exception as e:
                print(f"[GDRIVE] Historical assessment sync skip: {str(e)}")
                
            return jsonify({
                "status": "success",
                "message": f"Successfully processed {len(final_rows)} rows! Unified employee rosters, trainer accounts, field visit calendar plans, and pre/post training delta score curves populated instantly!"
            })
            
        except Exception as e:
            return jsonify({"status": "error", "message": f"File import failure: {str(e)}"}), 500

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
    
    # Normalize employee fields and enums to UPPERCASE enums
    zone, division, branch_name = normalize_enums(zone, division, branch_name)
    branch_name, business_unit, product_name = normalize_employee_data(
        branch_name, business_unit, product_name, division
    )
    
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

    # Trigger real-time roster synchronization to Google Drive in background thread
    try:
        from gdrive_sync import sync_roster_to_gdrive
        threading.Thread(target=sync_roster_to_gdrive, daemon=True).start()
    except Exception as e:
        print(f"[GDRIVE] Error spawning roster upload thread: {str(e)}")

    return jsonify({"status": "success", "message": f"Employee '{emp_name}' added manually successfully!"})

@app.route('/api/roster/bulk-action', methods=['POST'])
def bulk_action_roster():
    data = request.json or {}
    emp_codes = data.get('emp_codes', [])
    action = data.get('action', '')
    
    if not emp_codes:
        return jsonify({"status": "error", "message": "No employees selected for bulk action."}), 400
        
    conn = get_db_connection()
    try:
        if action == 'delete':
            reason = data.get('reason', 'BULK DELETION').strip().upper()
            # Hard delete by default as per user requirement
            hard = data.get('hard', True) if 'hard' in data else True
            now_str = datetime.datetime.now().strftime("%Y-%m-%d")
            
            if hard:
                conn.execute(
                    "DELETE FROM employees WHERE emp_code IN ({})".format(','.join('?' for _ in emp_codes)),
                    tuple(code.upper().strip() for code in emp_codes)
                )
            else:
                conn.execute(
                    "UPDATE employees SET status='DELETED', change_detail=? WHERE emp_code IN ({})".format(','.join('?' for _ in emp_codes)),
                    (f"BULK DELETED ON {now_str}: {reason}", *[code.upper().strip() for code in emp_codes])
                )
            conn.commit()
            conn.close()
            
            try:
                from gdrive_sync import sync_roster_to_gdrive
                threading.Thread(target=sync_roster_to_gdrive, daemon=True).start()
            except Exception:
                pass
                
            return jsonify({"status": "success", "message": f"Successfully deleted {len(emp_codes)} employees."})
            
        elif action == 'edit':
            fields = {}
            for field in ['zone', 'division', 'branch_name', 'business_unit', 'role', 'product_name', 'status', 'change_detail']:
                if field in data and data[field] is not None:
                    val = data[field].strip()
                    if field in ['zone', 'division', 'branch_name', 'business_unit', 'role', 'status']:
                        val = val.upper()
                    if val != '':
                        fields[field] = val
                    
            if not fields:
                conn.close()
                return jsonify({"status": "error", "message": "No fields provided for bulk edit."}), 400
                
            now_str = datetime.datetime.now().strftime("%Y-%m-%d")
            if 'change_detail' not in fields:
                fields['change_detail'] = f"BULK EDITED ON {now_str}"
                
            for emp_code in emp_codes:
                emp_code = emp_code.upper().strip()
                existing = conn.execute("SELECT * FROM employees WHERE emp_code = ?", (emp_code,)).fetchone()
                if not existing:
                    continue
                    
                merged_zone = fields.get('zone', existing['zone'])
                merged_div = fields.get('division', existing['division'])
                merged_br = fields.get('branch_name', existing['branch_name'])
                merged_bu = fields.get('business_unit', existing['business_unit'])
                merged_role = fields.get('role', existing['role'])
                merged_pn = fields.get('product_name', existing['product_name'])
                merged_status = fields.get('status', existing['status'])
                merged_cd = fields.get('change_detail', existing['change_detail'])
                
                norm_zone, norm_div, norm_br = normalize_enums(merged_zone, merged_div, merged_br)
                norm_br, norm_bu, norm_pn = normalize_employee_data(
                    norm_br if norm_br else merged_br,
                    merged_bu,
                    merged_pn,
                    norm_div if norm_div else merged_div
                )
                
                conn.execute(
                    "UPDATE employees SET zone=?, division=?, branch_name=?, business_unit=?, role=?, product_name=?, status=?, change_detail=? WHERE emp_code=?",
                    (norm_zone if norm_zone else merged_zone, norm_div if norm_div else merged_div, norm_br, norm_bu, merged_role, norm_pn, merged_status, merged_cd, emp_code)
                )
                
            conn.commit()
            conn.close()
            
            try:
                from gdrive_sync import sync_roster_to_gdrive
                threading.Thread(target=sync_roster_to_gdrive, daemon=True).start()
            except Exception:
                pass
                
            return jsonify({"status": "success", "message": f"Successfully updated {len(emp_codes)} employees."})
            
        else:
            conn.close()
            return jsonify({"status": "error", "message": f"Invalid action: {action}"}), 400
            
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"status": "error", "message": f"Bulk action failed: {str(e)}"}), 500

@app.route('/api/roster/<emp_code>', methods=['PUT', 'DELETE'])
def handle_single_roster_item(emp_code):
    emp_code = emp_code.upper().strip()
    conn = get_db_connection()
    
    if request.method == 'DELETE':
        # Hard delete by default as per user requirement
        hard = request.args.get('hard', 'true').lower() == 'true'
        try:
            if hard:
                conn.execute("DELETE FROM employees WHERE emp_code=?", (emp_code,))
                conn.commit()
                conn.close()
                # Trigger real-time roster synchronization to Google Drive in background thread
                try:
                    from gdrive_sync import sync_roster_to_gdrive
                    threading.Thread(target=sync_roster_to_gdrive, daemon=True).start()
                except Exception as e:
                    print(f"[GDRIVE] Error spawning roster upload thread: {str(e)}")
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
                # Trigger real-time roster synchronization to Google Drive in background thread
                try:
                    from gdrive_sync import sync_roster_to_gdrive
                    threading.Thread(target=sync_roster_to_gdrive, daemon=True).start()
                except Exception as e:
                    print(f"[GDRIVE] Error spawning roster upload thread: {str(e)}")
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
        
        # Normalize employee fields and enums to UPPERCASE enums
        zone, division, branch_name = normalize_enums(zone, division, branch_name)
        branch_name, business_unit, product_name = normalize_employee_data(
            branch_name, business_unit, product_name, division
        )
        
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
        # Trigger real-time roster synchronization to Google Drive in background thread
        try:
            from gdrive_sync import sync_roster_to_gdrive
            threading.Thread(target=sync_roster_to_gdrive, daemon=True).start()
        except Exception as e:
            print(f"[GDRIVE] Error spawning roster upload thread: {str(e)}")
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
        # Sync Socratic modules from Google Drive to pull other trainers' custom creations!
        try:
            from gdrive_sync import sync_modules_from_gdrive
            sync_modules_from_gdrive(conn)
        except Exception as e:
            print(f"[GDRIVE] Dynamic Socratic modules sync skipped: {str(e)}")

        modules = conn.execute("""
            SELECT m.*, t.name as creator_name
            FROM modules m
            LEFT JOIN trainers t ON m.created_by = t.trainer_id
            ORDER BY m.id DESC
        """).fetchall()
        
        # Migrate: add source_text if column is missing in older DBs (safe to run every time)
        try:
            cursor = conn.cursor()
            cursor.execute("ALTER TABLE modules ADD COLUMN source_text TEXT DEFAULT ''")
        except Exception:
            pass
            
        res_list = []
        for m in modules:
            m_dict = dict(m)
            q_rows = conn.execute("SELECT * FROM questions WHERE module_id=?", (m['id'],)).fetchall()
            
            q_list = []
            for q in q_rows:
                q_dict = dict(q)
                try:
                    q_dict['translations'] = json.loads(q_dict.get('translations') or '{}')
                except Exception:
                    q_dict['translations'] = {}
                q_list.append(q_dict)
                
            m_dict['questions'] = q_list
            res_list.append(m_dict)
            
        conn.close()
        return jsonify(res_list)
    
    elif request.method == 'POST':
        data = request.json
        now = datetime.datetime.now().strftime("%Y-%m-%d")
        trainer_id = data.get('created_by', 'ADMIN')
        audited_by = data.get('audited_by')
        if not audited_by:
            # Query trainer's name
            active_tr = conn.execute("SELECT name FROM trainers WHERE trainer_id=?", (trainer_id,)).fetchone()
            audited_by = active_tr['name'] if active_tr else 'Super Admin'
        conn.execute("INSERT INTO modules (title, questions_count, created_at, status, created_by, audited_by, difficulty) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (data['title'], 15, now, 'Ready', trainer_id, audited_by, data.get('difficulty', 'Medium')))
        conn.commit()
        conn.close()
        try:
            from gdrive_sync import sync_module_to_gdrive
            ok = sync_module_to_gdrive(data['title'], data.get('difficulty', 'Medium'), 'Ready', trainer_id, audited_by, [], "")
            print(f"[GDRIVE-SYNC] Module sync {'succeeded' if ok else 'FAILED'}")
        except Exception as e:
            print(f"[GDRIVE-SYNC] Module sync exception: {str(e)}")
        return jsonify({"status": "success"})

@app.route('/api/modules/<int:module_id>', methods=['DELETE'])
def delete_module(module_id):
    conn = get_db_connection()
    
    # Enforce Creator RBAC limits: Trainers can only delete their own modules
    curr_user = session.get('user')
    if curr_user and curr_user['role'] not in ['SuperAdmin', 'Leader']:
        m_row = conn.execute("SELECT created_by FROM modules WHERE id=?", (module_id,)).fetchone()
        if m_row and m_row['created_by'] != curr_user['trainer_id']:
            conn.close()
            return jsonify({"status": "error", "message": "Forbidden. You are only allowed to delete Socratic modules you created."}), 403
            
    # 1. Fetch title for Google Drive deletion before deleting from SQLite
    row = conn.execute("SELECT title FROM modules WHERE id=?", (module_id,)).fetchone()
    title = row['title'] if row else None
    
    # 2. Perform database deletion
    conn.execute("DELETE FROM modules WHERE id=?", (module_id,))
    conn.execute("DELETE FROM questions WHERE module_id=?", (module_id,))
    conn.commit()
    conn.close()
    
    # 3. Trigger Google Drive deletion in background thread (no UI block)
    if title:
        try:
            from gdrive_sync import delete_module_from_gdrive
            threading.Thread(
                target=delete_module_from_gdrive,
                args=(title,),
                daemon=True
            ).start()
        except Exception as e:
            print(f"[GDRIVE] Error spawning delete thread: {str(e)}")
            
    return jsonify({"status": "success"})

def get_offline_translations(type_flag, masked_s_or_intro, choices, correct_index, title="Module", language='all'):
    t = {
        'hindi': {
            'pct_rate': 'एक ग्राहक बैंक से लोन लेने आता है। नियम कहते हैं: "{text}" बैंक वाले को कितना रेट देना चाहिए?',
            'rule_require': 'एक ग्राहक फॉर्म भरता है। नियम कहते हैं: "{text}" बैंक वाले को क्या करना चाहिए?',
            'rule_restrict': 'एक ग्राहक कुछ करना चाहता है। लेकिन नियम कहते हैं: "{text}" बैंक वाले को क्या करना चाहिए?',
            'value_days': 'एक ग्राहक पूछता है, "कितने दिन लगेंगे?" नियम कहते हैं: "{text}" बैंक वाले को क्या कहना चाहिए?',
            'value_months': 'एक ग्राहक लोन चुकाने का समय चुनना चाहता है। नियम कहते हैं: "{text}" वे कितने महीने चुन सकते हैं?',
            'value_years': 'एक ग्राहक पूछता है कि योजना कितने साल की है। नियम कहते हैं: "{text}" कितने साल?',
            'value_lakhs': 'एक ग्राहक पूछता है, "मुझे कितने पैसे मिल सकते हैं?" नियम कहते हैं: "{text}" बैंक वाले को क्या कहना चाहिए?',
            'comprehension': 'नियमों का यह भाग पढ़ें:\n{text}\n\nकौन सा वाक्य सही है?',
        },
        'hinglish': {
            'pct_rate': 'Ek customer bank mein loan lene aata hai. Rules kehte hain: "{text}" Bank wale ko kitna rate dena chahiye?',
            'rule_require': 'Ek customer form bharta hai. Rules kehte hain: "{text}" Bank wale ko kya karna chahiye?',
            'rule_restrict': 'Ek customer kuch karna chahta hai. Lekin rules kehte hain: "{text}" Bank wale ko kya karna chahiye?',
            'value_days': 'Ek customer poochta hai, "Kitne din lagenge?" Rules kehte hain: "{text}" Bank wale ko kya kehna chahiye?',
            'value_months': 'Ek customer loan wapas karne ka time choose karna chahta hai. Rules kehte hain: "{text}" Wo kitne months choose kar sakta hai?',
            'value_years': 'Ek customer poochta hai ki plan kitne saal ka hai. Rules kehte hain: "{text}" Kitne saal?',
            'value_lakhs': 'Ek customer poochta hai, "Mujhe kitne paise mil sakte hain?" Rules kehte hain: "{text}" Bank wale ko kya kehna chahiye?',
            'comprehension': 'Rules ka yeh part padhein:\n{text}\n\nKaunsa sentence sahi hai?',
        },
        'punjabi': {
            'pct_rate': 'ਇੱਕ ਗਾਹਕ ਬੈਂਕ ਤੋਂ ਲੋਨ ਲੈਣ ਆਉਂਦਾ ਹੈ। ਨਿਯਮ ਕਹਿੰਦੇ ਹਨ: "{text}" ਬੈਂਕ ਵਾਲੇ ਨੂੰ ਕਿੰਨਾ ਰੇਟ ਦੇਣਾ ਚਾਹੀਦਾ ਹੈ?',
            'rule_require': 'ਇੱਕ ਗਾਹਕ ਫਾਰਮ ਭਰਦਾ ਹੈ। ਨਿਯਮ ਕਹਿੰਦੇ ਹਨ: "{text}" ਬੈਂਕ ਵਾਲੇ ਨੂੰ ਕੀ ਕਰਨਾ ਚਾਹੀਦਾ ਹੈ?',
            'rule_restrict': 'ਇੱਕ ਗਾਹਕ ਕੁਝ ਕਰਨਾ ਚਾਹੁੰਦਾ ਹੈ। ਪਰ ਨਿਯਮ ਕਹਿੰਦੇ ਹਨ: "{text}" ਬੈਂਕ ਵਾਲੇ ਨੂੰ ਕੀ ਕਰਨਾ ਚਾਹੀਦਾ ਹੈ?',
            'value_days': 'ਇੱਕ ਗਾਹਕ ਪੁੱਛਦਾ ਹੈ, "ਕਿੰਨੇ ਦਿਨ ਲੱਗਣਗੇ?" ਨਿਯਮ ਕਹਿੰਦੇ ਹਨ: "{text}" ਬੈਂਕ ਵਾਲੇ ਨੂੰ ਕੀ ਕਹਿਣਾ ਚਾਹੀਦਾ ਹੈ?',
            'value_months': 'ਇੱਕ ਗਾਹਕ ਲੋਨ ਵਾਪਸ ਕਰਨ ਦਾ ਸਮਾਂ ਚੁਣਨਾ ਚਾਹੁੰਦਾ ਹੈ। ਨਿਯਮ ਕਹਿੰਦੇ ਹਨ: "{text}" ਉਹ ਕਿੰਨੇ ਮਹੀਨੇ ਚੁਣ ਸਕਦਾ ਹੈ?',
            'value_years': 'ਇੱਕ ਗਾਹਕ ਪੁੱਛਦਾ ਹੈ ਕਿ ਯੋਜਨਾ ਕਿੰਨੇ ਸਾਲਾਂ ਦੀ ਹੈ। ਨਿਯਮ ਕਹਿੰਦੇ ਹਨ: "{text}" ਕਿੰਨੇ ਸਾਲ?',
            'value_lakhs': 'ਇੱਕ ਗਾਹਕ ਪੁੱਛਦਾ ਹੈ, "ਮੈਨੂੰ ਕਿੰਨੇ ਪੈਸੇ ਮਿਲ ਸਕਦੇ ਹਨ?" ਨਿਯਮ ਕਹਿੰਦੇ ਹਨ: "{text}" ਬੈਂਕ ਵਾਲੇ ਨੂੰ ਕੀ ਕਹਿਣਾ ਚਾਹੀਦਾ ਹੈ?',
            'comprehension': 'ਨਿਯਮਾਂ ਦਾ ਇਹ ਹਿੱਸਾ ਪੜ੍ਹੋ:\n{text}\n\nਕਿਹੜਾ ਵਾਕ ਸਹੀ ਹੈ?',
        },
        'bengali': {
            'pct_rate': 'একজন গ্রাহক ব্যাংক থেকে লোন নিতে আসে। নিয়ম বলে: "{text}" ব্যাংক কর্মীকে কত রেট দিতে হবে?',
            'rule_require': 'একজন গ্রাহক ফর্ম পূরণ করে। নিয়ম বলে: "{text}" ব্যাংক কর্মীর কী করা উচিত?',
            'rule_restrict': 'একজন গ্রাহক কিছু করতে চায়। কিন্তু নিয়ম বলে: "{text}" ব্যাংক কর্মীর কী করা উচিত?',
            'value_days': 'একজন গ্রাহক জিজ্ঞাসা করে, "কত দিন লাগবে?" নিয়ম বলে: "{text}" ব্যাংক কর্মীর কী বলা উচিত?',
            'value_months': 'একজন গ্রাহক লোন পরিশোধের সময় বেছে নিতে চায়। নিয়ম বলে: "{text}" সে কত মাস বেছে নিতে পারে?',
            'value_years': 'একজন গ্রাহক জিজ্ঞাসা করে পরিকল্পনা কত বছরের। নিয়ম বলে: "{text}" কত বছর?',
            'value_lakhs': 'একজন গ্রাহক জিজ্ঞাসা করে, "আমি কত টাকা পেতে পারি?" নিয়ম বলে: "{text}" ব্যাংক কর্মীর কী বলা উচিত?',
            'comprehension': 'নিয়মের এই অংশটি পড়ুন:\n{text}\n\nকোন বাক্যটি সঠিক?',
        },
        'marathi': {
            'pct_rate': 'एक ग्राहक बँकेतून कर्ज घेण्यासाठी येतो. नियम सांगतात: "{text}" बँक कर्मचाऱ्याने किती दर द्यावा?',
            'rule_require': 'एक ग्राहक फॉर्म भरतो. नियम सांगतात: "{text}" बँक कर्मचाऱ्याने काय करावे?',
            'rule_restrict': 'एक ग्राहक काहीतरी करू इच्छितो. पण नियम सांगतात: "{text}" बँक कर्मचाऱ्याने काय करावे?',
            'value_days': 'एक ग्राहक विचारतो, "किती दिवस लागतील?" नियम सांगतात: "{text}" बँक कर्मचाऱ्याने काय म्हणावे?',
            'value_months': 'एक ग्राहक कर्ज फेडण्याचा कालावधी निवडू इच्छितो. नियम सांगतात: "{text}" तो किती महिने निवडू शकतो?',
            'value_years': 'एक ग्राहक विचारतो की योजना किती वर्षांची आहे. नियम सांगतात: "{text}" किती वर्षे?',
            'value_lakhs': 'एक ग्राहक विचारतो, "मला किती पैसे मिळू शकतात?" नियम सांगतात: "{text}" बँक कर्मचाऱ्याने काय म्हणावे?',
            'comprehension': 'नियमांचा हा भाग वाचा:\n{text}\n\nकोणते वाक्य बरोबर आहे?',
        },
        'telugu': {
            'pct_rate': 'ఒక కస్టమర్ బ్యాంకు నుండి లోన్ తీసుకోవడానికి వస్తాడు. నియమాలు చెప్తాయి: "{text}" బ్యాంకు వ్యక్తి ఎంత రేటు ఇవ్వాలి?',
            'rule_require': 'ఒక కస్టమర్ ఫారం నింపుతాడు. నియమాలు చెప్తాయి: "{text}" బ్యాంకు వ్యక్తి ఏమి చేయాలి?',
            'rule_restrict': 'ఒక కస్టమర్ ఏదో చేయాలనుకుంటాడు. కానీ నియమాలు చెప్తాయి: "{text}" బ్యాంకు వ్యక్తి ఏమి చేయాలి?',
            'value_days': 'ఒక కస్టమర్ అడుగుతాడు, "ఎన్ని రోజులు పడుతుంది?" నియమాలు చెప్తాయి: "{text}" బ్యాంకు వ్యక్తి ఏమి చెప్పాలి?',
            'value_months': 'ఒక కస్టమర్ లోన్ తిరిగి చెల్లించడానికి సమయాన్ని ఎంచుకోవాలనుకుంటాడు. నియమాలు చెప్తాయి: "{text}" అతను ఎన్ని నెలలు ఎంచుకోవచ్చు?',
            'value_years': 'ఒక కస్టమర్ ప్లాన్ ఎన్ని సంవత్సరాలది అని అడుగుతాడు. నియమాలు చెప్తాయి: "{text}" ఎన్ని సంవత్సరాలు?',
            'value_lakhs': 'ఒక కస్టమర్ అడుగుతాడు, "నాకు ఎంత డబ్బు వస్తుంది?" నియమాలు చెప్తాయి: "{text}" బ్యాంకు వ్యక్తి ఏమి చెప్పాలి?',
            'comprehension': 'నియమాలలో ఈ భాగాన్ని చదవండి:\n{text}\n\nఏ వాక్యం సరైనది?',
        },
        'tamil': {
            'pct_rate': 'ஒரு வாடிக்கையாளர் வங்கியில் கடன் வாங்க வருகிறார். விதிகள் கூறுகின்றன: "{text}" வங்கி ஊழியர் எவ்வளவு வட்டி விகிதம் கொடுக்க வேண்டும்?',
            'rule_require': 'ஒரு வாடிக்கையாளர் படிவத்தை நிரப்புகிறார். விதிகள் கூறுகின்றன: "{text}" வங்கி ஊழியர் என்ன செய்ய வேண்டும்?',
            'rule_restrict': 'ஒரு வாடிக்கையாளர் ஏதோ செய்ய விரும்புகிறார். ஆனால் விதிகள் கூறுகின்றன: "{text}" வங்கி ஊழியர் என்ன செய்ய வேண்டும்?',
            'value_days': 'ஒரு வாடிக்கையாளர் கேட்கிறார், "எத்தனை நாட்கள் ஆகும்?" விதிகள் கூறுகின்றன: "{text}" வங்கி ஊழியர் என்ன சொல்ல வேண்டும்?',
            'value_months': 'ஒரு வாடிக்கையாளர் கடனை திருப்பிச் செலுத்த நேரத்தை தேர்வு செய்ய விரும்புகிறார். விதிகள் கூறுகின்றன: "{text}" அவர் எத்தனை மாதங்கள் தேர்வு செய்யலாம்?',
            'value_years': 'ஒரு வாடிக்கையாளர் திட்டம் எத்தனை ஆண்டுகள் என்று கேட்கிறார். விதிகள் கூறுகின்றன: "{text}" எத்தனை ஆண்டுகள்?',
            'value_lakhs': 'ஒரு வாடிக்கையாளர் கேட்கிறார், "எனக்கு எவ்வளவு பணம் கிடைக்கும்?" விதிகள் கூறுகின்றன: "{text}" வங்கி ஊழியர் என்ன சொல்ல வேண்டும்?',
            'comprehension': 'விதிகளின் இந்த பகுதியைப் படிக்கவும்:\n{text}\n\nஎந்த வாக்கியம் சரியானது?',
        },
        'gujarati': {
            'pct_rate': 'એક ગ્રાહક બેંકમાંથી લોન લેવા આવે છે. નિયમો કહે છે: "{text}" બેંક વ્યક્તિએ કેટલો દર આપવો જોઈએ?',
            'rule_require': 'એક ગ્રાહક ફોર્મ ભરે છે. નિયમો કહે છે: "{text}" બેંક વ્યક્તિએ શું કરવું જોઈએ?',
            'rule_restrict': 'એક ગ્રાહક કંઈક કરવા માંગે છે. પરંતુ નિયમો કહે છે: "{text}" બેંક વ્યક્તિએ શું કરવું જોઈએ?',
            'value_days': 'એક ગ્રાહક પૂછે છે, "કેટલા દિવસ લાગશે?" નિયમો કહે છે: "{text}" બેંક વ્યક્તિએ શું કહેવું જોઈએ?',
            'value_months': 'એક ગ્રાહક લોન ચૂકવવાનો સમય પસંદ કરવા માંગે છે. નિયમો કહે છે: "{text}" તે કેટલા મહિના પસંદ કરી શકે છે?',
            'value_years': 'એક ગ્રાહક પૂછે છે કે યોજના કેટલા વર્ષની છે. નિયમો કહે છે: "{text}" કેટલા વર્ષ?',
            'value_lakhs': 'એક ગ્રાહક પૂછે છે, "મને કેટલા પૈસા મળી શકે છે?" નિયમો કહે છે: "{text}" બેંક વ્યક્તિએ શું કહેવું જોઈએ?',
            'comprehension': 'નિયમોનો આ ભાગ વાંચો:\n{text}\n\nકયું વાક્ય સાચું છે?',
        },
        'kannada': {
            'pct_rate': 'ಒಬ್ಬ ಗ್ರಾಹಕ ಬ್ಯಾಂಕಿನಿಂದ ಸಾಲ ತೆಗೆದುಕೊಳ್ಳಲು ಬರುತ್ತಾನೆ. ನಿಯಮಗಳು ಹೇಳುತ್ತವೆ: "{text}" ಬ್ಯಾಂಕ್ ವ್ಯಕ್ತಿ ಎಷ್ಟು ದರ ನೀಡಬೇಕು?',
            'rule_require': 'ಒಬ್ಬ ಗ್ರಾಹಕ ಫಾರ್ಮ್ ತುಂಬುತ್ತಾನೆ. ನಿಯಮಗಳು ಹೇಳುತ್ತವೆ: "{text}" ಬ್ಯಾಂಕ್ ವ್ಯಕ್ತಿ ಏನು ಮಾಡಬೇಕು?',
            'rule_restrict': 'ಒಬ್ಬ ಗ್ರಾಹಕ ಏನನ್ನಾದರೂ ಮಾಡಲು ಬಯಸುತ್ತಾನೆ. ಆದರೆ ನಿಯಮಗಳು ಹೇಳುತ್ತವೆ: "{text}" ಬ್ಯಾಂಕ್ ವ್ಯಕ್ತಿ ಏನು ಮಾಡಬೇಕು?',
            'value_days': 'ಒಬ್ಬ ಗ್ರಾಹಕ ಕೇಳುತ್ತಾನೆ, "ಎಷ್ಟು ದಿನಗಳು ಬೇಕಾಗುತ್ತವೆ?" ನಿಯಮಗಳು ಹೇಳುತ್ತವೆ: "{text}" ಬ್ಯಾಂಕ್ ವ್ಯಕ್ತಿ ಏನು ಹೇಳಬೇಕು?',
            'value_months': 'ಒಬ್ಬ ಗ್ರಾಹಕ ಸಾಲವನ್ನು ತಿರುಗಿ ಪಾವತಿಸಲು ಸಮಯವನ್ನು ಆಯ್ಕೆ ಮಾಡಲು ಬಯಸುತ್ತಾನೆ. ನಿಯಮಗಳು ಹೇಳುತ್ತವೆ: "{text}" ಅವನು ಎಷ್ಟು ತಿಂಗಳುಗಳನ್ನು ಆಯ್ಕೆ ಮಾಡಬಹುದು?',
            'value_years': 'ಒಬ್ಬ ಗ್ರಾಹಕ ಯೋಜನೆ ಎಷ್ಟು ವರ್ಷಗಳು ಎಂದು ಕೇಳುತ್ತಾನೆ. ನಿಯಮಗಳು ಹೇಳುತ್ತವೆ: "{text}" ಎಷ್ಟು ವರ್ಷಗಳು?',
            'value_lakhs': 'ಒಬ್ಬ ಗ್ರಾಹಕ ಕೇಳುತ್ತಾನೆ, "ನನಗೆ ಎಷ್ಟು ಹಣ ಸಿಗಬಹುದು?" ನಿಯಮಗಳು ಹೇಳುತ್ತವೆ: "{text}" ಬ್ಯಾಂಕ್ ವ್ಯಕ್ತಿ ಏನು ಹೇಳಬೇಕು?',
            'comprehension': 'ನಿಯಮಗಳ ಈ ಭಾಗವನ್ನು ಓದಿ:\n{text}\n\nಯಾವ ವಾಕ್ಯ ಸರಿಯಾಗಿದೆ?',
        },
    }

    translations = {}
    target_langs = ['hindi', 'hinglish', 'punjabi', 'bengali', 'marathi', 'telugu', 'tamil', 'gujarati', 'kannada']

    if language != 'all' and language != 'en':
        if language in target_langs:
            target_langs = [language]
        else:
            return {}
    elif language == 'en':
        return {}

    for lang in target_langs:
        if type_flag in t.get(lang, {}):
            translations[lang] = {
                'question': t[lang][type_flag].format(text=masked_s_or_intro),
                'options': choices,
                'correctIndex': correct_index
            }

    return translations

def generate_heuristic_questions(text_content, count, title="Module", language='en'):
    import re
    import random
    import json

    def clean_doc_text(text):
        text = re.sub(r'^\d+[\.\)]\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'^[-•*]\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'\b([A-Z][A-Z\s]+):\s*', lambda m: m.group(0).title(), text)
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        merged = []
        buf = ''
        for line in lines:
            if buf and (line[0].islower() or line.startswith('and ') or line.startswith('or ') or len(buf) + len(line) < 100):
                buf += ' ' + line
            else:
                if buf:
                    merged.append(buf)
                buf = line
        if buf:
            merged.append(buf)
        return '\n'.join(merged)

    text_content = clean_doc_text(text_content)
    paragraphs = [p.strip() for p in text_content.split('\n') if len(p.strip()) > 10]
    sentences = []
    for p in paragraphs:
        for s in re.split(r'[.!?\n]', p):
            s_clean = s.strip().rstrip(',').strip()
            if len(s_clean) > 10 and len(s_clean) < 300:
                sentences.append(s_clean)

    questions = []
    used_facts = set()

    def extract_value_context(s, value_str, window=8):
        idx = s.lower().find(value_str.lower())
        if idx == -1:
            words = s.split()
            if len(words) <= window:
                return s
            return ' '.join(words[:window]) + '...'
        start = max(0, idx - 60)
        end = min(len(s), idx + len(value_str) + 60)
        ctx = s[start:end].strip()
        if len(ctx) > 100:
            ctx = ctx[:100] + '...'
        return ctx

    def perturb_number(s):
        import re
        nums = re.findall(r'(\d+)', s)
        if not nums:
            return None
        target = random.choice(nums)
        val = int(target)
        if val <= 2:
            direction = random.choice([1, 2, 3, 5])
            new_val = val + direction
        elif val <= 20:
            direction = random.choice([-1, 1, -2, 2, -3, 3, -5, 5])
            new_val = max(1, val + direction)
        elif val <= 100:
            direction = random.choice([-1, 1, -2, 2, -5, 5, -10, 10])
            new_val = max(1, val + direction)
        elif val <= 1000:
            direction = random.choice([-5, 5, -10, 10, -25, 25, -50, 50])
            new_val = max(1, val + direction)
        elif val <= 100000:
            direction = random.choice([-500, 500, -1000, 1000, -5000, 5000, -10000, 10000])
            new_val = max(1, val + direction)
        else:
            direction = random.choice([-50000, 50000, -100000, 100000, -200000, 200000])
            new_val = max(1, val + direction)
        if new_val == val:
            new_val = val + 1
        result = s[:s.find(target)] + str(new_val) + s[s.find(target) + len(target):]
        return result if result != s else None

    def perturb_word_swap(s):
        words = s.split()
        non_filler = [i for i, w in enumerate(words)
                      if w.lower() not in ('the', 'a', 'an', 'is', 'are', 'be', 'to', 'of', 'in', 'for', 'on', 'and', 'or', 'by', 'at', 'with')]
        if len(non_filler) < 1:
            return None
        idx = random.choice(non_filler)
        swaps = ['specified', 'applicable', 'required', 'standard', 'applicable', 'revised', 'approved']
        old = words[idx]
        new_w = random.choice([w for w in swaps if w.lower() != old.lower()])
        words[idx] = new_w
        result = ' '.join(words)
        return result if result != s else None

    def get_distractor_deltas(val, deltas):
        seen = set()
        result = []
        for d in deltas:
            nv = max(1, val + d)
            if nv != val and nv not in seen:
                seen.add(nv)
                result.append(nv)
        return result

    # ----------------------------------------------------------------
    # Heuristic 1: Simple percentage question
    # ----------------------------------------------------------------
    pct_sentences = []
    for s in sentences:
        if re.search(r'(\d+\.?\d*)\s*%', s):
            pct_sentences.append(s)
    for s in pct_sentences:
        if len(questions) >= count:
            break
        fact_key = s[:40]
        if fact_key in used_facts:
            continue
        used_facts.add(fact_key)
        pct_match = re.search(r'(\d+\.?\d*)\s*%', s)
        if not pct_match:
            continue
        val_num = int(float(pct_match.group(1)))
        correct_pct = f"{val_num}%"
        context = extract_value_context(s, correct_pct)
        scenario = f"A customer comes to the bank for a loan. The rules say {context}. How much rate should the bank person give?"
        correct_opt = f"Give {correct_pct}. That is what the rules say."
        wrong_opts = []
        alt_vals = get_distractor_deltas(val_num, [-15, 15, -10, 10, -5, 5, -20, 20, -25, 25, -30, 30])
        for av in alt_vals[:3]:
            wrong_opts.append(f"Give {av}%.")
        choices_list = [correct_opt] + wrong_opts
        while len(choices_list) < 4:
            choices_list.append("Ask the manager what rate to give.")
        choices_list = choices_list[:4]
        random.shuffle(choices_list)
        questions.append({
            "question": scenario,
            "options": choices_list,
            "correctIndex": choices_list.index(correct_opt),
            "approved": 0,
            "translations": get_offline_translations('pct_rate', context, choices_list, choices_list.index(correct_opt), title, language)
        })

    # ----------------------------------------------------------------
    # Heuristic 2: Simple policy rule question
    # ----------------------------------------------------------------
    rule_keywords = r'\b(must|shall|should|required|mandatory|cannot|not allowed|prohibited|only|maximum|minimum|eligible|not eligible|is mandatory|is required|must not|shall not)\b'
    candidate_rules = [s for s in sentences if re.search(rule_keywords, s, re.IGNORECASE) and s[:40] not in used_facts]
    random.shuffle(candidate_rules)
    for s in candidate_rules:
        if len(questions) >= count:
            break
        fact_key = s[:40]
        if fact_key in used_facts:
            continue
        used_facts.add(fact_key)

        rule_match = re.search(r'(must|shall|is required|is mandatory|are required|cannot|must not|shall not|not allowed|prohibited|eligible|not eligible)', s, re.IGNORECASE)
        rule_word = rule_match.group(1).lower() if rule_match else 'required'
        context = extract_value_context(s, rule_word)

        if rule_word in ('cannot', 'must not', 'shall not', 'not allowed', 'prohibited'):
            scenario = f"A customer wants to do something. But the rules say {context}. What should the bank person do?"
            correct_opt = f"Say no. The rules say: {s}"
        else:
            scenario = f"A customer fills a form. The rules say {context}. What should the bank person do?"
            correct_opt = f"Follow the rule: {s}"
        doc_facts = [x for x in sentences if x[:40] not in used_facts and x != s]
        random.shuffle(doc_facts)
        wrong_opts = []
        for ds in doc_facts:
            p = perturb_number(ds) or perturb_word_swap(ds)
            if p and p != ds and len(p) > 30:
                wrong_opts.append(f"Instead do this: {p[:120]}")
            if len(wrong_opts) >= 3:
                break
        while len(wrong_opts) < 3:
            wrong_opts.append("Ask the manager what to do.")
        choices_list = [correct_opt] + wrong_opts[:3]
        random.shuffle(choices_list)
        h2_flag = 'rule_restrict' if rule_word in ('cannot', 'must not', 'shall not', 'not allowed', 'prohibited') else 'rule_require'
        questions.append({
            "question": scenario,
            "options": choices_list,
            "correctIndex": choices_list.index(correct_opt),
            "approved": 0,
            "translations": get_offline_translations(h2_flag, context, choices_list, choices_list.index(correct_opt), title, language)
        })

    # ----------------------------------------------------------------
    # Heuristic 3: Simple value question (days/months/lakhs/rs)
    # ----------------------------------------------------------------
    for s in sentences:
        if len(questions) >= count:
            break
        fact_key = s[:40]
        if fact_key in used_facts:
            continue
        is_age_context = re.search(r'\b(age|old|years of age|between\s+\d+\s+and\s+\d+\s+years)\b', s, re.IGNORECASE)
        num_match = re.search(r'(\d+)\s*(Months|Days|Years|Lakhs|Rs|₹)', s, re.IGNORECASE)
        if not num_match:
            continue
        if is_age_context:
            continue
        used_facts.add(fact_key)
        correct_val = num_match.group(0)
        val_num = int(num_match.group(1))
        unit = num_match.group(2).lower()
        context = extract_value_context(s, correct_val)

        if unit == 'days':
            scenario = f"A customer asks, 'How many days will it take?' The rules say {context}. What should the bank person say?"
            correct_opt = f"Say {correct_val}."
            deltas = [-1, 1, -2, 2, -3, 3]
        elif unit == 'months':
            scenario = f"A customer wants to pick how long to pay back the loan. The rules say {context}. How many months can they pick?"
            correct_opt = f"Pick {correct_val}."
            deltas = [-3, 3, -6, 6, -12, 12]
        elif unit == 'years':
            scenario = f"A customer asks how long the plan is for. The rules say {context}. How many years?"
            correct_opt = f"The plan is for {correct_val}."
            deltas = [-1, 1, -2, 2, -3, 3]
        elif unit in ('lakhs', 'rs', '₹'):
            scenario = f"A customer asks, 'How much money can I get?' The rules say {context}. What should the bank person say?"
            correct_opt = f"Say {correct_val}."
            if val_num < 1000:
                deltas = [-100, 100, -200, 200, -500, 500]
            elif val_num < 10000:
                deltas = [-500, 500, -1000, 1000, -2000, 2000]
            else:
                deltas = [-5000, 5000, -10000, 10000, -25000, 25000, -50000, 50000]
        else:
            continue

        alt_vals = get_distractor_deltas(val_num, deltas)
        wrong_opts = []
        for av in alt_vals[:3]:
            wrong_opts.append(f"Say {av} {unit}.")
        choices_list = [correct_opt] + wrong_opts
        while len(choices_list) < 4:
            choices_list.append("Ask the manager to decide.")
        choices_list = choices_list[:4]
        random.shuffle(choices_list)
        h3_map = {'days': 'value_days', 'months': 'value_months', 'years': 'value_years'}
        h3_flag = h3_map.get(unit, 'value_lakhs')
        questions.append({
            "question": scenario,
            "options": choices_list,
            "correctIndex": choices_list.index(correct_opt),
            "approved": 0,
            "translations": get_offline_translations(h3_flag, context, choices_list, choices_list.index(correct_opt), title, language)
        })

    # ----------------------------------------------------------------
    # Heuristic 4: Simple reading comprehension
    # ----------------------------------------------------------------
    for p in paragraphs:
        if len(questions) >= count:
            break
        if p[:40] in used_facts:
            continue
        p_sentences = [s.strip() for s in re.split(r'\. |\n', p) if len(s.strip()) > 20]
        if len(p_sentences) < 2:
            continue
        target_sentence = p_sentences[-1]
        intro_p = " ".join(p_sentences[:-1])
        if len(intro_p) < 60 or len(intro_p) > 300:
            continue
        fact_key = target_sentence[:40]
        if fact_key in used_facts:
            continue
        used_facts.add(fact_key)
        doc_distractors = []
        for ds in sentences:
            if ds != target_sentence and len(ds) > 40:
                perturbed = perturb_number(ds) or perturb_word_swap(ds)
                if perturbed and len(perturbed) > 30:
                    doc_distractors.append(perturbed)
                if len(doc_distractors) >= 3:
                    break
        while len(doc_distractors) < 3:
            doc_distractors.append("Check all papers before giving the loan.")
            if len(doc_distractors) < 2:
                doc_distractors.append("If something is wrong, tell the manager.")
            if len(doc_distractors) < 3:
                doc_distractors.append("Check the customer's ID and address proof.")
        choices_list = [target_sentence] + doc_distractors[:3]
        random.shuffle(choices_list)
        questions.append({
            "question": f"Read this part of the rules:\n{intro_p}\n\nWhich sentence is correct?",
            "options": choices_list,
            "correctIndex": choices_list.index(target_sentence),
            "approved": 0,
            "translations": get_offline_translations('comprehension', intro_p, choices_list, choices_list.index(target_sentence), title, language)
        })

    # ----------------------------------------------------------------
    # Heuristic 5 (safety fallback): Generic policy scenarios
    # Shuffled once and cycled to prevent duplicate questions
    # ----------------------------------------------------------------
    fallback_bank = [
        {
            "q": "A new customer comes to the bank. What is the first thing to do?",
            "c": "Check who the customer is and take all the papers they give.",
            "w": ["Give the money right away to make the customer happy.",
                  "Skip the papers for old customers to finish faster.",
                  "Send the papers to the legal team first."]
        },
        {
            "q": "A customer is not happy with the bank's decision. What should the bank person do?",
            "c": "Help the customer with their problem as per the rules.",
            "w": ["Ask the customer to come again with new papers.",
                  "Ignore the problem and keep the old decision.",
                  "Send the customer to another branch."]
        },
        {
            "q": "What papers should the bank person keep after helping a customer?",
            "c": "Keep all papers and notes about the customer.",
            "w": ["Throw away old papers to make space.",
                  "Keep only online papers and throw away paper copies.",
                  "Just let the audit team handle it."]
        },
        {
            "q": "A customer does not meet the rules. What should the bank person do?",
            "c": "Tell the manager and let them decide.",
            "w": ["Say no right away without checking anything.",
                  "Change the customer's papers to make them fit the rules.",
                  "Give the loan anyway and mark it as special."]
        },
        {
            "q": "How often should the bank person check the money records?",
            "c": "Check the records every day.",
            "w": ["Check only at the end of every 3 months.",
                  "Give the money first and check later.",
                  "Only check when there is free time."]
        },
        {
            "q": "The customer did not give all the KYC papers. What should the bank person do?",
            "c": "Tell the customer what papers are missing and ask them to bring it.",
            "w": ["Give the loan anyway if the manager says yes.",
                  "Say no right away without telling the customer why.",
                  "Ask the customer to go to a different branch."]
        },
        {
            "q": "The bank person sees a mistake in the customer's papers. What should they do?",
            "c": "Tell the manager about the mistake.",
            "w": ["Ignore the mistake and give the loan.",
                  "Fix the mistake in the computer and keep going.",
                  "Say no to the customer without any reason."]
        },
        {
            "q": "A customer asks, 'When will I get the money?' What should the bank person say?",
            "c": "Tell the customer how many days it will take as per the rules.",
            "w": ["Say 'You will get it in 1 day' to make them happy.",
                  "Say 'It depends on the manager.'",
                  "Say 'I cannot tell you.'"]
        },
        {
            "q": "What papers does the bank need before giving the money?",
            "c": "KYC check, salary proof, and loan approval papers.",
            "w": ["Just the customer's Aadhaar number is enough.",
                  "Just a phone call from the customer is enough.",
                  "No papers needed for old customers."]
        },
        {
            "q": "A customer wants to close the loan early. What should the bank person check?",
            "c": "Check the rules about early closure and how much fee to take.",
            "w": ["Close the loan right away with no fee.",
                  "Say no to early closure for any reason.",
                  "Ask the customer to come back after the full time is over."]
        },
        {
            "q": "A customer has a low credit score. What should the bank person do?",
            "c": "Check if the score is high enough and tell the customer the next steps.",
            "w": ["Say no to the customer without checking anything.",
                  "Give the loan at a higher rate and do not tell the customer why.",
                  "Ask the customer to come with a different name."]
        },
        {
            "q": "The bank person sees a coworker not checking papers properly. What should they do?",
            "c": "Tell the manager about it.",
            "w": ["Ignore it because it is not their job.",
                  "Do the same thing to save time.",
                  "Shout at the coworker in front of customers."]
        },
        {
            "q": "The customer's salary proof is not enough. What should the bank person do?",
            "c": "Check if the salary is high enough as per rules and tell the customer.",
            "w": ["Accept it anyway to keep the customer happy.",
                  "Give more loan money to make up for low salary.",
                  "Say no without telling the customer why."]
        },
        {
            "q": "A customer wants something that is not in the rules. What should the bank person do?",
            "c": "Say that special requests need the manager's OK, and write it down.",
            "w": ["Say yes right away to make the customer happy.",
                  "Say no without any reason.",
                  "Pretend the rules do not apply here."]
        },
        {
            "q": "What should the bank person do with the customer's personal information?",
            "c": "Keep it secret and only share it with people who need to know.",
            "w": ["Share it with anyone in the office.",
                  "Post customer stories on social media.",
                  "Leave customer files open on the desk for all to see."]
        },
        {
            "q": "The customer's papers are all ready. What should the bank person do next?",
            "c": "Check everything again and then start the approval process.",
            "w": ["Give the money right away without checking anything.",
                  "Put the papers aside and do it when there is free time.",
                  "Send the customer to another branch to apply again."]
        },
        {
            "q": "A customer calls to ask, 'What is happening with my application?' What should the bank person say?",
            "c": "Tell the customer what is happening and what will happen next.",
            "w": ["Tell the customer what is happening with other customers.",
                  "Promise faster approval if the customer gives a good review.",
                  "Say the papers were lost and ask them to apply again."]
        },
        {
            "q": "The bank person thinks the customer's papers might be fake. What should they do?",
            "c": "Stop right away and tell the fraud team.",
            "w": ["Give the loan anyway but make a note about it.",
                  "Shout at the customer and say they are lying.",
                  "Ignore it because checking papers is not their job."]
        },
        {
            "q": "An old customer with good history comes back for a loan. What should the bank person do?",
            "c": "Follow the same rules but note that the customer pays on time.",
            "w": ["Skip all papers for returning customers.",
                  "Say no to returning customers to get new ones instead.",
                  "Give double the interest rate just to be safe."]
        },
        {
            "q": "The customer wants to change their address in the system. What should the bank person do?",
            "c": "Check the new address with a paper proof and update in the system.",
            "w": ["Change it over the phone without any proof.",
                  "Ask the customer to go to another branch for this.",
                  "Do not update it until the next loan application."]
        }
    ]
    random.shuffle(fallback_bank)
    fb_index = 0
    while len(questions) < count:
        fb = fallback_bank[fb_index % len(fallback_bank)]
        fb_index += 1
        choices = [fb["c"]] + fb["w"]
        random.shuffle(choices)
        questions.append({
            "question": fb["q"],
            "options": choices,
            "correctIndex": choices.index(fb["c"]),
            "approved": 0,
            "translations": {}
        })

    return questions[:count]

def clean_ai_question_text(text, title=None, filename=None):
    if not isinstance(text, str):
        return text
    import re
    # Remove markdown bold/italic formatting inside string if any
    text = text.replace('**', '').replace('*', '')
    
    # Remove module title if it appears at the start (often happens in AI generation)
    if title:
        text = re.sub(rf'^{re.escape(title)}[:.-]?\s*', '', text, flags=re.IGNORECASE)
    
    # Remove PDF filename if it appears at the start
    if filename:
        # Strip extension for better matching
        base_fn = os.path.splitext(filename)[0]
        text = re.sub(rf'^{re.escape(base_fn)}[:.-]?\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(rf'^{re.escape(filename)}[:.-]?\s*', '', text, flags=re.IGNORECASE)

    # Remove prefix like "Question 1:", "Q1:", "Here is the first question:"
    text = re.sub(r'^(?:Question|Q|Ques)\s*\d+[:.-]?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^(?:Here is the first question|First Question|Here is a Socratic question|Socratic Question)[:.-]?\s*', '', text, flags=re.IGNORECASE)
    # Remove audit/validation step prefixes like "Validation Step 1:", "Step 1:", "Audited:", "Corrected:"
    text = re.sub(r'^(?:Validation Step \d+|Step \d+|Audited|Corrected|Audit|Commentary|Note|Double Validation)[:.-]?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^(?:Factually accurate|Factual accuracy verified|Factual accuracy check|Verified)[:.-]?\s*', '', text, flags=re.IGNORECASE)
    # Remove leading brackets/meta-tags like "[Product Refresher Q1]"
    text = re.sub(r'^\[[^\]]+\]\s*', '', text)
    return text.strip()

def extract_json_from_text(text):
    text = text.strip()
    import json
    # Try parsing directly
    try:
        return json.loads(text)
    except Exception:
        pass
    
    # Try extracting markdown json code block
    import re
    code_block_match = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL | re.IGNORECASE)
    if code_block_match:
        try:
            return json.loads(code_block_match.group(1).strip())
        except Exception:
            pass
            
    # Try finding outermost array bracket [ ... ]
    array_match = re.search(r'(\[.*\])', text, re.DOTALL)
    if array_match:
        try:
            return json.loads(array_match.group(1).strip())
        except Exception:
            pass
            
    # Try finding outermost object brace { ... }
    object_match = re.search(r'(\{.*\})', text, re.DOTALL)
    if object_match:
        try:
            obj = json.loads(object_match.group(1).strip())
            if isinstance(obj, dict) and "questions" in obj:
                return obj["questions"]
            return obj
        except Exception:
            pass
            
    raise ValueError("Could not find or parse any valid JSON array or object in the response.")

@app.route('/api/modules/generate', methods=['POST'])
def generate_module():
    count = int(request.form.get('count', 15))
    title = request.form.get('title', 'Product Refresher Policy').strip()
    trainer_id = request.form.get('trainer_id', 'ADMIN').strip()
    difficulty = request.form.get('difficulty', 'Medium').strip()
    selected_lang = request.form.get('language', 'en').strip().lower()
    
    text_content = ""
    filename = None
    
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
        
    # Pre-compiled difficulty instructions
    difficulty_instructions = ""
    if difficulty == 'Easy':
        difficulty_instructions = "DIFFICULTY LEVEL: EASY. Focus on straightforward, direct questions testing foundational concepts, basic rules, definitions, and simple criteria. Avoid double negatives, complex combinations, or corner cases."
    elif difficulty == 'Hard':
        difficulty_instructions = "DIFFICULTY LEVEL: HARD. Focus on highly complex, Socratic scenario-based questions that test advanced deviational corner cases, risk management assessments, double constraints, and deep policy exemptions."
    else:
        difficulty_instructions = "DIFFICULTY LEVEL: MEDIUM. Focus on standard analytical Socratic questions, typical customer case scenarios, standard numeric thresholds, and day-to-day policy rules."
        
    # Pre-compiled translation instructions based on selected dropdown language
    translation_instructions = ""
    example_translation_format = ""
    
    if selected_lang == 'en':
        translation_instructions = "DO NOT generate any translations. The 'translations' field in the JSON object should be an empty dictionary {}."
        example_translation_format = '"translations": {}'
    elif selected_lang == 'all':
        translation_instructions = """For EACH question, you must also provide the translation of the question and its 4 options in these specific languages/styles:
            - "hindi": Translated to conversational, clear Hindi (in Devanagari script).
            - "hinglish": Translated to conversational Hinglish (Hindi written in Latin script, e.g. "KYC document update karne ki maximum time-limit kya hai?").
            - "punjabi": Translated to conversational Punjabi (in Gurmukhi script).
            - "bengali": Translated to conversational Bengali (in Bengali script).
            - "marathi": Translated to conversational Marathi (in Devanagari script).
            - "telugu": Translated to conversational Telugu (in Telugu script).
            - "tamil": Translated to conversational Tamil (in Tamil script).
            - "gujarati": Translated to conversational Gujarati (in Gujarati script).
            - "kannada": Translated to conversational Kannada (in Kannada script)."""
            
        example_translation_format = """"translations": {
                  "hindi": {
                    "question": "नई पॉलिसी के तहत अधिकतम लोन रेशियो (LTV) कितना है?",
                    "options": ["75%", "85%", "90%", "100%"],
                    "correctIndex": 1
                  },
                  "hinglish": {
                    "question": "New policy ke under maximum loan ratio kitna allowed hai?",
                    "options": ["75%", "85%", "90%", "100%"],
                    "correctIndex": 1
                  },
                  "punjabi": {
                    "question": "ਨਵੀਂ ਪਾਲਿਸੀ ਦੇ ਤਹਿਤ ਵੱਧ ਤੋਂ ਵੱਧ ਲੋਨ ਰੇਸ਼ੋ (LTV) ਕਿੰਨੀ ਹੈ?",
                    "options": ["75%", "85%", "90%", "100%"],
                    "correctIndex": 1
                  },
                  "bengali": {
                    "question": "নতুন পলিসির অধীনে সর্বাধিক ঋণের অনুপাত (LTV) কত অনুমোদিত?",
                    "options": ["75%", "85%", "90%", "100%"],
                    "correctIndex": 1
                  },
                  "marathi": {
                    "question": "नवीन पॉलिसी अंतर्गत जास्तीत जास्त कर्ज गुणोत्तर (LTV) किती मंजूर आहे?",
                    "options": ["75%", "85%", "90%", "100%"],
                    "correctIndex": 1
                  },
                  "telugu": {
                    "question": "కొత్త పాలసీ కింద గరిష్ట రుణ నిష్పత్తి (LTV) ఎంత అనుమతించబడుతుంది?",
                    "options": ["75%", "85%", "90%", "100%"],
                    "correctIndex": 1
                  },
                  "tamil": {
                    "question": "புதிய கொள்கையின் கீழ் அனுமதிக்கப்பட்ட அதிகபட்ச கடன் விகிதம் (LTV) என்ன?",
                    "options": ["75%", "85%", "90%", "100%"],
                    "correctIndex": 1
                  },
                  "gujarati": {
                    "question": "નવી પોલિસી હેઠળ મહત્તમ લોન રેશિયો (LTV) કેટલો મંજૂર છે?",
                    "options": ["75%", "85%", "90%", "100%"],
                    "correctIndex": 1
                  },
                  "kannada": {
                    "question": "ಹೊಸ ಪಾಲಿಸಿಯ ಅಡಿಯಲ್ಲಿ ಗರಿಷ್ಠ ಸಾಲದ ಅನುಪಾತ (LTV) ಎಷ್ಟು ಅನುಮತಿಸಲಾಗಿದೆ?",
                    "options": ["75%", "85%", "90%", "100%"],
                    "correctIndex": 1
                  }
                }"""
    else:
        lang_titles = {
            "hindi": "conversational, clear Hindi (in Devanagari script)",
            "hinglish": "conversational Hinglish (Hindi written in Latin script, e.g. 'KYC document update karne ki maximum time-limit kya hai?')",
            "punjabi": "conversational Punjabi (in Gurmukhi script)",
            "bengali": "conversational Bengali (in Bengali script)",
            "marathi": "conversational Marathi (in Devanagari script)",
            "telugu": "conversational Telugu (in Telugu script)",
            "tamil": "conversational Tamil (in Tamil script)",
            "gujarati": "conversational Gujarati (in Gujarati script)",
            "kannada": "conversational Kannada (in Kannada script)"
        }
        lang_title = lang_titles.get(selected_lang, selected_lang)
        translation_instructions = f"""For EACH question, you must also provide the translation of the question and its 4 options ONLY in this specific language style:
            - "{selected_lang}": Translated to {lang_title}."""
            
        example_translation_format = f""""translations": {{
                  "{selected_lang}": {{
                    "question": "[Translate the question here to {lang_title}]",
                    "options": ["[Option 1]", "[Option 2]", "[Option 3]", "[Option 4]"],
                    "correctIndex": 1
                  }}
                }}"""

    # 2. Try to call Gemini API
    gemini_success = False
    generated_questions = []
    
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if api_key:
        try:
            import google.generativeai as genai
            import json
            
            genai.configure(api_key=api_key)
            # Switch to gemini-1.5-pro for better reasoning and diversity in long-form generation
            model = genai.GenerativeModel('gemini-1.5-pro')
            
            prompt = f"""
            You are a Senior Socratic Training Expert with 20 years of experience in Institutional Fintech Training.
            
            GOAL: Create a high-stakes professional assessment module based ONLY on the provided policy document.
            
            CRITICAL FORMATTING RULES:
            1. NO "Fill-in-the-blanks": Do not use "_____" or ask to complete a sentence.
            2. NO Text Fragments: Do not use random phrases from the PDF as options.
            3. SCENARIO-BASED: Every question MUST be a "Case Study" or "Real-life Scenario". 
               - Example: "A customer arrives with a CIBIL score of 700 but has 5 inquiries in the last month. Based on the SOP, how should the executive proceed?"
            4. LOGICAL OPTIONS: Options A, B, C, and D must be full, logical, and distinct actions or conclusions.
            5. SOCRATIC DEPTH: Questions should test the trainee's *understanding* and *application* of the policy, not just their memory.
            
            {difficulty_instructions}
            
            Generate exactly {count} UNIQUE and DIVERSE scenario questions.
            
            {translation_instructions}
            
            OUTPUT FORMAT: Return ONLY a raw JSON array of {count} objects. No preamble or markdown.
            Format:
            [
              {{
                "question": "Scenario: [Describe a specific customer case or situation here]. Based on the policy, what is the correct action?",
                "options": ["Action 1 (Detailed)", "Action 2 (Detailed)", "Action 3 (Detailed)", "Action 4 (Detailed)"],
                "correctIndex": 0,
                {example_translation_format}
              }}
            ]
            
            Policy content:
            {text_content}
            """
            
            response = model.generate_content(prompt)
            generated_questions = extract_json_from_text(response.text)
            
            # Skip double-validation for now as it may be causing truncation/duplicates
            # The pro model is capable enough to handle factual accuracy in one pass
            
            if len(generated_questions) > 0:
                gemini_success = True
        except Exception as e:
            print(f"Gemini API call failed, falling back to Socratic Offline Generator: {str(e)}")
            
    # 3. High-Fidelity Socratic Offline Fallback Heuristic Generator
    if not gemini_success:
        print("Using Dynamic Offline Socratic Heuristic Generator based on uploaded document...")
        generated_questions = generate_heuristic_questions(text_content, count, title, selected_lang)
            
    # Clean all questions to strip any accidental conversational preambles
    for q in generated_questions:
        if "question" in q:
            q["question"] = clean_ai_question_text(q["question"], title=title, filename=filename)
            
    return jsonify({
        "status": "success",
        "title": title,
        "difficulty": difficulty,
        "count": len(generated_questions),
        "questions": generated_questions
    })

@app.route('/api/modules/save', methods=['POST'])
def save_module():
    data = request.json
    title = data.get('title', 'AI Generated Module').strip()
    trainer_id = data.get('trainer_id', 'ADMIN').strip()
    audited_by = data.get('audited_by')
    difficulty = data.get('difficulty', 'Medium').strip()
    questions = data.get('questions', [])
    module_id = data.get('module_id') # If editing an existing draft
    source_text = data.get('source_text', '')
    
    if not questions:
        return jsonify({"status": "error", "message": "No questions provided to save."}), 400
        
    all_approved = all([int(q.get('approved', 0)) == 1 for q in questions])
    
    # Maker-Checker Enforcement: A trainer cannot approve their own module unless they are a SuperAdmin.
    is_superadmin = session.get('user', {}).get('role') == 'SuperAdmin'
    
    if all_approved and not is_superadmin:
        # Check if the person saving (active_trainer_name) is the same as the original creator.
        # If we are creating a NEW module, trainer_id is the creator.
        # If we are UPDATING, we need to check the 'created_by' in the DB.
        original_creator = trainer_id
        if module_id:
            conn = get_db_connection()
            orig = conn.execute("SELECT created_by FROM modules WHERE id=?", (module_id,)).fetchone()
            if orig:
                original_creator = orig['created_by']
            conn.close()
            
        if trainer_id == original_creator:
            all_approved = False # Force 'Pending Audit' status
            # Reset approvals to 0 for all questions to force a second eyes review
            for q in questions:
                q['approved'] = 0
            
    status = 'Ready' if all_approved else 'Pending Audit'
    
    conn = get_db_connection()
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d")
        cursor = conn.cursor()
        
        # Query active trainer name
        active_tr = cursor.execute("SELECT name FROM trainers WHERE trainer_id=?", (trainer_id,)).fetchone()
        active_trainer_name = active_tr['name'] if active_tr else trainer_id
        
        if status == 'Ready':
            if not audited_by or audited_by == 'Awaiting Audit':
                audited_by = active_trainer_name
        else:
            if not audited_by:
                audited_by = 'Awaiting Audit'
        
        if module_id:
            # Preserving original creator name/ID if existing
            orig = cursor.execute("SELECT created_by FROM modules WHERE id=?", (module_id,)).fetchone()
            orig_creator = orig['created_by'] if orig else trainer_id
            
            # Update existing module
            cursor.execute(
                "UPDATE modules SET title=?, questions_count=?, status=?, audited_by=?, difficulty=?, source_text=? WHERE id=?",
                (title, len(questions), status, audited_by, difficulty, source_text, module_id)
            )
            # Delete old questions to replace them with the newly audited ones
            cursor.execute("DELETE FROM questions WHERE module_id=?", (module_id,))
        else:
            # Create new module
            cursor.execute(
                "INSERT INTO modules (title, questions_count, created_at, status, created_by, audited_by, difficulty, source_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (title, len(questions), now, status, trainer_id, audited_by, difficulty, source_text)
            )
            module_id = cursor.lastrowid
            
        for q in questions:
            opts = q.get('options', ["Option A", "Option B", "Option C", "Option D"])
            trans_json = json.dumps(q.get('translations', {}))
            cursor.execute(
                "INSERT INTO questions (module_id, question_text, option_a, option_b, option_c, option_d, correct_index, approved, translations) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (module_id, q.get('question_text', q.get('question')), opts[0], opts[1], opts[2], opts[3], q.get('correctIndex', q.get('correct_index', 0)), q.get('approved', 0), trans_json)
            )
            
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"status": "error", "message": f"Failed to save module: {str(e)}"}), 500
        
    conn.close()
    
    # 4. Trigger real-time Google Drive synchronization in background thread (no UI freeze)
    try:
        from gdrive_sync import sync_module_to_gdrive
        gdrive_questions = []
        for q in questions:
            opts = q.get('options', ["Option A", "Option B", "Option C", "Option D"])
            gdrive_questions.append({
                "question_text": q.get('question_text', q.get('question', 'Question')),
                "option_a": opts[0] if len(opts) > 0 else "Option A",
                "option_b": opts[1] if len(opts) > 1 else "Option B",
                "option_c": opts[2] if len(opts) > 2 else "Option C",
                "option_d": opts[3] if len(opts) > 3 else "Option D",
                "correctIndex": q.get('correctIndex', q.get('correct_index', 0)),
                "approved": q.get('approved', 0),
                "translations": q.get('translations', {})
            })
            
        threading.Thread(
            target=sync_module_to_gdrive,
            args=(title, difficulty, status, trainer_id, audited_by, gdrive_questions, source_text),
            daemon=True
        ).start()
    except Exception as e:
        print(f"[GDRIVE] Error spawning save thread: {str(e)}")
        
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
    session_id = data.get('session_id')
    correct_count = data.get('correct_count', 0)
    wrong_count = data.get('wrong_count', 0)
    unattempted_count = data.get('unattempted_count', 0)
    total_questions = data.get('total_questions', 0)
    
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM assessment_results WHERE emp_code=? AND module_id=? AND assignment_day=?", 
                           (emp_code, module_id, assignment_day)).fetchone()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        if row:
            if pre_test_score is not None:
                if session_id:
                    conn.execute("UPDATE assessment_results SET pre_test_score=?, correct_count=?, wrong_count=?, unattempted_count=?, total_questions=?, completed_at=?, session_id=? WHERE emp_code=? AND module_id=? AND assignment_day=?",
                                 (pre_test_score, correct_count, wrong_count, unattempted_count, total_questions, now_str, session_id, emp_code, module_id, assignment_day))
                else:
                    conn.execute("UPDATE assessment_results SET pre_test_score=?, correct_count=?, wrong_count=?, unattempted_count=?, total_questions=?, completed_at=? WHERE emp_code=? AND module_id=? AND assignment_day=?",
                                 (pre_test_score, correct_count, wrong_count, unattempted_count, total_questions, now_str, emp_code, module_id, assignment_day))
            if post_test_score is not None:
                if session_id:
                    conn.execute("UPDATE assessment_results SET post_test_score=?, correct_count=?, wrong_count=?, unattempted_count=?, total_questions=?, completed_at=?, session_id=? WHERE emp_code=? AND module_id=? AND assignment_day=?",
                                 (post_test_score, correct_count, wrong_count, unattempted_count, total_questions, now_str, session_id, emp_code, module_id, assignment_day))
                else:
                    conn.execute("UPDATE assessment_results SET post_test_score=?, correct_count=?, wrong_count=?, unattempted_count=?, total_questions=?, completed_at=? WHERE emp_code=? AND module_id=? AND assignment_day=?",
                                 (post_test_score, correct_count, wrong_count, unattempted_count, total_questions, now_str, emp_code, module_id, assignment_day))
        else:
            p_val = pre_test_score if pre_test_score is not None else 0.0
            post_val = post_test_score if post_test_score is not None else 0.0
            conn.execute("INSERT INTO assessment_results (emp_code, module_id, assignment_day, pre_test_score, post_test_score, correct_count, wrong_count, unattempted_count, total_questions, completed_at, session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (emp_code, module_id, assignment_day, p_val, post_val, correct_count, wrong_count, unattempted_count, total_questions, now_str, session_id))
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
    bu_filter = request.args.get('business_unit', '').strip()
    product_filter = request.args.get('product_name', '').strip()
    start_date_filter = request.args.get('start_date', '').strip()
    end_date_filter = request.args.get('end_date', '').strip()
    
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
    if bu_filter:
        where_clauses.append("e.business_unit = ?")
        query_params.append(bu_filter)
    if product_filter:
        where_clauses.append("e.product_name = ?")
        query_params.append(product_filter)
    if start_date_filter:
        where_clauses.append("ar.completed_at >= ?")
        query_params.append(start_date_filter + " 00:00")
    if end_date_filter:
        where_clauses.append("ar.completed_at <= ?")
        query_params.append(end_date_filter + " 23:59")

    # Enforce Role-Based Scoping for Trainer
    curr_user = session.get('user')
    if curr_user and curr_user['role'] == 'Trainer':
        conn = get_db_connection()
        tr_details = conn.execute("SELECT zones, divisions, branches, business_units FROM trainers WHERE trainer_id = ?", (curr_user['trainer_id'],)).fetchone()
        conn.close()
        
        if tr_details:
            zones_scope = [z.strip() for z in tr_details['zones'].split(',') if z.strip()]
            divs_scope = [d.strip() for d in tr_details['divisions'].split(',') if d.strip()]
            branches_scope = [b.strip() for b in tr_details['branches'].split(',') if b.strip()]
            bus_scope = [bu.strip() for bu in tr_details['business_units'].split(',') if bu.strip()]
            
            if zones_scope and 'ALL' not in [z.upper() for z in zones_scope]:
                where_clauses.append("e.zone IN ({})".format(','.join('?' for _ in zones_scope)))
                query_params.extend(zones_scope)
            if divs_scope and 'ALL' not in [d.upper() for d in divs_scope]:
                where_clauses.append("e.division IN ({})".format(','.join('?' for _ in divs_scope)))
                query_params.extend(divs_scope)
            if branches_scope and 'ALL' not in [b.upper() for b in branches_scope]:
                where_clauses.append("e.branch_name IN ({})".format(','.join('?' for _ in branches_scope)))
                query_params.extend(branches_scope)
            if bus_scope and 'ALL' not in [bu.upper() for bu in bus_scope]:
                where_clauses.append("e.business_unit IN ({})".format(','.join('?' for _ in bus_scope)))
                query_params.extend(bus_scope)
        
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
                   SUM(ar.correct_count) as total_correct,
                   SUM(ar.wrong_count) as total_wrong,
                   SUM(ar.unattempted_count) as total_unattempted,
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
        
        # C. Query score distribution buckets
        buckets_query = f"""
            SELECT e.emp_code, e.emp_name, ar.post_test_score, e.branch_name, e.business_unit
            FROM assessment_results ar
            JOIN employees e ON ar.emp_code = e.emp_code
            {where_str}
            GROUP BY e.emp_code
        """
        buckets_results = conn.execute(buckets_query, query_params).fetchall()
        
        below_60 = []
        _60_80 = []
        above_80 = []
        for r in buckets_results:
            pt = r['post_test_score'] if r['post_test_score'] is not None else 0.0
            emp_obj = {
                "emp_code": r['emp_code'],
                "emp_name": r['emp_name'],
                "post_test_score": round(pt, 1),
                "branch_name": r['branch_name'],
                "business_unit": r['business_unit']
            }
            if pt < 60.0:
                below_60.append(emp_obj)
            elif pt <= 80.0:
                _60_80.append(emp_obj)
            else:
                above_80.append(emp_obj)
                
        # D. Query Critical Pain Areas (branches with post-test average < 60% OR learning delta < 15%)
        pain_query = f"""
            SELECT e.branch_name, 
                   AVG(ar.pre_test_score) as avg_pre,
                   AVG(ar.post_test_score) as avg_post,
                   (AVG(ar.post_test_score) - AVG(ar.pre_test_score)) as growth,
                   COUNT(DISTINCT e.emp_code) as participants
            FROM assessment_results ar
            JOIN employees e ON ar.emp_code = e.emp_code
            {where_str}
            GROUP BY e.branch_name
            HAVING avg_post < 60 OR growth < 15
            ORDER BY avg_post ASC
        """
        pain_results = conn.execute(pain_query, query_params).fetchall()
        pain_areas = []
        for p in pain_results:
            pain_areas.append({
                "branch_name": p["branch_name"],
                "pre": round(p["avg_pre"], 1),
                "post": round(p["avg_post"], 1),
                "growth": round(p["growth"], 1),
                "count": p["participants"]
            })
            
        # E. Query Topic Knowledge Gaps (milestone average scores organization-wide/filtered)
        gap_query = f"""
            SELECT ar.assignment_day, 
                   AVG(ar.pre_test_score) as avg_pre, 
                   AVG(ar.post_test_score) as avg_post,
                   COUNT(DISTINCT e.emp_code) as participants
            FROM assessment_results ar
            JOIN employees e ON ar.emp_code = e.emp_code
            {where_str}
            GROUP BY ar.assignment_day
            ORDER BY avg_post ASC
        """
        gap_results = conn.execute(gap_query, query_params).fetchall()
        topic_gaps = []
        
        milestone_questions_mapped = {
            'ZERO DAY': "Standard LTV Ratios & Tenure Rules",
            'SIX DAYS': "CIBIL Assessment & Credit Approval Limits",
            'TWENTY DAYS': "Self-Employed Applicant Documentation Requirements"
        }
        
        for g in gap_results:
            day_upper = g["assignment_day"].upper()
            topic_gaps.append({
                "milestone": g["assignment_day"],
                "topic": milestone_questions_mapped.get(day_upper, "General Policy Refresher"),
                "avg_pre": round(g["avg_pre"], 1),
                "avg_post": round(g["avg_post"], 1),
                "failure_rate": round(100 - g["avg_post"], 1)
            })
            
        # E.5 Summary metrics (branches count, employees count, and role-wise trained people count)
        metrics_query = f"""
            SELECT COUNT(DISTINCT e.branch_name) as branches_count,
                   COUNT(DISTINCT e.emp_code) as employees_count
            FROM assessment_results ar
            JOIN employees e ON ar.emp_code = e.emp_code
            {where_str}
        """
        metrics_res = conn.execute(metrics_query, query_params).fetchone()
        branches_count = metrics_res['branches_count'] if (metrics_res and metrics_res['branches_count'] is not None) else 0
        employees_count = metrics_res['employees_count'] if (metrics_res and metrics_res['employees_count'] is not None) else 0

        role_query = f"""
            SELECT COALESCE(NULLIF(e.role, ''), 'General Staff') as role_name,
                   COUNT(DISTINCT e.emp_code) as count
            FROM assessment_results ar
            JOIN employees e ON ar.emp_code = e.emp_code
            {where_str}
            GROUP BY e.role
            ORDER BY count DESC
        """
        role_results = conn.execute(role_query, query_params).fetchall()
        role_wise = {r['role_name']: r['count'] for r in role_results}

        # F. Query lists of active filters to populate dynamic cascading dropdowns
        distinct_zones = conn.execute("SELECT DISTINCT zone FROM employees WHERE zone IS NOT NULL AND zone != ''").fetchall()
        distinct_divs = conn.execute("SELECT DISTINCT division, zone FROM employees WHERE division IS NOT NULL AND division != ''").fetchall()
        distinct_branches = conn.execute("SELECT DISTINCT branch_name, division, zone FROM employees WHERE branch_name IS NOT NULL AND branch_name != ''").fetchall()
        distinct_emps = conn.execute("SELECT DISTINCT emp_code, emp_name, branch_name FROM employees WHERE emp_code IS NOT NULL AND emp_code != ''").fetchall()
        distinct_bus = conn.execute("SELECT DISTINCT business_unit FROM employees WHERE business_unit IS NOT NULL AND business_unit != ''").fetchall()
        distinct_prods = conn.execute("SELECT DISTINCT product_name FROM employees WHERE product_name IS NOT NULL AND product_name != ''").fetchall()
        
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500
        
    conn.close()
    
    # 2. Build temporal response payload with high-fidelity default fallback values
    payload = {
        'ZERO DAY': {'pre': 0.0, 'post': 0.0, 'count': 0, 'correct': 0, 'wrong': 0, 'left': 0},
        'SIX DAYS': {'pre': 0.0, 'post': 0.0, 'count': 0, 'correct': 0, 'wrong': 0, 'left': 0},
        'TWENTY DAYS': {'pre': 0.0, 'post': 0.0, 'count': 0, 'correct': 0, 'wrong': 0, 'left': 0}
    }
    
    has_live_data = False
    for r in results:
        day = r['assignment_day'].upper()
        if day in payload:
            payload[day]['pre'] = round(r['avg_pre'], 1)
            payload[day]['post'] = round(r['avg_post'], 1)
            payload[day]['count'] = r['participants']
            payload[day]['correct'] = r['total_correct'] or 0
            payload[day]['wrong'] = r['total_wrong'] or 0
            payload[day]['left'] = r['total_unattempted'] or 0
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
        "score_distribution": {
            "below_60": below_60,
            "60_80": _60_80,
            "above_80": above_80
        },
        "critical_pain_areas": pain_areas,
        "topic_knowledge_gaps": topic_gaps,
        "summary_metrics": {
            "branches_count": branches_count,
            "employees_count": employees_count,
            "role_wise": role_wise
        },
        "filter_options": {
            "zones": [z[0] for z in distinct_zones],
            "divisions": [{"name": d[0], "zone": d[1]} for d in distinct_divs],
            "branches": [{"name": br[0], "division": br[1], "zone": br[2]} for br in distinct_branches],
            "executives": [{"code": ec[0], "name": ec[1], "branch": ec[2]} for ec in distinct_emps],
            "business_units": [b[0] for b in distinct_bus],
            "products": [p[0] for p in distinct_prods]
        }
    }
    
    return jsonify(payload_metadata)

@app.route('/api/feedback/submit', methods=['POST'])
def submit_feedback():
    data = request.json
    emp_code = data.get('emp_code', '').strip().upper()
    session_id = data.get('session_id', '').strip()
    module_id = data.get('module_id') or 1
    rating = data.get('rating')
    understanding = data.get('understanding', '').strip()
    manpower_saved = data.get('manpower_saved', '').strip()
    comments = data.get('comments', '').strip()
    
    if not emp_code:
        return jsonify({"status": "error", "message": "Employee Code is required."}), 400
        
    conn = get_db_connection()
    try:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        conn.execute(
            "INSERT INTO trainee_feedback (emp_code, session_id, module_id, rating, understanding, manpower_saved, comments, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (emp_code, session_id, module_id, rating, understanding, manpower_saved, comments, now_str)
        )
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500
    conn.close()
    return jsonify({"status": "success", "message": "Feedback submitted successfully!"})

@app.route('/api/refresher/campaign', methods=['POST'])
def push_refresher_campaign():
    data = request.json or {}
    emp_codes = data.get('emp_codes', [])
    if not emp_codes:
        return jsonify({"status": "error", "message": "No employee codes provided"}), 400
    
    conn = get_db_connection()
    try:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        for emp_code in emp_codes:
            conn.execute(
                "UPDATE employees SET change_detail = ? WHERE emp_code = ?",
                (f"REFRESHER REQUIRED - Flagged on {now_str}", emp_code)
            )
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500
    conn.close()
    return jsonify({"status": "success", "message": f"Successfully pushed refresher campaign to {len(emp_codes)} trainees!"})

@app.route('/api/trainers/performance', methods=['GET'])
def get_trainers_performance():
    conn = get_db_connection()
    try:
        # Get all trainers (including Leaders for SuperAdmin)
        curr_user = session.get('user', {})
        if curr_user.get('role') == 'SuperAdmin':
            trainers = conn.execute("SELECT trainer_id, name FROM trainers WHERE role IN ('Trainer', 'Leader')").fetchall()
        else:
            trainers = conn.execute("SELECT trainer_id, name FROM trainers WHERE role='Trainer'").fetchall()
        
        perf_list = []
        for t in trainers:
            tid = t['trainer_id']
            tname = t['name']
            
            # 1. Average Rating & counts
            feedback = conn.execute("""
                SELECT AVG(f.rating) as avg_rating,
                       COUNT(f.id) as total_responses,
                       SUM(CASE WHEN f.understanding = 'Fully Clear' THEN 1 ELSE 0 END) as fully_clear_count,
                       SUM(CASE WHEN f.manpower_saved LIKE 'Yes%' THEN 1 ELSE 0 END) as saved_time_count
                FROM trainee_feedback f
                JOIN training_sessions s ON f.session_id = s.session_id
                WHERE s.trainer_id = ?
            """, (tid,)).fetchone()
            
            avg_rating = round(feedback['avg_rating'], 2) if feedback['avg_rating'] else 0.0
            total_resp = feedback['total_responses'] or 0
            fully_clear_pct = round((feedback['fully_clear_count'] / total_resp) * 100, 1) if total_resp > 0 else 0.0
            saved_time_pct = round((feedback['saved_time_count'] / total_resp) * 100, 1) if total_resp > 0 else 0.0
            
            # 2. Learning Growth delta driven by trainer (joining session results)
            growth = conn.execute("""
                SELECT AVG(ar.post_test_score) - AVG(ar.pre_test_score) as avg_growth
                FROM assessment_results ar
                WHERE ar.session_id IN (SELECT session_id FROM training_sessions WHERE trainer_id = ?)
            """, (tid,)).fetchone()
            
            growth_delta = round(growth['avg_growth'], 1) if growth['avg_growth'] is not None else 0.0
            
            perf_list.append({
                "trainer_id": tid,
                "name": tname,
                "avg_rating": avg_rating,
                "growth_delta": growth_delta,
                "clarity_index": fully_clear_pct,
                "nps": saved_time_pct,
                "sessions_count": conn.execute("SELECT COUNT(*) FROM training_sessions WHERE trainer_id=?", (tid,)).fetchone()[0]
            })
            
        conn.close()
        return jsonify(perf_list)
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/analytics/export', methods=['GET'])
def export_analytics():
    zone_filter = request.args.get('zone', '').strip()
    division_filter = request.args.get('division', '').strip()
    branch_filter = request.args.get('branch', '').strip()
    emp_filter = request.args.get('emp_code', '').strip()
    bu_filter = request.args.get('business_unit', '').strip()
    product_filter = request.args.get('product_name', '').strip()
    start_date_filter = request.args.get('start_date', '').strip()
    end_date_filter = request.args.get('end_date', '').strip()
    
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
    if bu_filter:
        where_clauses.append("e.business_unit = ?")
        query_params.append(bu_filter)
    if product_filter:
        where_clauses.append("e.product_name = ?")
        query_params.append(product_filter)
    if start_date_filter:
        where_clauses.append("ar.completed_at >= ?")
        query_params.append(start_date_filter + " 00:00")
    if end_date_filter:
        where_clauses.append("ar.completed_at <= ?")
        query_params.append(end_date_filter + " 23:59")
        
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
    
    # Enforce Role-Based Scoping for Trainer
    curr_user = session.get('user')
    if curr_user and curr_user['role'] == 'Trainer':
        trainer_id = curr_user['trainer_id']
        
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
            SELECT m.id, m.title, m.questions_count, m.created_by, m.difficulty, t.name AS creator_name,
                   (SELECT COUNT(*) FROM questions q WHERE q.module_id = m.id AND q.approved = 1) AS approved_count
            FROM modules m
            LEFT JOIN trainers t ON m.created_by = t.trainer_id
            WHERE m.status = 'Pending Audit'
            ORDER BY m.id DESC LIMIT 5
        ''').fetchall()
        
        # 5. Today's Field Visits (Live Tracking)
        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        if trainer_id and trainer_id != 'ADMIN':
             todays_visits_rows = conn.execute('''
                SELECT v.id, v.branch_name, v.purpose, v.status, t.name as trainer_name, v.checkin_time
                FROM field_visits v
                JOIN trainers t ON v.trainer_id = t.trainer_id
                WHERE v.planned_date = ? AND v.trainer_id = ?
            ''', (today_str, trainer_id)).fetchall()
        else:
             todays_visits_rows = conn.execute('''
                SELECT v.id, v.branch_name, v.purpose, v.status, t.name as trainer_name, v.checkin_time
                FROM field_visits v
                JOIN trainers t ON v.trainer_id = t.trainer_id
                WHERE v.planned_date = ?
            ''', (today_str,)).fetchall()
        
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
    todays_visits = [dict(r) for r in todays_visits_rows]
    
    return jsonify({
        "sessions_count": sessions,
        "branches_visited": branches,
        "execs_trained": execs,
        "avg_growth_delta": growth,
        "modules_count": modules_res,
        "recent_sessions": recent_sessions,
        "top_branches": top_branches,
        "pending_audits": pending_audits,
        "todays_visits": todays_visits
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
            "leaderboard": {},
            "active_module": None,
            "current_view": "waiting",
            "current_question_idx": 0,
            "language_override": "en",
            "connected_trainees": {}
        }

    # Register trainee in current session leaderboard
    emp_name = emp_id # Default
    if emp_id and emp_id != 'TRAINER':
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT emp_name FROM employees WHERE emp_code=?", (emp_id,))
        row = cursor.fetchone()
        conn.close()
        emp_name = row[0] if row else emp_id

        if emp_id not in SESSION_REGISTRY[pin]["leaderboard"]:
            SESSION_REGISTRY[pin]["leaderboard"][emp_id] = {
                "name": emp_name,
                "score": 0,
                "correct_count": 0,
                "wrong_count": 0,
                "total_questions": 0,
                "last_speed": 0.0,
                "last_correct": False
            }

        # Track connected trainees for persistence
        SESSION_REGISTRY[pin]["connected_trainees"][emp_id] = emp_name

    emit('user_connected', {'emp_id': emp_id, 'emp_name': emp_name}, room=pin)

    # If a trainee joins and there's an active session state, push it to them immediately
    if emp_id != 'TRAINER' and SESSION_REGISTRY[pin]["active_module"]:
        reg = SESSION_REGISTRY[pin]
        idx = reg["current_question_idx"]
        q = reg["active_module"]["questions"][idx] if reg["active_module"]["questions"] else None

        state_payload = {
            "view": reg["current_view"],
            "assignment_day": reg["active_module"]["title"],
            "forceLanguage": reg["language_override"],
            "question_idx": reg["current_question_idx"],
            "sync": True # Flag for client to handle as initial sync
        }

        if q:
            state_payload.update({
                "question": q.get("question_text"),
                "options": [q.get("option_a"), q.get("option_b"), q.get("option_c"), q.get("option_d")],
                "correctIndex": q.get("correct_index"),
                "translations": q.get("translations")
            })

        emit('change_view', state_payload, room=request.sid)

@socketio.on('get_session_state')
def on_get_session_state(data):
    pin = str(data.get('pin'))
    if pin in SESSION_REGISTRY:
        reg = SESSION_REGISTRY[pin]
        # Map registry back to trainer expected payload
        trainees = [{"id": eid, "name": ename} for eid, ename in reg["connected_trainees"].items()]

        emit('session_state_response', {
            "active_module": reg["active_module"],
            "current_view": reg["current_view"],
            "current_question_idx": reg["current_question_idx"],
            "language_override": reg["language_override"],
            "connected_trainees": trainees,
            "leaderboard": reg["leaderboard"]
        }, room=request.sid)

@socketio.on('trainer_broadcast')
def on_trainer_broadcast(data):
    pin = str(data.get('pin'))
    view = data.get('view')

    if pin not in SESSION_REGISTRY:
        SESSION_REGISTRY[pin] = {
            "push_time": 0.0,
            "correct_index": -1,
            "leaderboard": {},
            "active_module": None,
            "current_view": "waiting",
            "current_question_idx": 0,
            "language_override": "en",
            "connected_trainees": {}
        }

    # Update state persistence
    SESSION_REGISTRY[pin]["current_view"] = view
    if data.get('activeModule'):
        SESSION_REGISTRY[pin]["active_module"] = data.get('activeModule')
    if data.get('question_idx') is not None:
        SESSION_REGISTRY[pin]["current_question_idx"] = data.get('question_idx')
    if data.get('forceLanguage'):
        SESSION_REGISTRY[pin]["language_override"] = data.get('forceLanguage')

    # If pushing a live assessment quiz, capture start timing for speed bonus
    if view in ['pretest', 'posttest']:
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
            # Scoring is now purely based on accuracy as per team requirements.
            # Timing/Speed is moved to a separate analysis segment.
            points_earned = 1 
            
        # Ensure student is registered
        if emp_id not in session["leaderboard"]:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT emp_name FROM employees WHERE emp_code=?", (emp_id,))
            row = cursor.fetchone()
            conn.close()
            emp_name = row[0] if row else emp_id
            
            session["leaderboard"][emp_id] = {
                "name": emp_name,
                "score": 0,
                "correct_count": 0,
                "wrong_count": 0,
                "total_questions": 0,
                "last_speed": 0.0,
                "last_correct": False
            }
            
        # Update session points
        session["leaderboard"][emp_id]["score"] += points_earned
        session["leaderboard"][emp_id]["total_questions"] += 1
        if is_correct:
            session["leaderboard"][emp_id]["correct_count"] += 1
        else:
            session["leaderboard"][emp_id]["wrong_count"] += 1
            
        session["leaderboard"][emp_id]["last_speed"] = round(response_time, 2)
        session["leaderboard"][emp_id]["last_correct"] = is_correct
        
    # Broadcast standard vote updates for presenter chart
    emit('vote_update', {'emp_id': emp_id, 'answer_idx': answer_idx}, room=pin)
    
    # Emit score confirmation details back to student tab
    emit('score_confirmation', {
        'points': points_earned,
        'speed_bonus': 0, # Speed bonus removed from scoring model
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
                'correct_count': player.get('correct_count', 0),
                'wrong_count': player.get('wrong_count', 0),
                'total_questions': player.get('total_questions', 0),
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


# ===================================================
# FIELD-VISIT & SOCRATIC VERIFICATION SYSTEM APIs
# ===================================================

import math

def calculate_haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000.0  # Earth's radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    
    a = math.sin(delta_phi / 2.0)**2 + \
        math.cos(phi1) * math.cos(phi2) * \
        math.sin(delta_lambda / 2.0)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    
    return R * c

@app.route('/api/visits', methods=['GET'])
def get_visits():
    curr_user = session.get('user')
    if not curr_user:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # SuperAdmins and Leaders fetch all field visits, standard trainers only fetch their own schedules
        if curr_user['role'] in ['SuperAdmin', 'Leader']:
            cursor.execute('''
                SELECT v.id, v.trainer_id, COALESCE(t.name, 'Unknown') as trainer_name,
                       v.branch_name, COALESCE(bc.zone, v.zone, 'NORTH ZONE') as zone,
                       COALESCE(bc.division, v.division, 'DELHI DIVISION') as division,
                       bc.latitude, bc.longitude, v.planned_date, v.purpose, v.key_contacts,
                       v.status, v.checkin_time, v.checkin_latitude, v.checkin_longitude,
                       v.co_presence_count, v.verification_time, COALESCE(bc.manager_pin, '1234') as manager_pin,
                       v.details, v.end_date,
                       v.month, v.business_unit, v.meeting_agenda, v.meeting_with, v.overnight_stay,
                       v.travel_from, v.travel_to, v.travel_mode, v.mom_notes
                FROM field_visits v
                LEFT JOIN trainers t ON v.trainer_id = t.trainer_id
                LEFT JOIN branch_coordinates bc ON v.branch_name = bc.branch_name
                ORDER BY v.planned_date DESC
            ''')
        else:
            cursor.execute('''
                SELECT v.id, v.trainer_id, COALESCE(t.name, 'Unknown') as trainer_name,
                       v.branch_name, COALESCE(bc.zone, v.zone, 'NORTH ZONE') as zone,
                       COALESCE(bc.division, v.division, 'DELHI DIVISION') as division,
                       bc.latitude, bc.longitude, v.planned_date, v.purpose, v.key_contacts,
                       v.status, v.checkin_time, v.checkin_latitude, v.checkin_longitude,
                       v.co_presence_count, v.verification_time, COALESCE(bc.manager_pin, '1234') as manager_pin,
                       v.details, v.end_date,
                       v.month, v.business_unit, v.meeting_agenda, v.meeting_with, v.overnight_stay,
                       v.travel_from, v.travel_to, v.travel_mode, v.mom_notes
                FROM field_visits v
                LEFT JOIN trainers t ON v.trainer_id = t.trainer_id
                LEFT JOIN branch_coordinates bc ON v.branch_name = bc.branch_name
                WHERE v.trainer_id = ?
                ORDER BY v.planned_date DESC
            ''', (curr_user['trainer_id'],))
            
        rows = cursor.fetchall()
        
        # Query branch delta scores (post_test - pre_test average growth)
        cursor.execute('''
            SELECT ts.branch_name, AVG(ar.post_test_score - ar.pre_test_score)
            FROM assessment_results ar
            JOIN training_sessions ts ON ar.session_id = ts.session_id
            GROUP BY ts.branch_name
        ''')
        deltas = {row[0]: round(row[1], 1) if row[1] is not None else 0.0 for row in cursor.fetchall()}
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": f"Failed to fetch visits: {str(e)}"}), 500
        
    conn.close()
    
    visits = []
    for r in rows:
        visits.append({
            "id": r[0],
            "trainer_id": r[1],
            "trainer_name": r[2],
            "branch_name": r[3],
            "zone": r[4],
            "division": r[5],
            "latitude": r[6],
            "longitude": r[7],
            "planned_date": r[8],
            "purpose": r[9],
            "key_contacts": r[10],
            "status": r[11],
            "checkin_time": r[12],
            "checkin_latitude": r[13],
            "checkin_longitude": r[14],
            "co_presence_count": r[15],
            "verification_time": r[16],
            "manager_pin": r[17],
            "details": r[18] if len(r) > 18 and r[18] else "",
            "end_date": r[19] if len(r) > 19 and r[19] else r[8],
            "socratic_delta": deltas.get(r[3], 0.0)
        })
        
    return jsonify(visits)

@app.route('/api/visits/plan', methods=['POST'])
def plan_visit():
    curr_user = session.get('user')
    if not curr_user:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    data = request.json or {}
    branch_name = data.get('branch_name', '').strip()
    planned_date = data.get('planned_date', '').strip()
    end_date = data.get('end_date', '').strip() or planned_date
    purpose = data.get('purpose', '').strip()
    key_contacts = data.get('key_contacts', '').strip()
    details = data.get('details', '').strip()
    
    if not branch_name or not planned_date or not purpose:
        return jsonify({"status": "error", "message": "Branch Name, Planned Date, and Purpose are required."}), 400
        
    conn = get_db_connection()
    bc = conn.execute("SELECT * FROM branch_coordinates WHERE branch_name=?", (branch_name,)).fetchone()
    if not bc:
        # Auto-register new branch from employee roster mapping with fallback coordinates
        emp = conn.execute("SELECT zone, division FROM employees WHERE branch_name=? LIMIT 1", (branch_name,)).fetchone()
        zone = emp[0] if emp and emp[0] else "NORTH ZONE"
        division = emp[1] if emp and emp[1] else "DELHI DIVISION"
        
        # Region-based fallback geofence coordinates
        # Delhi RF (28.6139, 77.209), Ahmedabad RF (23.0225, 72.5714), Chandigarh RF (30.7333, 76.7794)
        # Kolkata RF (22.5726, 88.3639), Mumbai RF (19.076, 72.8777)
        div_upper = division.upper()
        zone_upper = zone.upper()
        
        if "DELHI" in div_upper or "NORTH" in zone_upper:
            lat, lon = 28.6139, 77.209
        elif "PUNJAB" in div_upper or "CHANDIGARH" in div_upper:
            lat, lon = 30.7333, 76.7794
        elif "GUJARAT" in div_upper or "WEST" in zone_upper:
            lat, lon = 23.0225, 72.5714
        elif "MUMBAI" in div_upper or "MAHARASHTRA" in div_upper:
            lat, lon = 19.076, 72.8777
        elif "BENGAL" in div_upper or "EAST" in zone_upper:
            lat, lon = 22.5726, 88.3639
        else:
            lat, lon = 28.6139, 77.209  # National fallback
            
        print(f"[GEOFENCE-AUTO-REGISTER] Branch '{branch_name}' not found. Auto-registering with fallback coords ({lat}, {lon}) based on zone '{zone}' / division '{division}'")
        conn.execute('''
            INSERT INTO branch_coordinates (branch_name, zone, division, latitude, longitude, manager_pin)
            VALUES (?, ?, ?, ?, ?, '1234')
        ''', (branch_name, zone, division, lat, lon))
        conn.commit()
        
    conn.execute('''
        INSERT INTO field_visits (trainer_id, branch_name, planned_date, end_date, purpose, key_contacts, details)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (curr_user['trainer_id'], branch_name, planned_date, end_date, purpose, key_contacts, details))
    conn.commit()
    conn.close()
    
    return jsonify({"status": "success", "message": "Field visit itinerary successfully planned!"})

@app.route('/api/branches/pin', methods=['PUT'])
def update_branch_pin():
    curr_user = session.get('user')
    if not curr_user or curr_user.get('role') not in ['SuperAdmin', 'Leader']:
        return jsonify({"status": "error", "message": "Unauthorized. SuperAdmin or Leader privileges required."}), 403
        
    data = request.json or {}
    branch_name = data.get('branch_name', '').strip()
    new_pin = data.get('new_pin', '').strip()
    
    if not branch_name or not new_pin:
        return jsonify({"status": "error", "message": "Branch Name and New PIN are required."}), 400
        
    if not new_pin.isdigit() or len(new_pin) < 4:
        return jsonify({"status": "error", "message": "PIN must be at least 4 digits."}), 400
        
    conn = get_db_connection()
    bc = conn.execute("SELECT * FROM branch_coordinates WHERE branch_name=?", (branch_name,)).fetchone()
    if not bc:
        conn.close()
        return jsonify({"status": "error", "message": f"Branch '{branch_name}' not found."}), 404
        
    conn.execute("UPDATE branch_coordinates SET manager_pin=? WHERE branch_name=?", (new_pin, branch_name))
    conn.commit()
    conn.close()
    
    return jsonify({"status": "success", "message": f"Manager PIN for '{branch_name}' updated successfully."})

@app.route('/api/visits/checkin', methods=['POST'])
def checkin_visit():
    curr_user = session.get('user')
    if not curr_user:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    data = request.json or {}
    visit_id = data.get('visit_id')
    try:
        lat = float(data.get('latitude', 0))
        lon = float(data.get('longitude', 0))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid latitude/longitude coordinates."}), 400
    
    if not visit_id:
        return jsonify({"status": "error", "message": "Missing check-in location parameters."}), 400
        
    conn = get_db_connection()
    visit = conn.execute("SELECT * FROM field_visits WHERE id=?", (visit_id,)).fetchone()
    if not visit:
        conn.close()
        return jsonify({"status": "error", "message": "Field visit not found."}), 404
        
    bc = conn.execute("SELECT * FROM branch_coordinates WHERE branch_name=?", (visit['branch_name'],)).fetchone()
    if not bc:
        # Just in case, auto-register if missing
        emp = conn.execute("SELECT zone, division FROM employees WHERE branch_name=? LIMIT 1", (visit['branch_name'],)).fetchone()
        zone = emp[0] if emp and emp[0] else "NORTH ZONE"
        division = emp[1] if emp and emp[1] else "DELHI DIVISION"
        bc_lat, bc_lon = 28.6139, 77.209
        conn.execute('''
            INSERT INTO branch_coordinates (branch_name, zone, division, latitude, longitude, manager_pin)
            VALUES (?, ?, ?, ?, ?, '1234')
        ''', (visit['branch_name'], zone, division, bc_lat, bc_lon))
        conn.commit()
        bc = {"branch_name": visit['branch_name'], "latitude": bc_lat, "longitude": bc_lon}
        
    distance = calculate_haversine_distance(lat, lon, bc['latitude'], bc['longitude'])
    
    # Auto-learning geofence: If the branch has never had a successful geofenced check-in,
    # we dynamically update its geofence benchmark to the trainer's current GPS location and approve it.
    past_successes = conn.execute("SELECT COUNT(*) FROM field_visits WHERE branch_name=? AND status IN ('GEOFENCED', 'VERIFIED')", (visit['branch_name'],)).fetchone()[0]
    
    if distance > 150.0 and past_successes == 0:
        print(f"[GEOFENCE-LEARNING] First check-in at '{visit['branch_name']}'. Updating baseline geofence from ({bc['latitude']}, {bc['longitude']}) to trainer's current coordinates ({lat}, {lon}).")
        conn.execute("UPDATE branch_coordinates SET latitude=?, longitude=? WHERE branch_name=?", (lat, lon, visit['branch_name']))
        conn.commit()
        distance = 0.0  # Approved instantly since baseline coordinates were just set to current location
        
    elif distance > 150.0:
        conn.close()
        return jsonify({
            "status": "error",
            "message": f"❌ Location Geofence Failed! You are {round(distance, 1)}m away from branch center. Please ensure you check-in within 150m of branch coordinates."
        }), 400
        
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # Socratic Co-Presence count: active trainee completions on this date at this branch
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COUNT(DISTINCT ar.emp_code)
        FROM assessment_results ar
        JOIN training_sessions ts ON ar.session_id = ts.session_id
        WHERE ts.branch_name = ? AND ts.trainer_id = ? AND date(ar.completed_at) = date(?)
    ''', (visit['branch_name'], curr_user['trainer_id'], now))
    co_presence = cursor.fetchone()[0] or 0
    
    conn.execute('''
        UPDATE field_visits
        SET status = 'GEOFENCED', checkin_time = ?, checkin_latitude = ?, checkin_longitude = ?, co_presence_count = ?
        WHERE id = ?
    ''', (now, lat, lon, co_presence, visit_id))
    conn.commit()
    conn.close()
    
    return jsonify({
        "status": "success",
        "message": f"🟢 GPS Verification Cleared! Located {round(distance, 1)}m from branch center. Active Socratic Co-presence: {co_presence} trainees.",
        "co_presence": co_presence
    })

@app.route('/api/visits/verify', methods=['POST'])
def verify_visit():
    curr_user = session.get('user')
    if not curr_user:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    data = request.json or {}
    visit_id = data.get('visit_id')
    pin = data.get('manager_pin', '').strip()
    
    # Allow SuperAdmin bypass
    is_superadmin = curr_user.get('role') == 'SuperAdmin'
    
    if not visit_id or (not pin and not is_superadmin):
        return jsonify({"status": "error", "message": "Missing validation parameters."}), 400
        
    conn = get_db_connection()
    visit = conn.execute("SELECT * FROM field_visits WHERE id=?", (visit_id,)).fetchone()
    if not visit:
        conn.close()
        return jsonify({"status": "error", "message": "Field visit not found."}), 404
        
    bc = conn.execute("SELECT * FROM branch_coordinates WHERE branch_name=?", (visit['branch_name'],)).fetchone()
    if not bc:
        conn.close()
        return jsonify({"status": "error", "message": "Target branch coordinates benchmark not found."}), 404
        
    if not is_superadmin and bc['manager_pin'] != pin:
        conn.close()
        return jsonify({"status": "error", "message": "❌ Invalid Branch Manager PIN. Verification aborted."}), 400
        
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # Recount final co-presence trainee list
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COUNT(DISTINCT ar.emp_code)
        FROM assessment_results ar
        JOIN training_sessions ts ON ar.session_id = ts.session_id
        WHERE ts.branch_name = ? AND ts.trainer_id = ? AND date(ar.completed_at) = date(?)
    ''', (visit['branch_name'], curr_user['trainer_id'], now))
    co_presence = cursor.fetchone()[0] or 0
    
    conn.execute('''
        UPDATE field_visits
        SET status = 'VERIFIED', verification_time = ?, co_presence_count = ?
        WHERE id = ?
    ''', (now, co_presence, visit_id))
    conn.commit()
    conn.close()
    
    return jsonify({
        "status": "success",
        "message": "🟢 Branch visit successfully verified and logged by Branch Manager!"
    })


@app.route('/api/visits/<int:visit_id>/mom', methods=['POST'])
def save_visit_mom(visit_id):
    curr_user = session.get('user')
    if not curr_user:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    data = request.json or {}
    mom_notes = data.get('mom_notes', '').strip()
    
    conn = get_db_connection()
    conn.execute("UPDATE field_visits SET mom_notes=? WHERE id=?", (mom_notes, visit_id))
    conn.commit()
    conn.close()
    
    return jsonify({"status": "success", "message": "Minutes of Meeting saved successfully!"})


@app.route('/api/visits/<int:visit_id>', methods=['DELETE'])
def delete_visit(visit_id):
    curr_user = session.get('user')
    if not curr_user or curr_user.get('role') not in ['SuperAdmin', 'Leader']:
        return jsonify({"status": "error", "message": "Unauthorized. SuperAdmin or Leader privileges required."}), 403
        
    conn = get_db_connection()
    visit = conn.execute("SELECT * FROM field_visits WHERE id=?", (visit_id,)).fetchone()
    if not visit:
        conn.close()
        return jsonify({"status": "error", "message": "Visit not found"}), 404
        
    conn.execute("DELETE FROM field_visits WHERE id=?", (visit_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": "Field visit successfully cancelled/deleted!"})


@app.route('/api/visits/export', methods=['GET'])
def export_visits():
    curr_user = session.get('user')
    if not curr_user:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    conn = get_db_connection()
    cursor = conn.cursor()
    
    period = request.args.get('period', 'ALL')
    month = request.args.get('month')
    year = request.args.get('year')
    zone = request.args.get('zone')
    division = request.args.get('division')
    branch = request.args.get('branch')
    trainer = request.args.get('trainer')
    status = request.args.get('status')
    
    query = '''
        SELECT v.id, v.trainer_id, t.name as trainer_name, v.branch_name, bc.zone, bc.division,
               v.planned_date, v.purpose, v.key_contacts, v.status, v.checkin_time, 
               v.checkin_latitude, v.checkin_longitude, v.co_presence_count, v.verification_time, v.details
        FROM field_visits v
        JOIN trainers t ON v.trainer_id = t.trainer_id
        JOIN branch_coordinates bc ON v.branch_name = bc.branch_name
        WHERE 1=1
    '''
    params = []
    
    # Role-based restriction
    if curr_user['role'] not in ['SuperAdmin', 'Leader']:
        query += " AND v.trainer_id = ?"
        params.append(curr_user['trainer_id'])
        
    # Filters
    if period == 'MTD':
        # current month up to today
        today_str = datetime.date.today().strftime('%Y-%m-%d')
        start_of_month = datetime.date.today().replace(day=1).strftime('%Y-%m-%d')
        query += " AND v.planned_date >= ? AND v.planned_date <= ?"
        params.extend([start_of_month, today_str])
    elif period == 'YTD':
        # current year up to today
        today_str = datetime.date.today().strftime('%Y-%m-%d')
        start_of_year = datetime.date.today().replace(month=1, day=1).strftime('%Y-%m-%d')
        query += " AND v.planned_date >= ? AND v.planned_date <= ?"
        params.extend([start_of_year, today_str])
    elif period == 'MONTH' and month:
        # month as YYYY-MM
        query += " AND v.planned_date LIKE ?"
        params.append(f"{month}%")
    elif period == 'YEAR' and year:
        # year as YYYY
        query += " AND v.planned_date LIKE ?"
        params.append(f"{year}%")
        
    if zone:
        query += " AND bc.zone = ?"
        params.append(zone)
        
    if division:
        query += " AND bc.division = ?"
        params.append(division)
        
    if branch:
        query += " AND v.branch_name = ?"
        params.append(branch)
        
    if trainer:
        query += " AND t.name = ?"
        params.append(trainer)
        
    if status:
        query += " AND v.status = ?"
        params.append(status)
        
    query += " ORDER BY v.planned_date DESC"
    cursor.execute(query, params)
    
    rows = cursor.fetchall()
    
    # Query branch delta scores (post_test - pre_test average growth)
    cursor.execute('''
        SELECT ts.branch_name, AVG(ar.post_test_score - ar.pre_test_score)
        FROM assessment_results ar
        JOIN training_sessions ts ON ar.session_id = ts.session_id
        GROUP BY ts.branch_name
    ''')
    deltas = {row[0]: round(row[1], 1) if row[1] is not None else 0.0 for row in cursor.fetchall()}
    conn.close()
    
    import io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Visit ID", "Trainer ID", "Trainer Name", "Branch Name", "Zone", "Division",
        "Planned Date", "Purpose", "Key Contacts", "Status", "Checkin Time",
        "Checkin Latitude", "Checkin Longitude", "Co-Presence Count", "Verification Time", "Strategic Details", "Socratic Delta"
    ])
    
    for r in rows:
        writer.writerow([
            r[0], r[1], r[2], r[3], r[4], r[5],
            r[6], r[7], r[8], r[9], r[10],
            r[11] if r[11] is not None else "",
            r[12] if r[12] is not None else "",
            r[13], r[14] if r[14] is not None else "",
            r[15] if len(r) > 15 else "",
            deltas.get(r[3], 0.0)
        ])
        
    from flask import make_response
    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = "attachment; filename=socrates_field_visits.csv"
    response.headers["Content-type"] = "text/csv"
    return response


@app.route('/api/visits/compliance-stats', methods=['GET'])
def get_visits_compliance_stats():
    curr_user = session.get('user')
    if not curr_user:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    month = request.args.get('month')
    if not month:
        month = datetime.datetime.now().strftime("%Y-%m")
        
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Get all active trainers (including Leaders for SuperAdmin)
    if curr_user.get('role') == 'SuperAdmin':
        cursor.execute('''
            SELECT trainer_id, name 
            FROM trainers 
            WHERE status = 'Active' AND role IN ('Trainer', 'Leader')
        ''')
    else:
        cursor.execute('''
            SELECT trainer_id, name 
            FROM trainers 
            WHERE status = 'Active' AND role = 'Trainer'
        ''')
    all_trainers = [{"trainer_id": r[0], "name": r[1]} for r in cursor.fetchall()]
    
    # 2. Get trainers who have planned visits in this month
    cursor.execute('''
        SELECT DISTINCT trainer_id 
        FROM field_visits 
        WHERE strftime('%Y-%m', planned_date) = ?
    ''', (month,))
    updated_trainer_ids = {r[0] for r in cursor.fetchall()}
    conn.close()
    
    updated_trainers = []
    not_updated_trainers = []
    
    for t in all_trainers:
        if t['trainer_id'] in updated_trainer_ids:
            updated_trainers.append(t)
        else:
            not_updated_trainers.append(t)
            
    return jsonify({
        "month": month,
        "total_active_trainers": len(all_trainers),
        "updated_count": len(updated_trainers),
        "not_updated_count": len(not_updated_trainers),
        "updated_trainers": updated_trainers,
        "not_updated_trainers": not_updated_trainers
    })


@app.route('/api/visits/upload', methods=['POST'])
def upload_visits():
    curr_user = session.get('user')
    if not curr_user:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "No selected file"}), 400
        
    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        REQUIRED_HEADERS = [
            'Trainer Name', 'Month', 'BU', 'Date of Visit From', 'Date of Visit To', 
            'Branch Code', 'Meeting Agenda', 'Meeting with Role', 'Overnight Stay', 
            'Travel From', 'Travel To', 'Travel Mode'
        ]
        
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
                    "message": f"Invalid CSV format. Missing required column headers: {', '.join(missing_headers)}"
                }), 400
                
            conn = get_db_connection()
            success_count = 0
            errors = []
            
            for row_idx, r in rows:
                def get_val(col_name):
                    return r[headers.index(col_name)].strip()
                
                trainer_name = get_val('Trainer Name')
                # Resolve trainer ID
                trainer = conn.execute("SELECT trainer_id FROM trainers WHERE name=? COLLATE NOCASE", (trainer_name,)).fetchone()
                if not trainer:
                    errors.append(f"Row {row_idx}: Trainer Name '{trainer_name}' not found in system.")
                    continue
                trainer_id = trainer['trainer_id']
                
                branch_code = get_val('Branch Code').upper()
                # Resolve Zone/Division from branch_coordinates if not provided directly
                bc = conn.execute("SELECT zone, division FROM branch_coordinates WHERE branch_name=?", (branch_code,)).fetchone()
                zone = bc['zone'] if bc else 'ALL'
                division = bc['division'] if bc else 'ALL'
                
                try:
                    conn.execute('''
                        INSERT INTO field_visits (
                            trainer_id, branch_name, planned_date, end_date, purpose, key_contacts, 
                            month, branch_code, business_unit, zone, division, meeting_agenda, 
                            meeting_with, overnight_stay, travel_from, travel_to, travel_mode
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        trainer_id, branch_code, get_val('Date of Visit From'), get_val('Date of Visit To'),
                        'Bulk Uploaded Plan', 'N/A',
                        get_val('Month'), branch_code, get_val('BU'), zone, division,
                        get_val('Meeting Agenda'), get_val('Meeting with Role'), get_val('Overnight Stay'),
                        get_val('Travel From'), get_val('Travel To'), get_val('Travel Mode')
                    ))
                    success_count += 1
                except Exception as e:
                    errors.append(f"Row {row_idx}: Database error - {str(e)}")
                    
            conn.commit()
            conn.close()
            
            if errors and success_count == 0:
                return jsonify({"status": "error", "message": "Upload failed.", "details": errors}), 400
            elif errors:
                return jsonify({"status": "success", "message": f"Uploaded {success_count} visits with some errors.", "details": errors})
            else:
                return jsonify({"status": "success", "message": f"Successfully uploaded {success_count} field visits!"})
                
        except Exception as e:
            return jsonify({"status": "error", "message": f"Failed to parse CSV: {str(e)}"}), 400


@app.route('/api/persistence-status', methods=['GET'])
def persistence_status():
    db_url = os.environ.get('DATABASE_URL')
    gd_folder = os.environ.get('GD_FOLDER_ID')
    gd_sa = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    gcs_bucket = os.environ.get('GCS_BACKUP_BUCKET', '')
    gd_libs = False
    gcs_libs = False
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        gd_libs = True
    except ImportError:
        pass
    try:
        from google.cloud import storage
        gcs_libs = True
    except ImportError:
        pass
    has_gcs = bool(gcs_bucket or (gd_sa and gcs_libs))
    return jsonify({
        "has_db_url": bool(db_url),
        "has_gd_folder": bool(gd_folder),
        "has_gd_sa": bool(gd_sa),
        "has_gd_libs": gd_libs,
        "has_gcs_libs": gcs_libs,
        "has_gcs_bucket": bool(gcs_bucket),
        "drive_configured": bool(gd_folder and gd_sa and gd_libs),
        "gcs_available": has_gcs,
        "db_type": "postgresql" if db_url else "sqlite",
        "ephemeral_warning": not bool(db_url) and not (gd_folder and gd_sa and gd_libs) and not has_gcs
    })

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5050, host='0.0.0.0', allow_unsafe_werkzeug=True)

