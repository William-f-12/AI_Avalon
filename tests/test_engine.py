"""引擎纯函数单测：不调用任何 LLM，零 token。

运行：python -m pytest tests/ -q   或   python tests/test_engine.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avalon import game
from avalon.agent import LLMAgent
from avalon.roles import (
    Role,
    SETUP_7,
    build_known_evil,
    build_night_knowledge,
    full_roster_text,
    team_of,
)

# 固定座位分配，多处复用
FIXED_ROLES = {
    1: Role.MERLIN, 2: Role.PERCIVAL, 3: Role.LOYAL, 4: Role.LOYAL,
    5: Role.MORGANA, 6: Role.ASSASSIN, 7: Role.OBERON,
}


# ---- 任务参数 ----

def test_quest_sizes_and_thresholds():
    assert game.QUEST_SIZES == [2, 3, 3, 4, 4]
    # 仅第 4 个任务（idx=3）需要 2 票失败
    assert [game.quest_fail_threshold(i) for i in range(5)] == [1, 1, 1, 2, 1]


def test_apply_quest_double_fail_rule():
    # 第 4 个任务一张失败票不够，仍成功
    assert game.apply_quest(["success", "fail", "success", "success"], 3) is True
    # 两张失败票才失败
    assert game.apply_quest(["success", "fail", "fail", "success"], 3) is False
    # 普通任务一张即失败
    assert game.apply_quest(["success", "fail", "success"], 1) is False


def test_apply_vote_majority():
    # 7 人需严格过半（>=4）
    assert game.apply_vote(["approve"] * 4 + ["reject"] * 3, 7) is True
    assert game.apply_vote(["approve"] * 3 + ["reject"] * 4, 7) is False
    # 3:3 平票不算通过（偶数情形）
    assert game.apply_vote(["approve"] * 3 + ["reject"] * 3, 6) is False


# ---- 发言顺序 ----

def test_speaking_order_cw_ccw():
    seats = [1, 2, 3, 4, 5, 6, 7]
    assert game.speaking_order(3, "cw", seats) == [4, 5, 6, 7, 1, 2]
    assert game.speaking_order(3, "ccw", seats) == [2, 1, 7, 6, 5, 4]
    # 回到车主前结束，不包含车主自己
    assert 3 not in game.speaking_order(3, "cw", seats)


# ---- 胜负判定 ----

def _state(successes=0, fails=0, rejects=0):
    roles = {s: Role.LOYAL for s in range(1, 8)}
    st = game.GameState(
        num_players=7, roles_by_seat=roles, seats=list(range(1, 8)), leader=1)
    st.quest_results = [True] * successes + [False] * fails
    st.reject_count = rejects
    return st


def test_check_outcome_evil_three_fails():
    assert _state(fails=3).check_quest_outcome() == "evil"


def test_check_outcome_evil_five_rejects_cumulative():
    # 全局累计 5 次流局 → 坏人赢（与任务进度无关）
    assert _state(successes=2, rejects=5).check_quest_outcome() == "evil"
    assert _state(rejects=4).check_quest_outcome() is None


def test_check_outcome_good_quests_then_pending_assassination():
    assert _state(successes=3).check_quest_outcome() == "good_quests"


def test_reject_count_persists_across_quests():
    # reject_count 是全局字段，引擎不会在任务结束后清零
    st = _state(successes=1, rejects=3)
    # 模拟又过了一个任务（仅更新任务进度，不动 reject_count）
    st.quest_results.append(True)
    assert st.reject_count == 3
    assert st.successes == 2


# ---- 发牌与角色知识 ----

def test_deal_roles_is_a_permutation_with_seed():
    roles = game.deal_roles(7, seed=42)
    assert sorted(r.value for r in roles.values()) == sorted(r.value for r in SETUP_7)
    # 同 seed 可复现
    assert game.deal_roles(7, seed=42) == roles


def test_night_knowledge_oberon_asymmetry():
    # 构造确定的座位分配
    roles = {
        1: Role.MERLIN, 2: Role.PERCIVAL, 3: Role.LOYAL, 4: Role.LOYAL,
        5: Role.MORGANA, 6: Role.ASSASSIN, 7: Role.OBERON,
    }
    k = build_night_knowledge(roles)
    # 梅林应看到全部三个坏人（含奥伯伦 7 号）
    assert "玩家5" in k[1] and "玩家6" in k[1] and "玩家7" in k[1]
    # 莫甘娜（5）看到刺客（6），但看不到奥伯伦（7）
    assert "玩家6" in k[5] and "玩家7" not in k[5]
    # 刺客（6）看到莫甘娜（5），看不到奥伯伦（7）
    assert "玩家5" in k[6] and "玩家7" not in k[6]
    # 奥伯伦（7）拿不到任何队友座位
    assert "玩家5" not in k[7] and "玩家6" not in k[7]
    # 派西维尔看到梅林(1)与莫甘娜(5)两人
    assert "玩家1" in k[2] and "玩家5" in k[2]


def test_team_split():
    assert team_of(Role.MERLIN) == "good"
    assert team_of(Role.OBERON) == "evil"


# ---- 信任图（trust graph）----

def test_build_known_evil_only_morgana_assassin_pair():
    ke = build_known_evil(FIXED_ROLES)
    # 莫甘娜（5）与刺客（6）互认
    assert ke[5] == [6]
    assert ke[6] == [5]
    # 奥伯伦（7）不认任何队友
    assert ke[7] == []
    # 好人无任何信息
    assert ke[1] == [] and ke[2] == [] and ke[3] == [] and ke[4] == []


def test_trust_seed_and_isolation():
    # llm=None：只构造与读写信任行，不触发任何 LLM 调用（零 token）
    a5 = LLMAgent(5, Role.MORGANA, "", None, num_players=7, known_evil=[6])
    a6 = LLMAgent(6, Role.ASSASSIN, "", None, num_players=7, known_evil=[5])
    # 互认队友种子为 -1.0，其余 0.0，无自指条目
    assert a5.trust_row()[6] == -1.0
    assert a5.trust_row()[1] == 0.0
    assert 5 not in a5.trust_row()
    # trust_row 返回拷贝，外部改动不影响内部
    r = a5.trust_row()
    r[1] = 99.0
    assert a5.trust_row()[1] == 0.0
    # 隔离：改 a5 的信任不影响 a6
    before = a6.trust_row()
    a5._absorb_trust({"trust_update": {"1": -0.9}})
    assert a6.trust_row() == before


def test_revise_trust_pins_revealed_teammates():
    # 奥伯伦开局不认队友（trust 全 0）；刺杀阶段互认后应把队友兜底钉为 -1.0。
    # llm=None：_ask 内 LLM 调用失败 → 吸收 no-op，但确定性钉值仍生效。
    ob = LLMAgent(7, Role.OBERON, "", None, num_players=7)
    assert ob.trust_row()[5] == 0.0 and ob.trust_row()[6] == 0.0
    ob.revise_trust([5, 6], "本局坏人阵营：玩家5、玩家6、玩家7。")
    assert ob.trust_row()[5] == -1.0 and ob.trust_row()[6] == -1.0
    assert 7 not in ob.trust_row()  # 自己不在信任行里


def test_absorb_trust_validation_and_clamp():
    a = LLMAgent(1, Role.MERLIN, "", None, num_players=7)
    a._absorb_trust({"trust_update": {
        "2": 0.5,    # 正常
        "3": 5.0,    # 越界 → clamp 到 1.0
        "4": -2.0,   # 越界 → clamp 到 -1.0
        "9": 0.3,    # 座位越界 → 丢弃
        "1": 0.8,    # 自指 → 丢弃
        "bad": 0.2,  # 非数字座位 → 丢弃
    }})
    row = a.trust_row()
    assert row[2] == 0.5
    assert row[3] == 1.0
    assert row[4] == -1.0
    assert 9 not in row
    assert 1 not in row  # 自己始终不在信任行里
    # 绝对值语义：再次 absorb 直接覆盖而非累加
    a._absorb_trust({"trust_update": {"2": -0.3}})
    assert a.trust_row()[2] == -0.3
    # 非 dict 的 trust_update 静默忽略
    a._absorb_trust({"trust_update": "oops"})
    assert a.trust_row()[2] == -0.3


def test_render_board_covers_pass_reject_and_change():
    log = [
        # 任务1·第1轮：通过 + 成功
        game.RoundRecord(1, 0, 3, [3, 5], [3, 5], False,
                         [1, 3, 5, 6, 7], [2, 4], True, quest_success=True, fail_count=0),
        # 任务1·第2轮：流局（被否）
        game.RoundRecord(2, 0, 4, [1, 4], [1, 4], False,
                         [4, 6, 7], [1, 2, 3, 5], False),
        # 任务2·第3轮：改车 + 失败（2 张失败票）
        game.RoundRecord(3, 1, 5, [1, 2, 5], [2, 5, 6], True,
                         [3, 4, 5, 6], [1, 2, 7], True, quest_success=False, fail_count=2),
    ]
    out = game.render_board(log)
    # 车型如实呈现，改车要标注出来
    assert "提案[玩家3、玩家5]→定队[玩家3、玩家5]" in out
    assert "改为[玩家2、玩家5、玩家6](改过车)" in out
    # 流局与任务结果
    assert "否决·流局" in out
    assert "成功(0 张失败票)" in out
    assert "失败(2 张失败票)" in out
    # 比分由记录算出：1 成功 / 1 失败 / 1 流局
    assert "好1:坏1" in out
    assert "累计流局 1/5" in out
    # 空记录
    assert game.render_board([]) == "（暂无已结算的回合）"


def test_spinner_renders_single_line_rotates_and_clears():
    import io
    import time
    from avalon.spinner import Spinner, _FRAMES

    buf = io.StringIO()
    msgs = ["甲在思考", "乙在权衡", "丙在推理"]
    # 用很短的 phrase_every 保证 0.4s 内能轮换出多句；start_delay 调小以尽快开画。
    sp = Spinner(enabled=True, messages=msgs, stream=buf, use_color=False,
                 interval=0.02, phrase_every=0.05, start_delay=0.0)
    with sp:
        time.sleep(0.4)
    out = buf.getvalue()
    # 画过至少一个 braille 帧
    assert any(f in out for f in _FRAMES)
    # 只占一行：绝不含换行
    assert "\n" not in out
    # 原地刷新用了回车 + 清行；退出时就地擦除整行并恢复光标作收尾
    assert "\r" in out and "\033[K" in out
    assert out.endswith("\r\033[K\033[?25h")
    # 文案确实轮换过（至少出现 2 句不同的）
    assert sum(1 for m in msgs if m in out) >= 2


def test_spinner_compose_messages_lead_plus_phase_pool():
    from avalon.spinner import compose_messages, PHASE_POOLS
    # lead + 已知阶段：首句是具体动作，其后接该阶段俏皮话池
    msgs = compose_messages("玩家3 正在斟酌发言", "discuss")
    assert msgs[0] == "玩家3 正在斟酌发言"
    assert msgs[1:] == PHASE_POOLS["discuss"]
    # 只有 lead → 单句不轮换
    assert compose_messages("只有首句", None) == ["只有首句"]
    # 只有 phase → 纯池
    assert compose_messages(None, "vote") == PHASE_POOLS["vote"]
    # 未知 phase 且无 lead → None（交给 Spinner 用 DEFAULT_MESSAGES 兜底）
    assert compose_messages(None, "不存在的阶段") is None
    # 五个阶段池都存在且非空
    assert set(PHASE_POOLS) == {"discuss", "vote", "quest", "assassinate", "review"}
    assert all(PHASE_POOLS[k] for k in PHASE_POOLS)


def test_spinner_disabled_is_noop():
    import io
    import time
    from avalon.spinner import Spinner

    buf = io.StringIO()
    with Spinner(enabled=False, messages=["x"], stream=buf, start_delay=0.0):
        time.sleep(0.05)
    assert buf.getvalue() == ""


def test_full_roster_text_reveals_all_seats_and_sides():
    out = full_roster_text(FIXED_ROLES)
    # 按座位升序、含角色与阵营
    assert out.startswith("本局身份揭示——")
    assert "玩家1：梅林（好人）" in out
    assert "玩家5：莫甘娜（坏人）" in out
    assert "玩家7：奥伯伦（坏人）" in out
    # 7 席全揭示（6 个分隔符）
    assert out.count("；") == 6


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ok  {fn.__name__}")
    print(f"\n{passed}/{len(fns)} 测试通过")
