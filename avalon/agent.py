"""玩家 agent：把引擎需要的决策点映射到 LLM 调用（或随机基线）。

- LLMAgent：角色感知 prompt → LLM → 解析结构化 JSON 动作。
- RandomAgent：零 token 的随机/规则基线，用来离线冒烟测试整局流程。
"""

from __future__ import annotations

import json
import random
import re
from typing import Optional

from .llm import LLMClient
from .prompts import build_system, build_action
from .roles import Role, is_good


def _fmt_seats(seats: list[int]) -> str:
    return "[" + "、".join(f"玩家{s}" for s in seats) + "]" if seats else "[]"


def _parse_json(text: str) -> dict:
    """稳健解析 LLM 返回的 JSON：去掉代码块标记，必要时截取第一个 {...}。"""
    if not text:
        return {}
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return {}
    return {}


class LLMAgent:
    def __init__(self, seat: int, role: Role, night_knowledge: str,
                 llm: LLMClient, num_players: int = 7,
                 history_limit: int = 80, known_evil=()):
        self.seat = seat
        self.role = role
        self.llm = llm
        self.num_players = num_players
        self.history_limit = history_limit
        self._system = build_system(role, seat, night_knowledge, num_players)
        self._history: list[str] = []
        self._board: str = ""  # 引擎推送的结构化「局势战报」（公开事实，覆盖式）
        # 私有信任行：i 对各 j「是好人」的置信度 [-1,1]。仅本 agent 可见可改。
        self._trust: dict[int, float] = {
            s: 0.0 for s in range(1, num_players + 1) if s != seat}
        for s in known_evil:  # 夜晚互认队友直接确认为坏人（仅莫甘娜⇄刺客）
            if s in self._trust:
                self._trust[s] = -1.0

    # ---- 公开历史 ----
    def observe(self, text: str) -> None:
        self._history.append(text)

    def set_board(self, text: str) -> None:
        self._board = text

    def _history_text(self) -> str:
        lines = self._history[-self.history_limit:]
        return "\n".join(lines) if lines else "（暂无公开历史）"

    # ---- 信任行（私有，绝不外泄给其他 agent）----
    def _trust_text(self) -> str:
        # 数字是阵营中立的「好人度」(+1 好人 / -1 坏人)；标签按本 agent 阵营渲染策略含义。
        def label(v: float) -> str:
            if is_good(self.role):
                # 好人视角：好人度高=可信队友，低=要提防的坏人
                if v >= 0.6:
                    return "几乎确定好人·可信队友"
                if v >= 0.2:
                    return "偏好人"
                if v > -0.2:
                    return "阵营不明"
                if v > -0.6:
                    return "偏坏人·留意"
                return "几乎确定坏人·警惕"
            # 坏人视角：好人度高=敌方好人(欺骗/利用/破坏其身份)，低=己方同伙
            if v >= 0.6:
                return "几乎确定好人·欺骗/利用/搞坏其身份的目标"
            if v >= 0.2:
                return "偏好人·潜在目标"
            if v > -0.2:
                return "阵营不明"
            if v > -0.6:
                return "偏坏人·疑似同伙"
            return "几乎确定坏人·同阵营队友(可配合)"
        parts = [f"玩家{s}：{self._trust[s]:+.1f}（{label(self._trust[s])}）"
                 for s in sorted(self._trust)]
        return "｜".join(parts) if parts else "（暂无评估）"

    def _absorb_trust(self, data: dict) -> None:
        """合并 LLM 自报的 trust_update（{座位: 绝对值}）。逐项校验，非法静默丢弃。"""
        upd = data.get("trust_update")
        if not isinstance(upd, dict):
            return
        for k, v in upd.items():
            try:
                seat = int(k)
                val = float(v)
            except (TypeError, ValueError):
                continue
            if seat == self.seat or seat not in self._trust:
                continue
            self._trust[seat] = max(-1.0, min(1.0, val))

    def trust_row(self) -> dict[int, float]:
        """返回本 agent 信任行的拷贝，供引擎只读快照（不暴露给其他 agent）。"""
        return dict(self._trust)

    def _ask(self, kind: str, **ctx) -> dict:
        user = build_action(kind, history=self._history_text(),
                            trust=self._trust_text(),
                            board=self._board or "（暂无已结算的回合）", **ctx)
        try:
            raw = self.llm.complete(self._system, user, json_mode=True)
        except Exception:
            return {}
        data = _parse_json(raw)
        self._absorb_trust(data)
        return data

    # ---- 决策点 ----
    def propose_team(self, size: int) -> list[int]:
        data = self._ask("propose", size=size)
        return data.get("team", [])

    def choose_direction(self) -> str:
        data = self._ask("direction")
        d = str(data.get("direction", "cw")).lower()
        return d if d in ("cw", "ccw") else "cw"

    def speak(self, context: str) -> str:
        data = self._ask("speak", context=context)
        return str(data.get("speech", "")).strip()

    def finalize_team(self, size: int, initial_team: list[int]) -> list[int]:
        data = self._ask("finalize", size=size, initial_team=_fmt_seats(initial_team))
        return data.get("team", list(initial_team))

    def vote_team(self, team: list[int]) -> str:
        data = self._ask("vote", team=_fmt_seats(team))
        return str(data.get("vote", "approve")).lower()

    def quest_card(self) -> str:
        data = self._ask("quest")
        default = "success" if is_good(self.role) else "success"
        return str(data.get("card", default)).lower()

    def advise_merlin(self, candidates: list[int], evil_reveal: str) -> str:
        data = self._ask("advise_merlin",
                         candidates=_fmt_seats(candidates), evil_reveal=evil_reveal)
        advice = str(data.get("advice", "")).strip()
        target = data.get("target")
        if target is not None and advice:
            advice = f"（怀疑玩家{target}）{advice}"
        return advice

    def revise_trust(self, evil_seats: list[int], evil_reveal: str = "") -> None:
        """刺杀阶段坏人互认身份后，再更新一波自己的信任表。

        先让 LLM 结合坏人全名单复盘（_ask 内自动吸收 trust_update），再把此刻确知的
        坏人队友兜底钉为 -1.0（含奥伯伦此刻才得知的队友），保证候选好人池清晰。
        """
        self._ask("revise_trust", evil_reveal=evil_reveal)
        for s in evil_seats:
            if s in self._trust:
                self._trust[s] = -1.0

    def assassinate(self, candidates: list[int],
                    evil_reveal: str = "", consult: str = "") -> tuple[int, str]:
        data = self._ask("assassinate", candidates=_fmt_seats(candidates),
                         evil_reveal=evil_reveal, consult=consult)
        reason = str(data.get("reason", "")).strip()
        try:
            return int(data.get("guess", candidates[0])), reason
        except (TypeError, ValueError):
            return candidates[0], reason

    def review(self, reveal: str) -> str:
        """赛后复盘点评。reveal 为全员真实身份；身份已揭晓，agent 可脱离角色畅所欲言。"""
        data = self._ask("review", reveal=reveal)
        return str(data.get("review", "")).strip()


class RandomAgent:
    """零 token 基线：随机/简单规则。用于离线验证 run_game 串联是否正确。"""

    def __init__(self, seat: int, role: Role, night_knowledge: str = "",
                 num_players: int = 7, rng: Optional[random.Random] = None):
        self.seat = seat
        self.role = role
        self.num_players = num_players
        self.rng = rng or random.Random()

    def observe(self, text: str) -> None:  # 基线不需要历史
        pass

    def set_board(self, text: str) -> None:  # 基线不走 prompt，忽略战报
        pass

    def trust_row(self) -> dict:  # 基线不维护信任，零 token
        return {}

    def propose_team(self, size: int) -> list[int]:
        seats = list(range(1, self.num_players + 1))
        # 偏向把自己带上
        pick = {self.seat}
        while len(pick) < size:
            pick.add(self.rng.choice(seats))
        return sorted(pick)

    def choose_direction(self) -> str:
        return self.rng.choice(["cw", "ccw"])

    def speak(self, context: str) -> str:
        return self.rng.choice(["我觉得这队没问题。", "有点怀疑，再看看。", "先观察。", "我支持队长。"])

    def finalize_team(self, size: int, initial_team: list[int]) -> list[int]:
        # 大多数情况下维持原案
        if self.rng.random() < 0.7:
            return list(initial_team)
        return self.propose_team(size)

    def vote_team(self, team: list[int]) -> str:
        return self.rng.choice(["approve", "approve", "reject"])

    def quest_card(self) -> str:
        if is_good(self.role):
            return "success"
        # 坏人有一定概率投失败
        return "fail" if self.rng.random() < 0.6 else "success"

    def revise_trust(self, evil_seats=(), evil_reveal: str = "") -> None:
        pass  # 基线不维护信任

    def advise_merlin(self, candidates: list[int], evil_reveal: str = "") -> str:
        return ""  # 基线不进言，零 token

    def assassinate(self, candidates: list[int],
                    evil_reveal: str = "", consult: str = "") -> tuple[int, str]:
        return self.rng.choice(candidates), ""

    def review(self, reveal: str = "") -> str:
        return ""  # 基线不复盘，零 token（打印为「（无）」）
