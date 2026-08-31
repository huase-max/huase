# -*- coding: utf-8 -*-
import sys
import os
import json
import uuid
import tempfile
import threading
import requests
import traceback
import time
import csv
import io
import secrets
import string
from datetime import datetime, timedelta  # 新增导入

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request, jsonify, send_from_directory

from predictor import (
    fetch_history, Predictor,
    make_recommendations, make_custom_recommendations,
    calculate_budget_plans, calculate_custom_budget_plans,
    run_backtest,
    PAYOUT_RATIO, PROB_XIAN, RISK_PROFILES, RISK_DESC
)

app = Flask(__name__, static_folder='.', static_url_path='')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUSTOM_CONFIG_PATH = os.path.join(BASE_DIR, "custom_config.json")
KEYS_FILE = os.path.join(BASE_DIR, "keys.json")

_quant_jobs = {}

# ==================== AI 配置 ====================
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# ==================== 内存缓存 ====================
cache = {}
CACHE_EXPIRE = 3600

# ==================== 卡密管理（已升级）====================
def load_keys():
    if not os.path.exists(KEYS_FILE):
        return []
    try:
        with open(KEYS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # 向后兼容：为旧卡密补全字段
            for item in data:
                if 'created_at' not in item:
                    item['created_at'] = datetime.now().isoformat()
                if 'duration' not in item:
                    item['duration'] = 0   # 0 表示永久有效
            return data
    except:
        return []

def save_keys(keys):
    with open(KEYS_FILE, 'w', encoding='utf-8') as f:
        json.dump(keys, f, ensure_ascii=False, indent=2)

def init_keys():
    keys = load_keys()
    if not keys:
        alphabet = string.ascii_letters + string.digits
        for _ in range(5):
            new_key = ''.join(secrets.choice(alphabet) for _ in range(16))
            keys.append({
                "key": new_key,
                "used": False,
                "created_at": datetime.now().isoformat(),
                "duration": 30   # 默认30天
            })
        save_keys(keys)
        print("[卡密] 已生成 5 个默认卡密（有效期30天）")
init_keys()

def _is_expired(item):
    """判断卡密是否过期，返回 (是否过期, 剩余天数)"""
    if item.get('duration', 0) == 0:
        return False, None  # 永久
    created = datetime.fromisoformat(item['created_at'])
    expire_time = created + timedelta(days=item['duration'])
    now = datetime.now()
    if now > expire_time:
        return True, 0
    remain = (expire_time - now).days
    return False, remain

# ==================== 自定义配置管理 ====================
def _load_custom_config():
    if os.path.exists(CUSTOM_CONFIG_PATH):
        try:
            with open(CUSTOM_CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {
        "enabled": False,
        "positions": [[1,2,3,4,5], [6,7,8,9,0]],
        "weights": {"pos": [1,1,1,1,1], "digit": [1,1,1,1,1,1,1,1,1,1]},
        "budget": 100,
        "risk": "平衡"
    }

def _save_custom_config(config):
    with open(CUSTOM_CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

# ==================== 辅助函数 ====================
def _rec_to_json(rec):
    return {
        "pos": rec["pos"],
        "digit": rec["digit"],
        "score": rec.get("score", 0)
    }

# ==================== 路由 ====================

@app.route('/')
def index():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    index_path = os.path.join(current_dir, 'index.html')
    if os.path.exists(index_path):
        with open(index_path, 'r', encoding='utf-8') as f:
            return f.read()
    return "index.html 文件未找到", 404


@app.route('/api/verify', methods=['POST'])
def verify_key():
    """验证卡密（升级：检查有效期）"""
    data = request.json or {}
    key = data.get("key", "").strip()
    if not key:
        return jsonify({"ok": False, "error": "请输入卡密"}), 401

    keys = load_keys()
    for item in keys:
        if item["key"] == key:
            if item.get("used", False):
                return jsonify({"ok": False, "error": "卡密已被使用"}), 401
            expired, remain = _is_expired(item)
            if expired:
                return jsonify({"ok": False, "error": "卡密已过期"}), 401
            item["used"] = True
            save_keys(keys)
            return jsonify({"ok": True, "remain_days": remain})
    return jsonify({"ok": False, "error": "卡密无效"}), 401


@app.route('/api/generate_keys', methods=['POST'])
def generate_keys():
    """生成新卡密（升级：支持有效期）"""
    data = request.json or {}
    count = data.get("count", 5)
    admin_key = data.get("admin", "")
    duration = data.get("duration", 30)   # 默认30天
    if admin_key != "admin123456":
        return jsonify({"error": "管理员密码错误"}), 401
    if count > 20:
        return jsonify({"error": "一次最多生成20个"}), 400
    if duration not in [1, 30, 180, 365]:
        return jsonify({"error": "有效期必须为 1, 30, 180, 365 天之一"}), 400

    alphabet = string.ascii_letters + string.digits
    keys = load_keys()
    generated = []
    now = datetime.now().isoformat()
    for _ in range(count):
        new_key = ''.join(secrets.choice(alphabet) for _ in range(16))
        keys.append({
            "key": new_key,
            "used": False,
            "created_at": now,
            "duration": duration
        })
        generated.append(new_key)
    save_keys(keys)
    return jsonify({"keys": generated, "count": len(generated)})


@app.route('/api/list_keys', methods=['GET'])
def list_keys():
    """查看所有卡密（升级：返回状态和剩余天数）"""
    admin_key = request.args.get("admin", "")
    if admin_key != "admin123456":
        return jsonify({"error": "管理员密码错误"}), 401
    keys = load_keys()
    result = []
    for item in keys:
        expired, remain = _is_expired(item)
        status = "已用" if item.get("used", False) else ("已过期" if expired else "有效")
        result.append({
            "key": item["key"],
            "used": item.get("used", False),
            "duration": item["duration"],
            "created_at": item["created_at"],
            "status": status,
            "remain_days": remain if not expired and not item.get("used", False) else 0
        })
    return jsonify(result)


@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(_load_custom_config())


@app.route('/api/config', methods=['POST'])
def save_config():
    try:
        _save_custom_config(request.json)
        return jsonify({"ok": True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/predict', methods=['POST'])
def api_predict():
    # ---------- 您的完整原始实现，完全保留 ----------
    try:
        body = request.json or {}
        budget = float(body.get("budget", 100))
        risk = body.get("risk", "平衡")
        use_custom = bool(body.get("use_custom", False))
        use_ai = bool(body.get("use_ai", False))
        periods = int(body.get("periods", 50))

        if budget < 0:
            return jsonify({"error": "预算不能为负数"}), 400

        cache_key = f"history_{periods}"
        if cache_key in cache and time.time() - cache[cache_key]['time'] < CACHE_EXPIRE:
            history = cache[cache_key]['data']
        else:
            history = fetch_history(periods)
            cache[cache_key] = {'data': history, 'time': time.time()}

        if not history:
            return jsonify({"error": "无法获取开奖数据"}), 500

        Predictor.auto_train_if_needed(history)

        latest = history[0]
        predictor = Predictor(history)

        if use_ai:
            try:
                predictor.auto_optimize(generations=12, population=15)
                ai_info = "已启用 AI 权重优化"
            except Exception as e:
                traceback.print_exc()
                return jsonify({"error": f"AI 优化失败: {str(e)}"}), 500
        else:
            ai_info = "未启用"

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

        recent = [{"issue": h["issue"], "date": h["date"], "nums": h["nums"]} for h in history[:10]]

        return jsonify({
            "latest": {"issue": latest["issue"], "date": latest["date"], "nums": latest["nums"]},
            "recommendations": _rec_to_json(rec),
            "budget_plans": plans_clean,
            "pos_scores": pos_scores,
            "digit_scores": digit_scores,
            "recent_history": recent,
            "ai_info": ai_info,
            "total_periods": len(history),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/backtest', methods=['POST'])
def api_backtest():
    # ---------- 您原有的回测接口（保留原有内容，仅补全函数体）----------
    # 由于您原始代码中该函数只有定义头，没有完整实现，此处保留占位。
    # 您可以将自己的实现复制到此处。
    try:
        body = request.json or {}
        # 示例：调用 run_backtest 进行回测
        # result = run_backtest(...)
        return jsonify({"ok": True, "message": "回测接口待完善", "body": body})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ==================== 启动服务 ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
