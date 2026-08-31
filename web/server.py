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

# ========== Session 持久化配置（无 domain 限制，兼容 Render） ==========
app.config.update(
    SESSION_COOKIE_PATH='/',
    SESSION_COOKIE_SECURE=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax'
)
app.permanent_session_lifetime = timedelta(days=7)

# ========== 北京时间时区 ==========
BEIJING_TZ = timezone(timedelta(hours=8))
def now_beijing():
    return datetime.now(BEIJING_TZ)

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
                    item['created_at'] = now_beijing().isoformat()
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
                "created_at": now_beijing().isoformat(),
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
    now = now_beijing()
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

# ==================== 登录装饰器 ====================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'verified' not in session:
            return jsonify({"error": "请先登录"}), 401
        # 检查用户卡密是否过期（管理员无需检查）
        key_used = session.get('key_used')
        if key_used and 'is_admin' not in session:
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
        session['verified'] = True   # ✅ 关键修复：让管理员也能通过 login_required
        return jsonify({"ok": True})
    return jsonify({"error": "密码错误"}), 401

@app.route('/api/check_login', methods=['GET'])
def check_login():
    if 'verified' in session or 'is_admin' in session:
        # 检查用户卡密是否过期（管理员无需检查）
        if 'verified' in session and 'is_admin' not in session:
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
        return jsonify({"logged_in": True, "type": "admin" if 'is_admin' in session else "user"})
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
    now = now_beijing().isoformat()
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

# ==================== 业务接口（完整保留）====================
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
@login_required
def api_backtest():
    try:
        body = request.json or {}
        budget = float(body.get("budget", 100))
        risk = body.get("risk", "平衡")
        total_periods = int(body.get("total_periods", 500))

        if budget < 0:
            return jsonify({"error": "预算不能为负数"}), 400

        history = fetch_history(total_periods)
        if not history:
            return jsonify({"error": "无法获取开奖数据"}), 500

        train_window = max(30, min(500, len(history) // 2))
        result = run_backtest(history, train_window=train_window, budget=budget, risk=risk)
        result["total_used"] = len(history)

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
                "issue": d["issue"],
                "actual": d["actual"],
                "algo_cost": round(d["algo_eval"]["total_cost"], 2),
                "algo_payout": round(d["algo_eval"]["total_payout"], 2),
                "random_cost": round(d["random_eval"]["total_cost"], 2),
                "random_payout": round(d["random_eval"]["total_payout"], 2)
            })

        return jsonify({
            "train_window": result["train_window"],
            "n_test": result["n_test"],
            "budget": result["budget"],
            "risk": result["risk"],
            "totals": result["totals"],
            "play_stats": play_stats_clean,
            "details": details_clean,
            "total_used": result["total_used"],
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/train', methods=['POST'])
@login_required
def train_model():
    try:
        history = fetch_history(2000)
        if not history:
            return jsonify({"error": "无法获取历史数据"}), 500
        start = time.time()
        best_w, best_score = Predictor.train(history, generations=25, population=35)
        elapsed = time.time() - start
        return jsonify({
            "status": "success",
            "weights": best_w,
            "score": best_score,
            "elapsed": round(elapsed, 2),
            "periods_used": len(history)
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/quant', methods=['POST'])
@login_required
def start_quant():
    try:
        periods_raw = request.json.get("periods", "2000") if request.json else "2000"
        if str(periods_raw).strip().lower() in ("全部", "all", "0", ""):
            periods = 0
        else:
            periods = int(periods_raw)

        job_id = str(uuid.uuid4())
        _quant_jobs[job_id] = {"status": "running", "result": None, "error": None}
        print(f"[量化] 任务已创建: {job_id}")

        def run_job():
            try:
                sys.path.insert(0, os.path.dirname(__file__))
                import quant
                print(f"[量化] 任务 {job_id} 开始执行...")
                fd, tmp_path = tempfile.mkstemp(suffix='.json', prefix='quant_')
                os.close(fd)
                quant.run_quant_auto(periods=periods, budget=100.0, output_file=tmp_path, verbose=False)
                with open(tmp_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                _quant_jobs[job_id] = {"status": "done", "result": data, "error": None}
                print(f"[量化] 任务 {job_id} 完成")
            except Exception as e:
                traceback.print_exc()
                _quant_jobs[job_id] = {"status": "error", "result": None, "error": str(e)}
                print(f"[量化] 任务 {job_id} 失败: {e}")

        threading.Thread(target=run_job, daemon=True).start()
        return jsonify({"job_id": job_id, "status": "running"})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/quant/<job_id>', methods=['GET'])
@login_required
def get_quant_result(job_id):
    job = _quant_jobs.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(job)

@app.route('/api/ai_analyze', methods=['POST'])
@login_required
def ai_analyze():
    if not DEEPSEEK_API_KEY:
        return jsonify({
            "error": "请先配置 DeepSeek API Key（在 Render 环境变量中设置 DEEPSEEK_API_KEY）"
        }), 400

    try:
        body = request.json or {}
        periods = int(body.get("periods", 50))

        history = fetch_history(periods)
        if not history:
            return jsonify({"error": "无法获取开奖数据"}), 500

        sample_size = min(50, len(history))
        recent = history[:sample_size]

        recent_str = "\n".join([
            f"{h['issue']}: {h['nums'][0]}{h['nums'][1]}{h['nums'][2]}{h['nums'][3]}{h['nums'][4]}"
            for h in recent
        ])

        all_digits = []
        for h in history:
            all_digits.extend(h['nums'][:4])
        from collections import Counter
        cnt = Counter(all_digits)
        hot = [str(d) for d, _ in cnt.most_common(5)]
        cold = [str(d) for d, _ in cnt.most_common()[:-6:-1] if d not in hot]

        weights = Predictor.WEIGHTS
        weights_str = ", ".join([f"{k}={v:.2f}" for k, v in weights.items()])

        prompt = f"""你是排列五彩票数据分析专家。请基于以下信息给出策略建议：

最近 {sample_size} 期开奖号码（万位+千位+百位+十位）：
{recent_str}

基于 {len(history)} 期历史数据统计：
热号（出现频率最高5个）：{', '.join(hot) if hot else '无'}
冷号（出现频率最低5个）：{', '.join(cold) if cold else '无'}

当前模型权重配置：
{weights_str}

请从以下方面给出建议（总字数不超过300字）：
1. 权重调整建议（哪些因子应该提高/降低）
2. 当前适合保守还是激进玩法
3. 最近走势特征（热号、冷号、趋势）
4. 风险提示

要求：简洁明了，用中文回复，不要预测具体号码。"""

        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        }
        data = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 500,
            "temperature": 0.7,
        }

        resp = requests.post(DEEPSEEK_API_URL, headers=headers, json=data, timeout=30)
        resp.raise_for_status()
        result = resp.json()

        analysis = result["choices"][0]["message"]["content"]
        tokens_used = result.get("usage", {}).get("total_tokens", 0)

        return jsonify({
            "analysis": analysis,
            "tokens_used": tokens_used,
            "periods_used": len(history),
        })

    except requests.exceptions.Timeout:
        return jsonify({"error": "AI 服务请求超时，请稍后重试"}), 500
    except requests.exceptions.RequestException as e:
        traceback.print_exc()
        return jsonify({"error": f"AI 服务请求失败: {str(e)}"}), 500
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"AI 分析失败: {str(e)}"}), 500

@app.route('/api/export_csv', methods=['POST'])
@login_required
def export_csv():
    try:
        body = request.json or {}
        periods = int(body.get("periods", 50))
        history = fetch_history(periods)
        if not history:
            return jsonify({"error": "无法获取数据"}), 500
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["期号", "日期", "万位", "千位", "百位", "十位", "个位"])
        for h in history:
            writer.writerow([h["issue"], h["date"], *h["nums"]])
        csv_data = output.getvalue()
        return jsonify({"csv": csv_data})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

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
