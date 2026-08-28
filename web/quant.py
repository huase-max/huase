# -*- coding: utf-8 -*-
"""quant.py - 排列五量化选号引擎

设计思路：
1. 多因子评分：11 个因子给每位每个数字打分
2. 滚动回测学最优权重：训练窗 → 预测 → 算 IC（信息系数）→ 选出有效因子
3. 组合优化：按 Kelly 公式分配预算到 6 种玩法
4. 输出 JSON 文件 + 量化指标报告

输出文件 quant_output.json 供 GUI 加载展示。
"""
import json
import math
import random
import re
import sys
from collections import Counter

import requests

# ------------------------------------------------------------
# 配置
# ------------------------------------------------------------
API_URL = "https://jc.zhcw.com/port/client_json.php"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.zhcw.com/kjxx/pl5/",
}

PAYOUT = {"二定": 96, "三定": 960, "四定": 9600,
          "二现": 9, "三现": 45, "四现": 320}
PROB_XIAN = {2: 0.0974, 3: 0.0204, 4: 0.0024}

OUTPUT_FILE = "quant_output.json"
TARGET_PERIODS = 2000   # 目标总数据量
TRAIN_WINDOW = 500       # 因子权重学习：训练窗口(500期滚动回测学IC)
IC_TEST_COUNT = 300      # 因子IC测试期数
PREDICT_WINDOW = 50      # 预测算法：只取最近50期
BACKTEST_WINDOW = 500    # 量化回测：训练窗口
BUDGET = 100.0

POS_NAMES = ["千位", "百位", "十位", "个位"]


# ------------------------------------------------------------
# 数据加载
# ------------------------------------------------------------
def _fetch_page(end_issue: str = ""):
    """单页请求，返回 (rows, 本页最老期号)。"""
    params = {
        "transactionType": "10001001", "lotteryId": "284",
        "issueCount": "100", "pageNum": "1", "pageSize": "100",
        "startIssue": "", "endIssue": end_issue,
        "startDate": "", "endDate": "", "type": "1",
        "tt": str(random.random()), "callback": "cb",
    }
    resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    text = resp.text.strip()
    m = re.match(r"^\w+\((.*)\)\s*;?\s*$", text, re.DOTALL)
    if not m:
        raise ValueError("JSONP 格式异常")
    payload = json.loads(m.group(1))
    return payload.get("data", []) or []


def _fetch_api(count: int):
    """API 兜底分页爬取（数据库无数据时用）。"""
    all_rows = []
    old_issue = ""
    page = 0
    while len(all_rows) < count:
        page += 1
        rows = _fetch_page(old_issue)
        if not rows:
            break
        all_rows.extend(rows)
        new_old = rows[-1]["issue"]
        if new_old == old_issue:
            break
        old_issue = new_old
        print(f"      [API] 分页 {page}: 已拿 {len(all_rows)} 期")
        if page > 80:
            break
    history = []
    for row in all_rows:
        parts = row.get("frontWinningNum", "").strip().split()
        if len(parts) != 5 or not all(p.isdigit() for p in parts):
            continue
        history.append({
            "issue": row.get("issue", ""),
            "date": row.get("openTime", ""),
            "nums": [int(x) for x in parts],
        })
    return history


def fetch_history(count: int = 1000):
    """读取历史数据。优先从 MySQL 读，库为空才走 API。"""
    try:
        from db_utils import load_history
        data = load_history(count)
        if data:
            print(f"      从数据库读取 {len(data)} 期")
            return data
        print(f"      数据库为空，转 API")
    except Exception as e:
        print(f"      数据库不可用 ({e})，使用 API")
    return _fetch_api(count)


# ------------------------------------------------------------
# 因子计算
# ------------------------------------------------------------
def _normalize(row):
    lo, hi = min(row), max(row)
    if hi - lo < 1e-9:
        return [0.5] * len(row)
    return [(x - lo) / (hi - lo) for x in row]


class FactorEngine:
    """对给定的历史期数（chronological，旧→新）计算 11 个因子。

    返回 list[4][10]，即 4 个位置各 0-9 数字的综合因子原始值。
    调用 .to_score(weights) 得到加权评分。
    """

    FACTOR_NAMES = [
        "freq_100", "freq_20", "freq_5",          # 长期/中期/短期频率
        "miss_gap",                                 # 遗漏间隔
        "markov1", "markov2",                      # 一/二阶马尔可夫
        "cross_pos",                                # 跨位置关联
        "parity_bias",                              # 奇偶偏差
        "size_bias",                                # 大小偏差
        "sum_trend",                                # 和值趋势
        "cycle",                                    # 周期模式
    ]

    def __init__(self, draws):
        """draws: 从旧到新的 4 位开奖列表 [[d千,d百,d十,d个], ...]"""
        self.draws = draws
        self.n = len(draws)
        self.all_digits = [h["nums"] for h in getattr(self, '_history', [])] if False else None

    def compute(self):
        """返回 list[11][4][10] — 11 个因子 × 4 位置 × 10 数字"""
        factors = []
        n = self.n

        # --- F1-F3: 频率因子 ---
        for window in [n, 20, 5]:
            start = max(0, n - window)
            slice_ = self.draws[start:]
            scores = []
            for pos in range(4):
                cnt = Counter(d[pos] for d in slice_)
                row = [cnt.get(d, 0) / max(len(slice_), 1) for d in range(10)]
                scores.append(_normalize(row))
            factors.append(scores)

        # --- F4: 遗漏值 ---
        miss_scores = []
        for pos in range(4):
            row = []
            for d in range(10):
                gap = n
                for i in range(n - 1, -1, -1):
                    if self.draws[i][pos] == d:
                        gap = n - 1 - i
                        break
                row.append(gap)
            miss_scores.append(_normalize(row))
        factors.append(miss_scores)

        # --- F5-F6: 一/二阶马尔可夫转移概率 ---
        # order=1: P(d_t | d_{t-1}) 上一期同位置 → 本期
        # order=2: P(d_t | d_{t-2}) 上两期同位置 → 本期
        for order in [1, 2]:
            mk_scores = []
            if order > n:
                last_digits = (0,) * 4
            else:
                last_digits = tuple(self.draws[-order][p] for p in range(4))
            for pos in range(4):
                trans = [[1] * 10 for _ in range(10)]
                for i in range(n - order):
                    a = self.draws[i][pos]
                    b = self.draws[i + order][pos]
                    trans[a][b] += 1
                last = last_digits[pos]
                row_total = sum(trans[last])
                row = [trans[last][d] / row_total for d in range(10)]
                mk_scores.append(_normalize(row))
            factors.append(mk_scores)

        # --- F7: 跨位置共现 ---
        # 基于上一期其他三位实际出的数字，查历史共现矩阵给本位置每个 d 打分
        cross = []
        last_draw = self.draws[-1] if n > 0 else [0, 0, 0, 0]
        for pos in range(4):
            row = []
            for d in range(10):
                score = 0.0
                for other in range(4):
                    if other == pos:
                        continue
                    last_d_other = last_draw[other]
                    cnt_other = sum(1 for dr in self.draws if dr[other] == last_d_other)
                    cnt_both = sum(1 for dr in self.draws
                                   if dr[other] == last_d_other and dr[pos] == d)
                    if cnt_other > 0:
                        score += cnt_both / cnt_oth
