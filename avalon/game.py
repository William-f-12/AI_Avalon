"""阿瓦隆 7 人局引擎：状态、纯函数规则判定，以及主循环编排。

规则判定全部做成纯函数（不调 LLM），便于离线单测；run_game 负责把 LLM agent
的决策串成完整一局。
"""

from __future__ import annotations

import random
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional, Protocol

from .roles import (Role, ROLE_ZH, SETUPS, is_good, build_night_knowledge,
                    evil_roster_text, full_roster_text)

# 任务队伍人数（7 人局）
QUEST_SIZES = [2, 3, 3, 4, 4]
# 第 4 个任务（0-indexed 为 3）需要 2 张失败票
DOUBLE_FAIL_QUEST = 3
# 全局累计流局达到此数 → 坏人赢
MAX_REJECTS = 5
# 任一阵营达到此任务数即决出（好人需再经刺杀）
WINS_NEEDED = 3


# ---------------------------------------------------------------------------
# 纯函数规则判定
# ---------------------------------------------------------------------------

def quest_size(quest_idx: int) -> int:
    return QUEST_SIZES[quest_idx]


def quest_fail_threshold(quest_idx: int) -> int:
    """该任务需要多少张失败票才算失败。"""
    return 2 if quest_idx == DOUBLE_FAIL_QUEST else 1


def apply_vote(votes: list[str], num_players: int) -> bool:
    """多数赞成则通过（严格过半）。votes 为 'approve'/'reject' 列表。"""
    approvals = sum(1 for v in votes if v == "approve")
    return approvals * 2 > num_players


def apply_quest(cards: list[str], quest_idx: int) -> bool:
    """返回任务是否成功。cards 为 'success'/'fail' 列表。"""
    fails = sum(1 for c in cards if c == "fail")
    return fails < quest_fail_threshold(quest_idx)


def speaking_order(leader: int, direction: str, seats: list[int]) -> list[int]:
    """按顺/逆时针返回除车主外其余座位的发言顺序。

    seats 为全部座位（升序）。direction: 'cw' 顺时针(座位号递增) / 'ccw' 逆时针。
    """
    n = len(seats)
    idx = seats.index(leader)
    step = 1 if direction == "cw" else -1
    order = []
    for k in range(1, n):
        order.append(seats[(idx + step * k) % n])
    return order


def _seats_str(seats: list[int]) -> str:
    return "[" + "、".join(f"玩家{s}" for s in seats) + "]" if seats else "[空]"


def render_board(round_log: list["RoundRecord"]) -> str:
    """把每轮结构化记录渲染成公开「局势战报」文本（纯函数，零 token，可单测）。

    比分与累计流局直接从 round_log 算出，故只依赖记录本身。
    """
    if not round_log:
        return "（暂无已结算的回合）"
    lines = ["## 局势战报（公开事实，全员可见，按时间顺序）"]
    for r in round_log:
        team_part = (
            f"提案{_seats_str(r.proposed_team)}→改为{_seats_str(r.final_team)}(改过车)"
            if r.changed else
            f"提案{_seats_str(r.proposed_team)}→定队{_seats_str(r.final_team)}"
        )
        vote_part = (
            f"赞成{_seats_str(r.approve)}({len(r.approve)})/"
            f"反对{_seats_str(r.reject)}({len(r.reject)})"
        )
        head = f"- 任务{r.quest_idx + 1}·第{r.round_no}轮｜队长玩家{r.leader}｜{team_part}｜{vote_part}"
        if not r.approved:
            lines.append(f"{head}→否决·流局")
        else:
            res = "成功" if r.quest_success else "失败"
            lines.append(f"{head}→通过｜结果：{res}({r.fail_count} 张失败票)")
    successes = sum(1 for r in round_log if r.approved and r.quest_success)
    fails = sum(1 for r in round_log if r.approved and r.quest_success is False)
    rejects = sum(1 for r in round_log if not r.approved)
    lines.append(f"当前比分 好{successes}:坏{fails}｜累计流局 {rejects}/{MAX_REJECTS}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 游戏状态
# ---------------------------------------------------------------------------

@dataclass
class RoundRecord:
    """一次提队回合的公开事实快照，喂给所有 agent 作「局势战报」。"""
    round_no: int
    quest_idx: int                  # 0-based；任务号 = quest_idx + 1
    leader: int
    proposed_team: list[int]        # 车主最初提案
    final_team: list[int]           # 最终实际车型
    changed: bool                   # 是否改过车
    approve: list[int]              # 赞成座位
    reject: list[int]               # 反对座位
    approved: bool                  # 是否通过（False = 流局）
    quest_success: Optional[bool] = None   # 流局则 None
    fail_count: Optional[int] = None       # 流局则 None


@dataclass
class GameState:
    num_players: int
    roles_by_seat: dict[int, Role]
    seats: list[int]
    leader: int
    quest_results: list[bool] = field(default_factory=list)  # True=成功
    reject_count: int = 0                                     # 全局累计流局，跨任务不重置
    winner: Optional[str] = None                             # 'good' / 'evil'
    events: list[dict] = field(default_factory=list)          # 公开历史（机器可读）
    round_log: list[RoundRecord] = field(default_factory=list)  # 每轮结构化战报

    @property
    def current_quest_idx(self) -> int:
        return len(self.quest_results)

    @property
    def successes(self) -> int:
        return sum(1 for r in self.quest_results if r)

    @property
    def fails(self) -> int:
        return sum(1 for r in self.quest_results if not r)

    def next_leader(self) -> int:
        idx = self.seats.index(self.leader)
        return self.seats[(idx + 1) % self.num_players]

    def advance_leader(self) -> None:
        self.leader = self.next_leader()

    def check_quest_outcome(self) -> Optional[str]:
        """仅依据任务进度与流局数判定。

        返回：
          'evil'        坏人已锁定胜利（3 次任务失败，或全局累计 5 次流局）
          'good_quests' 好人已达成 3 次任务，需进入刺杀环节
          None          继续游戏
        """
        if self.fails >= WINS_NEEDED or self.reject_count >= MAX_REJECTS:
            return "evil"
        if self.successes >= WINS_NEEDED:
            return "good_quests"
        return None


def deal_roles(num_players: int, seed: Optional[int] = None) -> dict[int, Role]:
    """随机发牌：把发牌池打乱后分配到座位 1..n。"""
    if num_players not in SETUPS:
        raise ValueError(f"暂不支持 {num_players} 人局")
    pool = list(SETUPS[num_players])
    rng = random.Random(seed)
    rng.shuffle(pool)
    return {seat: pool[seat - 1] for seat in range(1, num_players + 1)}


def new_game(num_players: int = 7, seed: Optional[int] = None,
             leader: Optional[int] = None) -> GameState:
    roles_by_seat = deal_roles(num_players, seed)
    seats = list(range(1, num_players + 1))
    if leader is None:
        leader = random.Random(seed).choice(seats) if seed is not None else random.choice(seats)
    return GameState(
        num_players=num_players,
        roles_by_seat=roles_by_seat,
        seats=seats,
        leader=leader,
    )


# ---------------------------------------------------------------------------
# Agent 协议（agent.py 实现，引擎只依赖这些方法）
# ---------------------------------------------------------------------------

class AgentLike(Protocol):
    seat: int
    role: Role

    def propose_team(self, size: int) -> list[int]: ...
    def choose_direction(self) -> str: ...
    def speak(self, context: str) -> str: ...
    def finalize_team(self, size: int, initial_team: list[int]) -> list[int]: ...
    def vote_team(self, team: list[int]) -> str: ...
    def quest_card(self) -> str: ...
    def advise_merlin(self, candidates: list[int], evil_reveal: str) -> str: ...
    def assassinate(self, candidates: list[int],
                    evil_reveal: str = "", consult: str = "") -> tuple[int, str]: ...
    def revise_trust(self, evil_seats: list[int], evil_reveal: str = "") -> None: ...
    def review(self, reveal: str) -> str: ...
    def observe(self, text: str) -> None: ...
    def set_board(self, text: str) -> None: ...
    def trust_row(self) -> dict[int, float]: ...


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

def _snapshot_trust(agents, logger, round_no, phase: Optional[str] = None) -> None:
    """把各 agent 私有信任行组装成对齐的 NxN 二维数组，交给 logger（写 jsonl，并由
    finish 渲染进 .md 上帝视角段落）。仅记录、绝不回流给 agent。"""
    seats = sorted(agents)
    rows = {s: agents[s].trust_row() for s in seats}
    matrix = [[None if i == j else rows[i].get(j) for j in seats] for i in seats]
    payload = {"round": round_no, "seats": seats, "matrix": matrix}
    if phase:
        payload["phase"] = phase
    logger.snapshot("trust", payload)


def _gather(seats, agents, call, parallel: bool = True) -> dict:
    """对每个 seat 执行 call(agent)，返回 {seat: result}。

    用于彼此独立的决策（投票、暗投任务牌）：各 agent 决策互不依赖，可并发。
    parallel=False → 按座位序逐个串行（等价改动前行为，调试/规避限流用）。
    parallel=True  → 交互式 agent（人类，标了 interactive）留主线程避免 stdin 争用；
                     其余 LLM/Random agent 并发——人类思考时 LLM 同伴在后台同时计算。
    结果按 {seat: result} 回填，与完成顺序无关，故聚合输出仍确定。
    """
    if not parallel:
        return {s: call(agents[s]) for s in seats}
    concurrent_seats = [s for s in seats if not getattr(agents[s], "interactive", False)]
    interactive_seats = [s for s in seats if getattr(agents[s], "interactive", False)]
    results: dict = {}
    futs: dict = {}
    ex = None
    if concurrent_seats:
        ex = ThreadPoolExecutor(max_workers=len(concurrent_seats))
        futs = {ex.submit(call, agents[s]): s for s in concurrent_seats}
    for s in interactive_seats:  # 主线程处理人类（后台 LLM 并发进行）
        results[s] = call(agents[s])
    for fut in futs:
        results[futs[fut]] = fut.result()
    if ex:
        ex.shutdown()
    return results


def _push_board(agents, state: GameState) -> None:
    """把当前结构化战报渲染后推送给每个 agent（覆盖式，非追加）。"""
    text = render_board(state.round_log)
    for ag in agents.values():
        ag.set_board(text)


def _record_round(state: GameState, round_no: int, q_idx: int, leader: int,
                  initial_team, final_team, approve, reject, approved: bool,
                  quest_success: Optional[bool] = None,
                  fail_count: Optional[int] = None) -> None:
    """把一次提队回合的公开事实落入 state.round_log。"""
    state.round_log.append(RoundRecord(
        round_no=round_no, quest_idx=q_idx, leader=leader,
        proposed_team=list(initial_team), final_team=list(final_team),
        changed=set(final_team) != set(initial_team),
        approve=list(approve), reject=list(reject), approved=approved,
        quest_success=quest_success, fail_count=fail_count,
    ))


def run_game(agents: dict[int, "AgentLike"], state: GameState, logger,
             parallel: bool = True, review: bool = True, spinner: bool = True) -> str:
    """跑完整一局，返回 'good' / 'evil'。

    agents: {seat: agent}；logger 需提供 .event(kind, payload) 与 .reveal(...)。
    parallel：投票/任务牌是否并发执行（发言始终串行）；默认开，可关闭退回串行。
    review：终局后是否跑赛后复盘轮（各 agent 点评，打在结果横幅之前）；默认开。
    spinner：等待 LLM 时是否在终端最后一行显示「思考中」动态提示；默认开，但仅当
             输出是 TTY、非静默、且无人类玩家时才真正生效（否则会与管道输出/人类输入冲突）。
    """
    # 「思考中」spinner 启用条件：真 TTY + 非批量静默 + 无人类（人类在主线程 input() 交互，会抢最后一行）。
    has_human = any(getattr(a, "interactive", False) for a in agents.values())
    logger.spinner_enabled = (
        spinner and sys.stdout.isatty() and not logger.quiet and not has_human)

    def announce(kind: str, text: str, payload: Optional[dict] = None) -> None:
        """记录一条公开事件：写进 state.events、广播给所有 agent、打日志。"""
        entry = {"kind": kind, "text": text, **(payload or {})}
        state.events.append(entry)
        for ag in agents.values():
            ag.observe(text)
        logger.event(kind, entry)

    logger.start(state)
    announce("game_start", f"游戏开始：7 人局，初始队长为 玩家{state.leader}。")

    round_no = 0
    while state.winner is None:
        outcome = state.check_quest_outcome()
        if outcome == "evil":
            state.winner = "evil"
            break
        if outcome == "good_quests":
            state.winner = _run_assassination(agents, state, logger, announce, round_no, parallel)
            break

        round_no += 1
        q_idx = state.current_quest_idx
        size = quest_size(q_idx)
        leader = state.leader
        leader_agent = agents[leader]
        announce(
            "round_start",
            f"第 {round_no} 轮｜任务 {q_idx + 1}（需 {size} 人，失败阈值 "
            f"{quest_fail_threshold(q_idx)} 票）｜队长：玩家{leader}｜"
            f"当前任务比分 好{state.successes}:坏{state.fails}｜累计流局 {state.reject_count}/{MAX_REJECTS}。",
        )
        # 把「截至上一轮」的结构化战报推给所有 agent（本轮当前车型仍由 history/context 承载）
        _push_board(agents, state)

        # ---- 发言流程 ----
        # 1) 车主定初始车型
        with logger.thinking(f"玩家{leader} 正在组建队伍", phase="discuss"):
            proposed = leader_agent.propose_team(size)
        initial_team = _sanitize_team(proposed, size, state.seats)
        announce("propose", f"玩家{leader}（队长）提出车型：{_fmt(initial_team)}。",
                 {"leader": leader, "team": initial_team})
        # 2) 车主先发言
        _do_speak(leader_agent, announce, "你作为队长，先就这次提队发表看法", logger)
        # 3) 车主选方向
        with logger.thinking(f"玩家{leader} 正在选择发言方向", phase="discuss"):
            direction = leader_agent.choose_direction()
        direction = direction if direction in ("cw", "ccw") else "cw"
        dir_zh = "顺时针" if direction == "cw" else "逆时针"
        announce("direction", f"玩家{leader} 选择 {dir_zh} 发言。", {"direction": direction})
        # 4) 其余玩家绕圈发言
        for s in speaking_order(leader, direction, state.seats):
            _do_speak(agents[s], announce, f"轮到你（玩家{s}）发言", logger)
        # 5) 车主最终定队
        with logger.thinking(f"玩家{leader} 正在斟酌最终车型", phase="discuss"):
            finalized = leader_agent.finalize_team(size, initial_team)
        final_team = _sanitize_team(finalized, size, state.seats)
        if set(final_team) == set(initial_team):
            team = list(initial_team)
            announce("finalize", f"玩家{leader} 维持原车型 {_fmt(team)}，并补充发言。",
                     {"leader": leader, "team": team, "changed": False})
            _do_speak(leader_agent, announce, "你维持原车型，做最后补充发言", logger)
        else:
            team = final_team
            announce("finalize",
                     f"玩家{leader} 改变车型为 {_fmt(team)}（改型后不再发言）。",
                     {"leader": leader, "team": team, "changed": True})

        # ---- 投票（各 agent 独立，可并发）----
        with logger.thinking("玩家们正在投票表决", phase="vote"):
            raw_votes = _gather(state.seats, agents, lambda ag: ag.vote_team(team), parallel)
        votes = {s: _norm_vote(raw_votes[s]) for s in state.seats}
        approve_seats = [s for s in state.seats if votes[s] == "approve"]
        reject_seats = [s for s in state.seats if votes[s] == "reject"]
        approved = apply_vote(list(votes.values()), state.num_players)
        announce(
            "vote",
            f"投票结果：赞成 {_fmt(approve_seats)}｜反对 {_fmt(reject_seats)} → "
            f"{'通过' if approved else '否决'}。",
            {"votes": votes, "approved": approved, "team": team},
        )

        if not approved:
            state.reject_count += 1
            announce("reject", f"提队被否，累计流局 {state.reject_count}/{MAX_REJECTS}。",
                     {"reject_count": state.reject_count})
            _record_round(state, round_no, q_idx, leader, initial_team, team,
                          approve_seats, reject_seats, approved=False)
            _snapshot_trust(agents, logger, round_no)
            state.advance_leader()
            continue

        # ---- 执行任务（暗投，各队员独立，可并发）----
        with logger.thinking("队员们正在执行任务", phase="quest"):
            raw_cards = _gather(team, agents, lambda ag: ag.quest_card(), parallel)
        cards = [_norm_card(raw_cards[s], agents[s].role) for s in team]
        success = apply_quest(cards, q_idx)
        fail_count = cards.count("fail")
        state.quest_results.append(success)
        announce(
            "quest_result",
            f"任务 {q_idx + 1} {'成功' if success else '失败'}（{fail_count} 张失败票）。"
            f" 当前比分 好{state.successes}:坏{state.fails}。",
            {"quest_idx": q_idx, "success": success, "fail_count": fail_count, "team": team},
        )
        _record_round(state, round_no, q_idx, leader, initial_team, team,
                      approve_seats, reject_seats, approved=True,
                      quest_success=success, fail_count=fail_count)
        # 任务执行后流局计数清零？按规则不重置——保持全局累计，不动 reject_count。
        _snapshot_trust(agents, logger, round_no)
        state.advance_leader()

    logger.easter_egg(state)  # 定胜负后、复盘前的结算彩蛋（仅终端、不入日志）
    if review:
        _run_review(agents, state, logger, parallel)
    logger.finish(state)
    return state.winner


def _run_assassination(agents, state: GameState, logger, announce, round_no: int = 0,
                       parallel: bool = True) -> str:
    """好人凑齐 3 次任务后进入刺杀环节：坏人先合议（互认身份 + 同伴进言），刺客拍板猜梅林。

    猜中→坏人赢，否则好人赢。合议信息仅在坏人内部流通、并在上帝视角复盘可见，
    绝不通过 announce 广播给好人，也不写入公开 JSONL。
    """
    assassin_seat = next(s for s, r in state.roles_by_seat.items() if r == Role.ASSASSIN)
    # 坏人已互认，刺客不会指认队友：候选收窄到好人座位
    candidates = [s for s in state.seats if is_good(state.roles_by_seat[s])]
    announce("assassination_phase",
             f"好人已达成 3 次任务，进入刺杀环节：玩家{assassin_seat}（刺客）将指认梅林。")
    _push_board(agents, state)  # 刺杀阶段也让坏人看到完整局势战报

    # ---- 坏人合议（保密，仅复盘可见）----
    evil_reveal = evil_roster_text(state.roles_by_seat)
    logger.secret(f"刺杀阶段坏人互认 → {evil_reveal}")
    all_evil = [s for s in state.seats if not is_good(state.roles_by_seat[s])]
    # 互认身份后，各坏人（含奥伯伦——此刻才得知队友）再更新一波自己的信任表。
    # 各坏人只读写自身信任行、互不依赖，可并发；evil_seats 因人而异，用 ag.seat 现算。
    with logger.thinking("坏人阵营正在合议", phase="assassinate"):
        _gather(all_evil, agents,
                lambda ag: ag.revise_trust([e for e in all_evil if e != ag.seat], evil_reveal),
                parallel)
    _snapshot_trust(agents, logger, round_no, phase="assassination")

    # 进言者彼此看不到对方进言，可并发；收集后仍按座位序重组，保持 secret 输出与拼装确定。
    advisors = [s for s in all_evil if s != assassin_seat]
    with logger.thinking("坏人正在向刺客进言", phase="assassinate"):
        advice_by_seat = _gather(
            advisors, agents,
            lambda ag: (ag.advise_merlin(candidates, evil_reveal) or "").strip(),
            parallel)
    consult_lines: list[str] = []
    for s in advisors:
        advice = advice_by_seat[s]
        if advice:
            line = f"玩家{s}（{ROLE_ZH[state.roles_by_seat[s]]}）：{advice}"
            consult_lines.append(line)
            logger.secret(f"同伴进言 → {line}")
    consult = "\n".join(consult_lines) if consult_lines else "（无同伴进言）"

    with logger.thinking("刺客正在锁定梅林", phase="assassinate"):
        guess, reason = agents[assassin_seat].assassinate(
            candidates, evil_reveal=evil_reveal, consult=consult)
    if guess not in candidates:
        guess = candidates[0]
    hit = state.roles_by_seat.get(guess) == Role.MERLIN
    winner = "evil" if hit else "good"
    reason_txt = f"（理由：{reason}）" if reason else ""
    announce("assassination",
             f"刺客指认 玩家{guess} 为梅林{reason_txt} → "
             f"{'命中，坏人翻盘' if hit else '猜错，好人获胜'}。",
             {"guess": guess, "hit": hit, "reason": reason})
    return winner


def _run_review(agents, state: GameState, logger, parallel: bool = True) -> None:
    """终局后的赛后复盘轮：揭晓全员真实身份，各 agent 基于整局记录发表点评。

    各 agent 的点评互不依赖（只读自身 _history/_board/_trust + 入参 reveal）。为兼顾「并行提速」
    与「序号小的先得到回应、一个一个按 1→N 打印」：并行模式下把全部 reviewer 提交线程池，但
    **按座位序逐个等待 future 再打印**——seat1 先被等到并打印，其余在后台同时算，到点即出。
    **不走 announce**——赛后无人再决策；复盘只交给 logger，不入 state.events。
    人类（interactive）席不发表复盘，直接跳过。
    """
    reveal = full_roster_text(state.roles_by_seat)
    _push_board(agents, state)  # 确保各 agent 看到截至终局的完整战报
    reviewers = [s for s in sorted(state.seats)
                 if not getattr(agents[s], "interactive", False)]
    logger.review_start(reveal)

    def one(seat: int) -> str:
        # 取该 agent 的复盘并清洗：折叠所有空白/换行成单行，杜绝「乱换行」。
        raw = (agents[seat].review(reveal) or "").strip()
        return " ".join(raw.split())

    if parallel and len(reviewers) > 1:
        ex = ThreadPoolExecutor(max_workers=len(reviewers))
        try:
            futs = {s: ex.submit(one, s) for s in reviewers}
            for s in reviewers:  # 按座位序等待+打印：保证小号先得到回应、先出
                with logger.thinking(f"玩家{s} 正在复盘点评", phase="review"):
                    text = futs[s].result()
                logger.review_line(s, ROLE_ZH[state.roles_by_seat[s]], text or "（无）")
        finally:
            ex.shutdown()
    else:  # 串行：逐个思考、逐个打印
        for s in reviewers:
            with logger.thinking(f"玩家{s} 正在复盘点评", phase="review"):
                text = one(s)
            logger.review_line(s, ROLE_ZH[state.roles_by_seat[s]], text or "（无）")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _fmt(seats: list[int]) -> str:
    return "[" + "、".join(f"玩家{s}" for s in seats) + "]" if seats else "[空]"


def _do_speak(agent, announce, prompt_hint: str, logger) -> None:
    with logger.thinking(f"玩家{agent.seat} 正在斟酌发言", phase="discuss"):
        text = agent.speak(prompt_hint) or "（沉默）"
    announce("speak", f"玩家{agent.seat}：{text}", {"seat": agent.seat})


def _sanitize_team(team, size: int, seats: list[int]) -> list[int]:
    """容错：去重、过滤非法座位、补足/截断到 size。"""
    valid = []
    for s in team or []:
        try:
            s = int(s)
        except (TypeError, ValueError):
            continue
        if s in seats and s not in valid:
            valid.append(s)
    # 截断
    valid = valid[:size]
    # 补足（按座位号顺序找未入选的补上）
    for s in seats:
        if len(valid) >= size:
            break
        if s not in valid:
            valid.append(s)
    return sorted(valid)


def _norm_vote(v: str) -> str:
    return "approve" if str(v).strip().lower() in ("approve", "赞成", "yes", "y", "同意") else "reject"


def _norm_card(card: str, role: Role) -> str:
    c = str(card).strip().lower()
    wants_fail = c in ("fail", "失败", "f")
    # 好人强制成功，引擎层兜底防作弊
    if is_good(role):
        return "success"
    return "fail" if wants_fail else "success"
