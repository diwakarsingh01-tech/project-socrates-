#!/usr/bin/env python3
"""Seed demo data into Socrates SQLite database"""

import os
# Prevent PostgreSQL connection attempt during seeding
os.environ["DATABASE_URL"] = ""

import sqlite3, datetime

DB_FILE = "socrates.db"

def init_tables():
    """Create tables by importing app (which runs init_db on import)"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("app", "app.py")
    mod = importlib.util.module_from_spec(spec)
    # But don't fully run it - just call the db init part
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    tables = [
        """CREATE TABLE IF NOT EXISTS employees (
            emp_code TEXT PRIMARY KEY, emp_name TEXT, branch_name TEXT,
            zone TEXT, division TEXT, business_unit TEXT, role TEXT,
            product_name TEXT, status TEXT DEFAULT 'ACTIVE', change_detail TEXT DEFAULT 'ADDED MANUALLY'
        )""",
        """CREATE TABLE IF NOT EXISTS trainers (
            trainer_id TEXT PRIMARY KEY, name TEXT, zone TEXT, password TEXT,
            status TEXT DEFAULT 'Active', role TEXT DEFAULT 'Trainer', last_login TEXT,
            zones TEXT DEFAULT 'ALL', divisions TEXT DEFAULT 'ALL', branches TEXT DEFAULT 'ALL',
            business_units TEXT DEFAULT 'ALL', plain_password TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS modules (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, questions_count INTEGER,
            created_at TEXT, status TEXT DEFAULT 'Pending Audit', created_by TEXT DEFAULT 'ADMIN',
            difficulty TEXT DEFAULT 'Medium'
        )""",
        """CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, module_id INTEGER,
            question_text TEXT, option_a TEXT, option_b TEXT, option_c TEXT, option_d TEXT,
            correct_index INTEGER, approved INTEGER DEFAULT 0, translations TEXT,
            FOREIGN KEY(module_id) REFERENCES modules(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS training_sessions (
            session_id TEXT PRIMARY KEY, date TEXT, trainer_id TEXT,
            module_id INTEGER, branch_name TEXT,
            FOREIGN KEY(trainer_id) REFERENCES trainers(trainer_id)
        )""",
        """CREATE TABLE IF NOT EXISTS assessment_results (
            emp_code TEXT, module_id INTEGER, assignment_day TEXT,
            pre_test_score REAL, post_test_score REAL,
            correct_count INTEGER DEFAULT 0, wrong_count INTEGER DEFAULT 0,
            unattempted_count INTEGER DEFAULT 0, total_questions INTEGER DEFAULT 0,
            completed_at TEXT, session_id TEXT,
            PRIMARY KEY (emp_code, module_id, assignment_day)
        )""",
        """CREATE TABLE IF NOT EXISTS trainee_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT, emp_code TEXT, session_id TEXT,
            module_id INTEGER, rating INTEGER, understanding TEXT,
            manpower_saved TEXT, comments TEXT, submitted_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS branch_coordinates (
            branch_name TEXT PRIMARY KEY, zone TEXT NOT NULL, division TEXT NOT NULL,
            latitude REAL NOT NULL, longitude REAL NOT NULL, manager_pin TEXT NOT NULL DEFAULT '1234'
        )""",
        """CREATE TABLE IF NOT EXISTS field_visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trainer_id TEXT NOT NULL,
            branch_name TEXT NOT NULL, planned_date TEXT NOT NULL, end_date TEXT,
            purpose TEXT NOT NULL, key_contacts TEXT, status TEXT DEFAULT 'PLANNED',
            checkin_time TEXT, checkin_latitude REAL, checkin_longitude REAL,
            co_presence_count INTEGER DEFAULT 0, verification_time TEXT, details TEXT,
            month TEXT, branch_code TEXT, business_unit TEXT, zone TEXT, division TEXT,
            meeting_agenda TEXT, meeting_with TEXT, overnight_stay TEXT,
            travel_from TEXT, travel_to TEXT, travel_mode TEXT, mom_notes TEXT
        )""",
    ]
    for t in tables:
        c.execute(t)
    
    # Seed default branches
    defaults = [
        ("DELHI RF","NORTH ZONE","DELHI DIVISION",28.6139,77.209,"1234"),
        ("AHMEDABAD RF","WEST ZONE","GUJARAT DIVISION",23.0225,72.5714,"1234"),
        ("CHANDIGARH RF","NORTH ZONE","PUNJAB DIVISION",30.7333,76.7794,"1234"),
        ("KOLKATA RF","EAST ZONE","BENGAL DIVISION",22.5726,88.3639,"1234"),
        ("MUMBAI RF","WEST ZONE","MUMBAI DIVISION",19.076,72.8777,"1234"),
    ]
    for d in defaults:
        c.execute("INSERT OR IGNORE INTO branch_coordinates (branch_name,zone,division,latitude,longitude,manager_pin) VALUES (?,?,?,?,?,?)", d)
    
    # Seed ADMIN
    from werkzeug.security import generate_password_hash
    hashed = generate_password_hash('admin123')
    c.execute("INSERT OR IGNORE INTO trainers (trainer_id,name,zone,password,role,plain_password) VALUES ('ADMIN','Super Admin','All',?,'SuperAdmin','admin123')", (hashed,))
    
    conn.commit()
    conn.close()
    print("Tables initialized, branches seeded, ADMIN created.")

def seed():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d")

    print("Seeding employees...")
    emps = [
        ("SF-1001","RAHUL SINGH","DELHI RF","NORTH ZONE","DELHI DIVISION","TWO-WHEELER","PL EXE","SPLENDOR V2"),
        ("SF-1002","NEHA SHARMA","AHMEDABAD RF","WEST ZONE","GUJARAT DIVISION","TWO-WHEELER","PL EXE","ACTIVA 6G"),
        ("SF-1003","AMIT PATEL","CHANDIGARH RF","NORTH ZONE","PUNJAB DIVISION","TWO-WHEELER","PL EXE","ACTIVA 6G"),
        ("SF-1004","PRIYA DAS","KOLKATA RF","EAST ZONE","BENGAL DIVISION","RETAIL","CSE","N/A"),
        ("SF-1005","VIKRAM VERMA","MUMBAI RF","WEST ZONE","MUMBAI DIVISION","GOLD LOAN","BH / BPH","N/A"),
        ("SF-1006","ANJALI GUPTA","DELHI RF","NORTH ZONE","DELHI DIVISION","PERSONAL LOAN","SPH / SBH","N/A"),
        ("SF-1007","ROHIT KUMAR","AHMEDABAD RF","WEST ZONE","GUJARAT DIVISION","COMMERCIAL VEHICLE","DTL","N/A"),
        ("SF-1008","SONIA JAIN","KOLKATA RF","EAST ZONE","BENGAL DIVISION","TWO-WHEELER","PL EXE","ACCESS 125"),
    ]
    for e in emps:
        c.execute("INSERT OR REPLACE INTO employees VALUES (?,?,?,?,?,?,?,?,'ACTIVE','SEED DATA')", e)

    print("Seeding trainers...")
    from werkzeug.security import generate_password_hash
    for tid,name,zone,role in [("TRAINER1","Rajesh Khanna","NORTH ZONE","Trainer"),("TRAINER2","Sunita Sharma","WEST ZONE","Trainer"),("LEADER1","Amitabh Joshi","All","Leader")]:
        pwh = generate_password_hash("password123")
        c.execute("INSERT OR REPLACE INTO trainers (trainer_id,name,zone,password,status,role,plain_password,zones,divisions,branches,business_units) VALUES (?,?,?,?,'Active',?,?,'ALL','ALL','ALL','ALL')", (tid,name,zone,pwh,role,"password123"))

    print("Seeding modules...")
    c.execute("INSERT OR REPLACE INTO modules (id,title,questions_count,created_at,status,created_by,difficulty) VALUES (1,'Product Knowledge - Two Wheeler Loans',3,?,'Ready','ADMIN','Medium')", (now,))
    c.execute("INSERT OR REPLACE INTO modules (id,title,questions_count,created_at,status,created_by,difficulty) VALUES (2,'Gold Loan Policy Refresher',3,?,'Ready','TRAINER1','Hard')", (now,))

    print("Seeding questions...")
    for q in [
        (1,1,"What is the minimum down payment for a two-wheeler loan?","10%","15%","20%","25%",0,1),
        (2,1,"What is the maximum tenure for a two-wheeler loan?","3 years","5 years","7 years","10 years",1,1),
        (3,1,"Which document is NOT required for a two-wheeler loan?","Aadhaar Card","PAN Card","Passport","Voter ID",2,1),
        (4,2,"What is the current gold loan interest rate?","7.5%","8.5%","9.5%","10.5%",1,1),
        (5,2,"Maximum LTV ratio for gold loan?","60%","70%","75%","80%",2,1),
        (6,2,"What is the minimum gold purity accepted?","18 carat","20 carat","22 carat","24 carat",2,1),
    ]:
        c.execute("INSERT OR REPLACE INTO questions (id,module_id,question_text,option_a,option_b,option_c,option_d,correct_index,approved) VALUES (?,?,?,?,?,?,?,?,?)", q)

    print("Seeding sessions and assessments...")
    c.execute("INSERT OR REPLACE INTO training_sessions (session_id,date,trainer_id,module_id,branch_name) VALUES ('SESS-001',?,'TRAINER1',1,'DELHI RF')", (now,))
    for emp,pre,post in [("SF-1001",40.0,85.0),("SF-1006",35.0,78.0)]:
        c.execute("INSERT OR REPLACE INTO assessment_results (emp_code,module_id,assignment_day,pre_test_score,post_test_score,session_id,completed_at) VALUES (?,?,?,?,?,?,?)", (emp,1,"ZERO DAY",pre,post,"SESS-001",now))

    print("Seeding field visits...")
    c.execute("INSERT OR REPLACE INTO field_visits (trainer_id,branch_name,planned_date,purpose,status) VALUES ('TRAINER1','DELHI RF',?,'Training & Business Discussion','PLANNED')", (now,))
    c.execute("INSERT OR REPLACE INTO field_visits (trainer_id,branch_name,planned_date,purpose,status,checkin_time,verification_time,co_presence_count) VALUES ('TRAINER2','AHMEDABAD RF',?,'Routine Check','VERIFIED',?,?,3)", (now, f"{now} 10:00", f"{now} 11:30"))

    conn.commit()
    conn.close()
    print("DONE! Seeded:", len(emps), "employees, 3 trainers, 2 modules, 6 questions, 1 session, 2 assessments, 2 visits.")

if __name__ == "__main__":
    init_tables()
    seed()
