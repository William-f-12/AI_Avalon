"""阿瓦隆多 Agent 对战入口。

用法：
  python main.py                  # 用配置里的模型跑一局（需 .env 内的 API key）
  python main.py --games 5        # 连跑 5 局并统计胜率
  python main.py --seed 42        # 固定随机种子复现发牌
  python main.py --random         # 零 token 随机基线，离线验证整局流程
"""

from __future__ import annotations

import argparse
import os
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

from avalon.agent import LLMAgent, RandomAgent
from avalon.game import new_game, run_game
from avalon.human import HumanAgent
from avalon.llm import LLMClient
from avalon.logger import GameLogger
from avalon.roles import build_night_knowledge, build_known_evil

DEFAULT_MODEL = {
    "model": "deepseek-v4-flash",
    "base_url": "https://api.deepseek.com",
    "api_key_env": "DEEPSEEK_API_KEY",
}


def load_env(path: str = ".env") -> None:
    """极简 .env 加载：把 KEY=VALUE 注入环境变量（不覆盖已存在的）。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


def load_config(path: str) -> dict:
    cfg = {"num_players": 7, "seed": None, "parallel": True, "review": True,
           "game_workers": 1, "default_model": dict(DEFAULT_MODEL), "seats": []}
    if not path or not os.path.exists(path):
        return cfg
    try:
        import yaml
    except ImportError:
        print("[警告] 未安装 pyyaml，忽略 config.yaml，使用默认配置。")
        return cfg
    with open(path, encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    cfg.update({k: v for k, v in loaded.items() if v is not None})
    if "default_model" not in cfg or not cfg["default_model"]:
        cfg["default_model"] = dict(DEFAULT_MODEL)
    return cfg


def _client_cache():
    cache: dict[tuple, LLMClient] = {}

    def get(model_cfg: dict) -> LLMClient:
        m = {**DEFAULT_MODEL, **(model_cfg or {})}
        timeout = float(m.get("timeout", 60.0))   # 本地大模型可在 config 调高防超时
        key = (m["model"], m["base_url"], m["api_key_env"], timeout)
        if key not in cache:
            cache[key] = LLMClient(
                model=m["model"], base_url=m["base_url"],
                api_key_env=m["api_key_env"], timeout=timeout)
        return cache[key]

    return get


def _resolve_human_seat(args, state, seed: Optional[int]) -> int:
    """确定人类玩家座位：优先 --human-seat，否则随机挑一席。"""
    if args.human_seat is not None:
        if args.human_seat not in state.seats:
            raise SystemExit(
                f"--human-seat 必须在 1..{state.num_players} 之间，收到 {args.human_seat}")
        return args.human_seat
    rng = random.Random(seed) if seed is not None else random.Random()
    return rng.choice(state.seats)


def build_agents(state, cfg: dict, random_mode: bool, seed: Optional[int],
                 human_seat: Optional[int] = None):
    knowledge = build_night_knowledge(state.roles_by_seat)
    known_evil = build_known_evil(state.roles_by_seat)
    agents = {}
    get_client = _client_cache()
    seats_cfg = cfg.get("seats") or []
    for seat in state.seats:
        role = state.roles_by_seat[seat]
        if seat == human_seat:
            rng = random.Random((seed or 0) + seat) if seed is not None else random.Random()
            agents[seat] = HumanAgent(seat, role, knowledge[seat], state.num_players, rng)
        elif random_mode:
            rng = random.Random((seed or 0) + seat) if seed is not None else random.Random()
            agents[seat] = RandomAgent(seat, role, knowledge[seat], state.num_players, rng)
        else:
            seat_cfg = seats_cfg[seat - 1] if seat - 1 < len(seats_cfg) else {}
            model_cfg = {**cfg["default_model"], **(seat_cfg or {})}
            agents[seat] = LLMAgent(
                seat, role, knowledge[seat], get_client(model_cfg), state.num_players,
                known_evil=known_evil[seat])
    return agents


def main():
    ap = argparse.ArgumentParser(description="多 Agent 阿瓦隆对战")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--games", type=int, default=1, help="连跑局数")
    ap.add_argument("--seed", type=int, default=None, help="随机种子（可复现发牌）")
    ap.add_argument("--players", type=int, default=None, help="玩家数（默认读配置，7）")
    ap.add_argument("--random", action="store_true", help="用随机基线，零 token 离线测试")
    ap.add_argument("--no-color", action="store_true", help="关闭终端颜色")
    ap.add_argument("--human", action="store_true",
                    help="人类玩家加入，随机顶替一个座位（座位与角色随机）")
    ap.add_argument("--human-seat", type=int, default=None,
                    help="人类玩家加入并指定座位号（1..N，隐含 --human）")
    ap.add_argument("--no-parallel", action="store_true",
                    help="关闭投票/任务牌并发，逐个串行调用（调试或规避限流用）")
    ap.add_argument("--no-review", action="store_true",
                    help="关闭终局后的赛后复盘轮（默认开；批量调参时可关以省 token）")
    ap.add_argument("--no-spinner", action="store_true",
                    help="关闭等待 LLM 时的「思考中」动态状态行（默认开；仅真 TTY、无人类时本就生效）")
    ap.add_argument("--game-workers", type=int, default=None,
                    help="跨局并行的并发局数（>1 时多局同时跑，终端静默只看汇总；"
                         "默认读 config 的 game_workers，再默认 1=串行。峰值 API 并发≈7×该值，按额度调）")
    args = ap.parse_args()

    load_env()
    cfg = load_config(args.config)
    num_players = args.players or cfg.get("num_players", 7)
    base_seed = args.seed if args.seed is not None else cfg.get("seed")
    # 并发默认开（config 可设 parallel: false），--no-parallel 命令行优先关闭
    parallel = cfg.get("parallel", True) and not args.no_parallel
    # 复盘默认开（config 可设 review: false），--no-review 命令行优先关闭
    review_enabled = cfg.get("review", True) and not args.no_review

    human_mode = args.human or args.human_seat is not None
    if human_mode and args.games > 1:
        print("[提示] 人类模式建议单局，已忽略 --games，只跑 1 局。\n")
        args.games = 1

    # 跨局并发局数：命令行优先，否则读 config，再默认 1（串行）。人类在场强制单局、独占终端。
    game_workers = args.game_workers if args.game_workers is not None else cfg.get("game_workers", 1)
    game_workers = 1 if human_mode else max(1, int(game_workers))
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    def play_one_game(g: int, quiet: bool) -> str:
        seed = (base_seed + g) if base_seed is not None else None
        state = new_game(num_players, seed=seed)
        human_seat = _resolve_human_seat(args, state, seed) if human_mode else None
        agents = build_agents(state, cfg, args.random, seed, human_seat)
        # 唯一 game_id：秒级时间戳 + 局序号，避免并行（或同秒串行）撞日志文件名。
        logger = GameLogger(game_id=f"game_{run_ts}_{g + 1:03d}",
                            use_color=not args.no_color,
                            whisper_to_terminal=human_seat is None,
                            quiet=quiet)
        return run_game(agents, state, logger, parallel=parallel, review=review_enabled,
                        spinner=not args.no_spinner)

    tally = {"good": 0, "evil": 0}
    if game_workers == 1:
        for g in range(args.games):
            winner = play_one_game(g, quiet=False)
            tally[winner] += 1
            if args.games > 1:
                print(f"[{g + 1}/{args.games}] 本局：{'好人' if winner == 'good' else '坏人'}胜"
                      f"｜累计 好{tally['good']}:坏{tally['evil']}\n")
    else:
        print(f"[并行] {args.games} 局以 {game_workers} 并发跑（终端静默，逐局详情见 logs/）。\n")
        # 各局 build_agents 各自新建 client 与状态，跨局无共享可变状态；tally 只在主线程累加。
        with ThreadPoolExecutor(max_workers=game_workers) as ex:
            futs = {ex.submit(play_one_game, g, True): g for g in range(args.games)}
            done = 0
            for fut in as_completed(futs):
                g = futs[fut]
                winner = fut.result()
                tally[winner] += 1
                done += 1
                print(f"[{done}/{args.games}] 第{g + 1}局：{'好人' if winner == 'good' else '坏人'}胜"
                      f"｜累计 好{tally['good']}:坏{tally['evil']}")

    if args.games > 1:
        total = tally["good"] + tally["evil"]
        print(f"\n=== 统计（{total} 局）===")
        print(f"好人胜率：{tally['good'] / total:.0%}（{tally['good']}）")
        print(f"坏人胜率：{tally['evil'] / total:.0%}（{tally['evil']}）")


if __name__ == "__main__":
    main()
