# -*- coding: utf-8 -*-
"""排列五预测器 - 核心预测引擎"""
import json
import os
import re
import math
import random
import threading
import tkinter as tk
import unicodedata
from tkinter import ttk, scrolledtext, messagebox
from collections import Counter
from itertools import combinations

import pymysql
import requests

def _vwidth(s: str) -> int:
    w = 0
    for c in s:
        if unicodedata.east_asian_width(c) in ("F", "W"):
            w += 2
        else:
            w += 1
    return w

def _vpad(s: str, width: int, align: str = "left") -> str:
    pad = max(0, width - _vwidth(s))
    if align == "right":
        return " " * pad + s
    return s + " " * pad

API_URL = "https://jc.zhcw.com/port/client_json.php"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.zhcw.com/kjxx/pl5/",
}

PAYOUT_RATIO = {
    "二定": 96,
    "三定": 960,
    "四定": 9600,
    "二现": 9,
    "三现": 45,
    "四现": 320,
}

PROB_XIAN = {
    2: 0.0974,
    3: 0.0204,
    4: 0.0024,
}

RISK_PROFILES = {
    "保守": {
        "二定单码": 0.00,
        "三定单码": 0.00,
        "四定单码": 0.00,
        "二定包码": 0.30,
        "三定包码": 0.10,
        "四定包码": 0.00,
        "二现":     0.40,
        "三现":     0.20,
        "四现":     0.00,
    },
    "平衡": {
        "二定单码": 0.05,
        "三定单码": 0.05,
        "四定单码": 0.05,
        "二定包码": 0.10,
        "三定包码": 0.20,
        "四定包码": 0.10,
        "二现":     0.10,
        "三现":     0.25,
        "四现":     0.10,
    },
    "激进": {
        "二定单码": 0.00,
        "三定单码": 0.10,
        "四定单码": 0.25,
        "二定包码": 0.05,
        "三定包码": 0.10,
        "四定包码": 0.20,
        "二现":     0.00,
        "三现":     0.10,
        "四现":     0.20,
    },
}

RISK_DESC = {
    "保守": "高命中率优先 — 二现(9.74%)+二定包码(9%)为主，单注小额、回报稳定",
    "平衡": "六种玩法均衡分配 — 兼顾命中率与赔付倍数",
    "激进": "高赔付搏大奖 — 四定(9600倍)+四现(320倍)为主，命中率低但单中收益高",
}

DB_CONFIG = dict(host="localhost", user="root", password="root",
                 database="pl5_predictor", charset="utf8mb4")

def db_connect():
    return pymysql.connect(**DB_CONFIG)

def db_save_draws(history):
    if not history:
        return
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            for h in history:
                nums = h["nums"]
                cur.execute(
                    "INSERT IGNORE INTO draws (issue, open_date, d1, d2, d3, d4, d5) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (h["issue"], h["date"], nums[0], nums[1], nums[2], nums[3], nums[4])
                )
        conn.commit()
    finally:
        conn.close()

def db_save_prediction(target_issue, budget, risk, rec, plans, pos_scores, digit_scores):
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO predictions (target_issue, budget, risk, recommendations, "
                "budget_plans, pos_scores, digit_scores) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (target_issue, budget, risk,
                 json.dumps(rec, ensure_ascii=False),
                 json.dumps(plans, ensure_ascii=False),
                 json.dumps(pos_scores), json.dumps(digit_scores))
            )
        conn.commit()
    finally:
        conn.close()

def db_save_backtest(result):
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            t = result["totals"]
            algo_hits = {p: result["play_stats"][p]["algo_hits"] for p in result["play_stats"]}
            random_hits = {p: result["play_stats"][p]["random_hits"] for p in result["play_stats"]}
            cur.execute(
                "INSERT INTO backtests (train_window, test_periods, budget, risk, "
                "total_cost, total_payout, net_return, roi, algo_hit_rate, random_hit_rate, summary) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (result["train_window"], result["n_test"], result["budget"], result["risk"],
                 t["algo_cost"], t["algo_payout"], t["algo_net"], t["algo_roi"],
                 json.dumps(algo_hits, ensure_ascii=False),
                 json.dumps(random_hits, ensure_ascii=False),
                 json.dumps(result["play_stats"], ensure_ascii=False))
            )
            backtest_id = cur.lastrowid
            for d in result["details"]:
                cur.execute(
                    "INSERT INTO backtest_details (backtest_id, test_issue, actual_nums, "
                    "algo_recommendations, random_recommendations, algo_hits, random_hits, "
                    "cost, payout) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (backtest_id, d["issue"], d["actual"],
                     json.dumps(d["algo_rec"], ensure_ascii=False),
                     json.dumps(d["random_rec"], ensure_ascii=False),
                     json.dumps({k: v["hit"] for k, v in d["algo_eval"]["results"].items()}, ensure_ascii=False),
                     json.dumps({k: v["hit"] for k, v in d["random_eval"]["results"].items()}, ensure_ascii=False),
                     d["algo_eval"]["total_cost"], d["algo_eval"]["total_payout"])
                )
        conn.commit()
        return backtest_id
    finally:
        conn.close()

def _fetch_api(count: int = 50):
    params = {
        "transactionType": "10001001",
        "lotteryId": "284",
        "issueCount": str(count),
        "startIssue": "", "endIssue": "",
        "startDate": "", "endDate": "",
        "type": "0",
        "pageNum": "1", "pageSize": str(count),
        "tt": "0.123", "callback": "cb",
    }
    resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    text = resp.text.strip()
    m = re.match(r"^\w+\((.*)\)\s*;?\s*$", text, re.DOTALL)
    if not m:
        raise ValueError("接口响应不是预期的 JSONP 格式")
    payload = json.loads(m.group(1))
    rows = payload.get("data", []) or []
    history = []
    for row in rows:
        nums_raw = row.get("frontWinningNum", "").strip()
        parts = nums_raw.split()
        if len(parts) != 5 or not all(p.isdigit() for p in parts):
            continue
        history.append({
            "issue": row.get("issue", ""),
            "date": row.get("openTime", ""),
            "nums": [int(x) for x in parts],
        })
    if not history:
        raise ValueError("未取到任何开奖数据")
    return history

def fetch_history(count: int = 50):
    try:
        from db_utils import load_history
        data = load_history(count)
        if data and len(data) >= min(count, 10):
            return data
    except Exception:
        pass
    return _fetch_api(count)

class Predictor:
    POS_NAMES = ["千位", "百位", "十位", "个位"]
    SHORT_WINDOW = 10
    WEIGHTS = {
        "freq_long":  0.25,
        "freq_short": 0.30,
        "miss":       0.20,
        "markov":     0.25,
    }

    def __init__(self, history):
        if not history:
            r
