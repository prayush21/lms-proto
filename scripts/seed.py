import json
import uuid

import psycopg

from app.config import DATABASE_ADMIN_URL
from scripts.ids import (
    AUTHOR_A,
    AUTHOR_B,
    PLATFORM,
    STUDENT_A,
    STUDENT_B,
    TENANT_A,
    TENANT_B,
)


def uid(namespace: str, value: str) -> str:
    return str(uuid.uuid5(uuid.UUID(namespace), value))


def add_question(
    conn, owner: str, author: str | None, label: str, number: int, difficulty: str
):
    question_id = uid(owner, f"question:{label}:{number}")
    version_id = uid(owner, f"version:{label}:{number}:1")
    answer = str(number + number)
    options = [
        {"id": "a", "text": answer},
        {"id": "b", "text": str(number + number + 1)},
        {"id": "c", "text": str(number * number)},
        {"id": "d", "text": str(number)},
    ]
    conn.execute(
        """INSERT INTO questions (tenant_id,id,created_by) VALUES (%s,%s,%s)
           ON CONFLICT DO NOTHING""",
        (owner, question_id, author),
    )
    conn.execute(
        """INSERT INTO question_versions
           (tenant_id,id,question_id,version,subject,topic,subtopic,difficulty,stem,
            options,correct_option_id,explanation,created_by)
           VALUES (%s,%s,%s,1,'Math',%s,'Addition',%s,%s,%s::jsonb,'a',%s,%s)
           ON CONFLICT DO NOTHING""",
        (
            owner,
            version_id,
            question_id,
            f"{label} arithmetic",
            difficulty,
            f"[{label}] What is {number} + {number}?",
            json.dumps(options),
            f"Adding {number} twice gives {answer}.",
            author,
        ),
    )
    conn.execute(
        "UPDATE questions SET current_version_id=%s WHERE tenant_id=%s AND id=%s",
        (version_id, owner, question_id),
    )
    return version_id


def main():
    with psycopg.connect(DATABASE_ADMIN_URL) as conn:
        conn.execute(
            """INSERT INTO tiers (name,max_concurrent_attempts)
               VALUES ('learning',1000),('small',2)
               ON CONFLICT (name) DO UPDATE SET max_concurrent_attempts=excluded.max_concurrent_attempts"""
        )
        conn.execute(
            """INSERT INTO tenants (id,slug,name,tier_id,is_platform) VALUES
               (%s,'platform','Platform catalog',(SELECT id FROM tiers WHERE name='learning'),true),
               (%s,'alpha','Alpha Prep',(SELECT id FROM tiers WHERE name='learning'),false),
               (%s,'beta','Beta Learning',(SELECT id FROM tiers WHERE name='learning'),false)
               ON CONFLICT (id) DO UPDATE SET name=excluded.name""",
            (PLATFORM, TENANT_A, TENANT_B),
        )
        conn.execute(
            """INSERT INTO users (id,email,display_name) VALUES
               (%s,'student-a@example.test','Alpha Student'),
               (%s,'author-a@example.test','Alpha Author'),
               (%s,'student-b@example.test','Beta Student'),
               (%s,'author-b@example.test','Beta Author')
               ON CONFLICT (id) DO NOTHING""",
            (STUDENT_A, AUTHOR_A, STUDENT_B, AUTHOR_B),
        )
        conn.execute(
            """INSERT INTO memberships (tenant_id,user_id,role) VALUES
               (%s,%s,'student'),(%s,%s,'admin'),(%s,%s,'student'),(%s,%s,'admin')
               ON CONFLICT (tenant_id,user_id) DO UPDATE SET role=excluded.role""",
            (
                TENANT_A,
                STUDENT_A,
                TENANT_A,
                AUTHOR_A,
                TENANT_B,
                STUDENT_B,
                TENANT_B,
                AUTHOR_B,
            ),
        )
        global_versions, alpha_versions = [], []
        for number in range(1, 10):
            difficulty = ("easy", "medium", "hard")[(number - 1) % 3]
            global_versions.append(
                add_question(conn, PLATFORM, None, "Global", number + 20, difficulty)
            )
            alpha_versions.append(
                add_question(conn, TENANT_A, AUTHOR_A, "Alpha", number, difficulty)
            )
            add_question(conn, TENANT_B, AUTHOR_B, "Beta", number + 10, difficulty)
        blueprint_id = uid(TENANT_A, "blueprint:mock-1")
        section_ids = [
            uid(TENANT_A, "blueprint:mock-1:section:1"),
            uid(TENANT_A, "blueprint:mock-1:section:2"),
        ]
        conn.execute(
            """INSERT INTO blueprints (tenant_id,id,name,created_by)
               VALUES (%s,%s,'Two-section mini mock',%s) ON CONFLICT DO NOTHING""",
            (TENANT_A, blueprint_id, AUTHOR_A),
        )
        for position, section_id in enumerate(section_ids):
            conn.execute(
                """INSERT INTO blueprint_sections
                   (tenant_id,id,blueprint_id,position,name,time_limit_seconds)
                   VALUES (%s,%s,%s,%s,%s,300) ON CONFLICT DO NOTHING""",
                (TENANT_A, section_id, blueprint_id, position, f"Math {position + 1}"),
            )
            versions = (alpha_versions + global_versions)[
                position * 3 : position * 3 + 3
            ]
            for item_position, version_id in enumerate(versions):
                owner = TENANT_A if version_id in alpha_versions else PLATFORM
                conn.execute(
                    """INSERT INTO blueprint_items
                       (tenant_id,id,section_id,position,question_owner_tenant_id,question_version_id)
                       VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (
                        TENANT_A,
                        uid(TENANT_A, f"blueprint-item:{section_id}:{item_position}"),
                        section_id,
                        item_position,
                        owner,
                        version_id,
                    ),
                )
    print(
        "seeded Alpha Prep, Beta Learning, 27 questions, and one Alpha mock blueprint"
    )
    print(f"Alpha student: tenant={TENANT_A} user={STUDENT_A}")
    print(f"Beta student:  tenant={TENANT_B} user={STUDENT_B}")


if __name__ == "__main__":
    main()
