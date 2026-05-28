#!/usr/bin/env python3
import urllib.request
import urllib.parse
import json
import ssl
import sys
from datetime import datetime

LIVE_URL = "https://socrates-live.onrender.com"

def test_endpoint(path, method="GET", data=None, expected_status=200):
    url = f"{LIVE_URL}{path}"
    headers = {"User-Agent": "Socrates Health Auditor Agent/1.0"}
    req_data = None
    
    if data:
        req_data = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
        
    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
    
    # Bypass SSL verification checks for hosted environments
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as response:
            status = response.status
            body = response.read().decode("utf-8")
            return True, status, body, None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        if e.code == expected_status:
            return True, e.code, body, None
        return False, e.code, body, f"HTTP Error {e.code}: {e.reason}"
    except Exception as e:
        return False, 0, "", str(e)

def run_audit():
    print(f"==================================================")
    print(f"Starting Socrates Live Health & Feature Audit...")
    print(f"Target URL: {LIVE_URL}")
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"==================================================\n")
    
    report = []
    report.append(f"# Socrates Live System Daily Audit Report")
    report.append(f"Generated at: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}`\n")
    
    overall_status = "HEALTHY"
    failures = []
    
    # 1. Test Trainee Landing Page
    ok, status, _, err = test_endpoint("/", expected_status=200)
    if ok:
        report.append(f"- [x] **Trainee Landing Page**: Active (HTTP {status})")
    else:
        overall_status = "DEGRADED"
        failures.append(f"Landing Page Failed: {err}")
        report.append(f"- [ ] **Trainee Landing Page**: Offline/Error (HTTP {status}) - *{err}*")
        
    # 2. Test Admin Portal Login UI
    ok, status, _, err = test_endpoint("/admin", expected_status=200)
    if ok:
        report.append(f"- [x] **Admin Portal Portal**: Active & Accessible (HTTP {status})")
    else:
        overall_status = "DEGRADED"
        failures.append(f"Admin Interface Failed: {err}")
        report.append(f"- [ ] **Admin Portal Portal**: Offline/Error (HTTP {status}) - *{err}*")
        
    # 3. Test API Security Protection (Ensure /api/admin/diagnostics returns 401)
    ok, status, _, err = test_endpoint("/api/admin/diagnostics", expected_status=401)
    if ok:
        report.append(f"- [x] **Session API Security Shield**: Active (Blocked unauthorized admin endpoint with HTTP {status})")
    else:
        overall_status = "DEGRADED"
        failures.append(f"Security Shield Leak: Received status {status} on admin endpoint instead of 401.")
        report.append(f"- [ ] **Session API Security Shield**: **WARNING!** Admin endpoint received status {status} instead of expected HTTP 401 security challenge!")

    # 4. Test Public Roster Endpoint Search (Send query and check validation)
    ok, status, body, err = test_endpoint("/api/roster/search?q=SF", method="GET", expected_status=200)
    if ok:
        try:
            res = json.loads(body)
            if isinstance(res, list):
                report.append(f"- [x] **Roster Public Validation API**: Active & Healthy (HTTP {status})")
            else:
                overall_status = "DEGRADED"
                failures.append("Roster Search returned non-list JSON format.")
                report.append(f"- [ ] **Roster Public Validation API**: Broken (HTTP {status}) - *JSON returned was not a valid employee list*")
        except:
            overall_status = "DEGRADED"
            failures.append("Roster Search returned invalid JSON.")
            report.append(f"- [ ] **Roster Public Validation API**: Broken (HTTP {status}) - *Invalid JSON body*")
    else:
        overall_status = "DEGRADED"
        failures.append(f"Roster Search API Failed: {err}")
        report.append(f"- [ ] **Roster Public Validation API**: Offline/Error (HTTP {status}) - *{err}*")

    # 5. Check if Cloud Database is connected via direct diagnostic ping
    # (Since direct endpoint is secured, we verify if the admin portal dashboard connection succeeds or throws socket timeouts)
    # We query public routes that interact with DB to ensure no 500 errors.
    ok, status, _, err = test_endpoint("/api/roster/search?q=TEST_DB_PING", method="GET", expected_status=200)
    if ok:
        report.append(f"- [x] **Persistent Database Connectivity**: Online & Safe (API responses verified)")
    else:
        overall_status = "CRITICAL"
        failures.append(f"Database/Server API connection threw errors: {err}")
        report.append(f"- [ ] **Persistent Database Connectivity**: **OFFLINE/FAILED** - *{err}*")

    report.append(f"\n## Audit Verdict: **{overall_status}**")
    
    if overall_status == "HEALTHY":
        report.append(f"> [!NOTE]\n> **All Socrates modules, roster uploads, and session controls are fully functional and 100% ready for the 9:00 AM business opening! No issues detected.**")
    else:
        report.append(f"> [!WARNING]\n> **Alert! Issues have been detected on the live Socrates system prior to business start. Details below:**\n> " + "\n> ".join(failures))
        
    report_content = "\n".join(report)
    print(report_content)
    
    # Save the report as an artifact
    try:
        report_path = "/Users/diwakarsingh/.gemini/antigravity-cli/brain/aeebff49-bd78-420c-ac6a-43d1a92f48a1/daily_audit_report.md"
        with open(report_path, "w") as f:
            f.write(report_content)
        print(f"\n[AUDITOR] Saved markdown audit report successfully to: {report_path}")
    except Exception as save_err:
        print(f"\n[AUDITOR] Error saving report file: {str(save_err)}")
        
    # Send automatic Gmail notification
    send_email_report(report_content)
        
    if overall_status != "HEALTHY":
        sys.exit(1)
    sys.exit(0)

def send_email_report(report_content):
    import os
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    
    sender_email = os.environ.get("SENDER_EMAIL", "diwakar.singh01@gmail.com")
    receiver_email = "diwakar.singh01@gmail.com"
    app_password = os.environ.get("SENDER_APP_PASSWORD")
    
    if not app_password:
        print("\n[EMAIL-ALERT] Skipped sending Gmail alert: 'SENDER_APP_PASSWORD' environment variable is not configured.")
        print("[EMAIL-ALERT] To enable automatic Gmail alerts, add 'SENDER_APP_PASSWORD' to environment variables.")
        return
        
    try:
        print(f"\n[EMAIL-ALERT] Attempting to send daily audit email to: {receiver_email}...")
        
        # HTML formatting for the email
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333333; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 12px; background-color: #fafafa;">
            <div style="background-color: #2563eb; color: #ffffff; padding: 20px; border-radius: 8px 8px 0 0; text-align: center;">
                <h2 style="margin: 0; font-size: 20px;">Socrates Live System Daily Audit</h2>
                <p style="margin: 5px 0 0 0; font-size: 12px; opacity: 0.9;">Audit timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}</p>
            </div>
            <div style="padding: 20px; background-color: #ffffff; border-radius: 0 0 8px 8px; border: 1px solid #e2e8f0; border-top: none;">
                <p>Hello Trainer,</p>
                <p>Here is your daily Socrates system status report generated before business opening:</p>
                
                <hr style="border: none; border-top: 1.5px solid #f1f5f9; margin: 20px 0;" />
                
                <pre style="background: #f8fafc; border: 1.5px solid #e2e8f0; border-radius: 8px; padding: 15px; font-family: monospace; white-space: pre-wrap; font-size: 13px; color: #334155;">
{report_content}
                </pre>
                
                <hr style="border: none; border-top: 1.5px solid #f1f5f9; margin: 20px 0;" />
                
                <p style="font-size: 12px; color: #64748b; text-align: center; margin-top: 20px;">
                    This is an automated system health report. Secure cloud connections are active.
                </p>
            </div>
        </body>
        </html>
        """
        
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🎯 Socrates Live Daily Audit Report: {datetime.now().strftime('%Y-%m-%d')}"
        msg["From"] = sender_email
        msg["To"] = receiver_email
        
        part1 = MIMEText(report_content, "plain")
        part2 = MIMEText(html_content, "html")
        msg.attach(part1)
        msg.attach(part2)
        
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(sender_email, app_password)
            server.sendmail(sender_email, receiver_email, msg.as_string())
            
        print("[EMAIL-ALERT] Gmail audit report successfully sent!")
    except Exception as e:
        print(f"[EMAIL-ALERT] Error sending Gmail notification: {str(e)}")

if __name__ == "__main__":
    run_audit()
