# AI Avalon — 多 Agent 阿瓦隆对战平台

7 个 LLM agent 互相玩《抵抗组织：阿瓦隆》。纯终端运行，落地结构化 JSONL + Markdown 复盘日志。
完整设计见 [`AVALON_PLAN.md`](./AVALON_PLAN.md)。

## 角色（7 人局，经典全角色）
- 好人 4：梅林 Merlin、派西维尔 Percival、忠臣 Loyal ×2
- 坏人 3：莫甘娜 Morgana、刺客 Assassin、**奥伯伦 Oberon**

夜晚信息要点：梅林看到全部坏人（含奥伯伦）；派西维尔看到梅林+莫甘娜两人但分不清；
莫甘娜与刺客互认、**奥伯伦独狼**（不认识坏人、坏人也不认识他，但梅林能看到他）。

## 规则要点
- 任务队伍人数 `[2,3,3,4,4]`；**第 4 个任务需 2 张失败票**才算失败。
- **流局全局累计**：提队被否就 +1，跨任务不重置；累计 **5 次坏人直接获胜**。
- 好人达成 3 次任务 → 进入**刺杀环节**：坏人阵营先互认身份并由莫甘娜、奥伯伦向刺客私下进言（仅坏人可见、不进公开历史），刺客综合后猜梅林，猜中坏人翻盘，否则好人胜；坏人先拿 3 次失败任务则获胜。
- 发言流程：队长定车型并发言 → 选顺/逆时针 → 其余玩家绕圈发言 → 队长定队（改型则不再发言，维持原型可补一次发言）→ 投票。

## 安装
```bash
pip install -r requirements.txt
cp .env.example .env      # 填入 DEEPSEEK_API_KEY
```

## 运行
```bash
python main.py               # 跑一局（调用 DeepSeek，需 API key）
python main.py --games 5     # 连跑 5 局并统计胜率
python main.py --games 20 --game-workers 8  # 多局同时跑，批量算胜率提速（终端静默，详情见 logs/）
python main.py --seed 42     # 固定种子复现发牌
python main.py --random      # 零 token 随机基线，离线验证整局流程
python main.py --no-color    # 关闭终端颜色
python main.py --no-parallel # 投票/任务牌改回逐个串行（默认并发加速；调试或规避限流时用）
python main.py --no-review   # 关闭终局后的赛后复盘轮（默认开；批量调参省 token 时用）
python main.py --no-spinner  # 关闭等待 LLM 时的「思考中」动态状态行（默认开）
```

### 并行加速
平台有两层相互独立的并行，都为缩短等待：

- **轮内并行（默认开）**：每轮的**投票**与**任务牌暗投**各 agent 决策互不依赖，引擎并发发起，明显缩短每轮等待；**发言**因需依次看到前面的话仍严格串行。刺杀环节里坏人各自更新信任、各自向刺客进言也并发。用 `--no-parallel` 或 `config.yaml` 的 `parallel: false` 退回逐个串行（调试或规避限流时用）。
- **跨局并行（批量算胜率）**：`--game-workers N`（或 `config.yaml: game_workers`，默认 1=串行）让 N 局**同时开跑**——每局完全独立，墙钟时间近似除以 N。
  - 终端转播会静默，只逐局打印 `[完成数/总数] 第g局：…胜｜累计 好x:坏y` 汇总行；每局完整复盘照常写入 `logs/`。
  - 峰值 API 并发 ≈ **7×N**（每局内投票本就最多 7 并发），按你的额度调 N。
  - **可复现**：同一 `--seed` 下，串行与并行的统计结果完全一致（每局按 `base_seed+g` 独立播种，与完成顺序无关）。
  - 人类模式强制单局，`--game-workers` 被忽略。

> 例：`python main.py --games 50 --game-workers 10` 跑 50 局、10 局并发，约 1/10 墙钟时间拿到胜率统计。

> 两层可叠加：跨局并发 N、每局内投票/任务牌再并发，故真正的峰值并发是二者之积——`--no-parallel` 只关轮内那层，不影响跨局。

### 「思考中」动态状态行
等待 LLM 回应时，终端**最后一行**会显示一个会动的提示（旋转字符 + 每 ~1.5s 轮换文案，如「⠹ 玩家3 正在斟酌发言..」），让你一眼看出在算而非卡死。文案**首句是当前具体在做什么**（玩家N 正在组队/发言/投票/复盘…），随后**轮换该阶段专属的俏皮话池**——讨论、投票、任务、刺杀、复盘各有一组贴合时机的句子（见 `avalon/spinner.py` 的 `PHASE_POOLS`）。它**只占一行、原地刷新**，agent 想好、要打印发言/投票/任务结果时**就地擦除不留痕**，既不滚屏也**不写进任何日志**（jsonl/md）。仅在**真 TTY、非批量静默、且无人类玩家**时生效——管道/重定向（如 `... | cat`、`</dev/null`）、`--game-workers>1` 批量、以及人类在场（避免与输入菜单抢最后一行）时自动关闭，`--no-spinner` 可强制关。

### 人类玩家加入
你可以亲自下场顶替一个座位的 agent：每轮发言、当队长时定车、投票、在队伍里时定任务成败、是刺客时选刺杀目标。除发言为自由文本外，其余决策都以**选项菜单**呈现。自由文本（发言/进言/刺杀理由）**每次最多 200 字**，超出会提示并要求精简重输，以免超长输入塞爆各 agent 的 prompt 上下文（上限即 `avalon/human.py` 的 `MAX_INPUT_CHARS`，可按需调）。
```bash
python main.py --human            # 随机顶替一席（座位与角色都随发牌随机），对战 6 个 DeepSeek
python main.py --human-seat 3     # 指定坐第 3 席（角色仍随机），对战 6 个 DeepSeek
python main.py --human --random   # 零 token 练习：对手换成随机基线，不消耗 API
```
开局会私密展示你的角色与夜晚信息（仅你可见，按回车继续）。人类在场时，终端会**隐藏上帝视角悄悄话**（坏人互认、同伴进言）以防剧透——这些仍完整保留在 `logs/<id>.md` 复盘里。人类模式只跑单局（忽略 `--games`）。任何输入点按 **Ctrl-D（EOF）会采用默认选择**（菜单选第一项、选人补最小未选座位、发言沉默）而非崩溃；据此可非交互冒烟跑完整局：`python main.py --human --random </dev/null`。

### 赛后复盘轮
每局分出胜负后、揭示结果之前，引擎会跑一个**复盘轮**：先向所有 agent 揭晓全部 7 席的真实角色，然后每个 agent 基于整局的提队/发言/投票票型/任务暗牌发表一段点评（**2~7 句、单段、不换行**），评判谁玩得好、谁玩得差（语气可较犀利）。各 agent 并行思考，但**按座位序逐个等待并打印**——座位号小的先得到回应、先输出（`玩家1`→`玩家7`，一个一个出），写入终端与 `logs/<id>.md` 的「复盘环节」段。人类玩家**不发表**复盘，引擎直接跳过其座位。默认开启，`--no-review`（或 `config.yaml: review: false`）可关——每局会给每个非人类 agent 多一次 LLM 调用，批量调参想省 token 时建议关。

### 终端配色
游戏中的**发言**与复盘点评统一配色：**名字「玩家N」按座位取一个独立颜色**（7 席各异，一眼区分谁在说话），**发言/点评内容用灰色**。系统旁白（提队/投票/任务结果等）仍按事件类型着色。`--no-color` 可整体关闭颜色（日志 md/jsonl 本就不含颜色码）。

## 局势战报（给 agent 的公共事实视角）
每轮决策前，引擎都会把一份**结构化战报**推送给每个 agent：逐轮记录车主提案车型、最终实际车型（含是否改车）、投票赞成/反对、是否流局、任务成败与失败票数，并附当前比分与累计流局。它作为公开事实喂进 prompt（与承载发言的流水历史并存），让 agent 据实判断局势、不必从聊天记录里猜车型。逻辑在 `game.py` 的 `RoundRecord` / `render_board`。

日志输出在 `logs/<game_id>.jsonl`（机器可读）与 `logs/<game_id>.md`（含上帝视角复盘）。

## 离线单测（零 token）
```bash
python tests/test_engine.py
```

## 模型对战 / 切换 provider
LLM 层 provider 无关（OpenAI 兼容）。在 `config.yaml` 的 `seats` 给某座位单独指定
`model` / `base_url` / `api_key_env` 即可让不同座位用不同模型对战。

## 目录结构
```
main.py            入口：读配置→发牌→建 agent→跑对局→统计
config.yaml        玩家数、随机种子、各座位模型
avalon/
  roles.py         角色枚举、阵营、夜晚知识推导
  game.py          引擎：规则纯函数 + 局势战报（RoundRecord/render_board）+ run_game 主循环
  llm.py           OpenAI 兼容 LLM 客户端（默认 DeepSeek）
  agent.py         LLMAgent（角色感知 prompt→LLM）+ RandomAgent（离线基线）
  human.py         HumanAgent（人类玩家，控制台菜单/输入）
  logger.py        彩色终端 + JSONL + Markdown 复盘
  prompts/         按角色/决策点分文件的 prompt 模板
tests/             引擎纯函数单测
```
