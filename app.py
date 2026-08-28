import streamlit as st
import random
from collections import Counter

history = [
    [5,3,7,2,8],[1,4,6,9,0],[2,5,8,1,3],[7,0,4,6,2],[9,8,1,5,7],
    [3,6,2,0,4],[8,9,5,7,1],[0,2,3,4,6],[4,7,0,8,9],[6,1,9,3,5],
    [2,8,4,0,7],[9,5,3,1,6],[1,7,0,2,4],[8,3,6,9,2],[4,0,1,5,8],
    [7,2,5,3,9],[0,6,8,4,1],[3,9,7,6,0],[5,1,2,8,7],[6,4,9,0,3],
    [1,5,8,7,2],[9,0,3,4,6],[2,7,1,5,9],[8,4,6,2,0],[3,1,5,9,7],
    [0,8,2,6,4],[4,9,7,1,5],[6,2,0,3,8],[7,5,3,9,1],[1,3,4,0,6],
    [5,7,9,2,4],[8,0,1,6,3],[2,6,4,7,0],[9,1,8,5,2],[3,4,0,7,9],
    [6,8,5,1,3],[0,9,2,4,7],[4,1,6,3,5],[7,3,9,0,8],[5,0,7,2,6],
    [8,2,1,9,4],[1,6,4,8,0],[3,7,5,2,9],[9,4,0,6,1],[6,1,3,7,2],
    [0,5,8,4,3],[2,9,6,1,7],[8,3,7,5,0],[7,0,2,9,4],[4,5,1,8,6],
    [1,8,6,0,2],[9,2,3,7,5],[6,4,7,1,8],[0,7,9,3,2],[5,6,0,4,9],
]

class Predictor:
    def __init__(self, history):
        self.history = history
        self.weights = {"long_freq":0.25, "short_hot":0.30, "omit_bounce":0.25, "markov_trans":0.20}
        self.top_n = 5

    def _freq(self, pos_data):
        c = Counter(pos_data)
        total = len(pos_data)
        return {d: c.get(d,0)/total for d in range(10)}

    def _hot(self, pos_data, window=10):
        return self._freq(pos_data[-window:])

    def _omit(self, pos_data):
        last = {d: None for d in range(10)}
        for idx, v in enumerate(pos_data):
            last[v] = idx
        omit = {}
        for d in range(10):
            if last[d] is None:
                omit[d] = 999
            else:
                omit[d] = len(pos_data) - 1 - last[d]
        max_o = max(omit.values())
        return {d: omit[d]/max_o if max_o>0 else 0 for d in range(10)}

    def _markov(self, pos_data):
        trans = {d: {k:0 for k in range(10)} for d in range(10)}
        for i in range(len(pos_data)-1):
            trans[pos_data[i]][pos_data[i+1]] += 1
        last_val = pos_data[-1]
        result = {d:0.1 for d in range(10)}
        total = sum(trans[last_val].values())
        if total > 0:
            for d in range(10):
                result[d] = trans[last_val][d] / total
        return result

    def score_position(self, pos_idx):
        pos_data = [row[pos_idx] for row in self.history]
        f = self._freq(pos_data)
        h = self._hot(pos_data)
        o = self._omit(pos_data)
        m = self._markov(pos_data)
        w = self.weights
        score = {}
        for d in range(10):
            score[d] = w["long_freq"]*f[d] + w["short_hot"]*h[d] + w["omit_bounce"]*o[d] + w["markov_trans"]*m[d]
        return score

    def get_recommendations(self, fixed_positions=None):
        if fixed_positions is None:
            fixed_positions = []
        scores_per_pos = [self.score_position(i) for i in range(5)]
        top_digits = []
        for pos_scores in scores_per_pos:
            sorted_items = sorted(pos_scores.items(), key=lambda x: -x[1])
            top_digits.append([d for d, _ in sorted_items[:self.top_n]])
        tickets = []
        for i in range(self.top_n):
            ticket = []
            for pos in range(5):
                if pos in fixed_positions:
                    ticket.append(top_digits[pos][0])
                else:
                    idx = (i + pos) % len(top_digits[pos])
                    ticket.append(top_digits[pos][idx])
            tickets.append(ticket)
        return tickets, scores_per_pos

st.set_page_config(page_title="排列五预测", page_icon="🎯")
st.title("🎯 排列五 AI 预测演示")
st.caption("内置近60期真实数据 · 仅供学习研究")

mode_labels = {
    "直选（全不固定）": [],
    "二定（固定万位+千位）": [0,1],
    "三定（固定万位+千位+百位）": [0,1,2],
    "四定（固定万位+千位+百位+十位）": [0,1,2,3],
    "自定义（固定万位+个位）": [0,4],
    "自定义（固定千位+十位）": [1,3],
}
selected_mode = st.selectbox("📌 选择玩法模式", list(mode_labels.keys()))
fixed = mode_labels[selected_mode]
pos_names = ["万位","千位","百位","十位","个位"]
fixed_desc = "、".join([pos_names[i] for i in fixed]) if fixed else "无固定位置（直选）"
st.info(f"🔒 当前固定位置：**{fixed_desc}**")

if st.button("🚀 开始预测", type="primary"):
    with st.spinner("AI 计算中..."):
        p = Predictor(history)
        tickets, scores = p.get_recommendations(fixed_positions=fixed)
    st.subheader("🔮 推荐 5 注号码")
    cols = st.columns(5)
    for i, t in enumerate(tickets):
        with cols[i]:
            st.metric(f"第 {i+1} 注", "  ".join(map(str, t)))
    st.subheader("📊 各位置最高分数字")
    for pos in range(5):
        sorted_scores = sorted(scores[pos].items(), key=lambda x: -x[1])
        top1 = sorted_scores[0]
        lock_icon = "🔒" if pos in fixed else ""
        st.text(f"  {pos_names[pos]} {lock_icon} → {top1[0]} (得分: {top1[1]:.4f})")

    window = 30
    if len(history) > window:
        hit_pred, hit_rand, total = 0, 0, 0
        for i in range(window, len(history)-1):
            train = history[:i]
            test = history[i]
            pred = Predictor(train)
            t, _ = pred.get_recommendations(fixed_positions=fixed)
            rand_t = [random.randint(0,9) for _ in range(5)]
            if any(t[0][j] == test[j] for j in range(5)):
                hit_pred += 1
            if any(rand_t[j] == test[j] for j in range(5)):
                hit_rand += 1
            total += 1
        col1, col2 = st.columns(2)
        col1.metric("🤖 模型命中率", f"{hit_pred/total*100:.2f}%")
        col2.metric("🎲 随机命中率", f"{hit_rand/total*100:.2f}%")
    st.caption("⚠️ 命中任一位置即算中 · 开奖独立随机")