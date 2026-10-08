# OpenKB Jonex（jonex）改动点清单

> 本文件记录Jonex 平台对 vendored OpenKB 源码的所有改动点，便于后续升级 OpenKB 时
> 快速定位、重新 apply。**所有改动均以 `# [jonex]` 注释标记**，可全局搜索 `[jonex]` 定位。

## 一、注释规范

- 单行改动：行尾加 `# [jonex]`。
- 代码块改动：块首加 `# ── [jonex] <说明> ───`，块尾可加 `# ── [jonex] end ───`。
- 新增参数/字段：行尾 `# [jonex]`，参数名统一加前缀 `_jonex_` 或 `X-Jonex-`（HTTP 头）。
- 新增整文件：文件头注释标明 `# [jonex] 新增文件`。

## 二、改动分类

改动分两类：
1. **计量上下文透传（metering）**：让 OpenKB 内部发起的 LLM 调用带上 `X-Jonex-*` 头，使 llm-gateway 能记录 tenant/kb/doc/scene/trace 维度。
2. **配置适配（config）**：支持 `OPENKB_LLM_*` 环境变量走 llm-gateway。

---

## 三、改动清单

### (A) 计量上下文透传（metering）

| 文件 | 位置 | 说明 |
|------|------|------|
| `openkb/jonex_metering.py` | **新增文件** | contextvar 存取 + `build_metering_headers()` — 供 adapter 层按请求注入 X-Jonex-* headers |

scene 取值枚举：

| scene 值 | 触发场景 |
|----------|---------|
| `openkb_compile` | Wiki 编译（摘要/概念/实体页生成） |
| `openkb_query` | 知识库查询 |
| `openkb_chat` | 多轮对话 |
| `openkb_lint` | 语义 Lint 检查 |

### (B) LLM Gateway 配置适配

| 文件 | 位置 | 说明 |
|------|------|------|
| `openkb/config.py` | `resolve_credential_bundle()` 函数内 | 新增 `OPENKB_LLM_BASE_URL` / `OPENKB_LLM_API_KEY` 环境变量优先于标准 `OPENAI_API_BASE` / `LLM_API_KEY` |

---

## 四、改动汇总

| 文件 | 改动类型 | 行数估计 |
|------|---------|---------|
| `openkb/jonex_metering.py` | **新建** | ~70 |
| `openkb/config.py` | 修改 | ~8 |

**总计**：~80 行改动/新增。不修改 `api.py`、`compiler.py`。

---

## 五、agent/query.py — run_query 支持返回工具调用轨迹（2026-08-07）

- `run_query()` 新增 keyword-only 参数 `return_trace: bool = False`。
- `False`（默认）：行为与上游完全一致，返回 `str`。CLI / chat 路径不受影响。
- `True`（仅非流式）：返回 `(answer, trace)`，`trace` 是 agent 的 wiki 浏览轨迹（工具名 + 参数 + 输出预览），供 Jonex `/search/llmwiki` 渲染推理链。
- 新增模块级 helper `_extract_tool_trace(result)`，从 Agents SDK `RunResult.new_items` 提取 `tool_call_item` / `tool_call_output_item`。
- 配对复用已有的 `_resolve_tool_call_id()`（优先 call_id、回退 id），与 `iter_agent_response_events` 的 pending_calls 口径一致 — LiteLLM 路径的 output item 只有 id，按相邻顺序或纯 call_id 配对都会错。
- `get_image` 的输出不记录内容（base64 体积），只保留 `<image>` 占位与参数中的路径。
- 工具输出预览截断至 200 字符（`_TRACE_PREVIEW_LIMIT`）。
- trace 提取整体 try/except 兜底：失败时返回已收集的部分并打 warning 日志，绝不影响 answer 返回。为此新增模块级 `logger = logging.getLogger(__name__)`（该文件原先没有 logger）。
- **2026-08-10 语言**：compiler/query/linter 的 system prompt 从 `"Write all content in {language} language."` 改为 `"Write all content in the same language as the source document."`，不再依赖 config.yaml 的 `language` 字段。原文中文→输出中文，原文英文→输出英文。
- **2026-08-07 重构**：`_extract_tool_trace` → `_extract_run_trace`，产出改为按轮分组（`turns`），每轮含 `thinking`（从 `reasoning_item`/`message_output_item` 提取）、`llm_ms`（LLM 耗时）、`calls`（工具调用）。新增 `_TurnTimingHooks`（`RunHooks`）采集 `on_llm_start`/`on_llm_end` 耗时。新增 `_THINKING_PREVIEW_LIMIT=300`。新增 `_extract_thinking` helper（D16：最后一轮 message 与 final_output 一致则跳过）。D8 截断改为保留所有轮 + 只截 calls。`Runner.run` 传入 `hooks=hooks`（仅 `return_trace=True` 时）。

---

## 六、model 解析去 gpt-5.4 兜底（2026-09-24）

**背景**：内网 LLM 网关（LiteLLM Proxy）没有 `gpt-5.4` 渠道。OpenKB 原把 `DEFAULT_CONFIG["model"]="gpt-5.4"` 作为运行时兜底，凡 config.yaml 未显式写 model 的 KB（含 seed/demo KB、init 时 env 未进容器的 KB）都会静默发出 gpt-5.4 撞 "No available channel"。

**改动**：
- `openkb/config.py`：`DEFAULT_CONFIG["model"]` 从 `"gpt-5.4"` 改为 `None`；新增 `ModelNotConfiguredError` + `resolve_llm_model(config)`。
  - 解析优先级：**config 显式值（KB config.yaml / global.yaml）> `OPENKB_LLM_MODEL` > 抛 `ModelNotConfiguredError`**。
  - config 显式写了 model 的（含历史遗留的 gpt-5.4）**原样保留**——config 是最高权重。
- 引擎 21 处「发 LLM」读取点（`cli.py` 13 处、`agent/chat.py` 3 处、`api_helpers.py` 3 处、`api.py`、`indexer.py`）从 `config.get("model", DEFAULT_CONFIG["model"])` 改为 `resolve_llm_model(config)`。
- `cli.py init` 交互式默认值 + `initialize_kb()` 的 else 兜底：不再写 gpt-5.4，回落 `OPENKB_LLM_MODEL`，二者皆无则报错（init 入口 fail-fast）。
- 平台侧 `jonex_core/capability/atomic/openkb/openkb_adapter.py`：`query`/`compile` 改用引擎 `resolve_llm_model`（query 原先只读 `config["model"]`，不读 env）；`execute` 新增 `except ModelNotConfiguredError` → `CapabilityResponse.error(code=400, error_code="MODEL_NOT_CONFIGURED")`。
- 示例文件 `config.yaml.example` / `.env.example` 同步更新。

**语义不变点**：config 显式写了 model 的存量 KB 行为不变（含 gpt-5.4 的仍按原样发给网关——由网关侧渠道表决定成败）。

> **更正（2026-10-08）**：上面这句的后半段不准确。`gpt-5.4` **不会**原样到达上游：本平台自己的 `llm-gateway` 原先有一张别名表（`jonex_core/llm_gateway/upstream.py` 的 `MODEL_ALIAS_MAP`，2026-08-10 `fae774d2` 引入，比本节更早），入站 `model == "gpt-5.4"` 会在**转发前被改写**成部署里的模型名，并打印 `模型别名映射 | gpt-5.4 → <新名>`。所以准确说法是：
> 1. openkb **层**原样透传（`resolve_llm_model` 不碰 config 里显式写的值）；
> 2. 到本平台 **llm-gateway** 后被 `MODEL_ALIAS_MAP` 改写；
> 3. 改写后的名字才由上游渠道表决定成败。
>
> 也就是说存量 `gpt-5.4` KB 其实是靠这张别名表**照常工作**的，并没有因上游改名而坏掉 —— 真正会坏的是"别名表里也写死旧名字"的那个隐患（2026-10-08 已把它的值改成环境变量名）。

> **再更正（2026-10-08 晚，见 §八）**：上面第 2 步现在**不再成立** —— `MODEL_ALIAS_MAP` 与 `_apply_model_alias()` 已于当日按决定**整体删除**。链路简化为两步：openkb 层解析 → 上游渠道表。因此存量 `gpt-5.4` KB **不再有网关兜底**，必须把 config.yaml 里的 model 改写成当前部署的模型名。

---

## 七、env 链补齐 `DEFAULT_LLM_MODEL` 兜底（2026-10-08）

> ⛔ **本节已被 §九 推翻（2026-10-08 晚，线上 `400004` 事故后）。**
> 本节补的"`DEFAULT_LLM_MODEL` 兜底"**形态不成立** —— openkb 的模型名原样交给
> LiteLLM 会被剥掉 `provider/` 前缀，而 `DEFAULT_LLM_MODEL` 是裸名口径。
> **保留本节只为记录沿革**；当前实现以 §九 为准（该兜底已删除）。
> 本节里「同一条 env 链在两处各写一份」的教训、`_read_env` 的空白判空、
> `env_llm_model()` 作为唯一实现这三点**依然有效**，§九 全部继承。

**背景**：平台侧把 24 处硬编码模型名收敛成「环境变量解析 + `DEFAULT_LLM_MODEL` 全局兜底」后，OpenKB 走的仍是它自己那份 `resolve_llm_model`，链上**没有** `DEFAULT_LLM_MODEL`。后果：运维只配平台统一的 `DEFAULT_LLM_MODEL`（期望"按部署统一换模型只改一处"）时，OpenKB 侧照样报 `ModelNotConfiguredError`。根因是**同一条 env 链在 `config.py` 与 `cli.py` 各写了一份**，补兜底时只想到了 resolver 那份。

**改动**：
- `openkb/config.py`：
  - 新增 `env_llm_model() -> str | None` —— env 链的**唯一实现**：`OPENKB_LLM_MODEL` → `DEFAULT_LLM_MODEL` → `None`（不抛错）。
  - 新增 `OPENKB_MODEL_ENV_VAR` / `DEFAULT_MODEL_ENV_VAR` 两个常量（后者与平台侧 `jonex_core.common.model_config.DEFAULT_MODEL_ENV_VAR` 同名，两边必须一致）。
  - `resolve_llm_model(config)` 改调 `env_llm_model()`，优先级变为 **config 显式值 > `OPENKB_LLM_MODEL` > `DEFAULT_LLM_MODEL` > 抛错**。
  - 环境变量读值统一 **strip 后判空**：`X=` / 纯空格 / 换行一律按「未配置」处理（容器 env 里的常见误配形态，`os.environ.get` 会当成有值放行，把空模型名发上游）。
- `cli.py`：`init` 交互式默认值从手读 `os.environ.get("OPENKB_LLM_MODEL")` 改为 `env_llm_model()`；报错文案（`:1021-1022`）补齐 `DEFAULT_LLM_MODEL`。`initialize_kb()` 的 else 兜底原本就走 `resolve_llm_model({})`，自动获得新的一层。
- 示例文件 `config.yaml.example` / `.env.example` 同步更新。
- 新增 `tests/unit/test_openkb_model_resolution.py`（平台侧，20 例）：用最小 `portalocker` 桩 import `openkb.config`（本仓库只 vendored 了 `openkb` 包、没带它自己的 `tests/`），覆盖优先级全序、空白穿透、拉伸、报错文案，并带一条守卫闸——**除 `config.py` 外 openkb 包内不得再直读模型 env**，防止第二次出现"两处各写一份链"。

**语义不变点**：config 显式写 model 的存量 KB 行为不变；`config.get("model")` 层的真值判断也未改动（保持 §六「config 是最高权重」的承诺），strip 只作用于环境变量层。

**「原样保留」的准确含义**（见 §六 更正）：只在 **openkb 层**原样透传 —— `resolve_llm_model` 不修改 config 里显式写的值。~~请求到本平台 `llm-gateway` 后，`MODEL_ALIAS_MAP` 仍会把入站的 `gpt-5.4` 改写成 `DEFAULT_LLM_MODEL` 的值；因此别名路径是唯一必须配 `DEFAULT_LLM_MODEL` 的地方。~~ **（2026-10-08 晚已作废：`MODEL_ALIAS_MAP` 整体删除，见 §八。现在 openkb 侧 `DEFAULT_LLM_MODEL` 是纯兜底，没有"唯一必填"的路径了。）**

---

## 八、删除网关模型别名改写；openkb 初始化直接写环境变量模型（2026-10-08）

**背景**：§六 之后，存量的 `gpt-5.4` KB 一直靠 `llm-gateway` 的 `MODEL_ALIAS_MAP` 兜着（入站 `gpt-5.4` 在转发前被改写成部署模型名）。这层隐藏改写有两个问题：① 模型名在 KB 的 `config.yaml` 里看不出真实值，不可审计；② 排查「模型名对不上」要跨三层（openkb 层 / 网关别名表 / 上游渠道表），而日志只显示"映射成功"，等于把问题往后挪。

**决定**：模型名应当在 KB 自己的 `config.yaml` 里显式可见，去掉网关的隐藏改写。

**改动**：
- `jonex_core/llm_gateway/upstream.py`：
  - **删除** `MODEL_ALIAS_MAP` 与 `_apply_model_alias()`，以及两处调用（非流式 + 流式各一）。
  - 删除随之不再使用的 `from jonex_core.common.model_config import DEFAULT_MODEL_ENV_VAR, resolve_llm_model`。
  - 原地留一段注释说明这是**故意删除**、以及存量 KB 的补救要求（避免后人以为漏了实现又加回来）。
  - 保留 `MODEL_ROUTE_OVERRIDES`：那是"按 model 名路由到不同上游 host"的另一个功能（当前为空），与别名改写无关。
- `jonex_core/capability/atomic/openkb/openkb_adapter.py`：三处 `initialize_kb(...)` 的 model 参数从手读 `os.getenv("OPENKB_LLM_MODEL")` 改为 `env_llm_model()`（§七 的 env 链唯一实现）：
  - `_handle_init`：`data.get("model") or env_llm_model()`（请求显式值优先）；
  - `_ensure_kb_initialized`（同时删掉随之不再使用的 `import os`）；
  - `_handle_compile_parsed_document` 的首次 init。
  这样 KB 的 `config.yaml` 在创建时就把部署实际在用的模型名**显式记下来** —— 网关不再改写，这个值就是最终发给上游的名字。
- 新增 `tests/unit/test_openkb_init_model.py`：4 例覆盖 init 写 env 模型 / 请求显式值优先 / env 取不到时传 None 交 `initialize_kb` 自己 fail-fast / 幂等；另 2 例是**故意**的守卫闸 —— 断言 `llm-gateway` 不再有 `MODEL_ALIAS_MAP` / `_apply_model_alias`（含源码级检查），将来要加回来必须连同测试一起改并说明理由。

**⚠️ 升级注意（存量数据）**：去掉别名后，`config.yaml` 里仍写着 `gpt-5.4` 的存量 KB 会**直接撞上游 `No available channel`**。升级时用改写脚本处理（见 §10.5）。

**推荐跑法：`docker exec` 进已经在跑的 `jonex-openkb`**（`--root` / `--to` 都能自动取到，见下）::

```bash
docker cp scripts/migrate_openkb_model.py jonex-openkb:/tmp/
docker exec jonex-openkb python /tmp/migrate_openkb_model.py --all            # 预演（默认不落盘）
docker exec jonex-openkb python /tmp/migrate_openkb_model.py --all --apply    # 真跑
docker exec jonex-openkb rm -f /tmp/migrate_openkb_model.py                   # 收尾
```

**两种匹配口径**（`--all` 是 2026-10-08 后补的，也是**以后每次换模型的常规动作**）：

- **不给 `--all`**：只改写 `model` 值恰好等于 `--from`（默认 `gpt-5.4`）的 KB。保守口径。
- **`--all`**：不看当前值，凡 `model` 不等于 `--to` 的一律改写。模型名的唯一来源是环境变量，所以**换完 env 就用这条把存量 KB 一次性对齐**。⚠️ 它会连带改掉**有意指定了别的模型**的 KB，先跑预演看清楚。

两种口径都**不动没写 `model` 键的 KB**（它们本来就走 env 回落，已经是对的），也**不会替它们补上这个键** —— 补上等于把「跟随环境变量」变成「钉死」，反而挡住下一次换模型。两种口径都幂等，重复跑是空操作。

`--root` 默认 `$OPENKB_KB_ROOT`（容器内 = `/app/data/kb_storage`）；`--to` 默认取 `OPENKB_LLM_MODEL`（与 §八 之后 init 写进 `config.yaml` 的同一个值）。⚠ **自 §九 起不再回落 `DEFAULT_LLM_MODEL`** —— 那是 openai SDK 的裸名口径，写进 `config.yaml` 会被 LiteLLM 剥掉前缀、变成一个必然报错的模型名。**这两个变量只在容器的 `env_file` 里，宿主机上没有** —— 这正是必须 `docker exec`（而非另起容器）的原因之一。

**⛔ 不要套用 `docker run --rm -v <卷>:/data ... python:3.12-slim` 那套写法**（2026-10-08 实测三条都成立）：

1. `python:3.12-slim` **没有 pyyaml** → 实测 `ModuleNotFoundError: No module named 'yaml'`。本脚本需要 YAML 解析才能安全地"只改 `model:` 那一行"。
2. **全局配置不在卷里**：`~/.config/openkb/global.yaml` 住在 `jonex-openkb` 容器的**可写层**，不在 `jonex-openkb-storage` 卷内。新起的 `--rm` 容器 `$HOME=/root`，该路径不存在 → 脚本打印"全局配置不存在，跳过"后继续 —— **一个全局 `model: gpt-5.4` 会被静默漏掉**。
3. `docker run` 不继承 `env_file`，`OPENKB_LLM_MODEL` / `OPENKB_KB_ROOT` 都取不到，`--to` 与 `--root` 必须手写死。

（确实要用 `docker run --rm`——比如服务已停——就用 openkb 自己的镜像 `jonex-openkb-source`（含 pyyaml），并把全局配置目录也挂上：`-v jonex-openkb-globalcfg:/root/.config/openkb`。）

**要不要停服：不必。** 落盘走同目录临时文件 + `os.replace`，是**原子替换** —— 运行中的 openkb 每请求都 `load_config()` 读这个文件，但也只会读到"旧的"或"新的"，不会读到半截 YAML。想更稳妥可先 `docker stop jonex-openkb`，改完 `docker start`。

**实际影响面（2026-10-08 对本机 `deploy` 栈实测）**：12 个 KB 中 **8 个**写着 `gpt-5.4`（`YZ-SZ` 3 个 + `tenant_jonex_demo` 5 个），另 4 个未写 `model`（走 env，无需迁移）。**服务器的数字尚未统计**。

**语义不变点**：新建 KB 的模型来源不变（都是环境变量），只是从"网关转发时改写"提前到"init 时写进 config.yaml"。

---

## 九、删除 env 链的 `DEFAULT_LLM_MODEL` 回落（2026-10-08，**推翻 §七**）

**触发**：2026-10-08 线上 openkb 报上游 `400004`（服务 ID 不存在）。根因不是"模型名写错了"，而是 §七 补的那条兜底**形态不成立**。

**为什么不成立（核心事实）**：openkb 的模型名是**原样**交给 LiteLLM 的（`agent/query.py::build_run_config_from_bundle` → `LitellmModel(model=...)` → `litellm.acompletion`），而 LiteLLM 把 `X/Y` 一律当成 `provider/model`、**剥掉第一段**才发给上游。所以 openkb 要的是 `provider/上游服务ID` 形态。

`DEFAULT_LLM_MODEL` 则是给「原样发名」那批消费者（平台 / JonexWiki / RAG，都走 `openai` SDK）用的**裸名**形态 —— 两者形态不同，**跨形态回落必然出错**：

| 回落值（= 平台全局 `DEFAULT_LLM_MODEL`） | LiteLLM 剥一层后上游收到 | 结果 |
| --- | --- | --- |
| `deepseek/deepseek-flash`（服务 ID 自带斜杠） | `deepseek-flash` | ❌ `400004` 服务 ID 不存在（**线上实况**） |
| `deepseek-v4-flash-202605` | — | ❌ `LLM Provider NOT provided` |

即这条回落**只会把「没配」这个一眼能看懂的错误，换成一个看不懂的上游错误**。故删除 —— 缺失即响亮失败。

**改动**：
- `openkb/config.py`：
  - **删除常量 `DEFAULT_MODEL_ENV_VAR`**（留着它就是个"待补回"的邀请）。平台侧同名常量在 `jonex_core.common.model_config`，与本模块无关。
  - `env_llm_model()` 改为**只读** `OPENKB_LLM_MODEL` → `None`。
  - `resolve_llm_model()` 优先级变为 **config 显式值 > `OPENKB_LLM_MODEL` > 抛错**；`ModelNotConfiguredError` 文案点明「必须是 `provider/上游服务ID` 形态」以及**为什么不回落** `DEFAULT_LLM_MODEL`（否则后人看到空兜底会想补回来）。
  - `OPENKB_MODEL_ENV_VAR` 的长注释承载全部理由（含 `400004` 实例）。
- `openkb/cli.py`：交互式 `init` 的 `ModelNotConfiguredError` 文案同步（`:1021` 附近）——`DEFAULT_LLM_MODEL` 只在"为什么不看它"的语境里出现。
- `jonex_core/capability/atomic/openkb/openkb_adapter.py`：两处注释同步（`ModelNotConfiguredError` 的 `except` 分支、`_handle_init` 的 init 注释）。
- `scripts/migrate_openkb_model.py`：`--to` 默认值**只取** `OPENKB_LLM_MODEL`（原为回落到 `DEFAULT_LLM_MODEL`）。它写进 `config.yaml` 的值同样喂给 LiteLLM，所以有着**完全相同**的形态问题 —— 回落会写出一个必然报错的模型名。缺失时退出码 1，文案说明前缀要求。
- 测试同步翻转：
  - `tests/unit/test_openkb_model_resolution.py`：原「必须认 `DEFAULT_LLM_MODEL`」的三例翻转为「**必须不认**」；新增 `test_no_fallback_constant_left_behind`（断言常量已消失）与 `test_error_message_explains_the_provider_prefix`。
  - `tests/unit/test_migrate_openkb_model.py`：原 `test_to_falls_back_to_default_llm_model` 翻转为 `test_to_ignores_default_llm_model`；新增 `test_missing_to_error_explains_the_provider_prefix` / `test_explicit_to_still_wins`。

**⚠️「按部署统一换模型只改一处」对 openkb 明确不成立，而且不该试图让它成立**：`DEFAULT_LLM_MODEL` 只对 `env_file` 里含 `.env` 的服务（platform / sidecar / llm-gateway / knowledge-base / atomic-rag / business-domain / task-center）生效；openkb / jonex-wiki / lightrag 各带**专属 env 文件**，本来就收不到它。换模型时这几个文件要一起改，且 openkb 那个必须带 `provider/` 前缀 —— 详见平台侧 `deploy/.env.example` 的 `DEFAULT_LLM_MODEL` 注释。

**语义不变点**：config 显式写 model 的存量 KB 行为不变；新建 KB 的模型来源不变（仍是环境变量），只是**缺变量时不再可能借兜底蒙混过关**。

---

## 十、同步上游时怎么用这份文件

`Reference/OpenKB/` 是**上游 OpenKB 的 vendored 副本**（不是 fork 出去的独立仓，改动直接落在原地、随本仓一起提交）。同步新上游版本时，本文档就是"哪些改动是 jonex 独有的、必须重新落到新版上"的唯一依据。

### 10.1 先列全 jonex 侧到底改了什么

```bash
# 本仓对 Reference/OpenKB/ 的全部提交
git log --oneline -- Reference/OpenKB/

# 逐个看动了哪些文件（同步前先摸清面）
git log --name-only --format="--- %h %s" -- Reference/OpenKB/
```

**不要**指望用 `git diff` 对着上游比 —— 上游不是本仓的 remote，没有可比的基线。以本文档各节的"改动"清单为准。

### 10.2 必须保留的块（精确到符号）

同步后按此表逐项比对；缺一项都会以**静默功能退化**的形式表现出来（见"漏掉的后果"列）。

| 文件 | 符号 / 位置 | 漏掉的后果 |
| --- | --- | --- |
| `openkb/config.py` | `OPENKB_MODEL_ENV_VAR` 常量（**不应再有 `DEFAULT_MODEL_ENV_VAR`**，见 §九） | env 链断在第一环 / 兜底被补回来 |
| `openkb/config.py` | `_read_env(name) -> str \| None`（strip 后判空） | `OPENKB_LLM_MODEL=` 空值被当成有效值发上游 |
| `openkb/config.py` | `env_llm_model() -> str \| None`（env 链**唯一实现**） | §七 的"两处各写一份链"复发 |
| `openkb/config.py` | `ModelNotConfiguredError` | 报错类型失去稳定契约，平台侧 `except` 抓不到 |
| `openkb/config.py` | `resolve_llm_model()` 优先级 **config 显式值 > `OPENKB_LLM_MODEL` > 抛错**（**无 `DEFAULT_LLM_MODEL` 一环**，见 §九） | 退回静默用 `gpt-5.4` 撞上游；或形态错配 → 上游 400004 |
| `openkb/config.py` | `DEFAULT_CONFIG["model"]` 必须是 `None`（**不能**是 `"gpt-5.4"`） | §六 的 bug 原样复发 |
| `openkb/config.py` | `~509` 起的 `[jonex] LLM Gateway 环境变量映射` 块 | LiteLLM 指不到本平台网关 |
| `openkb/cli.py` | `:55` 的 `env_llm_model` import | `init` 走不回统一 env 链 |
| `openkb/cli.py` | `:1004-1008` 交互式 `init` 默认值走 `env_llm_model()` | 交互式 init 静默写死 `gpt-5.4` |
| `openkb/cli.py` | `:3577` 注释所述"最后兜底不再是 `gpt-5.4`" 的代码 | 同上 |
| `openkb/cli.py` | `:1021` 起的报错文案点明 `OPENKB_LLM_MODEL` 与 **`provider/上游服务ID` 形态要求** | 运维不知道值要带前缀，配了也报 400004 |
| `config.yaml.example` / `.env.example` | §七 同步更新的示例值 | 文档与新默认不一致 |

### 10.3 合并后怎么验（必须跑）

jonex 侧的守卫测试都在**平台仓库** `tests/unit/` 下（不在 vendored 目录里，见 10.4），同步后逐条跑：

```bash
pytest tests/unit/test_openkb_model_resolution.py -q   # env 链优先级 + "除 config.py 外不得直读模型 env" 守卫
pytest tests/unit/test_openkb_init_model.py -q         # init 写 env 模型 + 网关无别名改写的守卫
pytest tests/unit/test_openkb_adapter_failure_logging.py -q   # logger 必须存在（见 §六 的 NameError 事故）
```

三份全绿才算同步完成。**若同步带来了新的模型名解析路径，先看 `test_no_other_module_reads_the_model_env_directly` 是不是还在守** —— 它是防"同一条链第三次被写一份"的那道闸。

### 10.4 两个已知的同步副作用

1. **`openkb/config.py` 超过 800 行上限**。上游 `AGENTS.md` 的硬约束是"modules < 800 lines"（由 `tests/test_file_size.py` 强制，本仓没 vendored 那个测试所以本地不报）。实测沿革：上游 vendored 进来时 **772 行**（`5cec22c4`，2026-07-30）→ `d79cd8da` **780** → **§六 的 `32cefb4a` 已推到 809（这天就越线了）** → 加上 §七 到 **842** → 加上 §九（注释大多于代码）现在是 **868**。同步上游若把它的 `tests/` 一起带进来，这个测试会直接红。要么同步时把 `config.py` 按职责拆开（env 解析抽成独立模块最自然），要么把名字加进上游的 grandfather 名单 —— **别默默无视它**。
2. **本仓没有 vendored openkb 自己的 `tests/`**，所以上游对这些改动的原有测试也不在。jonex 侧的等价覆盖就是 10.3 那三份平台测试；同步时如果上游改了同名符号的语义，要回头审这三份测试是否还表达原意。

### 10.5 改了本文档之外的东西？

本次还新增了 **`scripts/migrate_openkb_model.py`**（存量 KB 模型名改写，服务 §八 的升级动作，也是**以后每次换模型后的全量对齐**入口 —— 用 `--all`；同时处理 `~/.config/openkb/global.yaml` 的全局 `model`，因为它对每个 KB 都生效）与其测试 `tests/unit/test_migrate_openkb_model.py`（46 例）。它**不属于 vendored 上游**（在仓库根的 `scripts/` 下，与 `backfill_*.py` 等运维脚本同处），同步上游时不受影响，但 §八 的升级流程依赖它。
