"""PipelineBuilder — flexible stage composition with insert/replace."""

from typing import List

from raganything.pipeline.base import Stage
from raganything.pipeline.stages import (
    AssetUploadStage,
    EntityExtractStage,
    EntityMergeStage,
    FileValidateStage,
    MultimodalChunkStage,
    MultimodalStage,
    OfficePreviewUploadStage,
    ParseStage,
    PushChunksStage,
    TextInsertStage,
)
from raganything.pipeline_mode import PipelineMode


class PipelineBuilder:
    """Build a DocumentPipeline with named stages, supporting insert and replace."""

    def __init__(self):
        self._stages: List[Stage] = []
        self._names: List[str] = []

    def add_stage(self, name: str, stage: Stage) -> "PipelineBuilder":
        self._stages.append(stage)
        self._names.append(name)
        return self

    def add_before(self, before_name: str, name: str, stage: Stage) -> "PipelineBuilder":
        idx = self._names.index(before_name)
        self._stages.insert(idx, stage)
        self._names.insert(idx, name)
        return self

    def add_after(self, after_name: str, name: str, stage: Stage) -> "PipelineBuilder":
        idx = self._names.index(after_name)
        self._stages.insert(idx + 1, stage)
        self._names.insert(idx + 1, name)
        return self

    def replace(self, old_name: str, name: str, stage: Stage) -> "PipelineBuilder":
        idx = self._names.index(old_name)
        self._stages[idx] = stage
        self._names[idx] = name
        return self

    def build(self):
        from raganything.pipeline import DocumentPipeline
        return DocumentPipeline(list(self._stages))


def create_default_pipeline(mode: PipelineMode = PipelineMode.STANDALONE):
    """Return the standard document processing pipeline.

    DocStatus is handled by EventBus + DocStatusListener, not a pipeline stage.
    """
    return (
        PipelineBuilder()
        .add_stage("validate", FileValidateStage())
        .add_stage("parse", ParseStage())
        .add_stage("text_insert", TextInsertStage())
        .add_stage("multimodal", MultimodalStage(mode=mode))
        .add_stage("multimodal_chunk", MultimodalChunkStage())
        .add_stage("entity_extract", EntityExtractStage())
        .add_stage("entity_merge", EntityMergeStage())
        .build()
    )


def _http_pipeline_prefix():
    """[jonex] HTTP 链路里 push_chunks **之前**的那一段。

    单独抽出来是因为任务中心把上传链路拆成了两步：
      - 第一步 prepare（本函数产出的五段）—— 吃掉所有需要「本地文件还在」的活：
        解析、多模态描述、图片资产上传、office 预览上传
      - 第二步 push_chunks —— 只吃 ctx 里的 content_list / multimodal_results /
        asset_exts，不碰任何文件

    切分点为什么放在 office_preview_upload **之后**而不是 multimodal 之后：
    AssetUploadStage 是 os.path.isfile(img_path) + read_bytes，它需要解析那一刻
    还存在的本地图片；放到第二步就意味着要把图片抄到持久卷并改写 img_path
    （MinerU 的 img_path 指向未挂卷的 /app/output，跨任务必然失效）。放在第一步
    则一步到位，既不用抄图也不用改写路径。代价只是 asset_exts 要落盘传给第二步。

    两处共用这一个定义，避免 create_http_pipeline() 与
    create_prepare_only_pipeline() 漂移。
    """
    return [
        ("validate", FileValidateStage()),
        ("parse", ParseStage()),
        ("multimodal", MultimodalStage(mode=PipelineMode.STANDALONE)),
        # [jonex] §image-refs P1-2: 图片资产上传（multimodal 之后、push_chunks
        # 之前——aext 必须赶在 chunk 写入 file_source 前就位；best-effort，
        # RAG_ASSET_UPLOAD_ENABLED 关闭时为空转）
        ("asset_upload", AssetUploadStage()),
        # [jonex] office 查看原文定位：office 转换 PDF 上传（parse 之后、push_chunks
        # 之前——preview 赶在 chunk 写入前就位；best-effort，
        # OFFICE_PREVIEW_UPLOAD_ENABLED 关闭时为空转）
        ("office_preview_upload", OfficePreviewUploadStage()),
    ]


def create_http_pipeline():
    """Return the HTTP-mode document processing pipeline.

    HTTP mode: validate → parse → multimodal → push_chunks
    Entity extraction and graph merge are delegated to :9621 server-side.

    Used when RAGAnything is initialized with an http_client (production path).
    """
    builder = PipelineBuilder()
    for name, stage in _http_pipeline_prefix():
        builder.add_stage(name, stage)
    return builder.add_stage("push_chunks", PushChunksStage()).build()


def create_prepare_only_pipeline():
    """[jonex] 任务中心三步拆分的**第一步**：validate → parse → multimodal →
    asset_upload → office_preview_upload（= create_http_pipeline() 去掉 push_chunks）。

    产物（content_list / multimodal_results / asset_exts）由调用方
    RAGAnything.prepare_only() 落到共享卷，第二步 push_chunks_only() 读回后单独跑。
    """
    builder = PipelineBuilder()
    for name, stage in _http_pipeline_prefix():
        builder.add_stage(name, stage)
    return builder.build()
