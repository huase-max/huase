# -*- coding: utf-8 -*-
"""排列五预测器 - 核心预测引擎"""
import json
import re
import math
import random
import time
import xml.etree.ElementTree as ET
import os
from collections import Counter

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.zhcw.com/kjxx/pl5/",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
}

BEST_WEIGHTS_FILE = "best_weights.json"
TRAIN_CHECK_PERIODS = 100
LAST_TRAIN_FILE = "last_train_info.json"


def fetch_history(count: int = 50):
    """从多个数据源分页获取历史数据"""
    try:
        history = fetch_from_zhcw_paginated(count)
        if history and len(history) >= min(count, 10):
            print(f"[数据源] ✅ 从 zhcw 获取 {len(history)} 期")
            return history
    except Exception as e:
        print(f"[数据源] zhcw 分页失败: {e}")

    try:
        history = fetch_from_500(count)
        if history and len(history) >= min(count, 10):
            print(f"[数据源] ✅ 从 500.com 获取 {len(history)} 期")
            return history
    except Exception as e:
        print(f"[数据源] 500.com 失败: {e}")

    raise ValueError("所有数据源均失败，无法获取实时数据")


def fetch_from_zhcw_paginated(count: int = 50):
    """分页从 zhcw 获取指定期数"""
    all_history = []
    page_size = 100
    pages_needed = (count + page_size - 1) // page_size

    first_page = _fetch_zhcw_page(count=page_size, end_issue="")
    if not first_page:
        raise ValueError("第一页无数据")
    all_history.extend(first_page)

    if len(all_history) < count and len(first_page) == page_size:
        last_issue = first_page[-1]["issue"]
        for page in range(2, pages_needed + 1):
            next_page = _fetch_zhcw_page(count=page_size, end_issue=last_issue)
            if not next_page:
                break
            all_history.extend(next_page)
            if len(all_history) >= count:
                break
            last_issue = next_page[-1]["issue"]
            if len(next_page) < page_size:
                break

    return all_history[:count]


def _fetch_zhcw_page(count: int = 100, end_issue: str = ""):
    """单页从 zhcw 获取数据（内部使用）"""
    url = "https://jc.zhcw.com/port/client_json.php"
    params = {
        "transactionType": "10001001",
        "lotteryId": "284",
        "issueCount": str(count),
        "startIssue": "",
        "endIssue": end_issue,
        "startDate": "",
        "endDate": "",
        "type": "0",
        "pageNum": "1",
        "pageSize": str(count),
        "tt": str(random.random()),
        "callback": "cb",
    }
    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    text = resp.text.strip()
    m = re.match(r"^\w+\((.*)\)\s*;?\s*$", text, re.DOTALL)
    if not m:
        raise ValueError("接口响应格式异常")
    payload = json.loads(m.group(1))
    rows = payload.get("data", []) or []
    if not rows:
        return []
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
    return history


def fetch_from_500(count: int = 50):
    """从 500.com 获取数据（备用源）"""
    url = "https://www.500.com/static/info/kaijiang/xml/plw/list.xml"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.500.com/",
    }
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    history = []
    for row in root.findall(".//row"):
        issue = row.get("expect", "")
        open_time = row.get("opentime", "")
        open_code = row.get("opencode", "")
        parts = open_code.split(",")
        if len(parts) != 5 or not all(p.isdigit() for p in parts):
            continue
        history.append({
            "issue": issue,
            "date": open_time[:10] if open_time else "",
            "nums": [int(x) for x in parts],
        })
    if not history:
        raise ValueError("500.com 未获取到数据")
    return history[:count]


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
        best = Predictor.load_best_weights()
        if best:
            self.WEIGHTS = best

    @classmethod
    def load_best_weights(cls):
        if os.path.exists(BEST_WEIGHTS_FILE):
            try:
                with open(BEST_WEIGHTS_FILE, 'r') as f:
                    data = json.load(f)
                    return data.get("weights")
            except:
                pass
        return None

    @classmethod
    def save_best_weights(cls, weights):
        with open(BEST_WEIGHTS_FILE, 'w') as f:
            json.dump({"weights": weights}, f)

    @classmethod
    def train(cls, history, generations=20, population=30):
        if len(history) < 50:
            raise ValueError("历史数据不足，至少需要50期")
        temp = Predictor(history)
        best_w, best_score = temp.auto_optimize(generations=generations, population=population)
        cls.save_best_weights(best_w)
        return best_w, best_score

    # ==================== 自动训练 ====================

    @classmethod
    def _get_last_train_info(cls):
        if os.path.exists(LAST_TRAIN_FILE):
            try:
                with open(LAST_TRAIN_FILE, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {"periods": 0}

    @classmethod
    def _save_last_train_info(cls, periods):
        with open(LAST_TRAIN_FILE, 'w') as f:
            json.dump({"periods": periods}, f)

    @classmethod
    def auto_train_if_needed(cls, history):
        if len(history) < 50:
            return
        last_info = cls._get_last_train_info()
        last_periods = last_info.get("periods", 0)
        current_periods = len(history)
        if current_periods - last_periods >= TRAIN_CHECK_PERIODS:
            print(f"[自动训练] 数据增加了 {current_periods - last_periods} 期，开始自动训练...")
            try:
                best_w, best_score = cls.train(history, generations=15, population=20)
                cls._save_last_train_info(current_periods)
                print(f"[自动训练] 完成！新权重: {best_w}, 命中率: {best_score:.2%}")
            except Exception as e:
                print(f"[自动训练] 失败: {e}")

    # ==================== 特征计算 ====================

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

    # ==================== AI 自动优化（遗传算法） ====================

    @staticmethod
    def _evaluate_weights(weights, history, test_window=20):
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
            pred_digit = max(range(10), key=lambda d: scores[0][d])
            if pred_digit == test["nums"][0]:
                hits += 1
            total += 1
            if total >= test_window:
                break
        return hits / total if total > 0 else 0.0

    def auto_optimize(self, generations=15, population=20):
        best_weights = self.WEIGHTS.copy()
        best_score = self._evaluate_weights(best_weights, self.history)

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
            elite_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:4]
            new_pop = [pop[idx].copy() for idx in elite_idx]
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


# ==================== 推荐生成、预算分配、回测 ====================
# (以下代码与之前完全相同，为节省篇幅省略，实际项目中必须保留)
# 由于篇幅限制，这里只示意，实际部署时应包含完整的 make_recommendations,
# calculate_budget_plans, run_backtest 等函数。
# 完整代码可参考上一轮提供的 predictor.py。
