import os
import json
import io
import sqlite3
import datetime

# Robust import wrapping to prevent crashes if libraries are missing
try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
    from google.oauth2 import service_account
    GOOGLE_LIBS_AVAILABLE = True
except ImportError:
    GOOGLE_LIBS_AVAILABLE = False

SCOPES = ['https://www.googleapis.com/auth/drive']
DB_FILE = "socrates.db"
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
        # Load the credentials JSON directly from memory
        info = json.loads(sa_json)
        credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        service = build('drive', 'v3', credentials=credentials)
        return service
    except Exception as e:
        print(f"[GDRIVE-SYNC] Error initializing Google Drive service: {str(e)}")
        return None

def sync_module_to_gdrive(title, difficulty, status, created_by, audited_by, questions):
    """
    Saves a module's full JSON representation to Google Drive.
    If the file [title].json already exists in the designated folder, it is updated.
    Otherwise, a new file is created.
    """
    service = get_gdrive_service()
    if not service:
        print(f"[GDRIVE-SYNC] Skipping Google Drive sync for '{title}' (service not initialized).")
        return False

    folder_id = os.environ.get('GD_FOLDER_ID')
    filename = f"{title}.json"

    # Construct clean payload representing the full module state
    payload = {
        "title": title,
        "difficulty": difficulty,
        "status": status,
        "created_by": created_by,
        "audited_by": audited_by,
        "questions": questions
    }

    try:
        # 1. Search for existing file with the exact name in the folder
        escaped_title = title.replace("'", "\\'")
        query = f"name = '{escaped_title}.json' and '{folder_id}' in parents and trashed = false"
        results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        files = results.get('files', [])

        # 2. Encode payload as binary bytes
        json_bytes = json.dumps(payload, indent=2, ensure_ascii=False).encode('utf-8')
        media = MediaIoBaseUpload(io.BytesIO(json_bytes), mimetype='application/json', resumable=True)

        if files:
            # Update existing file
            file_id = files[0]['id']
            print(f"[GDRIVE-SYNC] Updating existing module file '{filename}' (ID: {file_id}) in Google Drive.")
            service.files().update(fileId=file_id, media_body=media).execute()
        else:
            # Create new file
            file_metadata = {
                'name': filename,
                'parents': [folder_id]
            }
            print(f"[GDRIVE-SYNC] Creating new module file '{filename}' in Google Drive.")
            service.files().create(body=file_metadata, media_body=media).execute()

        print(f"[GDRIVE-SYNC] Successfully synchronized '{title}' to Google Drive.")
        return True
    except Exception as e:
        print(f"[GDRIVE-SYNC] Error synchronizing '{title}' to Google Drive: {str(e)}")
        return False

def delete_module_from_gdrive(title):
    """
    Locates and deletes the file [title].json from the Google Drive folder.
    """
    service = get_gdrive_service()
    if not service:
        print(f"[GDRIVE-SYNC] Skipping Google Drive delete for '{title}' (service not initialized).")
        return False

    folder_id = os.environ.get('GD_FOLDER_ID')

    try:
        # Search for the file in the designated folder
        escaped_title = title.replace("'", "\\'")
        query = f"name = '{escaped_title}.json' and '{folder_id}' in parents and trashed = false"
        results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        files = results.get('files', [])

        if not files:
            print(f"[GDRIVE-SYNC] Module file '{title}.json' not found in Google Drive folder. Nothing to delete.")
            return True

        # Delete the file
        for f in files:
            file_id = f['id']
            print(f"[GDRIVE-SYNC] Deleting file '{f['name']}' (ID: {file_id}) from Google Drive.")
            service.files().delete(fileId=file_id).execute()

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
    service = get_gdrive_service()
    if not service:
        print("[GDRIVE-SYNC] Skipping database import from Google Drive (service not initialized).")
        return False

    folder_id = os.environ.get('GD_FOLDER_ID')

    try:
        # 1. Fetch list of JSON files from Google Drive
        query = f"'{folder_id}' in parents and name ends with '.json' and trashed = false"
        results = service.files().list(q=query, spaces='drive', fields='files(id, name)', pageSize=100).execute()
        files = results.get('files', [])

        if not files:
            print("[GDRIVE-SYNC] No custom Socratic modules found in Google Drive folder.")
            return True

        print(f"[GDRIVE-SYNC] Found {len(files)} module files in Google Drive. Starting database restoration...")

        # If a connection wasn't passed, manage our own SQLite connection
        local_conn = False
        if conn is None:
            conn = sqlite3.connect(DB_FILE)
            local_conn = True

        cursor = conn.cursor()
        now = datetime.datetime.now().strftime("%Y-%m-%d")

        for f in files:
            file_id = f['id']
            filename = f['name']
            
            try:
                # 2. Download file content
                request = service.files().get_media(fileId=file_id)
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
                    cursor.execute(
                        "UPDATE modules SET questions_count = ?, status = ?, audited_by = ?, difficulty = ? WHERE id = ?",
                        (len(questions), status, audited_by, difficulty, module_id)
                    )
                    # Prune old questions to prevent duplicates/conflicts
                    cursor.execute("DELETE FROM questions WHERE module_id = ?", (module_id,))
                else:
                    print(f"[GDRIVE-SYNC] Restoring new module '{title}' from Google Drive...")
                    # Insert new module
                    cursor.execute(
                        "INSERT INTO modules (title, questions_count, created_at, status, created_by, audited_by, difficulty) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (title, len(questions), now, status, created_by, audited_by, difficulty)
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
