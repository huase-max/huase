# -*- coding: utf-8 -*-
import sys
import os
import json
import uuid
import tempfile
import threading
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request, jsonify, send_from_directory

from predictor import (
    fetch_history, Predictor,
    make_recommendations, make_custom_recommendations,
    calculate_budget_plans, calculate_custom_budget_plans,
    run_backtest,
    PAYOUT_RATIO, PROB_XIAN, RISK_PROFILES, RISK_DESC
)
import sync_data

app = Flask(__name__, static_folder='.', static_url_path='')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUSTOM_CONFIG_PATH = os.path.join(BASE_DIR, "custom_config.json")
DB_PATH = os.path.join(BASE_DIR, "lottery.db")

_quant_jobs = {}

def auto_sync_if_needed():
    print("[AutoSync] 检查数据库状态...")
    if not os.path.exists(DB_PATH):
        print("[AutoSync] 数据库不存在，开始自动同步 2000 期...")
        sync_data.full_sync(2000)
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM draws")
        count = cur.fetchone()[0]
        conn.close()
        if count < 2000:
            print(f"[AutoSync] 当前仅有 {count} 期数据，少于 2000 期，开始自动同步...")
            sync_data.full_sync(2000)
        else:
            print(f"[AutoSync] 数据充足 (当前 {count} 期)")
    except Exception as e:
        print(f"[AutoSync] 检查数据库失败: {e}，尝试强制同步...")
        sync_data.full_sync(2000)

def _default_config():
    return {
        "enabled": ["二定包码", "三定包码", "二现", "三现"],
        "bao_pos": {
            "二定": [0, 3, 0, 3],
            "三定": [3, 3, 3, 0],
            "四定": [2, 2, 2, 2]
        },
        "xian_manual": {},
        "__active__": False
    }

def _load_custom_config():
    if not os.path.exists(CUSTOM_CONFIG_PATH):
        return _default_config()
    try:
        with open(CUSTOM_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "enabled": data.get("enabled", []),
            "bao_pos": data.get("bao_pos", _default_config()["bao_pos"]),
            "xian_manual": data.get("xian_manual", {}),
            "__active__": bool(data.get("__active__", False))
        }
    except Exception:
        return _default_config()

def _save_custom_config(config):
    data = {
        "__active__": bool(config.get("__active__", False)),
        "enabled": list(config.get("enabled", [])),
        "bao_pos": config.get("bao_pos", {}),
        "xian_manual": config.get("xian_manual", {})
    }
    with open(CUSTOM_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _rec_to_json(rec):
    result = {}
    for k, v in rec.items():
        if isinstance(v, dict):
            result[k] = {}
            for sub_k, sub_v in v.items():
                result[k][sub_k] = [
                    (p, list(ds) if isinstance(ds, list) else ds)
                    for p, ds in sub_v
                ]
        else:
            result[k] = list(v)
    return result

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(_load_custom_config())

@app.route('/api/config', methods=['POST'])
def save_config():
    try:
        _save_custom_config(request.json)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/predict', methods=['POST'])
def api_predict():
    try:
        body = request.json or {}
        budget = float(body.get("budget", 100))
        risk = body.get("risk", "平衡")
        use_custom = bool(body.get("use_custom", False))
        if budget < 0:
            return jsonify({"error": "预算不能为负数"}), 400
        history = fetch_history(50)
        if not history:
            return jsonify({"error": "无法获取开奖数据"}), 500
        latest = history[0]
        predictor = Predictor(history)
        if use_custom:
            config = body.get("custom_config", _load_custom_config())
            rec, pos_scores, digit_scores, enabled = make_custom_recommendations(predictor, config)
            budget_plans = calculate_custom_budget_plans(budget, rec, enabled)
        else:
            rec, pos_scores, digit_scores = make_recommendations(predictor)
            budget_plans = calculate_budget_plans(budget, rec, risk)
        plans_clean = {}
        for k, v in budget_plans.items():
            if k.startswith("__"):
                plans_clean[k] = v
            else:
                plans_clean[k] = {
                    "倍数": v["倍数"],
                    "组合数": v["组合数"],
                    "单份成本": v.get("单份成本", 0),
                    "实际投入": v["实际投入"],
                    "命中概率": v["命中概率"],
                    "单注赔付": v["单注赔付"],
                    "中奖金额": v["中奖金额"],
                    "净收益": v["净收益"]
                }
        recent = []
        for h in history[:10]:
            recent.append({
                "issue": h["issue"],
                "date": h["date"],
                "nums": h["nums"]
            })
        return jsonify({
            "latest": {
                "issue": latest["issue"],
                "date": latest["date"],
                "nums": latest["nums"]
            },
            "recommendations": _rec_to_json(rec),
            "budget_plans": plans_clean,
            "pos_scores": pos_scores,
            "digit_scores": digit_scores,
            "recent_history": recent
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/backtest', methods=['POST'])
def api_backtest():
    try:
        body = request.json or {}
        budget = float(body.get("budget", 100))
        risk = body.get("risk", "平衡")
        if budget < 0:
            return jsonify({"error": "预算不能为负数"}), 400
        history = fetch_history(100)
        if not history:
            return jsonify({"error": "无法获取开奖数据"}), 500
        result = run_backtest(history, train_window=50, budget=budget, risk=risk)
        play_stats_clean = {}
        for k, v in result["play_stats"].items():
            play_stats_clean[k] = {
                "algo_bets": v["algo_bets"],
                "algo_hits": v["algo_hits"],
                "algo_cost": round(v["algo_cost"], 2),
                "algo_payout": round(v["algo_payout"], 2),
                "algo_hit_rate": v.get("algo_hit_rate", 0),
                "algo_roi": v.get("algo_roi", 0),
                "random_bets": v["random_bets"],
                "random_hits": v["random_hits"],
                "random_cost": round(v["random_cost"], 2),
                "random_payout": round(v["random_payout"], 2),
                "random_hit_rate": v.get("random_hit_rate", 0),
                "random_roi": v.get("random_roi", 0)
            }
        details_clean = []
        for d in result["details"]:
            details_clean.append({
                "issue": d["i
