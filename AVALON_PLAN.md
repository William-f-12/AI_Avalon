# 多 Agent 阿瓦隆（Avalon）对战平台 — 实施方案

## Context（为什么做这个）

目标：搭一个让 7 个 LLM agent 互相玩《抵抗组织：阿瓦隆》的平台，先固定 7 人局，后续可扩展更多座位。要求体量小、**一天内用 Claude 能写完**。

参考成熟方案：学术界已有 **AvalonBench**（`github.com/jonathanmli/Avalon-LLM`，论文 *AVALONBENCH*, arXiv:2310.05036）——正是 7 个 LLM agent 玩阿瓦隆的基准。我们不直接依赖它（它偏重 benchmark、依赖较重），而是借鉴它的结构：游戏引擎 + 角色感知的 ReAct 风格 agent，做一个**轻量自实现版**。

已确认的设计选择：
- **界面**：纯终端运行，彩色打印每一步；同时落地结构化 `JSONL` + `Markdown` 复盘日志。
- **角色**：经典全角色 7 人局。好人 4（梅林 Merlin、派西维尔 Percival、忠臣 Loyal Servant ×2），坏人 3（莫甘娜 Morgana、刺客 Assassin、**奥伯伦 Oberon**）。
- **模型**：每个 agent 默认 `deepseek-v4-pro`（出于成本），但**每个座位可在配置里单独指定模型**，方便日后做模型对战。因此 LLM 层做成「OpenAI 兼容、provider 无关」的可替换客户端，而非绑定某家 SDK。

> 注意：本项目用 **DeepSeek**（非 Anthropic）。DeepSeek API 与 OpenAI 格式兼容，用官方 `openai` Python SDK 改 `base_url` 即可。模型名 `deepseek-v4-pro`，`base_url=https://api.deepseek.com`，密钥走环境变量 `DEEPSEEK_API_KEY`。

## 技术栈

- 语言：**Python 3.10+**（纯标准库 + 极少依赖）
- 依赖（`requirements.txt`）：`openai`（调 DeepSeek，OpenAI 兼容）、`pyyaml`（读配置）、`rich`（彩色终端，可选；不想加依赖可用 ANSI 码）
- 密钥：`.env`（`DEEPSEEK_API_KEY=...`），用 `os.environ` 读取，不硬编码

## 阿瓦隆规则（7 人局，引擎需实现）

- 阵营：好人 4 / 坏人 3。
- **夜晚信息**：
  - 梅林看到**所有**坏人（含奥伯伦）→ 看到 Morgana、Assassin、Oberon
  - 派西维尔看到「梅林和莫甘娜」两个身份但不知谁是谁
  - **奥伯伦 Oberon 不认识其他坏人，其他坏人也不认识奥伯伦**：只有 Morgana 与 Assassin 互相可见；Oberon 夜晚什么队友都看不到（但梅林能看到他）
  - 忠臣无任何信息
- **任务队伍人数**：5 个任务依次为 `[2, 3, 3, 4, 4]`。
- **第 4 个任务需要 2 张失败票**才算失败，其余任务 1 张即失败。
- **随机发牌**：开局把 `SETUP_7`（6 种身份的列表）用 `random.Random(seed).shuffle(...)` 打乱后按顺序分配到 7 个座位；`seed` 可在 `config.yaml` 配置以复现，留空则用系统随机。发牌结果只进上帝视角日志，不进任何公开历史。
- **发言流程（每次提队，按用户规则）**：
  1. 车主（当前 leader）**先决定车型**（提出一支队伍）。
  2. 车主**先发言**。
  3. 车主**选择发言方向**：顺时针或逆时针。
  4. 其余玩家按该方向**依次绕圈发言**，直到轮回到车主。
  5. 车主**最终决定**：
     - 若**改变车型**（与第 1 步不同）→ **车主不能再发言**，直接进入投票（投票针对改后的车型）。
     - 若**车型不变** → 车主**可以再补发一次言**，然后进入投票。
  6. **全体公开投票**（赞成/反对）→ 多数赞成则出发执行任务；否则 leader 顺延到下一位、全局 `reject_count+1`。
- **流局规则（全局累计）**：`reject_count` 是**整局游戏的全局计数**，每次提队被否就 +1，**跨任务累加、不在任务之间重置**；一旦**累计达到 5 次流局，坏人直接赢**。（注意：不是「单个任务内连续 5 次」，而是全局累计 5 次。）
- **执行任务**：队员各自暗投「成功/失败」。好人只能投成功；坏人可投任意。按上面失败票阈值判定任务成败。
- **胜负**：先达成 3 个成功任务 → 好人进入**刺杀环节**；坏人先拿 3 个失败任务 → 坏人赢。
- **刺杀环节（含坏人合议）**：
  1. 坏人阵营此刻**互相公开身份**（含奥伯伦），把已知队友从候选里排除——刺杀候选收窄为仅剩好人座位。
  2. 莫甘娜与奥伯伦各向刺客**私下进言**「谁最像梅林」；进言只在坏人内部流通，**绝不广播给好人、不写入公开 JSONL**，仅在上帝视角复盘（终端/Markdown 的 🕵️ 行）可见。
  3. 刺客综合公开历史 + 坏人名单 + 同伴进言后**最终拍板**猜梅林：猜中则坏人翻盘赢，否则好人赢。
  - 编排见 `game.py:_run_assassination`；进言决策点为 `prompts/actions/advise_merlin.md`。
- **赛后复盘轮（终局后、揭示结果之前）**：向所有 agent 揭晓全部 7 席真实角色（`roles.py:full_roster_text`），每个 agent 基于整局公开记录发表一段点评（**2~7 句、单段、不换行**，`_run_review` 内 `" ".join(text.split())` 折叠换行兜底）。并行模式下把全部 reviewer 提交线程池，但**按座位序逐个等待 future 再打印**——小号先得到回应、`玩家1→玩家7` 一个一个出；**不走 announce、不回灌历史**（赛后无人再决策）。人类（interactive）席**跳过不发表**。默认开启，`run_game(review=...)` / `--no-review` / `config.yaml: review: false` 可关。编排见 `game.py:_run_review`，决策点为 `prompts/actions/review.md`。

## 目录结构与各文件职责

```
AI_Avalon/
  main.py              # 入口：读配置→建局→跑 game loop→打印结果
  config.yaml          # 座位数、每座位模型、发言轮数、随机种子等
  requirements.txt
  .env.example         # DEEPSEEK_API_KEY=
  README.md
  avalon/
    __init__.py
    roles.py           # 角色枚举、阵营、夜晚知识推导 build_night_knowledge()
    game.py            # GameState + 引擎状态机（提队/投票/任务/刺杀/胜负）
    agent.py           # LLMAgent：组装角色感知 prompt、调 LLM、解析结构化动作
    llm.py             # provider 无关的 LLM 客户端（默认 DeepSeek，OpenAI 兼容）
    logger.py          # 彩色终端 + JSONL + Markdown 复盘日志（含 thinking() spinner 工厂）
    spinner.py         # 等待 LLM 时终端最后一行的「思考中」动态状态行（瞬态，不进日志）
    prompts/           # prompt 拆分成文件夹，按角色/决策点分文件，便于独立调优
      __init__.py      #   加载器：build_system(role,...) / build_action(kind,...) 拼装各片段
      common.md        #   通用规则总览 + 公开局势渲染说明 + JSON 输出格式约定（所有角色共享）
      actions/         #   各决策点指令模板（基本与角色无关）
        propose.md     #     提队（初始定车型）
        direction.md   #     车主选顺/逆时针发言方向
        speak.md       #     发言/分析（首发言、绕圈发言、补发言共用）
        finalize.md    #     绕圈发言后车主最终定队（改/不改车型）
        vote.md        #     投票（赞成/反对）
        quest.md       #     任务暗投（成功/失败）
        advise_merlin.md #   刺杀前莫甘娜/奥伯伦向刺客私下进言（仅坏人可见）
        assassinate.md #     刺客综合同伴进言后猜梅林
        review.md      #     赛后复盘点评（身份已揭晓，脱离角色评判全场）
      roles/           #   每个角色一个文件：阵营目标 + 夜晚私有信息的措辞模板
        merlin.md
        percival.md
        loyal.md
        morgana.md
        assassin.md
        oberon.md
```

### `avalon/roles.py`
- `Role` 枚举：`MERLIN, PERCIVAL, LOYAL, MORGANA, ASSASSIN, OBERON`；`ROLE_TEAM` 映射到好/坏。
- `SETUP_7 = [MERLIN, PERCIVAL, LOYAL, LOYAL, MORGANA, ASSASSIN, OBERON]`。
- `build_night_knowledge(roles_by_seat) -> dict[seat -> str]`：按上面夜晚规则为每个座位生成它**私有看到的信息**文本（喂给该 agent 的 system prompt，绝不泄露给别人）。关键点：梅林可见全部坏人含 Oberon；坏人互认名单**排除** Oberon，且 Oberon 自己拿不到任何队友信息。
- `full_roster_text(roles_by_seat) -> str`：复盘轮专用，揭示全部 7 席真实角色 + 阵营（仅终局复盘时使用，绝不进对局中的公开历史）。

### `avalon/game.py`
- `QUEST_SIZES = [2,3,3,4,4]`，`DOUBLE_FAIL_QUEST = 3`（0-indexed 第 4 个任务）。
- `GameState` dataclass：座位→角色、当前 leader、任务进度（成功/失败计数）、当前任务编号、`reject_count`（**全局累计流局数，跨任务不重置**）、公开历史 `events`。
- 引擎方法（**纯函数式、不调 LLM**，便于单测）：
  - `current_quest_size()`, `quest_fail_threshold(quest_idx)`
  - `apply_vote(votes) -> approved: bool`（多数赞成）
  - `apply_quest(cards) -> success: bool`
  - `check_winner() -> "good"|"evil"|None`（含**全局累计 5 次流局** `reject_count>=5`、3 成功/3 失败判定）
- `run_game(agents, state, logger, parallel=True, review=True, spinner=True)`：主循环，编排各阶段并调用 agent 决策、把每步写进 logger 和 `events`。其中**提队+发言阶段**严格按发言流程：`propose_team` → 车主 `speak` → `choose_direction` → 按方向对其余座位逐个 `speak` → 车主 `finalize_team`；若最终车型与初始一致则再调一次车主 `speak`（补发言），否则跳过补发言；随后进入投票。终局分胜负后、`logger.finish` 之前，若 `review=True` 跑一轮赛后复盘（`_run_review`）。每个 LLM 阻塞段用 `with logger.thinking(...)` 包裹显示「思考中」动态行（`spinner` 且真 TTY、非静默、无人类时才生效）。
- 工具函数 `speaking_order(leader, direction, n)`：按顺/逆时针生成除车主外其余座位的发言顺序。

### `avalon/llm.py`
- `class LLMClient`：`__init__(model, base_url, api_key_env)`；`complete(system, messages, json_mode=True) -> str`。
- 内部用 `openai.OpenAI(base_url=..., api_key=os.environ[...])` 调 `chat.completions.create(...)`；`json_mode` 时传 `response_format={"type":"json_object"}`。
- **provider 无关**：换 OpenAI/Claude 只需在 `config.yaml` 改 `base_url`/`model`/`api_key_env`。带简单重试（429/5xx，指数退避 2~3 次）。

### `avalon/agent.py`
- `class LLMAgent`：持有 `seat`、`role`、私有夜晚知识、`LLMClient`、可见的公开历史。
- 决策方法（各自组装 prompt → `llm.complete(json_mode=True)` → 解析 JSON，解析失败有兜底）：
  - `propose_team(size) -> list[seat]`（车主第 1 步定初始车型）
  - `choose_direction() -> "cw"|"ccw"`（车主选顺/逆时针发言方向）
  - `speak() -> str`（按发言流程被调用：车主首发言、其余玩家绕圈发言、以及车主「车型不变」时的补发言）
  - `finalize_team(size) -> list[seat]`（绕圈发言结束后车主最终定队；与初始相同表示不改车型→随后允许补发言，不同表示改车型→不再发言）
  - `vote_team(proposal) -> "approve"|"reject"`
  - `quest_card() -> "success"|"fail"`（好人强制 success，引擎层再校验一次防作弊）
  - `advise_merlin(candidates, evil_reveal) -> str`（刺杀前莫甘娜/奥伯伦向刺客的私下进言；好人/基线返回空串）
  - `assassinate(candidates, evil_reveal, consult) -> seat`（仅刺客；综合坏人名单与同伴进言后拍板）
  - `review(reveal) -> str`（赛后复盘点评；身份已揭晓，脱离角色评判全场。基线/人类返回空串）
- 每个 agent 的 system prompt = 通用规则 + 自己的角色 + 私有知识；user 消息 = 当前公开局势 + 本次决策指令 + 要求的 JSON schema。

### `avalon/human.py`（人类玩家模式）
- `class HumanAgent`：实现同一套 `AgentLike` 协议，把每个决策点映射到**控制台菜单/输入**，让真人顶替一个座位下场。引擎 `game.py` 与 prompts 均无需改动。
  - 结构化决策（提队/方向/改车/投票/任务牌/进言/刺杀）一律用**选项菜单**（输数字），唯独 `speak` 用自由文本；好人任务牌自动判成功。
  - 人类**不维护信任表**：`trust_row()` 返回 `{}`，`revise_trust` 为空操作。
  - 人类**不发表赛后复盘**：`review()` 返回空串，且 `_run_review` 按 `interactive` 标记直接跳过其座位（不会被调用）。
  - 开局私密展示玩家角色与夜晚信息（仅本人可见，回车继续）。
- 入口：`main.py --human`（随机顶替一席）/ `--human-seat N`（指定座位），可叠加 `--random` 做零 token 练习。人类在场时 `GameLogger(whisper_to_terminal=False)` 隐藏上帝视角悄悄话防剧透（复盘 md 仍保留）。

### `avalon/prompts/`（文件夹，按角色/决策点分文件）
- `common.md`：中文规则总览、公开局势如何渲染、统一的 JSON 输出格式约定（如 `{"team":[1,3,5],"reason":"..."}`）。
- `roles/<role>.md`：每个角色单独一文件，写它的阵营目标与夜晚私有信息的措辞模板（含可填充占位符，如 `{teammates}`、`{merlin_candidates}`）。新增/调整某角色的策略只改对应文件，互不影响。
- `actions/<kind>.md`：各决策点（propose/direction/speak/finalize/vote/quest/advise_merlin/assassinate）的指令与该点要求的 JSON schema。
- `__init__.py`：加载器，提供 `build_system(role, night_knowledge)` 与 `build_action(kind, context)`，把 `common + role + action` 片段拼成最终 system/user 文本。用 `.md`/`.txt` 纯文本存放，调 prompt 不必改 Python。

### `avalon/logger.py`
- `GameLogger`：`event(kind, public_payload)` 同时
  - 彩色打印到终端（`rich` 或 ANSI）
  - 追加一行到 `logs/<game_id>.jsonl`（结构化，机器可读）
  - 生成 `logs/<game_id>.md`（人类复盘，含每回合提队/投票/任务结果；可在末尾附「上帝视角」身份揭示）。
- 区分**公开信息**（写公共历史、喂给所有 agent）与**私密信息**（仅落地到日志的上帝视角，不进入任何 agent 的 prompt）。

### `config.yaml`（示例）
```yaml
num_players: 7
discussion_rounds: 1          # 每次提队后绕圈发言的轮数（默认 1 轮，按发言流程）
seed: null                    # 固定随机种子可复现发牌
default_model:
  model: deepseek-v4-pro
  base_url: https://api.deepseek.com
  api_key_env: DEEPSEEK_API_KEY
seats:                        # 可逐座位覆盖模型，留空则用 default_model
  - {}
  - {}
  # ... 想做模型对战时在这里给某座位指定别的 model/base_url/api_key_env
```

### `main.py`
读 `config.yaml` → 随机发牌（可固定 seed）→ 构造 7 个 agent（默认 `LLMAgent`，`--random` 用 `RandomAgent`，`--human`/`--human-seat` 把某席换成 `HumanAgent`）→ `run_game(...)` → 打印胜负与日志路径。支持 `--games N` 连跑多局、`--seed`；人类模式只跑单局。`--game-workers N`（或 `config.yaml: game_workers`，默认 1）开跨局并行：N 局各自独立经 `ThreadPoolExecutor` 同时跑、`as_completed` 主线程聚合胜率，logger 静默只写文件、`game_id` 带局序号防撞名；同 `--seed` 下串/并行结果一致，人类模式强制单局。

## 扩展性（满足「后续加更多 agent」）
- 角色/人数表抽成 `SETUPS = {5: [...], 6: [...], 7: SETUP_7, ...}` 与 `QUEST_SIZES_BY_N`，加人数只需补表，引擎逻辑不变。
- agent 数量、模型来自配置，天然支持后续增加。

## 复用与不重复造轮子
- 规则结构、角色知识推导、ReAct 风格 agent 直接借鉴 **AvalonBench**（`github.com/jonathanmli/Avalon-LLM`）的设计，但自实现轻量版。
- LLM 调用统一走 `openai` SDK 的 OpenAI 兼容接口（DeepSeek 官方推荐方式），不手写 HTTP。

## 验证方式（端到端）
1. `pip install -r requirements.txt`，把 `.env.example` 复制为 `.env` 填入 `DEEPSEEK_API_KEY`。
2. **先离线测引擎**：写几个 `game.py` 的纯函数单测（任务阈值、**全局累计 5 次流局判负、reject_count 跨任务不重置**、3 胜判定、刺杀结算），不花 token 就能验证规则正确。
3. **跑一局真实对战**：`python main.py`，终端应看到夜晚发牌 → 逐回合提队/发言/投票/任务 → 最终胜负 + 刺杀结果；`logs/` 下生成对应 `.jsonl` 和 `.md`。
4. 检查 `.md` 复盘：身份、每个任务的队伍/投票/成败、刺客猜测是否都正确记录；确认 agent 的私有信息**没有**泄漏进公开历史。
5. `python main.py --games 5` 连跑统计好人/坏人胜率，确认稳定不崩。

## 一天内可完成的拆分顺序
1. `roles.py` + `game.py` 引擎与单测（~半天，纯逻辑、零 token）
2. `llm.py` + `prompts/`（各角色与决策点文件）+ `agent.py`（接 DeepSeek，跑通单个决策）
3. `logger.py` + `main.py` 串成完整 loop，跑通整局
4. 打磨 prompt、彩色输出、多局统计
