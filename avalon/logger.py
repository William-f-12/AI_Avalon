"""对局日志：彩色终端 + 结构化 JSONL + 人类可读 Markdown 复盘。

严格区分公开信息（写入所有渠道）与上帝视角（仅 Markdown/终端的复盘部分揭示身份，
绝不进入任何 agent 的 prompt —— 后者由 run_game 控制，logger 只负责记录）。
"""

from __future__ import annotations

import json
import os
import random
from datetime import datetime
from typing import Optional

from .roles import ROLE_ZH, team_of
from .spinner import Spinner, compose_messages

# 终端颜色（ANSI）；若安装了 rich 则用 rich 渲染。
_ANSI = {
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m", "gray": "\033[90m",
}

# 发言/复盘里「玩家N」名字统一用这一个颜色（内容仍用灰色），让名字和正文一眼区分。
_NAME_COLOR = "cyan"


# 事件类型 → 颜色（speak 与 review 的名字另用 _NAME_COLOR，见 event/review_line）
_KIND_COLOR = {
    "game_start": "bold", "round_start": "cyan", "propose": "blue",
    "speak": "gray", "direction": "blue", "finalize": "blue",
    "vote": "yellow", "reject": "red", "quest_result": "green",
    "assassination_phase": "magenta", "assassination": "magenta",
    "review_start": "bold", "review": "yellow",
}


# 终局彩蛋：定胜负后、复盘前随机打一句俏皮话。**仅终端、不进任何日志**（纯气氛组）。
# 三种局面分别取一池：好人获胜 / 坏人靠任务（或流局）获胜 / 梅林被刺杀翻盘。
_EASTER_EGGS = {
    # 好人获胜（必经刺杀，即刺客没猜中梅林）
    "good": [
        "莫甘娜气得当场扇了刺客一个大嘴巴子：连个梅林都猜不中！",
        "梅林跳到刺客脸上疯狂嘲讽：就这？就这水平也配刺杀我？",
        "派西维尔长舒一口气，决定回去给梅林在圆桌上留个首席。",
        "亚瑟王举杯：敬光明！坏人那桌的酒今晚格外苦。",
        "忠臣们簇拥着梅林欢呼，奥伯伦在角落独自喝闷酒——他到现在都没搞懂队友是谁。",
    ],
    # 坏人靠任务失败/流局正常获胜（梅林还没来得及被刺）
    "evil_quests": [
        "忠臣们连夜上书亚瑟王，强烈建议开除这个弱智梅林。",
        "莫甘娜优雅地行了个礼：感谢各位好人今晚的精彩配合。",
        "刺客把匕首又揣回兜里：今天连出手的机会都省了。",
        "派西维尔懊悔不已：我守了一整局，结果守错了人。",
        "奥伯伦一脸懵：我们……赢了？可我连谁是队友都不知道。",
    ],
    # 梅林被刺杀翻盘（好人已凑齐 3 次任务，却被刺客一刀掀桌）
    "evil_assassinate": [
        "刺客的匕首精准命中——梅林发出嘶声裂肺的惨叫：不——！！！",
        "莫甘娜冷笑：藏得再深，也逃不过刺客的眼睛。",
        "好人三票任务到手，却被刺客一刀掀了桌：到嘴的胜利飞了。",
        "梅林瘫坐在地：我明明什么都知道，却输在了最后一秒。",
        "刺客擦了擦刀锋，意味深长：知道得太多，可不是什么好事。",
    ],
}


class GameLogger:
    def __init__(self, game_id: Optional[str] = None, logs_dir: str = "logs",
                 use_color: bool = True, reveal_roles: bool = True,
                 whisper_to_terminal: bool = True, quiet: bool = False):
        self.game_id = game_id or datetime.now().strftime("game_%Y%m%d_%H%M%S")
        self.logs_dir = logs_dir
        self.use_color = use_color
        self.reveal_roles = reveal_roles
        # 上帝视角悄悄话（坏人互认、同伴进言）是否打到终端。人类玩家在场时关掉防剧透。
        self.whisper_to_terminal = whisper_to_terminal
        # 静默：跨局并行时多局抢终端会交错成一团，故只写 jsonl/md、不打终端。
        self.quiet = quiet
        # 「思考中」spinner 是否启用：由 run_game 按 TTY/quiet/有无人类综合设定。默认关。
        self.spinner_enabled = False
        os.makedirs(logs_dir, exist_ok=True)
        self.jsonl_path = os.path.join(logs_dir, f"{self.game_id}.jsonl")
        self.md_path = os.path.join(logs_dir, f"{self.game_id}.md")
        self._jsonl = open(self.jsonl_path, "w", encoding="utf-8")
        self._md_lines: list[str] = []
        # 各轮 trust 快照（仅供 finish 渲染进 .md 上帝视角；agent 永不读 .md）。
        self._trust_snaps: list[dict] = []

    # ---- 「思考中」动态状态行 ----
    def thinking(self, lead=None, phase=None) -> Spinner:
        """返回一个 Spinner 上下文管理器，等待 LLM 期间在终端最后一行显示会动的提示。

        lead：当前具体在做什么（如「玩家3 正在斟酌发言」），作首句；
        phase：阶段名（discuss/vote/quest/assassinate/review），其后轮换该阶段俏皮话池。
        spinner 只写终端、绝不碰 _md_lines/_jsonl，故对日志零影响；enabled 取决于
        self.spinner_enabled（由 run_game 设定），关闭时为空操作。
        """
        return Spinner(enabled=self.spinner_enabled,
                       messages=compose_messages(lead, phase),
                       use_color=self.use_color)

    # ---- 着色 ----
    def _c(self, text: str, color: str) -> str:
        if not self.use_color:
            return text
        code = _ANSI.get(color, "")
        return f"{code}{text}{_ANSI['reset']}" if code else text

    # ---- 生命周期 ----
    def start(self, state) -> None:
        title = f"=== 阿瓦隆 {state.num_players} 人局 [{self.game_id}] ==="
        if not self.quiet:
            print(self._c(title, "bold"))
        self._md_lines.append(f"# 阿瓦隆对局复盘 — {self.game_id}\n")
        self._md_lines.append(f"- 玩家数：{state.num_players}")
        self._md_lines.append(f"- 初始队长：玩家{state.leader}\n")
        self._md_lines.append("## 对局过程\n")

    def event(self, kind: str, entry: dict) -> None:
        # JSONL（机器可读，仅公开信息）
        self._jsonl.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._jsonl.flush()
        text = entry.get("text", "")
        # 终端
        if not self.quiet:
            if kind == "speak":
                # 发言：名字「玩家N」用座位色、内容用灰色，让 7 个发言人一眼区分。
                seat = entry.get("seat")
                name = f"玩家{seat}"
                body = text[len(name) + 1:] if text.startswith(name + "：") else text
                print("  " + self._c(name, _NAME_COLOR) + self._c("：" + body, "gray"))
            else:
                color = _KIND_COLOR.get(kind, "reset")
                print(self._c(text, color))
        # Markdown
        if kind == "round_start":
            self._md_lines.append(f"\n### {text}\n")
        elif kind == "speak":
            self._md_lines.append(f"- 💬 {text}")
        else:
            self._md_lines.append(f"- {text}")

    def snapshot(self, kind: str, payload: dict) -> None:
        """结构化快照（复盘/可视化用），写 JSONL、不打终端。

        trust 快照额外攒进 _trust_snaps，由 finish() 渲染成矩阵表格写入 .md 的
        上帝视角段落（仍不打终端）。.md 是写出文件、agent 从不读取，故不泄露。
        """
        self._jsonl.write(json.dumps({"kind": kind, **payload}, ensure_ascii=False) + "\n")
        self._jsonl.flush()
        if kind == "trust":
            self._trust_snaps.append(payload)

    def secret(self, text: str) -> None:
        """上帝视角私密记录：写入 Markdown 复盘，绝不写入公开 JSONL。

        终端输出受 whisper_to_terminal 控制——人类玩家在场时关闭，避免在刺杀环节
        提前剧透坏人阵营。
        """
        if self.whisper_to_terminal and not self.quiet:
            print("  " + self._c(text, "gray"))
        self._md_lines.append(f"- 🕵️ {text}")

    # ---- 赛后复盘（角色已揭晓，属公开信息，不受 whisper_to_terminal 约束）----
    def review_start(self, reveal_text: str) -> None:
        """开启复盘段落：终端横幅 + 全员身份；md 起新段头；jsonl 记一条。"""
        self._jsonl.write(json.dumps(
            {"kind": "review_start", "reveal": reveal_text}, ensure_ascii=False) + "\n")
        self._jsonl.flush()
        if not self.quiet:
            print(self._c("=== 复盘环节（身份已揭晓，各玩家赛后点评）===", "bold"))
            print("  " + self._c(reveal_text, "dim"))
        self._md_lines.append("\n## 复盘环节\n")
        self._md_lines.append(f"> {reveal_text}\n")

    def review_line(self, seat: int, role_zh: str, text: str) -> None:
        """打印一位玩家的复盘点评（终端 + md + jsonl）。"""
        self._jsonl.write(json.dumps(
            {"kind": "review", "seat": seat, "role": role_zh, "text": text},
            ensure_ascii=False) + "\n")
        self._jsonl.flush()
        name = f"玩家{seat}（{role_zh}）"
        if not self.quiet:
            # 与游戏中发言统一：名字用 _NAME_COLOR、内容用灰色
            print(self._c(name, _NAME_COLOR) + self._c("：" + text, "gray"))
        self._md_lines.append(f"- 🗯️ {name}：{text}")

    # ---- 上帝视角信任矩阵（仅 .md 复盘）----
    @staticmethod
    def _fmt_trust_cell(val, diagonal: bool) -> str:
        if diagonal:
            return "—"          # 自己（无评分）
        if val is None:
            return "·"          # 未维护（随机/人类玩家）
        if val == 0:
            return "0.0"
        return f"{val:+.1f}"     # 带符号，一眼看正负

    def _trust_md(self, state) -> list[str]:
        """把各轮 trust 快照渲染成 markdown 矩阵表格（行=持有者，列=对象）。"""
        if not self._trust_snaps:
            return []
        roles = getattr(state, "roles_by_seat", {})
        lines = ["\n## 上帝视角信任矩阵（仅复盘，agent 不可见）\n",
                 "> 行=持有者，列=对象；数值为该持有者对该对象的「好人度」(-1~1，越高越信任为好人)。"
                 "`—`=自己，`·`=未维护（随机/人类玩家）。\n"]
        for snap in self._trust_snaps:
            seats = snap["seats"]
            matrix = snap["matrix"]
            phase = snap.get("phase")
            title = "刺杀阶段" if phase == "assassination" else f"第 {snap.get('round')} 轮"
            lines.append(f"\n### {title}\n")
            lines.append("| 行＼列 | " + " | ".join(f"P{s}" for s in seats) + " |")
            lines.append("|" + "---|" * (len(seats) + 1))
            for i, s in enumerate(seats):
                if self.reveal_roles and s in roles:
                    label = f"P{s} {ROLE_ZH[roles[s]]}"
                else:
                    label = f"P{s}"
                cells = [self._fmt_trust_cell(matrix[i][j], i == j) for j in range(len(seats))]
                lines.append(f"| **{label}** | " + " | ".join(cells) + " |")
        return lines

    def easter_egg(self, state) -> None:
        """终局结算彩蛋：定胜负后、复盘前随机打一句俏皮话烘托气氛。

        **仅终端、绝不进 jsonl/md**（纯气氛组，不入复盘）。quiet（跨局并行）时静默。
        局面三分：好人获胜 / 坏人靠任务（或流局）获胜 / 梅林被刺杀翻盘。
        刺杀只在好人凑齐 3 次任务后才触发，故 winner=evil 且 successes==3 必为翻盘局。
        """
        if self.quiet:
            return
        if state.winner == "good":
            pool = _EASTER_EGGS["good"]
        elif state.successes >= 3:
            pool = _EASTER_EGGS["evil_assassinate"]
        else:
            pool = _EASTER_EGGS["evil_quests"]
        print(self._c("✨ " + random.choice(pool), "magenta"))

    def finish(self, state) -> None:
        result = "好人获胜 🛡️" if state.winner == "good" else "坏人获胜 🗡️"
        banner = f"=== 结果：{result} ==="
        if not self.quiet:
            print(self._c(banner, "bold"))
        self._md_lines.append(f"\n## 结果\n\n**{result}**\n")

        # 上帝视角身份揭示（仅复盘，不曾进入任何 agent）
        if self.reveal_roles:
            if not self.quiet:
                print(self._c("--- 上帝视角身份 ---", "dim"))
            self._md_lines.append("\n## 上帝视角身份揭示\n")
            for seat in sorted(state.roles_by_seat):
                role = state.roles_by_seat[seat]
                side = "好人" if team_of(role) == "good" else "坏人"
                line = f"玩家{seat}：{ROLE_ZH[role]}（{side}）"
                if not self.quiet:
                    print("  " + self._c(line, "gray"))
                self._md_lines.append(f"- {line}")

        # 上帝视角信任矩阵（同样仅进 .md 复盘，不打终端、不进 jsonl 公开渠道）
        self._md_lines.extend(self._trust_md(state))

        with open(self.md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(self._md_lines) + "\n")
        self._jsonl.close()
        if not self.quiet:
            print(self._c(f"日志已保存：{self.jsonl_path} / {self.md_path}", "dim"))
