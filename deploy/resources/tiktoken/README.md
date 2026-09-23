# tiktoken 离线词表（vendored）

本目录存放 **atomic-rag 与 lightrag 两个镜像**内置的 tiktoken 词表（共用同一份），
让容器启动时**不联网下载**。两个镜像的 Dockerfile 各有一层 `COPY` + 构建期 `sha256sum -c`
校验（`atomic-rag.Dockerfile` 第 6.5 层、`lightrag-source.Dockerfile` builder 阶段）。

## 文件说明

| 文件 | 编码 | 大小 | 消费方 |
|---|---|---|---|
| `fb374d419588a4632f3f557e76b4b70aebbca790` | `o200k_base` | 3,613,922 B (3.6 MB) | atomic-rag：`PushChunksStage` 的表格/打包超限断言<br>lightrag：全文切块（`chunking_by_token_size`） |

只带 `o200k_base`：两侧的 tokenizer 默认值都是 `gpt-4o-mini`，且没有任何环境变量
入口能改成别的模型（lightrag 侧见 `lightrag.py:326` / `rerank.py:26` / `ollama_api.py:162`，
`api/config.py` 无相关配置项），故不存在需要 `cl100k_base` 的代码路径。

源 URL：`https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken`

内容 sha256（= tiktoken 内置 `expected_hash`，加载时自校验，污染即报错）：

```
446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d
```

## 文件名不能改

tiktoken 用「**下载源 URL 的 sha1**」作为缓存文件的查找键（`tiktoken/load.py` 的
`read_file_cached`）。改名后 tiktoken 在缓存目录里找不到该文件，会退回联网下载，
本目录就白放了。当前名字即 `sha1("https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken")`。

## 为什么需要它

**atomic-rag 侧**：`Reference/Rag-anything/raganything/pipeline/stages.py` 的
`PushChunksStage` 在推送 chunk 给 LightRAG 前，用 `TiktokenTokenizer("gpt-4o-mini")`
实测 token 数，判断是否超过 LightRAG 的 `CHUNK_SIZE`（默认 1200）。超限会被 LightRAG
按 token 下标二次切分，而切出的第二片仍继承整块的 `file_source` 元数据 → 表头/行号/
页码锚点错位。不内置该表时 tiktoken 退回默认路径 `/tmp/data-gym-cache`，而 `/tmp` 随
容器重建清空——**每次启动都要重下 3.6 MB**。实测 17~90 秒（网络波动极大），占
atomic-rag 启动耗时的 94%（2026-09-16 实测：71.6s 下载 / 总启动 76s）。

**lightrag 侧**：全文切块走 `chunking_by_token_size`，同样需要该编码表。运行阶段的
`TIKTOKEN_CACHE_DIR=/app/data/tiktoken` 指向镜像内路径，若构建期没烘进去，每次容器
重建都会在运行期重下（2026-09-16 实测启动多花 21s）。

## 如何更新

编码表极少变动（o200k_base 自 2024 年发布至今未变）。确需更新时：

```bash
# 1. 取新表到本目录（文件名由 tiktoken 自动生成为源 URL 的 sha1）
docker run --rm -v "$PWD/deploy/resources/tiktoken:/out" python:3.12-slim \
  sh -c 'pip install -q tiktoken && TIKTOKEN_CACHE_DIR=/out \
         python -c "import tiktoken; tiktoken.get_encoding(\"o200k_base\")"'

# 2. 校验内容哈希，须与 tiktoken 源码 tiktoken_ext/openai_public.py 里
#    o200k_base() 的 expected_hash 一致
sha256sum deploy/resources/tiktoken/*

# 3. 同步更新本文件上方的大小与哈希
```

⚠️ 不要用 `lightrag-download-cache` CLI 的默认行为更新：它一次下 8 个模型，实测单个
就要 68~90 秒，全量常被 timeout 掐断并留下**空缓存**（`deploy/docker/lightrag-source.Dockerfile`
里那份 `timeout 90` 就有这个隐患）。只需一个编码时加 `--models gpt-4o-mini`。
