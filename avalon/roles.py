"""角色定义、阵营划分与夜晚私有信息推导。

7 人局经典全角色：
- 好人 4：梅林 Merlin、派西维尔 Percival、忠臣 Loyal Servant ×2
- 坏人 3：莫甘娜 Morgana、刺客 Assassin、奥伯伦 Oberon

夜晚信息（关键不对称）：
- 梅林看到全部坏人（含奥伯伦）。
- 派西维尔看到「梅林和莫甘娜」两个身份，但分不清谁是谁。
- 坏人互认名单**排除**奥伯伦：只有莫甘娜与刺客互相可见；奥伯伦看不到任何队友（但梅林能看到他）。
- 忠臣无任何信息。
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    MERLIN = "Merlin"
    PERCIVAL = "Percival"
    LOYAL = "Loyal"
    MORGANA = "Morgana"
    ASSASSIN = "Assassin"
    OBERON = "Oberon"


# 中文名，用于展示
ROLE_ZH: dict[Role, str] = {
    Role.MERLIN: "梅林",
    Role.PERCIVAL: "派西维尔",
    Role.LOYAL: "忠臣",
    Role.MORGANA: "莫甘娜",
    Role.ASSASSIN: "刺客",
    Role.OBERON: "奥伯伦",
}

GOOD_ROLES = {Role.MERLIN, Role.PERCIVAL, Role.LOYAL}
EVIL_ROLES = {Role.MORGANA, Role.ASSASSIN, Role.OBERON}

# 7 人局发牌池（发牌时会被打乱后分配到座位）
SETUP_7 = [
    Role.MERLIN,
    Role.PERCIVAL,
    Role.LOYAL,
    Role.LOYAL,
    Role.MORGANA,
    Role.ASSASSIN,
    Role.OBERON,
]

SETUPS: dict[int, list[Role]] = {
    7: SETUP_7,
}


def team_of(role: Role) -> str:
    """返回角色阵营：'good' 或 'evil'。"""
    return "good" if role in GOOD_ROLES else "evil"


def is_good(role: Role) -> bool:
    return role in GOOD_ROLES


def _seats_with_role(roles_by_seat: dict[int, Role], target: Role) -> list[int]:
    return sorted(s for s, r in roles_by_seat.items() if r == target)


def _evil_seats(roles_by_seat: dict[int, Role]) -> list[int]:
    return sorted(s for s, r in roles_by_seat.items() if r in EVIL_ROLES)


def evil_roster_text(roles_by_seat: dict[int, Role]) -> str:
    """刺杀阶段坏人互相公开身份用的文本（含奥伯伦）。仅供坏人 prompt，绝不进公开历史。"""
    parts = [f"玩家{s}（{ROLE_ZH[roles_by_seat[s]]}）" for s in _evil_seats(roles_by_seat)]
    return "本局坏人阵营：" + "、".join(parts) + "。"


def full_roster_text(roles_by_seat: dict[int, Role]) -> str:
    """复盘环节用：揭示全部 7 席真实角色 + 阵营。仅复盘阶段使用，绝不进对局中的公开历史。"""
    parts = [
        f"玩家{s}：{ROLE_ZH[roles_by_seat[s]]}（{'好人' if is_good(roles_by_seat[s]) else '坏人'}）"
        for s in sorted(roles_by_seat)
    ]
    return "本局身份揭示——" + "；".join(parts) + "。"


def build_known_evil(roles_by_seat: dict[int, Role]) -> dict[int, list[int]]:
    """每个座位「夜晚确知是坏人」的其他座位，用于 trust 种子。

    与 build_night_knowledge 的 minion_partners 同源：只有莫甘娜与刺客互认，
    奥伯伦不认任何队友（队友也不认他），好人无任何信息。返回 {seat: [确知坏人座位...]}。
    """
    minion_partners = sorted(
        s for s, r in roles_by_seat.items()
        if r in (Role.MORGANA, Role.ASSASSIN)
    )
    known: dict[int, list[int]] = {}
    for seat, role in roles_by_seat.items():
        if role in (Role.MORGANA, Role.ASSASSIN):
            known[seat] = [s for s in minion_partners if s != seat]
        else:
            known[seat] = []
    return known


def build_night_knowledge(roles_by_seat: dict[int, Role]) -> dict[int, str]:
    """为每个座位生成它的私有夜晚信息文本。

    返回 {seat: 中文描述}，仅供该座位自己的 prompt 使用，绝不进入公开历史。
    座位编号在描述里用「玩家N」表示（N 即座位号）。
    """
    knowledge: dict[int, str] = {}

    merlin_seat = _seats_with_role(roles_by_seat, Role.MERLIN)
    morgana_seat = _seats_with_role(roles_by_seat, Role.MORGANA)
    evil_seats = _evil_seats(roles_by_seat)
    # 莫甘娜与刺客互认（排除奥伯伦）
    minion_partners = sorted(
        s for s, r in roles_by_seat.items()
        if r in (Role.MORGANA, Role.ASSASSIN)
    )

    for seat, role in roles_by_seat.items():
        if role == Role.MERLIN:
            others = sorted(s for s in evil_seats)
            names = "、".join(f"玩家{s}" for s in others)
            knowledge[seat] = (
                f"你是梅林。你能看到所有坏人（含奥伯伦）：{names}。"
                "但你不知道谁是刺客；务必隐藏身份，否则结局会被刺客猜中而落败。"
            )
        elif role == Role.PERCIVAL:
            candidates = sorted(merlin_seat + morgana_seat)
            names = "、".join(f"玩家{s}" for s in candidates)
            knowledge[seat] = (
                f"你是派西维尔。你看到 {names} 两人，其中一个是梅林、一个是莫甘娜，"
                "但你分不清谁是谁。请设法辨认并保护真正的梅林。"
            )
        elif role == Role.LOYAL:
            knowledge[seat] = "你是亚瑟的忠臣，没有任何夜晚信息，只能靠推理找出坏人。"
        elif role in (Role.MORGANA, Role.ASSASSIN):
            partners = [s for s in minion_partners if s != seat]
            names = "、".join(f"玩家{s}" for s in partners) if partners else "（无）"
            extra = "你是刺客，游戏结束时若好人达成 3 次任务，你将有一次猜梅林的机会。" \
                if role == Role.ASSASSIN else "你是莫甘娜，会被派西维尔误认成梅林，可加以利用。"
            knowledge[seat] = (
                f"你是坏人。你的坏人同伴（不含奥伯伦）：{names}。"
                f"注意：奥伯伦也是坏人，但你看不到他、他也看不到你。{extra}"
            )
        elif role == Role.OBERON:
            knowledge[seat] = (
                "你是奥伯伦，属于坏人阵营，但你不认识其他坏人，其他坏人也不认识你。"
                "你需要独自判断局势并暗中破坏任务。注意：梅林能看到你。"
            )
    return knowledge
