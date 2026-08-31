# -*- coding: utf-8 -*-
"""sync_data.py — 数据同步脚本（SQLite 版，API 获取）"""
import json
import re
import sys
import random
import sqlite3
import os
import requests

API_URL = "https://jc.zhcw.com/port/client_json.php"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.zhcw.com/kjxx/pl5/",
}
DB_PATH = os.path.join(os.path.dirname(__file__), "lottery.db")
PAGE_SIZE = 100

def db_connect():
    return sqlite3.connect(DB_PATH)

def ensure_tables():
    conn = db_connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS draws (
                issue TEXT PRIMARY KEY,
                open_date TEXT NOT NULL,
                d1 INTEGER NOT NULL, d2 INTEGER NOT NULL,
                d3 INTEGER NOT NULL, d4 INTEGER NOT NULL, d5 INTEGER NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()

def fetch_page(end_issue=""):
    params = {
        "transactionType": "10001001", "lotteryId": "284",
        "issueCount": str(PAGE_SIZE), "pageNum": "1", "pageSize": str(PAGE_SIZE),
        "startIssue": "", "endIssue": end_issue,
        "startDate": "", "endDate": "", "type": "1",
        "tt": str(random.random()), "callback": "cb",
    }
    resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    text = resp.text.strip()
    m = re.match(r"^\w+\((.*)\)\s*;?\s*$", text, re.DOTALL)
    if not m:
        return []
    payload = json.loads(m.group(1))
    return payload.get("data", []) or []

def save_to_db(rows):
    conn = db_connect()
    cnt = 0
    try:
        for row in rows:
            nums_raw = row.get("frontWinningNum", "").strip()
            parts = nums_raw.split()
            if len(parts) != 5 or not all(p.isdigit() for p in parts):
                continue
            conn.execute(
                "INSERT OR IGNORE INTO draws (issue, open_date, d1, d2, d3, d4, d5) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["issue"], row["openTime"],
                 int(parts[0]), int(parts[1]), int(parts[2]),
                 int(parts[3]), int(parts[4]))
            )
            if conn.total_changes > 0:
                cnt += 1
        conn.commit()
    finally:
        conn.close()
    return cnt

def full_sync(target: int):
    print(f"[SYNC] 目标: {target} 期, 分页大小: {PAGE_SIZE}")
    ensure_tables()
    total_saved = 0
    old_issue = ""
    page = 0
    while total_saved < target:
        page += 1
        rows = fetch_page(old_issue)
        if not rows:
            break
        cnt = save_to_db(rows)
        total_saved += cnt
        new_old = rows[-1]["issue"]
        print(f"  第{page:3d}页: {len(rows)}条 API → {cnt}条新入库 | "
              f"累计{total_saved}期 | {rows[0]['issue']}→{rows[-1]['issue']}")
        if new_old == old_issue:
            break
        old_issue = new_old
        if page >= (target // PAGE_SIZE + 20):
            break
    print(f"\n[SYNC] 完成! 共入库 {total_saved} 期")
    return total_saved

def daily_sync():
    print("[DAILY] 拉取最新一期...")
    ensure_tables()
    rows = fetch_page("")
    if rows:
        cnt = save_to_db(rows[:1])
        if cnt:
            print(f"  新增: {rows[0]['issue']} ({rows[0]['openTime']}) "
                  f"{rows[0]['frontWinningNum']}")
        else:
            print(f"  无新增 (期号 {rows[0]['issue']} 已存在)")
    else:
        print("  未拉取到数据")

def main():
    print("=" * 50)
    print("  排列五数据同步 (SQLite)")
    print("=" * 50)
    if len(sys.argv) > 1 and sys.argv[1] == "--daily":
        daily_sync()
    else:
        target = 2000
        if len(sys.argv) > 1:
            try:
                target = int(sys.argv[1])
            except ValueError:
                print("Usage: python sync_data.py [数量]  或  python sync_data.py --daily")
                sys.exit(1)
        full_sync(target)

if __name__ == "__main__":
    main()
