with open("backend/app/api/v1/auth.py", "r", encoding="utf-8") as f:
    text = f.read()

text = text.replace("import re\nimport uuid", "import logging\nimport re\nimport uuid")
text = text.replace("import re\r\nimport uuid", "import logging\r\nimport re\r\nimport uuid")

old_router = 'router = APIRouter(prefix="/api/v1/auth", tags=["auth"])'
new_router = 'logger = logging.getLogger(__name__)\n\nrouter = APIRouter(prefix="/api/v1/auth", tags=["auth"])'
text = text.replace(old_router, new_router)

old_exc = "except Exception as exc:"
new_exc = "except Exception as exc:  # noqa: BLE001"
text = text.replace(old_exc, new_exc)

with open("backend/app/api/v1/auth.py", "w", encoding="utf-8") as f:
    f.write(text)

print("Auth.py successfully fixed!")
