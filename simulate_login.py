import os
import json

# Set the Supabase DATABASE_URL so app.py connects to it
os.environ['DATABASE_URL'] = "postgresql://postgres.qeiejhlnnfcmshakvfpw:Ds@9983552441@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres"

# Force test mode so we see full tracebacks
from app import app

client = app.test_client()

print("Simulating admin login request in PostgreSQL mode...")
try:
    response = client.post(
        '/api/admin/login',
        data=json.dumps({"trainer_id": "ADMIN", "password": "admin123"}),
        content_type='application/json'
    )
    print("Status Code:", response.status_code)
    print("Response Headers:", dict(response.headers))
    print("Response Data:", response.data.decode('utf-8'))
except Exception as e:
    import traceback
    print("Exception occurred during request:")
    traceback.print_exc()
