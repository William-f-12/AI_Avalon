"""Prompt 加载器：把 common + 角色 + 决策点片段拼成最终 system / user 文本。

prompt 以纯文本（.md）存放，用 <<key>> 作占位符（避免与 JSON 大括号冲突）。
调 prompt 只改 .md，无需动 Python。
"""

from __future__ import annotations

import os
from functools import lru_cache

from ..roles import Role

_DIR = os.path.dirname(os.path.abspath(__file__))

# 角色 → 文件名
_ROLE_FILE = {
    Role.MERLIN: "merlin.md",
    Role.PERCIVAL: "percival.md",
    Role.LOYAL: "loyal.md",
    Role.MORGANA: "morgana.md",
    Role.ASSASSIN: "assassin.md",
    Role.OBERON: "oberon.md",
}


@lru_cache(maxsize=None)
def _read(relpath: str) -> str:
    with open(os.path.join(_DIR, relpath), encoding="utf-8") as f:
        return f.read()


def _fill(text: str, **kwargs) -> str:
    for k, v in kwargs.items():
        text = text.replace(f"<<{k}>>", str(v))
    return text


def build_system(role: Role, seat: int, night_knowledge: str, num_players: int) -> str:
    """组装某个 agent 的 system prompt：通用规则 + 该角色文案（含夜晚私有信息）。"""
    common = _fill(_read("common.md"), num_players=num_players, seat=seat)
    role_text = _fill(_read(f"roles/{_ROLE_FILE[role]}"),
                      seat=seat, night_knowledge=night_knowledge)
    return f"{common}\n\n{role_text}"


def build_action(kind: str, **context) -> str:
    """组装某个决策点的 user prompt。context 用于填充占位符。"""
    return _fill(_read(f"actions/{kind}.md"), **context)
