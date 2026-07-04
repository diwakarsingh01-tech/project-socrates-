#!/usr/bin/env python3
"""Seed demo data into Socrates SQLite database"""

import os, sqlite3, datetime
os.environ["DATABASE_URL"] = ""

DB_FILE = "socrates.db"

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def seed():
    conn = get_db()
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
        c.execute("INSERT OR REPLACE INTO employees (emp_code,emp_name,branch_name,zone,division,business_unit,role,product_name,status,change_detail) VALUES (?,?,?,?,?,?,?,?,?,?)", (*e,"ACTIVE","SEED DATA"))

    print("Seeding trainers...")
    from werkzeug.security import generate_password_hash
    trainers = [
        ("TRAINER1","Rajesh Khanna","NORTH ZONE","Trainer"),
        ("TRAINER2","Sunita Sharma","WEST ZONE","Trainer"),
        ("LEADER1","Amitabh Joshi","All","Leader"),
    ]
    for tid,name,zone,role in trainers:
        pwh = generate_password_hash("password123")
        c.execute("INSERT OR REPLACE INTO trainers (trainer_id,name,zone,password,status,role,plain_password,zones,divisions,branches,business_units) VALUES (?,?,?,?,'Active',?,?,'ALL','ALL','ALL','ALL')", (tid,name,zone,pwh,role,"password123"))

    print("Seeding module...")
    c.execute("INSERT OR REPLACE INTO modules (id,title,questions_count,created_at,status,created_by,difficulty) VALUES (1,'Product Knowledge - Two Wheeler Loans',3,?,'Ready','ADMIN','Medium')", (now,))
    c.execute("INSERT OR REPLACE INTO modules (id,title,questions_count,created_at,status,created_by,difficulty) VALUES (2,'Gold Loan Policy Refresher',3,?,'Ready','TRAINER1','Hard')", (now,))

    print("Seeding questions...")
    qs = [
        (1,1,"What is the minimum down payment for a two-wheeler loan?","10%","15%","20%","25%",0,1),
        (2,1,"What is the maximum tenure for a two-wheeler loan?","3 years","5 years","7 years","10 years",1,1),
        (3,1,"Which document is NOT required for a two-wheeler loan?","Aadhaar Card","PAN Card","Passport","Voter ID",2,1),
        (4,2,"What is the current gold loan interest rate?","7.5%","8.5%","9.5%","10.5%",1,1),
        (5,2,"Maximum LTV ratio for gold loan?","60%","70%","75%","80%",2,1),
        (6,2,"What is the minimum gold purity accepted?","18 carat","20 carat","22 carat","24 carat",2,1),
    ]
    for q in qs:
        c.execute("INSERT OR REPLACE INTO questions (id,module_id,question_text,option_a,option_b,option_c,option_d,correct_index,approved) VALUES (?,?,?,?,?,?,?,?,?)", q)

    print("Seeding sessions and assessments...")
    c.execute("INSERT OR REPLACE INTO training_sessions (session_id,date,trainer_id,module_id,branch_name) VALUES ('SESS-001',?,'TRAINER1',1,'DELHI RF')", (now,))
    for emp,pre,post in [("SF-1001",40.0,85.0),("SF-1006",35.0,78.0)]:
        c.execute("INSERT OR REPLACE INTO assessment_results (emp_code,module_id,assignment_day,pre_test_score,post_test_score,session_id,completed_at) VALUES (?,?,?,?,?,?,?)", (emp,1,"ZERO DAY",pre,post,"SESS-001",now))

    print("Seeding field visits...")
    c.execute("INSERT OR REPLACE INTO field_visits (trainer_id,branch_name,planned_date,purpose,status) VALUES ('TRAINER1','DELHI RF',?,'Training & Business Discussion','PLANNED')", (now,))
    c.execute("INSERT OR REPLACE INTO field_visits (trainer_id,branch_name,planned_date,purpose,status,checkin_time,verification_time,co_presence_count) VALUES ('TRAINER2','AHMEDABAD RF',?,'Routine Check','VERIFIED',?,?,3)", (now,now + " 10:00",now + " 11:30"))

    conn.commit()
    conn.close()
    print("DONE! Seeded 8 employees, 4 trainers (inc. ADMIN), 2 modules, 6 questions, 1 session, 2 assessments, 2 visits.")

if __name__ == "__main__":
    seed()
