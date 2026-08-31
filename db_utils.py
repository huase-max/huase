# -*- coding: utf-8 -*-
"""db_utils.py — 数据库读写工具（SQLite 版本）"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "lottery.db")

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def load_history(count: int = 100):
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT issue, open_date, d1, d2, d3, d4, d5 "
            "FROM draws ORDER BY open_date DESC LIMIT ?",
            (count,)
        )
        rows = cur.fetchall()
        return [
            {
                "issue": row["issue"],
                "date": row["open_date"],
                "nums": [row["d1"], row["d2"], row["d3"], row["d4"], row["d5"]],
            }
            for row in rows
        ]
    finally:
        conn.close()
