"""Script to export complete, Postman-ready Swagger/OpenAPI specification JSON."""

import json
import os
import sys
from pathlib import Path

# Add backend directory to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.main import app  # noqa: E402


def export_swagger() -> None:
    """Generate and write formatted swagger.json and openapi.json."""
    spec = app.openapi()

    # Ensure rich metadata for Postman
    spec["info"]["contact"] = {
        "name": "EduSense Engineering Team",
        "email": "contact@edusense.edu.vn",
        "url": "https://github.com/C1SE-64-EduSense/EduSense",
    }
    spec["info"]["license"] = {
        "name": "Proprietary",
    }

    # Ensure servers array is present
    spec["servers"] = [
        {"url": "http://localhost:8000", "description": "Local Development Server"},
        {"url": "http://127.0.0.1:8000", "description": "Localhost IP"},
    ]

    # Ensure security schemes are properly formatted for Postman Bearer Token
    components = spec.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes["HTTPBearer"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "Enter JWT Bearer token obtained from POST /api/v1/auth/login",
    }

    # Target export destination
    destinations = [
        REPO_ROOT / "docs" / "openapi.json",
    ]

    json_str = json.dumps(spec, indent=2, ensure_ascii=False)

    for dest in destinations:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json_str, encoding="utf-8")
        print(f"Exported: {dest} ({len(json_str)} bytes, {len(spec.get('paths', {}))} paths)")


if __name__ == "__main__":
    export_swagger()
