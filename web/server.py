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
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, session
from functools import wraps

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from predictor import (
    fetch_history, Predictor,
    make_recommendations, make_custom_recommendations,
    calculate_budget_plans, calculate_custom_budget_plans,
    run_backtest,
    PAYOUT_RATIO, PROB_XIAN, RISK_PROFILES, RISK_DESC
)

app = Flask(__name__, static_folder='.', static_url_path='')
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-please-change-in-production')
# 设置 session 持久化时间（7天）
app.permanent_session_lifetime = timedelta(days=7)

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

# ==================== 管理员密码 ====================
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Aa1176760244")

# ==================== 卡密管理 ====================
def load_keys():
    if not os.path.exists(KEYS_FILE):
        return []
    try:
        with open(KEYS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            for item in data:
                if 'created_at' not in item:
                    item['created_at'] = datetime.now().isoformat()
                if 'duration' not in item:
                    item['duration'] = 0
                if 'used_at' not in item:
                    item['used_at'] = None
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
                "duration": 30,
                "used_at": None
            })
        save_keys(keys)
        print("[卡密] 已生成 5 个默认卡密（有效期30天）")
init_keys()

def _is_expired(item):
    if not item.get('used', False):
        return False, None
    if item.get('duration', 0) == 0:
        return False, None
    used_at_str = item.get('used_at')
    if not used_at_str:
        return True, 0
    used_at = datetime.fromisoformat(used_at_str)
    duration = item['duration']
    if duration in [1, 30, 180, 365]:
        expire_time = used_at + timedelta(days=duration)
    else:
        expire_time = used_at + timedelta(minutes=duration)
    now = datetime.now()
    if now > expire_time:
        return True, 0
    if duration in [1, 30, 180, 365]:
        remain = (expire_time - now).days
    else:
        remain = int((expire_time - now).total_seconds() // 60)
    return False, remain

# ==================== 配置管理 ====================
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

def _rec_to_json(rec):
    if rec is None:
        return {"pos": [], "digit": [], "score": 0}
    return rec

# ==================== 登录装饰器（增加卡密过期检查）====================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'verified' not in session:
            return jsonify({"error": "请先登录"}), 401
        # 检查卡密是否过期
        key_used = session.get('key_used')
        if key_used:
            keys = load_keys()
            for item in keys:
                if item['key'] == key_used:
                    expired, _ = _is_expired(item)
                    if expired:
                        session.clear()
                        return jsonify({"error": "卡密已过期，请重新登录"}), 401
                    break
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'is_admin' not in session:
            return jsonify({"error": "需要管理员权限"}), 403
        return f(*args, **kwargs)
    return decorated

# ==================== 路由 ====================

@app.route('/')
def index():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    index_path = os.path.join(current_dir, 'index.html')
    if os.path.exists(index_path):
        with open(index_path, 'r', encoding='utf-8') as f:
            return f.read()
    return "index.html 文件未找到", 404

@app.route('/admin_panel_7f3a9b2c1d')
def admin_panel():
    return send_from_directory('.', 'admin.html')

# ==================== 登录/验证接口 ====================
@app.route('/api/verify', methods=['POST'])
def verify_key():
    """用户验证卡密，验证成功后建立 Session"""
    data = request.json or {}
    key = data.get("key", "").strip()
    if not key:
        return jsonify({"ok": False, "error": "请输入卡密"}), 401

    keys = load_keys()
    for item in keys:
        if item["key"] == key:
            if item.get("used", False):
                return jsonify({"ok": False, "error": "卡密已被使用"}), 401
            # 未使用，立即检查是否过期？未使用不过期，直接通过
            item["used"] = True
            item["used_at"] = datetime.now().isoformat()
            save_keys(keys)
            session.permanent = True
            session['verified'] = True
            session['key_used'] = key
            expired, remain = _is_expired(item)
            return jsonify({"ok": True, "remain_days": remain})
    return jsonify({"ok": False, "error": "卡密无效"}), 401

@app.route('/api/admin_login', methods=['POST'])
def admin_login():
    """管理员登录（通过密码）建立 Session"""
    data = request.json or {}
    password = data.get("password", "").strip()
    if password == ADMIN_PASSWORD:
        session.permanent = True
        session['is_admin'] = True
        return jsonify({"ok": True})
    return jsonify({"error": "密码错误"}), 401

@app.route('/api/check_login', methods=['GET'])
def check_login():
    """检查当前登录状态"""
    if 'verified' in session:
        # 额外检查卡密是否过期，若过期则清除session
        key_used = session.get('key_used')
        if key_used:
            keys = load_keys()
            for item in keys:
                if item['key'] == key_used:
                    expired, _ = _is_expired(item)
                    if expired:
                        session.clear()
                        return jsonify({"logged_in": False})
                    break
        return jsonify({"logged_in": True, "type": "user"})
    elif 'is_admin' in session:
        return jsonify({"logged_in": True, "type": "admin"})
    else:
        return jsonify({"logged_in": False})

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"ok": True})

# ==================== 卡密管理（管理员）====================
@app.route('/api/generate_keys', methods=['POST'])
@admin_required
def generate_keys():
    data = request.json or {}
    count = data.get("count", 5)
    duration = data.get("duration", 30)
    if count > 20:
        return jsonify({"error": "一次最多生成20个"}), 400
    if duration <= 0:
        return jsonify({"error": "有效期必须为正数"}), 400
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
            "duration": duration,
            "used_at": None
        })
        generated.append(new_key)
    save_keys(keys)
    return jsonify({"keys": generated, "count": len(generated)})

@app.route('/api/list_keys', methods=['GET'])
@admin_required
def list_keys():
    keys = load_keys()
    result = []
    for item in keys:
        expired, remain = _is_expired(item)
        if item.get("used", False):
            status = "已用" if not expired else "已过期"
            if expired:
                remain = 0
        else:
            status = "有效"
            remain = None
        result.append({
            "key": item["key"],
            "used": item.get("used", False),
            "duration": item["duration"],
            "created_at": item["created_at"],
            "used_at": item.get("used_at"),
            "status": status,
            "remain_days": remain
        })
    return jsonify(result)

@app.route('/api/delete_key', methods=['DELETE'])
@admin_required
def delete_key():
    key = request.args.get('key')
    if not key:
        return jsonify({"error": "缺少卡密参数"}), 400
    keys = load_keys()
    new_keys = [k for k in keys if k['key'] != key]
    if len(new_keys) == len(keys):
        return jsonify({"error": "卡密不存在"}), 404
    save_keys(new_keys)
    return jsonify({"ok": True})

# ==================== 业务接口（需登录）====================
# 以下接口都加了 @login_required，会检查卡密是否过期
@app.route('/api/config', methods=['GET'])
@login_required
def get_config():
    return jsonify(_load_custom_config())

@app.route('/api/config', methods=['POST'])
@login_required
def save_config():
    try:
        _save_custom_config(request.json)
        return jsonify({"ok": True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/predict', methods=['POST'])
@login_required
def api_predict():
    # 原有完整实现，此处省略，实际请保留全部代码
    # 注意：所有业务接口都要添加 @login_required
    pass

# ... 其他接口（backtest, train, quant, ai_analyze, export_csv）同样添加 @login_required
# 由于篇幅，此处省略，但您需要确保所有业务接口都添加了 @login_required 装饰器。

# ==================== 启动 ====================
if __name__ == '__main__':
    if not os.path.exists("best_weights.json"):
        print("首次启动，自动训练初始权重...")
        try:
            history = fetch_history(500)
            if history:
                Predictor.train(history, generations=15, population=20)
                Predictor._save_last_train_info(len(history))
        except Exception as e:
            print(f"初始训练失败: {e}")

    print("=" * 50)
    print("  海南排列五预测器 Web 服务")
    print("  打开浏览器访问 http://localhost:5173")
    print("=" * 50)
    port = int(os.environ.get("PORT", 5173))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
