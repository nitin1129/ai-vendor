"""Create or reset an admin login.

    python -m scripts.create_admin admin@suppai.in "Ops Team"
"""
import asyncio
import getpass
import sys

from app import db as database
from app.security import hash_password
from app.utils import new_id, utcnow


async def main(email: str, name: str) -> None:
    password = getpass.getpass("Password (min 10 chars): ")
    if len(password) < 10:
        sys.exit("Password too short")
    db = database.connect()
    await database.ensure_indexes(db)
    email = email.lower()
    existing = await db.admins.find_one({"email": email})
    if existing:
        await db.admins.update_one({"_id": existing["_id"]},
                                   {"$set": {"password_hash": hash_password(password), "active": True, "name": name}})
        print(f"Updated admin {email}")
    else:
        await db.admins.insert_one({"_id": new_id("adm"), "email": email, "name": name,
                                    "password_hash": hash_password(password), "active": True,
                                    "created_at": utcnow()})
        print(f"Created admin {email}")
    database.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "Admin"))
