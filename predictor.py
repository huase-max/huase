# -*- coding: utf-8 -*-
"""排列五预测器 - 核心预测引擎（无GUI版，用于Web服务）"""
import json
import re
import math
import random
from collections import Counter

import requests

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

PROB_XIAN = {2: 0.0974, 3: 0.0204, 4: 0.0024}

RISK_PROFILES = {
    "保守": {
        "二定单码": 0.00, "三定单码": 0.00, "四定单码": 0.00,
        "二定包码": 0.30, "三定包码": 0.10, "四定包码": 0.00,
        "二现": 0.40, "三现": 0.20, "四现": 0.00,
    },
    "平衡": {
        "二定单码": 0.05, "三定单码": 0.05, "四定单码": 0.05,
        "二定包码": 0.10, "三定包码": 0.20, "四定包码": 0.10,
        "二现": 0.10, "三现": 0.25, "四现": 0.10,
    },
    "激进": {
        "二定单码": 0.00, "三定单码": 0.10, "四定单码": 0.25,
        "二定包码": 0.05, "三定包码": 0.10, "四定包码": 0.20,
        "二现": 0.00, "三现": 0.10, "四现": 0.20,
    },
}

RISK_DESC = {
    "保守": "高命中率优先 — 二现(9.74%)+二定包码(9%)为主，单注小额、回报稳定",
    "平衡": "六种玩法均衡分配 — 兼顾命中率与赔付倍数",
    "激进": "高赔付搏大奖 — 四定(9600倍)+四现(320倍)为主，命中率低但单中收益高",
}


def fetch_history(count: int = 50):
    """从API获取历史数据，失败则抛出异常（不使用备用数据）"""
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
    resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    text = resp.text.strip()
    m = re.match(r"^\w+\((.*)\)\s*;?\s*$", text, re.DOTALL)
    if not m:
        raise ValueError("接口响应格式异常")
    payload = json.loads(m.group(1))
    rows = payload.get("data", []) or []
    if not rows:
        raise ValueError("未获取到任何开奖数据")
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
        raise ValueError("数据解析失败，未得到有效号码")
    return history


class Predictor:
    POS_NAMES = ["千位", "百位", "十位", "个位"]
    SHORT_WINDOW = 10
    WEIGHTS = {"freq_long": 0.25, "freq_short": 0.30, "miss": 0.20, "markov": 0.25}

    def __init__(self, history):
        if not history:
            raise ValueError("历史数据为空")
        self.history = list(reversed(history))
        self.n = len(self.history)
        self.draws = [h["nums"][:4] for h in self.history]

    def _freq_scores(self, draws):
        scores = []
        total = max(len(draws), 1)
        for pos in range(4):
            cnt = Counter(d[pos] for d in draws)
            row = [cnt.get(d, 0) / total for d in range(10)]
            scores.append(self._normalize(row))
        return scores

    def _miss_scores(self):
        scores = []
        for pos in range(4):
            row = []
            for d in range(10):
                miss = self.n
                for i in range(self.n - 1, -1, -1):
                    if self.draws[i][pos] == d:
                        miss = self.n - 1 - i
                        break
                row.append(miss)
            scores.append(self._normalize(row))
        return scores

    def _markov_scores(self):
        scores = []
        last_draw = self.draws[-1]
        for pos in range(4):
            transitions = [[1] * 10 for _ in range(10)]
            for i in range(self.n - 1):
                a = self.draws[i][pos]
                b = self.draws[i + 1][pos]
                transitions[a][b] += 1
            last_d = last_draw[pos]
            row_total = sum(transitions[last_d])
            row = [transitions[last_d][d] / row_total for d in range(10)]
            scores.append(self._normalize(row))
        return scores

    @staticmethod
    def _normalize(row):
        lo, hi = min(row), max(row)
        if hi - lo < 1e-9:
            return [0.5] * len(row)
        return [(x - lo) / (hi - lo) for x in row]

    def position_scores(self):
        long_s = self._freq_scores(self.draws)
        short_s = self._freq_scores(self.draws[-self.SHORT_WINDOW:])
        miss_s = self._miss_scores()
        mk_s = self._markov_scores()
        w = self.WEIGHTS
        result = []
        for pos in range(4):
            row = []
            for d in range(10):
                s = (w["freq_long"] * long_s[pos][d] +
                     w["freq_short"] * short_s[pos][d] +
                     w["miss"] * miss_s[pos][d] +
                     w["markov"] * mk_s[pos][d])
                row.append(s)
            result.append(row)
        return result

    def global_digit_scores(self):
        long_cnt = Counter()
        for d in self.draws:
            long_cnt.update(d)
        short_cnt = Counter()
        for d in self.draws[-self.SHORT_WINDOW:]:
            short_cnt.update(d)
        miss = []
        for d in range(10):
            m = self.n
            for i in range(self.n - 1, -1, -1):
                if d in self.draws[i]:
                    m = self.n - 1 - i
                    break
            miss.append(m)
        long_n = self._normalize([long_cnt.get(d, 0) for d in range(10)])
        short_n = self._normalize([short_cnt.get(d, 0) for d in range(10)])
        miss_n = self._normalize(miss)
        return [0.4 * long_n[d] + 0.4 * short_n[d] + 0.2 * miss_n[d] for d in range(10)]

    # ==================== AI 自动优化 ====================
    @staticmethod
    def _evaluate_weights(weights, history, test_window=20):
        """用给定权重在最近 test_window 期上进行回测，返回命中率"""
        if len(history) < test_window + 10:
            return 0.0
        chrono = list(reversed(history))
        hits = 0
        total = 0
        for i in range(test_window, len(chrono) - 1):
            if len(chrono[:i]) < 10:
                continue
            train = list(reversed(chrono[:i]))
            test = chrono[i]
            temp = Predictor(train)
            temp.WEIGHTS = weights
            scores = temp.position_scores()
            # 预测第一位
            pred_digit = max(range(10), key=lambda d: scores[0][d])
            if pred_digit == test["nums"][0]:
                hits += 1
            total += 1
            if total >= test_window:
                break
        return hits / total if total > 0 else 0.0

    def auto_optimize(self, generations=15, population=20):
        """使用遗传算法自动优化 WEIGHTS 权重，消耗计算资源"""
        best_weights = self.WEIGHTS.copy()
        best_score = self._evaluate_weights(best_weights, self.history)

        # 初始化种群
        pop = []
        for _ in range(population):
            w = {
                "freq_long": random.uniform(0.1, 0.5),
                "freq_short": random.uniform(0.1, 0.5),
                "miss": random.uniform(0.1, 0.5),
                "markov": random.uniform(0.1, 0.5),
            }
            s = sum(w.values())
            w = {k: v / s for k, v in w.items()}
            pop.append(w)

        for gen in range(generations):
            scores = [self._evaluate_weights(w, self.history) for w in pop]
            for i, w in enumerate(pop):
                if scores[i] > best_score:
                    best_score = scores[i]
                    best_weights = w.copy()
            # 选择精英
            elite_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:4]
            new_pop = [pop[idx].copy() for idx in elite_idx]
            # 填充剩余
            while len(new_pop) < population:
                total_score = sum(scores) + 1e-9
                p1 = random.choices(pop, weights=[s / total_score for s in scores])[0]
                p2 = random.choices(pop, weights=[s / total_score for s in scores])[0]
                child = {}
                for key in p1.keys():
                    child[key] = p1[key] if random.random() < 0.5 else p2[key]
                if random.random() < 0.15:
                    key = random.choice(list(child.keys()))
                    child[key] += random.uniform(-0.1, 0.1)
                    if child[key] < 0.05:
                        child[key] = 0.05
                s = sum(child.values())
                child = {k: v / s for k, v in child.items()}
                new_pop.append(child)
            pop = new_pop

        self.WEIGHTS = best_weights
        return best_weights, best_score


def _select_dynamic(scores, min_n, max_n, threshold):
    ranked = sorted(range(10), key=lambda d: scores[d], reverse=True)
    selected = [d for d in ranked if scores[d] >= threshold]
    if len(selected) < min_n:
        selected = ranked[:min_n]
    elif len(selected) > max_n:
        selected = selected[:max_n]
    return selected


def make_recommendations(predictor: Predictor):
    pos_scores = predictor.position_scores()
    digit_scores = predictor.global_digit_scores()
    top_per_pos = []
    for pos in range(4):
        ranked = sorted(range(10), key=lambda d: pos_scores[pos][d], reverse=True)
        top_per_pos.append(ranked)
    best_digit_each_pos = [tp[0] for tp in top_per_pos]
    digit_ranked = sorted(range(10), key=lambda d: digit_scores[d], reverse=True)
    rec = {}
    pos_names = Predictor.POS_NAMES
    pos_strength = [(pos, max(pos_scores[pos])) for pos in range(4)]
    pos_strength.sort(key=lambda x: x[1], reverse=True)
    two_def_positions = sorted([pos_strength[0][0], pos_strength[1][0]])
    rec["二定"] = {
        "单码": [(pos_names[p], best_digit_each_pos[p]) for p in two_def_positions],
        "包码": [(pos_names[p], sorted(_select_dynamic(pos_scores[p], 3, 6, 0.55)))
                 for p in two_def_positions],
    }
    three_def_positions = sorted([pos_strength[i][0] for i in range(3)])
    rec["三定"] = {
        "单码": [(pos_names[p], best_digit_each_pos[p]) for p in three_def_positions],
        "包码": [(pos_names[p], sorted(_select_dynamic(pos_scores[p], 3, 6, 0.55)))
                 for p in three_def_positions],
    }
    rec["四定"] = {
        "单码": [(pos_names[p], best_digit_each_pos[p]) for p in range(4)],
        "包码": [(pos_names[p], sorted(_select_dynamic(pos_scores[p], 2, 4, 0.6)))
                 for p in range(4)],
    }
    rec["二现"] = sorted(digit_ranked[:2])
    rec["三现"] = sorted(digit_ranked[:3])
    rec["四现"] = sorted(digit_ranked[:4])
    return rec, pos_scores, digit_scores


def make_custom_recommendations(predictor: Predictor, config: dict):
    pos_scores = predictor.position_scores()
    digit_scores = predictor.global_digit_scores()
    pos_names = Predictor.POS_NAMES
    top_per_pos = []
    for pos in range(4):
        ranked = sorted(range(10), key=lambda d: pos_scores[pos][d], reverse=True)
        top_per_pos.append(ranked)
    best_digit_each_pos = [tp[0] for tp in top_per_pos]
    digit_ranked = sorted(range(10), key=lambda d: digit_scores[d], reverse=True)
    enabled = config.get("enabled", set())
    bao_pos = config.get("bao_pos", {})
    xian_manual = config.get("xian_manual", {})
    rec = {}
    for name in ["二定", "三定", "四定"]:
        counts = bao_pos.get(name, [0, 0, 0, 0])
        user_active = [p for p in range(4) if counts[p] > 0]
        rec[name] = {
            "包码": [(pos_names[p], sorted(top_per_pos[p][:counts[p]])) for p in user_active],
        }
    pos_avg = [sum(pos_scores[p]) / 10 for p in range(4)]
    pos_ranked = sorted(range(4), key=lambda p: pos_avg[p], reverse=True)
    need_positions = {"二定": 2, "三定": 3, "四定": 4}
    for name in ["二定", "三定", "四定"]:
        n = need_positions[name]
        selected = pos_ranked[:n]
        rec[name]["单码"] = [(pos_names[p], best_digit_each_pos[p]) for p in selected]
    for name, default_n in [("二现", 2), ("三现", 3), ("四现", 4)]:
        manual = xian_manual.get(name)
        if manual and len(manual) == default_n:
            rec[name] = sorted(manual)
        else:
            rec[name] = sorted(digit_ranked[:default_n])
    return rec, pos_scores, digit_scores, enabled


def calculate_budget_plans(budget: float, rec: dict, risk: str = "平衡"):
    if budget <= 0:
        return {"__total__": 0.0, "__risk__": risk}
    weights = RISK_PROFILES.get(risk, RISK_PROFILES["平衡"])
    bao_combos = {}
    for name in ["二定", "三定", "四定"]:
        n = 1
        for _, ds in rec[name]["包码"]:
            n *= len(ds)
        bao_combos[name] = n
    schemes = {
        "二定单码": {"组合数": 1, "单份成本": 1.0, "单注赔付": float(PAYOUT_RATIO["二定"]), "命中概率": 1 / 100.0},
        "三定单码": {"组合数": 1, "单份成本": 1.0, "单注赔付": float(PAYOUT_RATIO["三定"]), "命中概率": 1 / 1000.0},
        "四定单码": {"组合数": 1, "单份成本": 1.0, "单注赔付": float(PAYOUT_RATIO["四定"]), "命中概率": 1 / 10000.0},
        "二定包码": {"组合数": bao_combos["二定"], "单份成本": round(bao_combos["二定"] * 0.1, 2), "单注赔付": 0.1 * PAYOUT_RATIO["二定"], "命中概率": bao_combos["二定"] / 100.0},
        "三定包码": {"组合数": bao_combos["三定"], "单份成本": round(bao_combos["三定"] * 0.1, 2), "单注赔付": 0.1 * PAYOUT_RATIO["三定"], "命中概率": bao_combos["三定"] / 1000.0},
        "四定包码": {"组合数": bao_combos["四定"], "单份成本": round(bao_combos["四定"] * 0.1, 2), "单注赔付": 0.1 * PAYOUT_RATIO["四定"], "命中概率": bao_combos["四定"] / 10000.0},
        "二现": {"组合数": 1, "单份成本": 1.0, "单注赔付": float(PAYOUT_RATIO["二现"]), "命中概率": PROB_XIAN[2]},
        "三现": {"组合数": 1, "单份成本": 1.0, "单注赔付": float(PAYOUT_RATIO["三现"]), "命中概率": PROB_XIAN[3]},
        "四现": {"组合数": 1, "单份成本": 1.0, "单注赔付": float(PAYOUT_RATIO["四现"]), "命中概率": PROB_XIAN[4]},
    }
    plans = {}
    total = 0.0
    for play, weight in weights.items():
        if weight <= 0:
            continue
        s = schemes[play]
        target = budget * weight
        multiples = int(target / s["单份成本"])
        if multiples < 1:
            continue
        cost = round(multiples * s["单份成本"], 2)
        payout = round(multiples * s["单注赔付"], 2)
        plans[play] = {
            "倍数": multiples,
            "组合数": s["组合数"],
            "单份成本": s["单份成本"],
            "实际投入": cost,
            "命中概率": s["命中概率"],
            "单注赔付": s["单注赔付"],
            "中奖金额": payout,
            "净收益": round(payout - cost, 2),
        }
        total += cost
    plans["__total__"] = round(total, 2)
    plans["__risk__"] = risk
    return plans


def calculate_custom_budget_plans(budget: float, rec: dict, enabled: set):
    if budget <= 0 or not enabled:
        return {"__total__": 0.0, "__risk__": "自定义"}
    bao_combos = {}
    for name in ["二定", "三定", "四定"]:
        n = 1
        for _, ds in rec[name]["包码"]:
            n *= len(ds)
        bao_combos[name] = n if rec[name]["包码"] else 0
    schemes = {
        "二定单码": {"组合数": 1, "单份成本": 1.0, "单注赔付": float(PAYOUT_RATIO["二定"]), "命中概率": 1 / 100.0},
        "三定单码": {"组合数": 1, "单份成本": 1.0, "单注赔付": float(PAYOUT_RATIO["三定"]), "命中概率": 1 / 1000.0},
        "四定单码": {"组合数": 1, "单份成本": 1.0, "单注赔付": float(PAYOUT_RATIO["四定"]), "命中概率": 1 / 10000.0},
        "二定包码": {"组合数": bao_combos["二定"], "单份成本": round(bao_combos["二定"] * 0.1, 2) if bao_combos["二定"] else 0, "单注赔付": 0.1 * PAYOUT_RATIO["二定"], "命中概率": bao_combos["二定"] / 100.0 if bao_combos["二定"] else 0},
        "三定包码": {"组合数": bao_combos["三定"], "单份成本": round(bao_combos["三定"] * 0.1, 2) if bao_combos["三定"] else 0, "单注赔付": 0.1 * PAYOUT_RATIO["三定"], "命中概率": bao_combos["三定"] / 1000.0 if bao_combos["三定"] else 0},
        "四定包码": {"组合数": bao_combos["四定"], "单份成本": round(bao_combos["四定"] * 0.1, 2) if bao_combos["四定"] else 0, "单注赔付": 0.1 * PAYOUT_RATIO["四定"], "命中概率": bao_combos["四定"] / 10000.0 if bao_combos["四定"] else 0},
        "二现": {"组合数": 1, "单份成本": 1.0, "单注赔付": float(PAYOUT_RATIO["二现"]), "命中概率": PROB_XIAN[2]},
        "三现": {"组合数": 1, "单份成本": 1.0, "单注赔付": float(PAYOUT_RATIO["三现"]), "命中概率": PROB_XIAN[3]},
        "四现": {"组合数": 1, "单份成本": 1.0, "单注赔付": float(PAYOUT_RATIO["四现"]), "命中概率": PROB_XIAN[4]},
    }
    valid = [p for p in enabled if p in schemes and schemes[p]["单份成本"] > 0]
    if not valid:
        return {"__total__": 0.0, "__risk__": "自定义"}
    each = budget / len(valid)
    plans = {}
    total_spent = 0.0
    for play in valid:
        s = schemes[play]
        multiples = int(each / s["单份成本"])
        if multiples >= 1:
            cost = round(multiples * s["单份成本"], 2)
        else:
            cost = s["单份成本"]
        plans[play] = {
            "倍数": multiples,
            "组合数": s["组合数"],
            "单份成本": s["单份成本"],
            "实际投入": cost,
            "命中概率": s["命中概率"],
            "单注赔付": s["单注赔付"],
            "中奖金额": round(multiples * s["单注赔付"], 2) if multiples > 0 else 0,
            "净收益": round((multiples * s["单注赔付"]) - cost, 2) if multiples > 0 else 0,
        }
        total_spent += cost
    remaining = budget - total_spent
    if remaining > 0:
        for play in valid:
            if play not in plans:
                s = schemes[play]
                allocate = min(s["单份成本"], remaining)
                if allocate > 0:
                    plans[play] = {
                        "倍数": 0,
                        "组合数": s["组合数"],
                        "单份成本": s["单份成本"],
                        "实际投入": allocate,
                        "命中概率": s["命中概率"],
                        "单注赔付": s["单注赔付"],
                        "中奖金额": 0,
                        "净收益": -allocate,
                    }
                    total_spent += allocate
                    remaining -= allocate
                    if remaining <= 0:
                        break
    plans["__total__"] = round(total_spent, 2)
    plans["__risk__"] = "自定义"
    return plans


PLAY_TO_DEF_NAME = {
    "二定单码": ("二定", "单码"),
    "三定单码": ("三定", "单码"),
    "四定单码": ("四定", "单码"),
    "二定包码": ("二定", "包码"),
    "三定包码": ("三定", "包码"),
    "四定包码": ("四定", "包码"),
}
POS_NAME_TO_IDX = {"千位": 0, "百位": 1, "十位": 2, "个位": 3}


def make_random_recommendations():
    pos_names = Predictor.POS_NAMES
    rec = {}
    pos_strength = list(range(4))
    random.shuffle(pos_strength)

    def rand_digits(k):
        return random.sample(range(10), k)

    two_pos = sorted(pos_strength[:2])
    rec["二定"] = {
        "单码": [(pos_names[p], random.randint(0, 9)) for p in two_pos],
        "包码": [(pos_names[p], sorted(rand_digits(3))) for p in two_pos],
    }
    three_pos = sorted(pos_strength[:3])
    rec["三定"] = {
        "单码": [(pos_names[p], random.randint(0, 9)) for p in three_pos],
        "包码": [(pos_names[p], sorted(rand_digits(3))) for p in three_pos],
    }
    rec["四定"] = {
        "单码": [(pos_names[p], random.randint(0, 9)) for p in range(4)],
        "包码": [(pos_names[p], sorted(rand_digits(2))) for p in range(4)],
    }
    rec["二现"] = sorted(rand_digits(2))
    rec["三现"] = sorted(rand_digits(3))
    rec["四现"] = sorted(rand_digits(4))
    return rec


def evaluate_bet(play, rec, plan, actual):
    cost = plan["实际投入"]
    multiples = plan["倍数"]
    if play in PLAY_TO_DEF_NAME:
        def_name, kind = PLAY_TO_DEF_NAME[play]
        if kind == "单码":
            single = rec[def_name]["单码"]
            hit = all(actual[POS_NAME_TO_IDX[pos]] == d for pos, d in single)
            payout = round(multiples * PAYOUT_RATIO[def_name], 2) if hit else 0.0
        else:
            bao = rec[def_name]["包码"]
            hit = all(actual[POS_NAME_TO_IDX[pos]] in digits for pos, digits in bao)
            payout = round(0.1 * PAYOUT_RATIO[def_name] * multiples, 2) if hit else 0.0
    else:
        digits = rec[play]
        hit = all(d in actual for d in digits)
        payout = round(multiples * PAYOUT_RATIO[play], 2) if hit else 0.0
    return hit, cost, payout


def run_backtest(history, train_window=50, budget=100.0, risk="平衡"):
    chrono = list(reversed(history))
    n = len(chrono)
    if n < train_window + 1:
        raise ValueError(f"数据不足，需要至少 {train_window + 1} 期，当前 {n} 期")
    play_stats = {}
    play_names = ["二定单码", "二定包码", "三定单码", "三定包码",
                  "四定单码", "四定包码", "二现", "三现", "四现"]
    for p in play_names:
        play_stats[p] = {
            "algo_bets": 0, "algo_hits": 0, "algo_cost": 0.0, "algo_payout": 0.0,
            "random_bets": 0, "random_hits": 0, "random_cost": 0.0, "random_payout": 0.0,
        }
    details = []
    algo_total_cost = algo_total_payout = 0.0
    rand_total_cost = rand_total_payout = 0.0
    for t in range(train_window, n):
        train_newest_first = list(reversed(chrono[:t]))
        target = chrono[t]
        actual = target["nums"][:4]
        predictor = Predictor(train_newest_first)
        algo_rec, _, _ = make_recommendations(predictor)
        algo_plans = calculate_budget_plans(budget, algo_rec, risk)
        random_rec = make_random_recommendations()
        random_plans = calculate_budget_plans(budget, random_rec, risk)
        algo_eval = {"results": {}, "total_cost": 0.0, "total_payout": 0.0}
        random_eval = {"results": {}, "total_cost": 0.0, "total_payout": 0.0}
        for play in play_names:
            if play in algo_plans and not play.startswith("__"):
                hit, cost, payout = evaluate_bet(play, algo_rec, algo_plans[play], actual)
                algo_eval["results"][play] = {"hit": hit, "cost": cost, "payout": payout}
                algo_eval["total_cost"] += cost
                algo_eval["total_payout"] += payout
                play_stats[play]["algo_bets"] += 1
                play_stats[play]["algo_hits"] += int(hit)
                play_stats[play]["algo_cost"] += cost
                play_stats[play]["algo_payout"] += payout
            if play in random_plans and not play.startswith("__"):
                hit, cost, payout = evaluate_bet(play, random_rec, random_plans[play], actual)
                random_eval["results"][play] = {"hit": hit, "cost": cost, "payout": payout}
                random_eval["total_cost"] += cost
                random_eval["total_payout"] += payout
                play_stats[play]["random_bets"] += 1
                play_stats[play]["random_hits"] += int(hit)
                play_stats[play]["random_cost"] += cost
                play_stats[play]["random_payout"] += payout
        algo_total_cost += algo_eval["total_cost"]
        algo_total_payout += algo_eval["total_payout"]
        rand_total_cost += random_eval["total_cost"]
        rand_total_payout += random_eval["total_payout"]
        details.append({
            "issue": target["issue"], "date": target["date"],
            "actual": "".join(str(x) for x in actual),
            "algo_rec": algo_rec, "random_rec": random_rec,
            "algo_eval": algo_eval, "random_eval": random_eval,
        })
    totals = {
        "algo_cost": round(algo_total_cost, 2),
        "algo_payout": round(algo_total_payout, 2),
        "algo_net": round(algo_total_payout - algo_total_cost, 2),
        "algo_roi": round((algo_total_payout - algo_total_cost) / algo_total_cost, 4) if algo_total_cost > 0 else 0.0,
        "random_cost": round(rand_total_cost, 2),
        "random_payout": round(rand_total_payout, 2),
        "random_net": round(rand_total_payout - rand_total_cost, 2),
        "random_roi": round((rand_total_payout - rand_total_cost) / rand_total_cost, 4) if rand_total_cost > 0 else 0.0,
    }
    for p, s in play_stats.items():
        s["algo_hit_rate"] = round(s["algo_hits"] / s["algo_bets"], 4) if s["algo_bets"] > 0 else 0.0
        s["random_hit_rate"] = round(s["random_hits"] / s["random_bets"], 4) if s["random_bets"] > 0 else 0.0
        s["algo_roi"] = round((s["algo_payout"] - s["algo_cost"]) / s["algo_cost"], 4) if s["algo_cost"] > 0 else 0.0
        s["random_roi"] = round((s["random_payout"] - s["random_cost"]) / s["random_cost"], 4) if s["random_cost"] > 0 else 0.0
    return {
        "train_window": train_window,
        "n_test": len(details),
        "budget": budget,
        "risk": risk,
        "totals": totals,
        "play_stats": play_stats,
        "details": details,
    }
