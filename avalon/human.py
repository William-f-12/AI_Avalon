"""人类玩家 agent：把引擎的决策点映射到控制台菜单/输入。

与 LLMAgent / RandomAgent 实现同一套 AgentLike 协议（见 game.py），区别只是
「决策来源」是真人而非模型——除了发言用自由文本，其余决策都给选项让玩家选。
人类不维护信任表（trust_row 返回空）。
"""

from __future__ import annotations

import random
from typing import Optional

try:
    import readline  # noqa: F401  导入即生效：input() 获得行编辑（方向键、中文删除、↑历史）
except ImportError:
    pass  # 个别平台无 readline，退回裸 input()

from .roles import Role, is_good


# ---------------------------------------------------------------------------
# 控制台输入辅助（容错：非法输入一律重问）
# ---------------------------------------------------------------------------

def _fmt_seats(seats) -> str:
    return "[" + "、".join(f"玩家{s}" for s in seats) + "]" if seats else "[空]"


def _menu(title: str, options: list[tuple[str, object]]) -> object:
    """打印带序号的菜单，读一个数字，返回对应的值。

    options 为 [(显示文案, 返回值), ...]。非法输入重问。
    """
    print(title)
    for i, (label, _) in enumerate(options, 1):
        print(f"  {i}. {label}")
    while True:
        try:
            raw = input("请输入序号 > ").strip()
        except EOFError:  # 输入流耗尽（管道/重定向）：自动选第一项，便于非交互冒烟
            print(f"  （输入结束，默认选「{options[0][0]}」）")
            return options[0][1]
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1][1]
        print(f"  （无效输入，请输入 1~{len(options)} 之间的序号）")


def _choose_seats(title: str, candidates: list[int], count: int) -> list[int]:
    """从 candidates 里选 count 个不重复座位，返回升序列表。

    直接用**座位号本身**作为输入键（不是位置序号），所以输入 4 永远是玩家4——
    避免「选过一个后菜单按剩余座位重排、序号不再等于座位号」导致选错人。
    每次都展示完整候选名单并标出已选，输入非数字/越界/重复均重问。
    """
    print(title)
    chosen: list[int] = []
    while len(chosen) < count:
        need = count - len(chosen)
        roster = "  ".join(f"[{s}]{'✓' if s in chosen else ''}" for s in candidates)
        print(f"已选 {_fmt_seats(sorted(chosen))}，还需 {need} 人。座位：{roster}")
        try:
            raw = input("请输入要上车的座位号 > ").strip()
        except EOFError:  # 输入流耗尽：用最小未选座位补足，便于非交互冒烟
            for s in candidates:
                if len(chosen) >= count:
                    break
                if s not in chosen:
                    chosen.append(s)
            print(f"  （输入结束，默认补足为 {_fmt_seats(sorted(chosen))}）")
            return sorted(chosen)
        if not raw.isdigit():
            print("  （请输入座位号数字）")
            continue
        seat = int(raw)
        if seat not in candidates:
            print(f"  （座位号须在 {candidates[0]}~{candidates[-1]} 之间）")
            continue
        if seat in chosen:
            print(f"  （玩家{seat} 已选，请换一个）")
            continue
        chosen.append(seat)
    return sorted(chosen)


def _choose_seat(title: str, candidates: list[int]) -> int:
    """从 candidates 里按**座位号本身**选 1 个，返回座位号。

    与 _choose_seats 同理用座位号作输入键（输入 4 即玩家4），而非位置序号，
    避免刺杀/进言时「序号≠座位号」选错人。候选可能不连续（如刺杀阶段仅好人席），
    故越界提示直接列出全部可选座位。非数字/越界均重问。
    """
    print(title)
    print(f"候选座位：{_fmt_seats(candidates)}")
    while True:
        try:
            raw = input("请输入座位号 > ").strip()
        except EOFError:  # 输入流耗尽：默认选第一个候选，便于非交互冒烟
            print(f"  （输入结束，默认选 玩家{candidates[0]}）")
            return candidates[0]
        if not raw.isdigit():
            print("  （请输入座位号数字）")
            continue
        seat = int(raw)
        if seat not in candidates:
            print(f"  （座位号须是 {_fmt_seats(candidates)} 之一）")
            continue
        return seat


# 人类自由文本（发言/进言/刺杀理由）每次最多字数：防止超长输入塞爆各 agent 的 prompt 上下文。
MAX_INPUT_CHARS = 200


def _read_multiline(prompt: str, max_chars: int = MAX_INPUT_CHARS) -> str:
    """读多行自由文本：回车换行继续，空一行发送；首行直接空行=空串。

    终端无法区分 Cmd+Enter 与 Enter，故以「空行」作发送键（Ctrl-D 兜底亦可发送）。
    返回 strip 后的整段文本；空串表示沉默/跳过，语义与原单行 input().strip() 一致。
    每行仍由 readline 提供行内编辑（方向键、中文删除、历史）。
    超过 max_chars 字会提示并要求精简后**重新输入**（不截断，内容由玩家掌控），以免超长
    发言污染各 agent 的上下文；Ctrl-D（EOF，非交互冒烟）则按上限截断返回、不再重问，
    保证 `</dev/null` 之类的非交互场景不会死循环。
    """
    print(prompt)
    print(f"  （回车换行继续输入；空一行=发送；直接空行=沉默/跳过；最多 {max_chars} 字）")
    while True:
        lines: list[str] = []
        while True:
            try:
                line = input("… " if lines else "> ")
            except EOFError:    # Ctrl-D / 输入流耗尽：截断返回，不再重问（非交互兜底）
                print()
                return "\n".join(lines).strip()[:max_chars]
            if not line:        # 空行：首行=沉默/跳过；有内容后=发送
                break
            lines.append(line)
        text = "\n".join(lines).strip()
        if len(text) <= max_chars:
            return text
        print(f"  （超出 {max_chars} 字上限：当前 {len(text)} 字，请精简后重新输入）")


def _banner(text: str) -> None:
    print("\n" + "=" * 56)
    print(text)
    print("=" * 56)


# ---------------------------------------------------------------------------
# HumanAgent
# ---------------------------------------------------------------------------

class HumanAgent:
    """人类玩家：控制台交互。实现 AgentLike 协议。"""

    def __init__(self, seat: int, role: Role, night_knowledge: str = "",
                 num_players: int = 7, rng: Optional[random.Random] = None):
        self.seat = seat
        self.role = role
        self.num_players = num_players
        self.rng = rng or random.Random()
        self._night_knowledge = night_knowledge
        self._intro_shown = False
        # 标记：引擎并发收集时把交互式 agent 留在主线程，避免与后台线程抢 stdin。
        self.interactive = True

    # ---- 开局私密提示（仅本玩家可见）----
    def _show_intro(self) -> None:
        if self._intro_shown:
            return
        self._intro_shown = True
        _banner(f"你是玩家{self.seat}（人类玩家）— 以下信息仅你可见")
        print(self._night_knowledge or "（无夜晚信息）")
        try:
            input("\n看完后按回车继续，准备开始……")
        except EOFError:  # 非交互（重定向 stdin）时无回车可读，直接继续
            print()

    def _turn_header(self, context: str) -> None:
        self._show_intro()
        _banner(f"轮到你（玩家{self.seat}）｜{context}")

    # ---- 公开历史 / 信任：人类直接看终端，不需要 ----
    def observe(self, text: str) -> None:
        # 首条公开事件（game_start）触发开局私密角色提示，保证玩家先看到自己身份。
        self._show_intro()

    def set_board(self, text: str) -> None:  # 人类看终端实时输出，暂不另外展示战报
        pass

    def trust_row(self) -> dict:
        return {}

    def revise_trust(self, evil_seats=(), evil_reveal: str = "") -> None:
        pass

    # ---- 决策点 ----
    def propose_team(self, size: int) -> list[int]:
        self._turn_header(f"你是队长，请组建 {size} 人任务队伍")
        seats = list(range(1, self.num_players + 1))
        return _choose_seats(f"从全部玩家中选 {size} 人上车（可含自己）：", seats, size)

    def choose_direction(self) -> str:
        self._turn_header("选择其余玩家的发言方向")
        return _menu("发言绕圈方向：", [
            ("顺时针（座位号递增）", "cw"),
            ("逆时针（座位号递减）", "ccw"),
        ])

    def speak(self, context: str) -> str:
        # 引擎传入的 context 已说明情境（含是否队长/补充发言），直接作为标题，避免重复。
        self._show_intro()
        _banner(context)
        return _read_multiline("请输入你的发言：")

    def finalize_team(self, size: int, initial_team: list[int]) -> list[int]:
        self._turn_header(f"最终定队（当前车型 {_fmt_seats(initial_team)}）")
        keep = _menu("是否维持原车型？", [
            (f"维持原车型 {_fmt_seats(initial_team)}（可再补一次发言）", True),
            ("改车（重新选人，改后不再发言）", False),
        ])
        if keep:
            return list(initial_team)
        seats = list(range(1, self.num_players + 1))
        return _choose_seats(f"重新选 {size} 人上车：", seats, size)

    def vote_team(self, team: list[int]) -> str:
        self._turn_header(f"对车型 {_fmt_seats(team)} 投票")
        return _menu("你的投票：", [("赞成", "approve"), ("反对", "reject")])

    def quest_card(self) -> str:
        self._turn_header("你在任务队伍中，暗投任务牌")
        if is_good(self.role):
            print("你是好人，任务牌强制为「成功」。")
            return "success"
        return _menu("你的任务牌（坏人可破坏）：", [
            ("成功", "success"), ("失败", "fail"),
        ])

    def advise_merlin(self, candidates: list[int], evil_reveal: str = "") -> str:
        self._turn_header("刺杀环节：向刺客进言（你不是刺客但同属坏人阵营）")
        if evil_reveal:
            print(f"坏人阵营：{evil_reveal}")
        target = _choose_seat("你怀疑谁是梅林？（供刺客参考）", candidates)
        comment = _read_multiline("补一句进言（可选）：")
        return f"（怀疑玩家{target}）{comment}" if comment else f"（怀疑玩家{target}）"

    def assassinate(self, candidates: list[int],
                    evil_reveal: str = "", consult: str = "") -> tuple[int, str]:
        self._turn_header("刺杀环节：你是刺客，指认谁是梅林")
        if evil_reveal:
            print(f"坏人阵营：{evil_reveal}")
        if consult and consult != "（无同伴进言）":
            print("同伴进言：")
            print(consult)
        guess = _choose_seat("从好人候选里指认梅林（猜中则坏人翻盘）：", candidates)
        reason = _read_multiline("刺杀理由（可选）：")
        return int(guess), reason

    def review(self, reveal: str = "") -> str:
        # 人类玩家不发表复盘：_run_review 已按 interactive 标记跳过本席，绝不会调到这里。
        # 仅为满足 AgentLike 协议完整性而保留。
        return ""
