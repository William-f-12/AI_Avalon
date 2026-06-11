"""终端「思考中」动态状态行：等待 LLM 时在最后一行原地刷新一个会动的提示。

设计要点（见 AVALON_PLAN.md「思考中动态状态行」）：
- 仅占**终端最后一行**，用 `\\r` 回到行首 + `\\033[K` 清到行尾原地刷新，绝不向下滚动。
- 退出时**就地擦除该行**（`\\r\\033[K`），让后续 print 复用这一行——终端滚动记录里不留痕。
- 纯终端瞬态，**只写 stream（默认 stdout）**，绝不经过 logger 的 jsonl/md 落盘路径 → 不进任何日志。
- 在后台守护线程里动画；主线程照常阻塞在 LLM 调用上。用作上下文管理器：
      with Spinner(enabled, messages): <阻塞调用>
  阻塞调用返回 → __exit__ 停线程→join→擦行 → with 之后再打印结果，顺序天然正确、无交错。
- enabled=False 时全程空操作（非 TTY / 静默 / 人类在场时由调用方传 False）。
"""

from __future__ import annotations

import itertools
import random
import sys
import threading
import time
from typing import Optional, Sequence, Union

# braille 旋转帧（绝大多数现代终端支持）
_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# 默认轮换文案池：中立、不泄露身份，纯为“正在进行”的氛围感（无指定阶段时兜底）
DEFAULT_MESSAGES = [
    "阿瓦隆高手们正在思考中",
    "桌上一片寂静，众人在权衡",
    "灯光下谋略正在重新交锋",
    "七张面孔正在彼此试探",
    "忠奸难辨，推理仍在继续",
]

# 各阶段专属俏皮话池：让等待文案贴合当前时机（首句仍是「玩家N 正在做X」，随后轮换这些）。
PHASE_POOLS = {
    # 提队 / 选方向 / 定车 / 发言：圆桌烛影下的唇枪舌剑
    "discuss": [
        "圆桌旁，烛火明灭",
        "骑士们正在唇枪舌剑",
        "酒过三巡，话里藏刀",
        "谁举杯结盟，谁暗中盘算",
        "宴席之下，暗流涌动",
        "忠诚的誓言里掺着谎",
        "大厅里，眼神在交锋",
        "真骑士与伪君子同桌",
        "谁的披风下藏着匕首",
        "一句敬酒，半句试探",
        "卡美洛的空气微妙起来",
        "老骑士正掂量着新面孔",
        "谎言裹着蜜糖递出",
        "阴谋在觥筹交错间发酵",
        "谁先失言，谁先现形",
        "面具下的笑容深不可测",
        "桌上谈笑，桌下交锋",
        "誓约与背叛只隔一杯酒",
        "谁在演忠臣，谁在演英雄",
        "烛光照不进人心深处",
        "风声里全是没说出口的话",
    ],
    "vote": [
        "王国的命运系于这一票",
        "举杯还是拔剑，就在此刻",
        "印章悬在赞成与反对间",
        "谁愿以骑士之名担保",
        "这一票，关乎卡美洛",
        "沉默的骑士正在抉择",
        "手按剑柄，迟迟未决",
        "赞成者未必是同袍",
        "反对者未必是仇敌",
        "票影之下，立场毕现",
        "谁随大流，谁逆风而立",
        "每一票都将载入史册",
        "城头的旗帜在看风向",
        "一念忠诚，一念背叛",
        "烛火摇曳，人心难测",
        "这一举手，藏着算计",
        "圆桌将由多数裁定",
        "是同盟还是陷阱，投了才知",
    ],
    "quest": [
        "任务的卷轴即将揭晓",
        "暗影中，有人握紧匕首",
        "圣剑还是毒刃，一念之间",
        "这趟远征藏着叛徒吗",
        "成功的荣光，失败的诅咒",
        "忠诚还是背叛，卷轴自证",
        "黑手会不会伸向王国",
        "牌影之下，杀机潜伏",
        "骑士们屏息等待结果",
        "一张暗牌，定王国生死",
        "谁会在远征途中倒戈",
        "烛光下，命运正被书写",
        "一颗硕鼠，毁一场远征",
        "英雄与叛徒，只差一张牌",
        "远征的成败悬于一线",
        "谁的剑会刺向自己人",
        "卷轴翻开，原形毕露",
        "空气凝固，只待结果",
    ],
    # 刺杀阶段：黑暗骑士合议 / 进言 / 锁定梅林
    "assassinate": [
        "刺客的匕首已然出鞘",
        "谁是梅林，正被推敲",
        "黑暗骑士在低声密谋",
        "最后一击，将载入史册",
        "魔法师的伪装能撑住吗",
        "一刀定卡美洛的归属",
        "邪恶的高光时刻降临",
        "赢了整场，只差这一剑",
        "刺客的直觉，准不准",
        "全场屏息，只待点名",
        "梅林：千万别看向我",
        "匕首在黑暗中游移",
        "找出贤者，绝地翻盘",
        "烛火熄灭前的最后一搏",
        "三道暗影正在对暗号",
        "一剑封喉，还是落空",
        "命运的骰子即将掷下",
        "善与恶，悬于这一指",
    ],
    "review": [
        "酒馆里，复盘开席",
        "吟游诗人正编排着战歌",
        "一壶麦酒，半席吐槽",
        "骑士们围炉翻旧账",
        "谁是英雄，谁是内鬼",
        "事后的诸葛亮纷纷登场",
        "真相揭晓，开始算账",
        "这一局，谁该背锅",
        "麦酒下肚，嘴炮不停",
        "友谊的酒杯说碎就碎",
        "名场面即将被传唱",
        "该敬的敬，该骂的骂",
        "输家沉默，赢家狂笑",
        "谁也别想全身而退",
        "战火已熄，唇枪又起",
        "马后炮响彻整座酒馆",
        "演技封神者今夜加冕",
        "卡美洛的八卦正在流传",
    ],
}


def compose_messages(lead=None, phase=None):
    """拼出 spinner 要轮换的文案：当前动作首句 `lead` + 该阶段俏皮话池 `PHASE_POOLS[phase]`。

    - lead + 已知 phase → [lead, 池1, 池2, …]（先显示具体在做什么，再轮换该情境俏皮话）
    - 只有 lead → [lead]（单句，不轮换）
    - 只有 phase → 纯池
    - 都没有 → None（交给 Spinner 用 DEFAULT_MESSAGES 兜底）
    """
    pool = PHASE_POOLS.get(phase) or []
    if lead and pool:
        return [lead] + list(pool)
    if lead:
        return [lead]
    if pool:
        return list(pool)
    return None


_CYAN = "\033[36m"
_RESET = "\033[0m"
_HIDE_CURSOR = "\033[?25l"
_SHOW_CURSOR = "\033[?25h"
_CLEAR_LINE = "\r\033[K"


class Spinner:
    """等待期间在终端最后一行显示旋转字符 + 轮换文案；停止时就地擦除，不留痕、不进日志。"""

    def __init__(
        self,
        enabled: bool,
        messages: Optional[Union[str, Sequence[str]]] = None,
        *,
        stream=None,
        use_color: bool = True,
        interval: float = 0.12,
        phrase_every: float = 2.0,
        start_delay: float = 0.15,
    ):
        self.enabled = enabled
        if messages is None:
            messages = DEFAULT_MESSAGES
        elif isinstance(messages, str):
            messages = [messages]
        self.messages = list(messages) or list(DEFAULT_MESSAGES)
        self.stream = stream if stream is not None else sys.stdout
        self.use_color = use_color
        self.interval = interval
        self.phrase_every = phrase_every
        self.start_delay = start_delay
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._rendered = False  # 是否真正画过（瞬时调用经 start_delay 后可能从未画过）

    # ---- 上下文管理器 ----
    def __enter__(self) -> "Spinner":
        if self.enabled:
            self._stop.clear()
            self._rendered = False
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc) -> bool:
        if self._thread is not None:
            self._stop.set()
            self._thread.join()
            self._thread = None
            self._clear()
        return False  # 不吞异常

    # ---- 动画线程 ----
    def _run(self) -> None:
        # 起始延迟：瞬时返回的调用（RandomAgent、命中缓存）期间不闪一下
        if self._stop.wait(self.start_delay):
            return
        frames = itertools.cycle(_FRAMES)
        phrase = self.messages[0]  # 首句仍是「玩家N 正在做X」作开场
        last_swap = time.monotonic()
        try:
            self._write(_HIDE_CURSOR)
            while not self._stop.is_set():
                now = time.monotonic()
                if now - last_swap >= self.phrase_every:
                    phrase = self._pick_other(phrase)  # 每次随机换一句（不重复当前）
                    last_swap = now
                glyph = next(frames)
                body = f"{glyph} {phrase}"
                line = f"{_CYAN}{body}{_RESET}" if self.use_color else body
                self._write(f"\r{line}\033[K")
                self._rendered = True
                if self._stop.wait(self.interval):
                    break
        finally:
            # 线程退出兜底恢复光标；正式擦行在 __exit__ 的 _clear()
            if self._rendered:
                self._write(_SHOW_CURSOR)

    def _pick_other(self, current: str) -> str:
        """从文案池里**随机**选一个与当前不同的句子；池里只有一句时保持不变。"""
        if len(self.messages) <= 1:
            return current
        choices = [m for m in self.messages if m != current]
        return random.choice(choices) if choices else current

    def _clear(self) -> None:
        """就地擦除整行并恢复光标，使后续 print 从干净的行开始。仅渲染过才动手。"""
        if self._rendered:
            self._write(_CLEAR_LINE + _SHOW_CURSOR)

    def _write(self, s: str) -> None:
        try:
            self.stream.write(s)
            self.stream.flush()
        except (ValueError, OSError):
            # stream 已关闭等极端情况，静默放弃，绝不影响主流程
            pass
