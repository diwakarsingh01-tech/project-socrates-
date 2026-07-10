import os
import json
import io
import sqlite3
import datetime
import threading

# Robust import wrapping to prevent crashes if libraries are missing
try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
    from google.oauth2 import service_account
    GOOGLE_LIBS_AVAILABLE = True
except ImportError:
    GOOGLE_LIBS_AVAILABLE = False

SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/drive.appdata']
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
            table_name = query.split()[2].lower().replace('(', '')
            if "employees" not in table_name and "trainers" not in table_name and "training_sessions" not in table_name:
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

    def __iter__(self):
        return iter(self.fetchall())

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
            
            # Handle standard "postgres://" to "postgresql://" url schemes
            if db_url.startswith("postgres://"):
                db_url = db_url.replace("postgres://", "postgresql://", 1)
                
            url = urlparse(db_url)
            username = unquote(url.username) if url.username else None
            password = unquote(url.password) if url.password else None
            database = url.path[1:]
            hostname = url.hostname
            port = url.port or 5432
            
            # Defensive Rewrite: Supabase poolers on port 5432 often time out in hosted environments
            # like Render due to outbound firewall restrictions on direct PostgreSQL ports.
            # We automatically switch to the Transaction Pooler port 6543 which is open.
            if hostname and ".pooler.supabase.com" in hostname.lower() and port == 5432:
                print("[POSTGRES] Automatically rewriting Supabase pooler port from 5432 to 6543 for Render compatibility.")
                port = 6543
            
            # Manual DNS Resolution: Bypasses buggy eventlet green DNS resolution in Gunicorn
            connection_host = hostname
            try:
                # First try using eventlet's unmonkeypatched original socket if eventlet is active
                try:
                    from eventlet.patcher import original
                    orig_socket = original('socket')
                    resolved_ip = orig_socket.gethostbyname(hostname)
                    print(f"[POSTGRES] Eventlet original socket resolved {hostname} to IP: {resolved_ip}")
                    connection_host = resolved_ip
                except Exception:
                    # Fallback to standard socket
                    import socket
                    resolved_ip = socket.gethostbyname(hostname)
                    print(f"[POSTGRES] Standard socket resolved {hostname} to IP: {resolved_ip}")
                    connection_host = resolved_ip
            except Exception as dns_err:
                print(f"[POSTGRES] DNS manual resolution failed: {str(dns_err)}")
                
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
                timeout=10  # Explicit connection timeout to prevent hangs
            )
            return PostgresConnectionWrapper(pg_conn)
        except Exception as e:
            print(f"[POSTGRES] Connection failed, falling back to SQLite: {str(e)}")
            
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def load_dotenv():
    """
    Manually parses the local .env file (if it exists) to load environment variables
    without needing an external python-dotenv package.
    """
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_path):
        try:
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        key, _, val = line.partition('=')
                        key = key.strip()
                        val = val.strip()
                        if val.startswith(('"', "'")) and val.endswith(val[0]):
                            val = val[1:-1]
                        os.environ[key] = val
        except Exception as e:
            print(f"[GDRIVE-SYNC] Error loading local .env file: {str(e)}")

# Automatically load environment variables from local .env on import
load_dotenv()

# --- EVENTLET SOCKET FIX ---
# Context manager that temporarily restores the original (unpatched) socket
# module for Google API calls, bypassing eventlet's green DNS/connection
# monkey-patching that can't resolve oauth2.googleapis.com.
import contextlib

@contextlib.contextmanager
def _original_socket_for_google():
    """Temporarily swap patched socket back to real socket for Google API calls."""
    sm = __import__('socket')
    saved = {}
    try:
        from eventlet.patcher import original
        orig = original('socket')
        for attr in ('getaddrinfo', 'create_connection', 'socket'):
            saved[attr] = getattr(sm, attr)
            setattr(sm, attr, getattr(orig, attr))
    except ImportError:
        pass
    try:
        yield
    finally:
        for attr, val in saved.items():
            setattr(sm, attr, val)

def clean_private_key(key_str):
    if not isinstance(key_str, str):
        return key_str
    
    # If the key contains literally '\n' as text, replace it with actual newlines
    if '\\n' in key_str:
        key_str = key_str.replace('\\n', '\n')
        
    # If the backslash was stripped and '\n' became literal 'n', we clean the headers
    if "-----BEGIN PRIVATE KEY-----n" in key_str:
        key_str = key_str.replace("-----BEGIN PRIVATE KEY-----n", "-----BEGIN PRIVATE KEY-----\n")
    if "n-----END PRIVATE KEY-----" in key_str:
        key_str = key_str.replace("n-----END PRIVATE KEY-----", "\n-----END PRIVATE KEY-----")
    
    # Normalize multiple newlines
    while "\n\n" in key_str:
        key_str = key_str.replace("\n\n", "\n")
        
    return key_str

def load_sa_json(sa_json_str):
    if not sa_json_str:
        return None
    sa_json_str = sa_json_str.strip()
    
    # Check if it is base64 encoded
    if not sa_json_str.startswith('{'):
        try:
            import base64
            decoded = base64.b64decode(sa_json_str).decode('utf-8')
            if decoded.strip().startswith('{'):
                sa_json_str = decoded.strip()
        except Exception:
            pass
            
    try:
        info = json.loads(sa_json_str)
    except Exception as e:
        try:
            # Attempt parsing after replacing double escaped backslashes
            info = json.loads(sa_json_str.replace('\\\\', '\\'))
        except Exception:
            raise e
            
    if isinstance(info, dict) and 'private_key' in info:
        info['private_key'] = clean_private_key(info['private_key'])
        
    return info

def get_gdrive_service():
    """
    Initializes and returns the Google Drive API service using Google Service Account credentials.
    Returns None if variables are missing or initialization fails.
    """
    if not GOOGLE_LIBS_AVAILABLE:
        print("[GDRIVE-SYNC] Warning: Google API client libraries are not available.")
        return None

    folder_id = os.environ.get('GD_FOLDER_ID')
    sa_json = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')

    if not folder_id or not sa_json:
        print("[GDRIVE-SYNC] Warning: GD_FOLDER_ID or GOOGLE_SERVICE_ACCOUNT_JSON is not configured in environment.")
        return None

    try:
        service = _build_drive_service()
        return service
    except Exception as e:
        print(f"[GDRIVE-SYNC] Error initializing Google Drive service: {str(e)}")
        return None

def _build_drive_service():
    sa_json = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    if not sa_json:
        return None
    info = load_sa_json(sa_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    with _original_socket_for_google():
        try:
            return build('drive', 'v3', credentials=creds, cache_discovery=False)
        except Exception as e2:
            print(f"[GDRIVE-SYNC] build failed: {e2}")
            return None

def _appdata_list(service):
    """List all files in the Drive appdata folder."""
    return service.files().list(spaces='appDataFolder', fields='files(id, name)', pageSize=100).execute().get('files', [])

def _appdata_upload(service, filename, content_bytes):
    """Upload a file to Drive appdata folder."""
    try:
        existing = service.files().list(spaces='appDataFolder', q=f"name='{filename}'", fields='files(id)').execute().get('files', [])
        media = MediaIoBaseUpload(io.BytesIO(content_bytes), mimetype='application/octet-stream', resumable=False)
        if existing:
            service.files().update(fileId=existing[0]['id'], media_body=media).execute()
        else:
            service.files().create(body={'name': filename, 'parents': ['appDataFolder']}, media_body=media).execute()
        return True
    except Exception as e:
        print(f"[APPDATA] Upload {filename} failed: {e}")
        return False

def _appdata_download(service, filename):
    """Download a file from Drive appdata folder."""
    try:
        files = service.files().list(spaces='appDataFolder', q=f"name='{filename}'", fields='files(id)').execute().get('files', [])
        if not files:
            return None
        fh = io.BytesIO()
        request = service.files().get_media(fileId=files[0]['id'])
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return fh.getvalue()
    except Exception as e:
        print(f"[APPDATA] Download {filename} failed: {e}")
        return None

def _appdata_delete(service, filename):
    """Delete a file from Drive appdata folder."""
    try:
        files = service.files().list(spaces='appDataFolder', q=f"name='{filename}'", fields='files(id)').execute().get('files', [])
        for f in files:
            service.files().delete(fileId=f['id']).execute()
        return True
    except Exception as e:
        print(f"[APPDATA] Delete {filename} failed: {e}")
        return False

def backup_db_to_appdata():
    """Upload socrates.db to Drive appdata folder."""
    service = get_gdrive_service()
    if not service or not os.path.exists(DB_FILE):
        return False
    with open(DB_FILE, 'rb') as f:
        data = f.read()
    with _original_socket_for_google():
        return _appdata_upload(service, 'socrates.db', data)

def restore_db_from_appdata():
    """Download socrates.db from Drive appdata folder."""
    service = get_gdrive_service()
    if not service:
        return False
    with _original_socket_for_google():
        data = _appdata_download(service, 'socrates.db')
    if data is None:
        return False
    with open(DB_FILE, 'wb') as f:
        f.write(data)
    print(f"[APPDATA] Database restored ({len(data)} bytes)")
    return True

def backup_module_to_appdata(title, payload):
    """Upload a module JSON to Drive appdata folder."""
    service = get_gdrive_service()
    if not service:
        return False
    data = json.dumps(payload, indent=2).encode('utf-8')
    with _original_socket_for_google():
        return _appdata_upload(service, f"module_{title}.json", data)

def fetch_modules_from_appdata():
    """List all module files from Drive appdata folder."""
    service = get_gdrive_service()
    if not service:
        return {}
    result = {}
    with _original_socket_for_google():
        files = _appdata_list(service)
    for f in files:
        name = f['name']
        if not name.startswith('module_') or not name.endswith('.json'):
            continue
        title = name.replace('module_', '').replace('.json', '')
        with _original_socket_for_google():
            data = _appdata_download(service, name)
        if data:
            try:
                result[title] = json.loads(data.decode('utf-8'))
            except Exception:
                pass
    return result

def sync_module_to_gdrive(title, difficulty, status, created_by, audited_by, questions, source_text=""):
    """Saves a module to Drive for persistence. Runs Drive calls in background thread."""
    payload = {"title": title, "difficulty": difficulty, "status": status,
               "created_by": created_by, "audited_by": audited_by,
               "source_text": source_text, "questions": questions}

    def _do_sync():
        # Try appdata folder first
        try:
            backup_module_to_appdata(title, payload)
        except Exception:
            pass
        # Try visible Drive folder
        folder_id = os.environ.get('GD_FOLDER_ID')
        if not folder_id:
            return
        try:
            with _original_socket_for_google():
                service = _build_drive_service()
                if not service:
                    return
                q = f"name = '{title}.json' and '{folder_id}' in parents and trashed = false"
                files = service.files().list(q=q, spaces='drive', fields='files(id, name)', pageSize=10, supportsAllDrives=True, includeItemsFromAllDrives=True).execute().get('files', [])
                json_bytes = json.dumps(payload, indent=2).encode('utf-8')
                media = MediaIoBaseUpload(io.BytesIO(json_bytes), mimetype='application/json', resumable=False)
                if files:
                    service.files().update(fileId=files[0]['id'], media_body=media, supportsAllDrives=True, fields='id').execute()
                else:
                    service.files().create(body={'name': f'{title}.json', 'parents': [folder_id]}, media_body=media, supportsAllDrives=True, fields='id').execute()
        except Exception as e:
            print(f"[GDRIVE-SYNC] Drive sync failed (background): {e}")

    import threading
    t = threading.Thread(target=_do_sync, daemon=True)
    t.start()
    return True

def delete_module_from_gdrive(title):
    """
    Locates and deletes the file [title].json from the Google Drive folder.
    """
    folder_id = os.environ.get('GD_FOLDER_ID')
    if not folder_id:
        return False

    try:
        with _original_socket_for_google():
            service = _build_drive_service()
            if not service:
                return False

            escaped_title = title.replace("'", "\\'")
            query = f"name = '{escaped_title}.json' and '{folder_id}' in parents and trashed = false"
            results = service.files().list(q=query, spaces='drive', fields='files(id, name)', supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
            files = results.get('files', [])

            if not files:
                print(f"[GDRIVE-SYNC] Module file '{title}.json' not found in Google Drive folder. Nothing to delete.")
                return True

            for f in files:
                file_id = f['id']
                print(f"[GDRIVE-SYNC] Deleting file '{f['name']}' (ID: {file_id}) from Google Drive.")
                service.files().delete(fileId=file_id, supportsAllDrives=True).execute()

        print(f"[GDRIVE-SYNC] Successfully deleted '{title}' from Google Drive.")
        return True
    except Exception as e:
        print(f"[GDRIVE-SYNC] Error deleting '{title}' from Google Drive: {str(e)}")
        return False

def sync_modules_from_gdrive(conn=None):
    """
    Queries the Google Drive folder for all .json files, parses them,
    and inserts or updates them in the active SQLite database.
    """
    service = _build_drive_service()
    if not service:
        print("[GDRIVE-SYNC] Skipping database import from Google Drive (service not initialized).")
        return False

    folder_id = os.environ.get('GD_FOLDER_ID')

    try:
        with _original_socket_for_google():
            query = f"'{folder_id}' in parents and trashed = false"
            results = service.files().list(q=query, spaces='drive', fields='files(id, name)', pageSize=100, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
            files = results.get('files', [])

            if not files:
                print("[GDRIVE-SYNC] No custom Socratic modules found in Google Drive folder.")
                return True

            print(f"[GDRIVE-SYNC] Found {len(files)} total files in Google Drive folder. Starting database restoration...")

        # If a connection wasn't passed, manage our own connection
        local_conn = False
        if conn is None:
            conn = get_db_connection()
            local_conn = True
        else:
            try:
                conn.row_factory = sqlite3.Row
            except Exception:
                pass

        cursor = conn.cursor()
        now = datetime.datetime.now().strftime("%Y-%m-%d")

        for f in files:
            file_id = f['id']
            filename = f['name']
            
            # Skip structured backup files and non-json files
            if not filename.endswith('.json') or filename in ["roster_backup.json", "trainers_backup.json"]:
                continue
            
            try:
                # 2. Download file content
                request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                
                content = fh.getvalue().decode('utf-8')
                module_data = json.loads(content)

                title = module_data.get('title')
                if not title:
                    print(f"[GDRIVE-SYNC] Warning: File '{filename}' is missing 'title'. Skipping.")
                    continue

                difficulty = module_data.get('difficulty', 'Medium')
                status = module_data.get('status', 'Pending Audit')
                created_by = module_data.get('created_by', 'ADMIN')
                audited_by = module_data.get('audited_by', 'Awaiting Audit')
                questions = module_data.get('questions', [])

                # 3. Check if module already exists in database
                cursor.execute("SELECT id FROM modules WHERE title = ?", (title,))
                existing_module = cursor.fetchone()

                if existing_module:
                    module_id = existing_module[0]
                    print(f"[GDRIVE-SYNC] Restoring existing module '{title}' (ID: {module_id}) from Google Drive...")
                    # Update module metadata
                    source_text = module_data.get('source_text', '')
                    cursor.execute(
                        "UPDATE modules SET questions_count = ?, status = ?, audited_by = ?, difficulty = ?, source_text = ? WHERE id = ?",
                        (len(questions), status, audited_by, difficulty, source_text, module_id)
                    )
                    # Prune old questions to prevent duplicates/conflicts
                    cursor.execute("DELETE FROM questions WHERE module_id = ?", (module_id,))
                else:
                    print(f"[GDRIVE-SYNC] Restoring new module '{title}' from Google Drive...")
                    # Insert new module
                    source_text = module_data.get('source_text', '')
                    cursor.execute(
                        "INSERT INTO modules (title, questions_count, created_at, status, created_by, audited_by, difficulty, source_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (title, len(questions), now, status, created_by, audited_by, difficulty, source_text)
                    )
                    module_id = cursor.lastrowid

                # 4. Insert restored questions
                for q in questions:
                    q_text = q.get('question_text', q.get('question', 'Question'))
                    
                    # Support both list-based options and structured keys
                    opts = q.get('options')
                    if not opts or len(opts) < 4:
                        opts = [
                            q.get('option_a', 'Option A'),
                            q.get('option_b', 'Option B'),
                            q.get('option_c', 'Option C'),
                            q.get('option_d', 'Option D')
                        ]
                    
                    correct_idx = q.get('correctIndex', q.get('correct_index', 0))
                    approved = q.get('approved', 1)
                    translations = json.dumps(q.get('translations', {}))

                    cursor.execute(
                        "INSERT INTO questions (module_id, question_text, option_a, option_b, option_c, option_d, correct_index, approved, translations) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (module_id, q_text, opts[0], opts[1], opts[2], opts[3], correct_idx, approved, translations)
                    )
                
                # Commit individual module transaction to keep other modules safe in case of failures
                conn.commit()
                print(f"[GDRIVE-SYNC] Successfully restored module '{title}' into SQLite.")

            except Exception as inner_e:
                print(f"[GDRIVE-SYNC] Error restoring module file '{filename}': {str(inner_e)}")
                conn.rollback()

        if local_conn:
            conn.close()

        print("[GDRIVE-SYNC] Database restoration from Google Drive finished successfully.")
        return True
    except Exception as e:
        print(f"[GDRIVE-SYNC] Error querying Google Drive files: {str(e)}")
        return False

# --- FULL SQLITE DATABASE SYNC DAEMON ---

DB_BACKUP_LOCK = threading.Lock()
LAST_BACKUP_TIME = 0
BACKUP_COOLDOWN = 10  # Minimum seconds between backups

def sync_roster_to_gdrive(conn=None):
    """
    Saves the entire employees roster to Google Drive as roster_backup.json.
    """
    service = get_gdrive_service()
    if not service:
        print("[GDRIVE-SYNC] Skipping roster backup to Google Drive (service not initialized).")
        return False

    folder_id = os.environ.get('GD_FOLDER_ID')
    filename = "roster_backup.json"

    local_conn = False
    if conn is None:
        conn = get_db_connection()
        local_conn = True

    try:
        if conn is not None:
            try:
                conn.row_factory = sqlite3.Row
            except Exception:
                pass
        employees = conn.execute("SELECT * FROM employees ORDER BY emp_code ASC").fetchall()
        roster_data = [dict(e) for e in employees]

        query = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
        results = service.files().list(q=query, spaces='drive', fields='files(id, name)', supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        files = results.get('files', [])

        payload_bytes = json.dumps(roster_data, indent=2).encode('utf-8')
        media = MediaIoBaseUpload(io.BytesIO(payload_bytes), mimetype='application/json', resumable=True)

        if files:
            file_id = files[0]['id']
            print(f"[GDRIVE-SYNC] Syncing roster to Google Drive (updating existing file ID: {file_id})...")
            service.files().update(fileId=file_id, media_body=media, supportsAllDrives=True).execute()
        else:
            file_metadata = {
                'name': filename,
                'parents': [folder_id]
            }
            print("[GDRIVE-SYNC] Syncing roster to Google Drive (creating new backup)...")
            service.files().create(body=file_metadata, media_body=media, supportsAllDrives=True).execute()

        print("[GDRIVE-SYNC] Roster synced to Google Drive successfully.")
        return True
    except Exception as e:
        print(f"[GDRIVE-SYNC] Error syncing roster to Google Drive: {str(e)}")
        return False
    finally:
        if local_conn:
            conn.close()

def sync_roster_from_gdrive(conn=None):
    """
    Downloads roster_backup.json from Google Drive and merges it into local SQLite employees table.
    """
    service = get_gdrive_service()
    if not service:
        print("[GDRIVE-SYNC] Skipping roster import from Google Drive (service not initialized).")
        return False

    folder_id = os.environ.get('GD_FOLDER_ID')
    filename = "roster_backup.json"

    try:
        query = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
        results = service.files().list(q=query, spaces='drive', fields='files(id, name)', supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        files = results.get('files', [])

        if not files:
            print("[GDRIVE-SYNC] No custom roster backup found in Google Drive.")
            return False

        file_id = files[0]['id']
        print(f"[GDRIVE-SYNC] Downloading roster backup from Google Drive (ID: {file_id})...")

        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()

        roster_data = json.loads(fh.getvalue().decode('utf-8'))

        local_conn = False
        if conn is None:
            conn = get_db_connection()
            local_conn = True
        else:
            try:
                conn.row_factory = sqlite3.Row
            except Exception:
                pass

        cursor = conn.cursor()

        for emp in roster_data:
            cursor.execute("""
                INSERT INTO employees (emp_code, emp_name, branch_name, zone, division, business_unit, role, product_name, status, change_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(emp_code) DO UPDATE SET
                    emp_name=excluded.emp_name,
                    branch_name=excluded.branch_name,
                    zone=excluded.zone,
                    division=excluded.division,
                    business_unit=excluded.business_unit,
                    role=excluded.role,
                    product_name=excluded.product_name,
                    status=excluded.status,
                    change_detail=excluded.change_detail
            """, (
                emp['emp_code'], emp['emp_name'], emp['branch_name'], emp['zone'], emp['division'],
                emp['business_unit'], emp['role'], emp['product_name'], emp['status'], emp['change_detail']
            ))

        conn.commit()
        if local_conn:
            conn.close()

        print(f"[GDRIVE-SYNC] Successfully restored {len(roster_data)} employees from Google Drive roster backup.")
        return True
    except Exception as e:
        print(f"[GDRIVE-SYNC] Error restoring roster from Google Drive: {str(e)}")
        return False

def sync_trainers_to_gdrive(conn=None):
    """
    Saves the entire trainers list to Google Drive as trainers_backup.json.
    """
    service = get_gdrive_service()
    if not service:
        print("[GDRIVE-SYNC] Skipping trainers backup to Google Drive (service not initialized).")
        return False

    folder_id = os.environ.get('GD_FOLDER_ID')
    filename = "trainers_backup.json"

    local_conn = False
    if conn is None:
        conn = get_db_connection()
        local_conn = True

    try:
        if conn is not None:
            try:
                conn.row_factory = sqlite3.Row
            except Exception:
                pass
        trainers = conn.execute("SELECT * FROM trainers ORDER BY trainer_id ASC").fetchall()
        trainers_data = [dict(t) for t in trainers]

        query = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
        results = service.files().list(q=query, spaces='drive', fields='files(id, name)', supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        files = results.get('files', [])

        payload_bytes = json.dumps(trainers_data, indent=2).encode('utf-8')
        media = MediaIoBaseUpload(io.BytesIO(payload_bytes), mimetype='application/json', resumable=True)

        if files:
            file_id = files[0]['id']
            print(f"[GDRIVE-SYNC] Syncing trainers to Google Drive (updating existing file ID: {file_id})...")
            service.files().update(fileId=file_id, media_body=media, supportsAllDrives=True).execute()
        else:
            file_metadata = {
                'name': filename,
                'parents': [folder_id]
            }
            print("[GDRIVE-SYNC] Syncing trainers to Google Drive (creating new backup)...")
            service.files().create(body=file_metadata, media_body=media, supportsAllDrives=True).execute()

        print("[GDRIVE-SYNC] Trainers synced to Google Drive successfully.")
        return True
    except Exception as e:
        print(f"[GDRIVE-SYNC] Error syncing trainers to Google Drive: {str(e)}")
        return False
    finally:
        if local_conn:
            conn.close()

def sync_trainers_from_gdrive(conn=None):
    """
    Downloads trainers_backup.json from Google Drive and merges it into local SQLite trainers table.
    """
    service = get_gdrive_service()
    if not service:
        print("[GDRIVE-SYNC] Skipping trainers import from Google Drive (service not initialized).")
        return False

    folder_id = os.environ.get('GD_FOLDER_ID')
    filename = "trainers_backup.json"

    try:
        query = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
        results = service.files().list(q=query, spaces='drive', fields='files(id, name)', supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        files = results.get('files', [])

        if not files:
            print("[GDRIVE-SYNC] No trainers backup found in Google Drive.")
            return False

        file_id = files[0]['id']
        print(f"[GDRIVE-SYNC] Downloading trainers backup from Google Drive (ID: {file_id})...")

        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()

        trainers_data = json.loads(fh.getvalue().decode('utf-8'))

        local_conn = False
        if conn is None:
            conn = get_db_connection()
            local_conn = True
        else:
            try:
                conn.row_factory = sqlite3.Row
            except Exception:
                pass

        cursor = conn.cursor()

        for t in trainers_data:
            cursor.execute("""
                INSERT INTO trainers (trainer_id, name, zone, password, status, role, last_login, zones, divisions, branches, business_units, plain_password)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trainer_id) DO UPDATE SET
                    name=excluded.name,
                    zone=excluded.zone,
                    password=excluded.password,
                    status=excluded.status,
                    role=excluded.role,
                    last_login=excluded.last_login,
                    zones=excluded.zones,
                    divisions=excluded.divisions,
                    branches=excluded.branches,
                    business_units=excluded.business_units,
                    plain_password=excluded.plain_password
            """, (
                t['trainer_id'], t['name'], t['zone'], t['password'], t.get('status', 'Active'),
                t.get('role', 'Trainer'), t.get('last_login'), t.get('zones', 'ALL'),
                t.get('divisions', 'ALL'), t.get('branches', 'ALL'), t.get('business_units', 'ALL'),
                t.get('plain_password')
            ))

        conn.commit()
        if local_conn:
            conn.close()

        print(f"[GDRIVE-SYNC] Successfully restored {len(trainers_data)} trainers from Google Drive trainers backup.")
        return True
    except Exception as e:
        print(f"[GDRIVE-SYNC] Error restoring trainers from Google Drive: {str(e)}")
        return False

def _restore_from_drive(conn):
    """Restore trainers, roster, modules from Drive JSON files."""
    try:
        sync_trainers_from_gdrive(conn)
    except Exception as e:
        print(f"[GDRIVE-SYNC] Trainers restore from Drive skipped: {str(e)}")
    try:
        sync_roster_from_gdrive(conn)
    except Exception as e:
        print(f"[GDRIVE-SYNC] Roster restore from Drive skipped: {str(e)}")
    try:
        sync_modules_from_gdrive(conn)
    except Exception as e:
        print(f"[GDRIVE-SYNC] Modules restore from Drive skipped: {str(e)}")

def _restore_from_appdata(conn):
    """Restore modules from Drive appdata folder."""
    if restore_db_from_appdata():
        return True
    modules = fetch_modules_from_appdata()
    if not modules:
        return False
    cursor = conn.cursor()
    for title, payload in modules.items():
        try:
            cursor.execute("SELECT id FROM modules WHERE title=?", (title,))
            existing = cursor.fetchone()
            payload.setdefault('source_text', '')
            if existing:
                cursor.execute("UPDATE modules SET difficulty=?, status=?, audited_by=?, source_text=? WHERE id=?",
                               (payload.get('difficulty', 'Medium'), payload.get('status', 'Ready'),
                                payload.get('audited_by', 'Awaiting Audit'), payload.get('source_text', ''), existing['id']))
            else:
                now = datetime.datetime.now().strftime("%Y-%m-%d")
                cursor.execute("INSERT INTO modules (title, questions_count, created_at, status, created_by, audited_by, difficulty, source_text) VALUES (?,?,?,?,?,?,?,?)",
                               (title, len(payload.get('questions', [])), now, payload.get('status', 'Ready'),
                                payload.get('created_by', 'ADMIN'), payload.get('audited_by', 'Awaiting Audit'),
                                payload.get('difficulty', 'Medium'), payload.get('source_text', '')))
            if payload.get('questions'):
                q_mid = existing['id'] if existing else cursor.lastrowid
                cursor.execute("DELETE FROM questions WHERE module_id=?", (q_mid,))
                for q in payload['questions']:
                    cursor.execute("INSERT INTO questions (module_id, question_text, option_a, option_b, option_c, option_d, correct_index, approved) VALUES (?,?,?,?,?,?,?,?)",
                                   (q_mid, q.get('question_text', ''), q.get('option_a', ''), q.get('option_b', ''),
                                    q.get('option_c', ''), q.get('option_d', ''), q.get('correct_index', 0), 1))
            conn.commit()
        except Exception as e:
            print(f"[APPDATA] Error restoring module '{title}': {e}")
    return True

def restore_db_from_gdrive():
    """
    Restores the complete database from GCS first (primary), then Drive (secondary).
    """
    if os.environ.get('DATABASE_URL'):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM trainers")
            t_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM employees")
            e_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM modules")
            m_count = cursor.fetchone()[0]
            conn.close()
            if t_count > 1 or e_count > 0 or m_count > 0:
                print("[GDRIVE-SYNC] Database is already populated. Skipping initial restore.")
                return True
        except Exception as e:
            print(f"[GDRIVE-SYNC] Error checking populate state: {str(e)}")

    print("[GDRIVE-SYNC] Trying appdata folder restore first...")
    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
    except Exception:
        pass

    if _restore_from_appdata(conn):
        conn.close()
        print("[GDRIVE-SYNC] Restored from appdata successfully.")
        return True

    print("[GDRIVE-SYNC] Appdata empty, trying Drive folder restore...")
    _restore_from_drive(conn)
    conn.close()
        
    conn.close()
    print("[GDRIVE-SYNC] Database tables successfully restored and rebuilt from Google Drive.")
    return True

def backup_db_to_gdrive():
    """
    Safely reads socrates.db and uploads to Drive appdata folder (primary)
    and the configured Drive folder (secondary, may fail if no storage quota).
    """
    global LAST_BACKUP_TIME
    import time

    current_time = time.time()
    if current_time - LAST_BACKUP_TIME < BACKUP_COOLDOWN:
        return False
    if not os.path.exists(DB_FILE):
        return False

    appdata_ok = backup_db_to_appdata()
    drive_ok = False

    service = get_gdrive_service()
    if service:
        folder_id = os.environ.get('GD_FOLDER_ID')
        filename = "socrates_backup.db"
        with DB_BACKUP_LOCK:
            try:
                with _original_socket_for_google():
                    query = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
                    results = service.files().list(q=query, spaces='drive', fields='files(id, name)', supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
                    files = results.get('files', [])
                with open(DB_FILE, 'rb') as f:
                    db_bytes = f.read()
                media = MediaIoBaseUpload(io.BytesIO(db_bytes), mimetype='application/x-sqlite3', resumable=True)
                with _original_socket_for_google():
                    if files:
                        service.files().update(fileId=files[0]['id'], media_body=media, supportsAllDrives=True).execute()
                    else:
                        service.files().create(body={'name': filename, 'parents': [folder_id]}, media_body=media, supportsAllDrives=True).execute()
                drive_ok = True
            except Exception as e:
                print(f"[GDRIVE-SYNC] Drive folder backup failed (non-fatal): {e}")

    LAST_BACKUP_TIME = time.time()
    return appdata_ok or drive_ok

def start_db_backup_daemon():
    """
    Spawns a background thread that periodically monitors both local and remote
    database modification states, enabling seamless bidirectional sync.
    If local changes are detected, they are uploaded.
    If remote updates from other trainers are found, they are dynamically restored.
    """
    import time

    def get_remote_mtime(service):
        folder_id = os.environ.get('GD_FOLDER_ID')
        filename = "socrates_backup.db"
        if not folder_id:
            return None
        try:
            with _original_socket_for_google():
                query = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
                results = service.files().list(q=query, spaces='drive', fields='files(id, modifiedTime)', supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
                files = results.get('files', [])
            if files:
                return files[0]['modifiedTime']
        except Exception as e:
            print(f"[GDRIVE-SYNC] Error getting remote database backup mtime: {str(e)}")
        return None

    def monitor_db():
        print("[GDRIVE-SYNC] Bidirectional database sync daemon started.")
        service = get_gdrive_service()
        
        last_local_mtime = 0
        if os.path.exists(DB_FILE):
            last_local_mtime = os.path.getmtime(DB_FILE)

        # Initialize tracking for the remote backup's modifiedTime
        last_remote_mtime = None
        if service:
            last_remote_mtime = get_remote_mtime(service)
            print(f"[GDRIVE-SYNC] Initial remote database mtime: {last_remote_mtime}")

        while True:
            time.sleep(30)  # Check every 30 seconds
            try:
                # Reload service account client just in case credentials rotate or reconnect is needed
                srv = get_gdrive_service()
                if not srv:
                    # Fallback to simple local-only tracking if GD is offline
                    if os.path.exists(DB_FILE):
                        current_local = os.path.getmtime(DB_FILE)
                        if current_local > last_local_mtime:
                            print("[GDRIVE-SYNC] GD Offline. Local database changed. (Pending sync)")
                            last_local_mtime = current_local
                    continue

                current_local = 0
                if os.path.exists(DB_FILE):
                    current_local = os.path.getmtime(DB_FILE)

                current_remote = get_remote_mtime(srv)

                # Scenario 1: Local changes detected -> Backup to Google Drive
                if current_local > last_local_mtime:
                    print("[GDRIVE-SYNC] Changes detected in local socrates.db. Backing up to Google Drive...")
                    if backup_db_to_gdrive():
                        # Update tracking stamps post-upload to avoid duplicate cycles
                        last_local_mtime = os.path.getmtime(DB_FILE)
                        last_remote_mtime = get_remote_mtime(srv)
                        print(f"[GDRIVE-SYNC] Backup complete. Sync state updated: remote={last_remote_mtime}")
                
                # Scenario 2: Remote database is newer than our last known sync -> Restore/Pull
                elif current_remote and current_remote != last_remote_mtime:
                    print(f"[GDRIVE-SYNC] Remote database backup updated by another trainer (new: {current_remote}, old: {last_remote_mtime}). Pulling changes...")
                    # Perform dynamic SQLite pull using existing lock
                    with DB_BACKUP_LOCK:
                        if restore_db_from_gdrive():
                            last_local_mtime = os.path.getmtime(DB_FILE)
                            last_remote_mtime = current_remote
                            print("[GDRIVE-SYNC] Dynamic database restore complete. Roster & Modules are now in sync.")
                            
            except Exception as e:
                print(f"[GDRIVE-SYNC] Error in database bidirectional sync daemon: {str(e)}")

    daemon = threading.Thread(target=monitor_db, name="DBBackupDaemon", daemon=True)
    daemon.start()
