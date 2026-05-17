"""SQLite persistence layer for jobs, applications, and agent sessions.

All data is stored in ``data/jobs.db`` relative to the project root.
Tables are created automatically on first access via :func:`get_db`.
"""

import sqlite3
import json
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "jobs.db")


def get_db():
    """Open (or create) the SQLite database and return a connection with WAL mode enabled."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_tables(conn)
    return conn


def _ensure_tables(conn):
    """Create the jobs, applications, and sessions tables if they don't already exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_url TEXT UNIQUE,
            job_key TEXT,
            title TEXT,
            company TEXT,
            location TEXT,
            salary TEXT,
            job_type TEXT,
            description TEXT,
            easy_apply INTEGER DEFAULT 0,
            timestamp_found TEXT DEFAULT (datetime('now')),
            score REAL,
            score_reasoning TEXT,
            status TEXT DEFAULT 'found',
            applied_at TEXT,
            form_steps_completed INTEGER,
            error_message TEXT,
            distance_miles REAL,
            commute_minutes REAL,
            distance_status TEXT
        );

        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER REFERENCES jobs(id),
            resume_used TEXT,
            answers_json TEXT,
            submitted_at TEXT DEFAULT (datetime('now')),
            confirmation_seen INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt TEXT,
            parsed_intent TEXT,
            started_at TEXT DEFAULT (datetime('now')),
            summary TEXT
        );
    """)
    conn.commit()

    # Migration: add distance columns if they don't exist (for existing DBs)
    _maybe_add_column(conn, "jobs", "distance_miles", "REAL")
    _maybe_add_column(conn, "jobs", "commute_minutes", "REAL")
    _maybe_add_column(conn, "jobs", "distance_status", "TEXT")


def _maybe_add_column(conn, table: str, column: str, col_type: str):
    """Add a column to a table if it doesn't already exist."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.commit()


def save_job(conn, job: dict) -> int:
    """Insert or update a job row. Returns the row ID."""
    cur = conn.execute("""
        INSERT INTO jobs (job_url, job_key, title, company, location, salary, job_type,
                          description, easy_apply, score, score_reasoning, status,
                          applied_at, form_steps_completed, error_message,
                          distance_miles, commute_minutes, distance_status)
        VALUES (:job_url, :job_key, :title, :company, :location, :salary, :job_type,
                :description, :easy_apply, :score, :score_reasoning, :status,
                :applied_at, :form_steps_completed, :error_message,
                :distance_miles, :commute_minutes, :distance_status)
        ON CONFLICT(job_url) DO UPDATE SET
            score=excluded.score,
            score_reasoning=excluded.score_reasoning,
            status=excluded.status,
            applied_at=excluded.applied_at,
            form_steps_completed=excluded.form_steps_completed,
            error_message=excluded.error_message,
            distance_miles=excluded.distance_miles,
            commute_minutes=excluded.commute_minutes,
            distance_status=excluded.distance_status
    """, {
        "job_url": job.get("job_url"),
        "job_key": job.get("job_key"),
        "title": job.get("title"),
        "company": job.get("company"),
        "location": job.get("location"),
        "salary": job.get("salary"),
        "job_type": job.get("job_type"),
        "description": job.get("description"),
        "easy_apply": 1 if job.get("easy_apply") else 0,
        "score": job.get("score"),
        "score_reasoning": job.get("score_reasoning"),
        "status": job.get("status", "found"),
        "applied_at": job.get("applied_at"),
        "form_steps_completed": job.get("form_steps_completed"),
        "error_message": job.get("error_message"),
        "distance_miles": job.get("distance_miles"),
        "commute_minutes": job.get("commute_minutes"),
        "distance_status": job.get("distance_status"),
    })
    conn.commit()
    return cur.lastrowid


def save_application(conn, job_id: int, resume_path: str, answers: dict):
    """Record a completed application with the answers given."""
    conn.execute("""
        INSERT INTO applications (job_id, resume_used, answers_json)
        VALUES (?, ?, ?)
    """, (job_id, resume_path, json.dumps(answers)))
    conn.commit()


def save_session(conn, prompt: str, parsed_intent: str, summary: str):
    """Log an agent run (original prompt, parsed intent JSON, and outcome summary)."""
    conn.execute("""
        INSERT INTO sessions (prompt, parsed_intent, summary)
        VALUES (?, ?, ?)
    """, (prompt, parsed_intent, summary))
    conn.commit()


def is_already_applied(conn, job_url: str) -> bool:
    """Check if we've already applied to this job."""
    row = conn.execute(
        "SELECT status FROM jobs WHERE job_url = ? AND status = 'applied'",
        (job_url,)
    ).fetchone()
    return row is not None
