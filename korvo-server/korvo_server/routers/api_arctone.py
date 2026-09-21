from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from korvo_server.config import ARCTONE_SAMPLES_DIR
from korvo_server.deps import KorvoDep

router = APIRouter(prefix="/api/arctone", tags=["arctone"])


class ArctonePersonIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    language_code: str = Field(min_length=2, max_length=16)
    language_name: str = Field(min_length=1, max_length=80)
    target_language_code: str = Field(min_length=2, max_length=16)
    target_language_name: str = Field(min_length=1, max_length=80)
    speaker_label: str = Field(min_length=1, max_length=24)


def _reject_duplicate_label(kdb, speaker_label: str, person_id: int | None = None) -> None:
    row = kdb.conn.execute(
        "SELECT id FROM arctone_people WHERE speaker_label = ? AND id != ?",
        (speaker_label, person_id or -1),
    ).fetchone()
    if row:
        raise HTTPException(400, "Speaker label is already in use")


def _row_to_person(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "language_code": row["language_code"],
        "language_name": row["language_name"],
        "target_language_code": row["target_language_code"],
        "target_language_name": row["target_language_name"],
        "speaker_label": row["speaker_label"],
        "authenticated": bool(row["authenticated"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("/people")
def list_people(kdb: KorvoDep):
    with kdb.lock:
        rows = kdb.conn.execute(
            """
            SELECT id, name, language_code, language_name, target_language_code,
                   target_language_name, speaker_label, authenticated, created_at, updated_at
            FROM arctone_people
            ORDER BY id ASC
            """
        ).fetchall()
    return {"people": [_row_to_person(r) for r in rows], "limit": 5}


@router.post("/people")
def upsert_person(body: ArctonePersonIn, kdb: KorvoDep):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Person name is required")
    with kdb.lock:
        count = kdb.conn.execute("SELECT COUNT(*) AS n FROM arctone_people").fetchone()["n"]
        if count >= 5:
            raise HTTPException(400, "Arctone supports up to five speakers")
        _reject_duplicate_label(kdb, body.speaker_label)
        kdb.conn.execute(
            """
            INSERT INTO arctone_people (
                name, language_code, language_name, target_language_code,
                target_language_name, speaker_label, authenticated, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
            """,
            (
                name,
                body.language_code,
                body.language_name,
                body.target_language_code,
                body.target_language_name,
                body.speaker_label,
            ),
        )
        kdb.conn.commit()
        row = kdb.conn.execute(
            """
            SELECT id, name, language_code, language_name, target_language_code,
                   target_language_name, speaker_label, authenticated, created_at, updated_at
            FROM arctone_people
            WHERE id = last_insert_rowid()
            """
        ).fetchone()
    return {"success": True, "person": _row_to_person(row)}


@router.patch("/people/{person_id}")
def update_person(person_id: int, body: ArctonePersonIn, kdb: KorvoDep):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Person name is required")
    with kdb.lock:
        current = kdb.conn.execute("SELECT id FROM arctone_people WHERE id = ?", (person_id,)).fetchone()
        if not current:
            raise HTTPException(404, "Person not found")
        _reject_duplicate_label(kdb, body.speaker_label, person_id)
        kdb.conn.execute(
            """
            UPDATE arctone_people
            SET name = ?,
                language_code = ?,
                language_name = ?,
                target_language_code = ?,
                target_language_name = ?,
                speaker_label = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                name,
                body.language_code,
                body.language_name,
                body.target_language_code,
                body.target_language_name,
                body.speaker_label,
                person_id,
            ),
        )
        kdb.conn.commit()
        row = kdb.conn.execute(
            """
            SELECT id, name, language_code, language_name, target_language_code,
                   target_language_name, speaker_label, authenticated, created_at, updated_at
            FROM arctone_people
            WHERE id = ?
            """,
            (person_id,),
        ).fetchone()
    return {"success": True, "person": _row_to_person(row)}


@router.delete("/people/{person_id}")
def delete_person(person_id: int, kdb: KorvoDep):
    with kdb.lock:
        cur = kdb.conn.execute("DELETE FROM arctone_people WHERE id = ?", (person_id,))
        kdb.conn.commit()
        deleted = cur.rowcount
    if deleted < 1:
        raise HTTPException(404, "Person not found")
    sample = ARCTONE_SAMPLES_DIR / f"{person_id}.bin"
    if sample.is_file():
        sample.unlink()
    return {"success": True}


@router.post("/people/{person_id}/sample")
async def store_voice_sample(person_id: int, request: Request, kdb: KorvoDep):
    """A stored recording is the only way authenticated becomes true."""
    data = await request.body()
    if len(data) < 4096 or len(data) > 2_000_000:
        raise HTTPException(400, "Voice sample must be between 4KB and 2MB")
    with kdb.lock:
        current = kdb.conn.execute("SELECT id FROM arctone_people WHERE id = ?", (person_id,)).fetchone()
        if not current:
            raise HTTPException(404, "Person not found")
        ARCTONE_SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
        (ARCTONE_SAMPLES_DIR / f"{person_id}.bin").write_bytes(data)
        kdb.conn.execute(
            "UPDATE arctone_people SET authenticated = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (person_id,),
        )
        kdb.conn.commit()
        row = kdb.conn.execute(
            """
            SELECT id, name, language_code, language_name, target_language_code,
                   target_language_name, speaker_label, authenticated, created_at, updated_at
            FROM arctone_people
            WHERE id = ?
            """,
            (person_id,),
        ).fetchone()
    return {"success": True, "person": _row_to_person(row)}
