"""Seed default demo accounts (Student & Admin) for EduSense.

Creates or updates:
1. student@edusense.edu.vn / Student123! (Role: student)
2. admin@edusense.edu.vn / AdminSecure123! (Role: admin)
"""

import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime

# Ensure app is discoverable on python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend")))
sys.path.insert(0, "/app")

from sqlalchemy import select
from sqlalchemy.orm.attributes import set_committed_value

from app.core.database import async_session_factory
from app.core.security import hash_password
from app.repositories.user_repo import Role, User, UserProfile, UserRole


async def seed():
    async with async_session_factory() as session:
        # 1. Fetch roles
        student_role_res = await session.execute(select(Role).where(Role.name == "student"))
        student_role = student_role_res.scalars().first()
        admin_role_res = await session.execute(select(Role).where(Role.name == "admin"))
        admin_role = admin_role_res.scalars().first()

        if not student_role or not admin_role:
            print("ERROR: Roles 'student' or 'admin' not found in database!")
            return

        accounts = [
            {
                "email": "student@edusense.edu.vn",
                "password": "Student123!",
                "full_name": "Nguyễn Văn Sinh Viên",
                "role": student_role,
                "plan": "free",
            },
            {
                "email": "admin@edusense.edu.vn",
                "password": "AdminSecure123!",
                "full_name": "Quản Trị Viên Hệ Thống",
                "role": admin_role,
                "plan": "pro",
            },
        ]

        for acc in accounts:
            email = acc["email"]
            pwd_hash = hash_password(acc["password"])
            existing_user_res = await session.execute(select(User).where(User.email == email))
            existing_user = existing_user_res.scalars().first()

            if existing_user:
                existing_user.password_hash = pwd_hash
                existing_user.is_active = True
                existing_user.is_email_verified = True
                existing_user.current_plan_code = acc["plan"]
                print(f"[UPDATED] {email} / Password: {acc['password']}")
            else:
                user_id = uuid.uuid4()
                user = User(
                    id=user_id,
                    email=email,
                    password_hash=pwd_hash,
                    is_active=True,
                    is_email_verified=True,
                    current_plan_code=acc["plan"],
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
                session.add(user)
                await session.flush()

                profile = UserProfile(
                    user_id=user.id,
                    full_name=acc["full_name"],
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
                session.add(profile)

                ur = UserRole(
                    user_id=user.id,
                    role_id=acc["role"].id,
                )
                session.add(ur)
                await session.flush()

                set_committed_value(user, "profile", profile)
                set_committed_value(user, "roles", [acc["role"]])
                print(f"[CREATED] {email} / Password: {acc['password']} / Role: {acc['role'].name}")

        await session.commit()
        print("\nAll demo accounts seeded successfully!")


if __name__ == "__main__":
    asyncio.run(seed())
