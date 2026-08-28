# -*- coding: utf-8 -*-
import sys
import os
import json
import uuid
import tempfile
import threading
import requests
import traceback

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

_quant_jobs = {}

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"


def _default_config():
    return {
        "enabled": ["二定包码", "三定包码", "二现", "三现"],
        "bao_pos": {"二定": [0, 3, 0, 3], "三定": [3, 3, 3, 0], "四定": [2, 2, 2, 2]},
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
                result[k][sub_k] = [(p, list(ds) if isinstance(ds, list) else ds) for p, ds in sub_v]
        else:
            result[k] = list(v)
    return result


@app.route('/')
def index():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    index_path = os.path.join(current_dir, 'index.html')
    if os.path.exists(index_path):
        with open(index_path, 'r', encoding='utf-8') as f:
            return f.read()
    return "index.html 文件未找到", 404


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
    try:
        body = request.json or {}
        budget = float(body.get("budget", 100))
        risk = body.get("risk", "平衡")
        use_custom = bool(body.get("use_custom", False))
        use_ai = bool(body.get("use_ai", False))

        if budget < 0:
            return jsonify({"error": "预算不能为负数"}), 400

        history = fetch_history(50)
        if not history:
            return jsonify({"error": "无法获取开奖数据"}), 500

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
        })
    except Exception as e:
        traceback.print_exc()
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
            "details": details_clean
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/quant', methods=['POST'])
def start_quant():
    try:
        periods_raw = request.json.get("periods", "2000") if request.json else "2000"
        if str(periods_raw).strip().lower() in ("全部", "all", "0", ""):
            periods = 0
        else:
            periods = int(periods_raw)

        job_id = str(uuid.uuid4())
        _quant_jobs[job_id] = {"status": "running", "result": None, "error": None}
        print(f"[量化] 任务已创建: {job_id}, 当前任务数: {len(_quant_jobs)}")  # 调试日志

        def run_job():
            try:
                sys.path.insert(0, os.path.dirname(__file__))
                import quant
                print(f"[量化] 任务 {job_id} 开始执行...")
                fd, tmp_path = tempfile.mkstemp(suffix='.json', prefix='quant_')
                os.close(fd)
                quant.run_quant(periods=periods, budget=100.0, output_file=tmp_path, verbose=False)
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
def get_quant_result(job_id):
    job = _quant_jobs.get(job_id)
    if not job:
        active_jobs = list(_quant_jobs.keys())
        print(f"[量化] 查询任务 {job_id} 不存在，当前任务: {active_jobs}")
        return jsonify({
            "error": f"任务不存在: {job_id}",
            "active_jobs": active_jobs[:10]
        }), 404
    return jsonify(job)


@app.route('/api/ai_analyze', methods=['POST'])
def ai_analyze():
    if not DEEPSEEK_API_KEY:
        return jsonify({
            "error": "请先配置 DeepSeek API Key（在 Render 环境变量中设置 DEEPSEEK_API_KEY）"
        }), 400

    try:
        history = fetch_history(50)
        if not history:
            return jsonify({"error": "无法获取开奖数据"}), 500

        recent = history[:10]
        recent_str = "\n".join([
            f"{h['issue']}: {h['nums'][0]}{h['nums'][1]}{h['nums'][2]}{h['nums'][3]}{h['nums'][4]}"
            for h in recent
        ])

        all_digits = []
        for h in recent:
            all_digits.extend(h['nums'][:4])
        from collections import Counter
        cnt = Counter(all_digits)
        hot = [str(d) for d, c in cnt.items() if c >= 3]
        cold = [str(d) for d, c in cnt.items() if c == 0]

        weights = Predictor.WEIGHTS
        weights_str = ", ".join([f"{k}={v:.2f}" for k, v in weights.items()])

        prompt = f"""你是排列五彩票数据分析专家。请基于以下信息给出策略建议：

最近10期开奖号码（万位+千位+百位+十位）：
{recent_str}

热号（出现≥3次）：{', '.join(hot) if hot else '无'}
冷号（出现0次）：{', '.join(cold) if cold else '无'}

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
        })

    except requests.exceptions.Timeout:
        return jsonify({"error": "AI 服务请求超时，请稍后重试"}), 500
    except requests.exceptions.RequestException as e:
        traceback.print_exc()
        return jsonify({"error": f"AI 服务请求失败: {str(e)}"}), 500
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"AI 分析失败: {str(e)}"}), 500


if __name__ == '__main__':
    print("=" * 50)
    print("  排列五预测器 Web 服务")
    print("  打开浏览器访问 http://localhost:5173")
    print("=" * 50)
    port = int(os.environ.get("PORT", 5173))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
