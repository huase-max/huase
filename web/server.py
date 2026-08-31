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
from datetime import datetime, timedelta, timezone
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
app.secret_key = os.environ.get('SECRET_KEY', 'your-fixed-secret-key-change-in-production')

app.config.update(
    SESSION_COOKIE_PATH='/',
    SESSION_COOKIE_SECURE=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax'
)
app.permanent_session_lifetime = timedelta(days=7)

BEIJING_TZ = timezone(timedelta(hours=8))
def now_beijing():
    return datetime.now(BEIJING_TZ)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUSTOM_CONFIG_PATH = os.path.join(BASE_DIR, "custom_config.json")
KEYS_FILE = os.path.join(BASE_DIR, "keys.json")

_quant_jobs = {}

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

cache = {}
CACHE_EXPIRE = 3600

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Aa1176760244")

def load_keys():
    if not os.path.exists(KEYS_FILE):
        return []
    try:
        with open(KEYS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            for item in data:
                if 'created_at' not in item:
                    item['created_at'] = now_beijing().isoformat()
                if 'duration' not in item:
                    item['duration'] = 0
                if 'used_at' not in item:
                    item['used_at'] = None
                if 'revoked' not in item:
                    item['revoked'] = False
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
                "created_at": now_beijing().isoformat(),
                "duration": 30,
                "used_at": None,
                "revoked": False
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
    now = now_beijing()
    if now > expire_time:
        return True, 0
    if duration in [1, 30, 180, 365]:
        remain = (expire_time - now).days
    else:
        remain = int((expire_time - now).total_seconds() // 60)
    return False, remain

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

# ==================== 登录装饰器 ====================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # 只检查 verified，管理员不能通过这里
        if 'verified' not in session:
            return jsonify({"error": "请先登录"}), 401
        key_used = session.get('key_used')
        if key_used:
            keys = load_keys()
            for item in keys:
                if item['key'] == key_used:
                    if item.get('revoked', False):
                        session.clear()
                        return jsonify({"error": "卡密已被管理员作废，请重新输入"}), 401
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

@app.route('/api/verify', methods=['POST'])
def verify_key():
    data = request.json or {}
    key = data.get("key", "").strip()
    if not key:
        return jsonify({"ok": False, "error": "请输入卡密"}), 401

    keys = load_keys()
    for item in keys:
        if item["key"] == key:
            if item.get("used", False):
                return jsonify({"ok": False, "error": "卡密已被使用"}), 401
            if item.get('revoked', False):
                return jsonify({"ok": False, "error": "卡密已被管理员作废"}), 401
            item["used"] = True
            item["used_at"] = now_beijing().isoformat()
            save_keys(keys)
            session.permanent = True
            session['verified'] = True
            session['key_used'] = key
            expired, remain = _is_expired(item)
            return jsonify({"ok": True, "remain_days": remain})
    return jsonify({"ok": False, "error": "卡密无效"}), 401

@app.route('/api/admin_login', methods=['POST'])
def admin_login():
    data = request.json or {}
    password = data.get("password", "").strip()
    if password == ADMIN_PASSWORD:
        session.permanent = True
        session['is_admin'] = True
        # 关键修改：不再设置 verified，防止前台误用
        return jsonify({"ok": True})
    return jsonify({"error": "密码错误"}), 401

@app.route('/api/check_login', methods=['GET'])
def check_login():
    if 'verified' in session:
        # 普通用户登录
        key_used = session.get('key_used')
        if key_used:
            keys = load_keys()
            for item in keys:
                if item['key'] == key_used:
                    if item.get('revoked', False):
                        session.clear()
                        return jsonify({"logged_in": False})
                    expired, _ = _is_expired(item)
                    if expired:
                        session.clear()
                        return jsonify({"logged_in": False})
                    break
        return jsonify({"logged_in": True, "type": "user"})
    elif 'is_admin' in session:
        # 管理员登录
        return jsonify({"logged_in": True, "type": "admin"})
    else:
        return jsonify({"logged_in": False})

@app.route('/api/check_key_validity', methods=['GET'])
@login_required
def check_key_validity():
    """供前端轮询检测当前卡密是否仍有效"""
    # 只有普通用户会调用，管理员不会
    key_used = session.get('key_used')
    if not key_used:
        return jsonify({"valid": False})
    keys = load_keys()
    for item in keys:
        if item['key'] == key_used:
            if item.get('revoked', False):
                session.clear()
                return jsonify({"valid": False})
            expired, _ = _is_expired(item)
            if expired:
                session.clear()
                return jsonify({"valid": False})
            return jsonify({"valid": True})
    return jsonify({"valid": False})

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
    # 作废所有旧卡密
    for item in keys:
        item['revoked'] = True
    save_keys(keys)
    # 生成新卡密
    generated = []
    now = now_beijing().isoformat()
    for _ in range(count):
        new_key = ''.join(secrets.choice(alphabet) for _ in range(16))
        keys.append({
            "key": new_key,
            "used": False,
            "created_at": now,
            "duration": duration,
            "used_at": None,
            "revoked": False
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
            "remain_days": remain,
            "revoked": item.get('revoked', False)
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

# ==================== 业务接口（完整）====================
# 以下接口使用 @login_required，只允许普通用户访问
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
    # 完整代码与之前相同，此处省略，实际使用时请保留
    pass

@app.route('/api/backtest', methods=['POST'])
@login_required
def api_backtest():
    # 完整代码与之前相同
    pass

@app.route('/api/train', methods=['POST'])
@login_required
def train_model():
    # 完整代码
    pass

@app.route('/api/quant', methods=['POST'])
@login_required
def start_quant():
    # 完整代码
    pass

@app.route('/api/quant/<job_id>', methods=['GET'])
@login_required
def get_quant_result(job_id):
    # 完整代码
    pass

@app.route('/api/ai_analyze', methods=['POST'])
@login_required
def ai_analyze():
    # 完整代码
    pass

@app.route('/api/export_csv', methods=['POST'])
@login_required
def export_csv():
    # 完整代码
    pass

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
