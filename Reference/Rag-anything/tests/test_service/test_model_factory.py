"""Tests for ModelFactory + ModelRegistry — profile parsing and model function creation."""

import os
import tempfile
from pathlib import Path

import pytest

from raganything.models import ModelRegistry
from raganything.models.drivers.openai import OpenAIDriver
from raganything.models.drivers.echo import EchoDriver
from raganything.service.model_factory import ModelFactory


@pytest.fixture
def profile_dir(tmp_path):
    """Create a minimal profile directory structure."""
    profile = tmp_path / "dev"
    profile.mkdir()

    # models.env
    (profile / "models.env").write_text(
        "qwen3-72b = ollama://http://gpu:11434/v1\n"
        "bge-m3 = ollama://?dim=1024\n"
        "qwen2.5-vl-7b = vllm://http://vision:8000/v1\n"
    )

    # models.yaml — VLM capability
    import yaml
    (profile / "models.yaml").write_text(yaml.dump({
        "models": {
            "qwen2.5-vl-7b": {
                "supports_vision": True,
                "supports_vision_base64": True,
                "supports_vision_url": True,
                "max_context_tokens": 32768,
            },
        }
    }))

    # secrets.env
    (profile / "secrets.env").write_text(
        "ollama=not-needed\n"
        "vllm=sk-test-key\n"
    )

    return profile


@pytest.fixture
def registry(profile_dir):
    """Create a registry with the test profile loaded."""
    r = ModelRegistry(str(profile_dir.parent))
    r.register_driver(OpenAIDriver())
    r.register_driver(EchoDriver())
    return r


class TestModelRegistry:
    """Tests for ModelRegistry — models.env + models.yaml parsing."""

    def test_load_profile(self, registry, profile_dir):
        specs = registry.load_profile("dev")
        assert len(specs) == 3

        qwen = registry._specs["qwen3-72b"]
        assert qwen.binding == "ollama"
        assert qwen.host == "http://gpu:11434/v1"
        assert qwen.capability.supports_vision is False  # no yaml entry → default

        vl = registry._specs["qwen2.5-vl-7b"]
        assert vl.binding == "vllm"
        assert vl.capability.supports_vision is True
        assert vl.capability.supports_vision_base64 is True

    def test_parse_binding_url(self):
        binding, host = ModelRegistry._parse_binding_url(
            "ollama://http://gpu:11434/v1"
        )
        assert binding == "ollama"
        assert host == "http://gpu:11434/v1"

    def test_parse_binding_url_with_params_stripped(self):
        binding, host = ModelRegistry._parse_binding_url(
            "ollama://?dim=1024"
        )
        assert binding == "ollama"
        assert host == ""

    def test_parse_models_env_comments_and_blanks(self, tmp_path):
        env = tmp_path / "models.env"
        env.write_text(
            "# comment line\n"
            "\n"
            "  \n"
            "model-a = ollama://http://host:11434/v1\n"
        )
        registry = ModelRegistry(str(tmp_path.parent))
        registry.register_driver(OpenAIDriver())
        specs = registry._load_models_env(env, {})
        assert len(specs) == 1
        assert specs[0].model_id == "model-a"

    def test_bind_vlm_model(self, registry, profile_dir):
        registry.load_profile("dev")
        bound = registry.bind("qwen2.5-vl-7b")
        assert bound.capability.supports_vision is True
        assert bound.spec.binding == "vllm"

    def test_bind_with_alias_routing(self, registry, profile_dir):
        """vllm:// binding → routed to OpenAIDriver via BINDING_ALIASES."""
        registry.load_profile("dev")
        bound = registry.bind("qwen3-72b")  # binding=ollama → aliased to openai
        assert isinstance(bound.driver, OpenAIDriver)

    def test_list_models_filter(self, registry, profile_dir):
        registry.load_profile("dev")
        vlms = registry.list_models(supports_vision=True)
        assert len(vlms) == 1
        assert vlms[0].model_id == "qwen2.5-vl-7b"

    def test_resolve_api_key_from_env(self, registry, monkeypatch):
        monkeypatch.setenv("QWEN3_72B_API_KEY", "sk-test-123")
        key = ModelRegistry._resolve_api_key("qwen3-72b", "vllm")
        assert key == "sk-test-123"

    def test_resolve_api_key_fallback(self, registry, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fallback")
        key = ModelRegistry._resolve_api_key("unknown-model", "vllm")
        assert key == "sk-fallback"


class TestModelFactoryBuild:
    """Tests for ModelFactory.build() with registry integration."""

    def test_build_returns_all_keys(self, registry, profile_dir):
        registry.load_profile("dev")
        factory = ModelFactory(registry=registry, profiles_dir=str(profile_dir.parent))
        funcs = factory.build(profile="dev", working_dir="/tmp/test_rag")

        assert "config" in funcs
        assert "llm_model_func" in funcs
        assert funcs["llm_model_func"] is not None
        assert "embedding_func" in funcs
        assert "vlm_model_func" in funcs
        assert "asr_model_func" in funcs
        assert "lightrag_kwargs" in funcs

        assert funcs["config"].working_dir == "/tmp/test_rag"

    def test_build_with_overrides(self, registry, profile_dir):
        registry.load_profile("dev")
        factory = ModelFactory(registry=registry, profiles_dir=str(profile_dir.parent))
        funcs = factory.build(
            profile="dev",
            overrides={"parser": "docling", "enable_video_processing": False},
            working_dir="/custom/wd",
        )

        assert funcs["config"].parser == "docling"
        assert funcs["config"].enable_video_processing is False
        assert funcs["config"].working_dir == "/custom/wd"

    def test_legacy_llm_adapter_produces_string(self, registry, profile_dir):
        """The returned llm_model_func is a legacy callable → bound to EchoDriver."""
        from raganything.models.drivers.echo import EchoDriver
        r2 = ModelRegistry(str(profile_dir.parent))
        r2.register_driver(EchoDriver())
        spec = r2._load_models_env(profile_dir / "models.env", {})[0]
        # Override binding to echo for test
        from raganything.models.types import ModelSpec
        echo_spec = ModelSpec(
            model_id=spec.model_id, binding="echo", host="http://localhost",
        )
        r2._specs[echo_spec.model_id] = echo_spec

        factory = ModelFactory(registry=r2, profiles_dir=str(profile_dir.parent))
        factory._ensure_registry = lambda: None  # skip re-init
        factory._ensure_profile = lambda p: None  # skip reload
        factory._registry = r2
        funcs = factory.build(profile="dev")
        # The adapter wraps BoundModel → async callable
        assert funcs["llm_model_func"] is not None

    def test_build_missing_profile_raises(self, tmp_path):
        r = ModelRegistry(str(tmp_path))
        r.register_driver(OpenAIDriver())
        factory = ModelFactory(registry=r, profiles_dir=str(tmp_path))
        with pytest.raises(Exception):  # FileNotFoundError from registry or model_factory
            factory.build(profile="nonexistent")


# ── [jonex] 计量 scene 区分：VLM 与文本 LLM/embedding 分属不同 scene ──────────


class _FakeResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


class _CapturingClient:
    """Minimal httpx.AsyncClient stand-in that records outgoing request headers."""

    last_headers: dict = {}

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        _CapturingClient.last_headers = dict(headers or {})
        return _FakeResponse()


class TestMeteringScene:
    """[jonex] X-Jonex-Scene 按模型类型区分。

    背景：VLM 调用原名 raganything_ingest，与文本抽取同 scene —— 导致
    llm-gateway 计量分不出 VLM 消耗，且会被 LLMGW_DISABLE_THINKING_SCENES
    连带注入 thinking.disabled。
    """

    def _bind(self, registry, profile_dir, model_id, prefix):
        factory = ModelFactory(registry=registry, profiles_dir=str(profile_dir.parent))
        return factory._bind_with_overrides(
            model_id, {}, prefix=prefix, tenant_id="t1", kb_id="kb1",
        )

    def test_vlm_binding_uses_vlm_scene(self, registry, profile_dir):
        registry.load_profile("dev")
        bound = self._bind(registry, profile_dir, "qwen2.5-vl-7b", "vlm")
        assert bound.spec.extra_headers["X-Jonex-Scene"] == "raganything_vlm"

    def test_llm_binding_keeps_ingest_scene(self, registry, profile_dir):
        registry.load_profile("dev")
        bound = self._bind(registry, profile_dir, "qwen3-72b", "llm")
        assert bound.spec.extra_headers["X-Jonex-Scene"] == "raganything_ingest"

    def test_embedding_binding_keeps_ingest_scene(self, registry, profile_dir):
        registry.load_profile("dev")
        bound = self._bind(registry, profile_dir, "bge-m3", "embedding")
        assert bound.spec.extra_headers["X-Jonex-Scene"] == "raganything_ingest"

    def test_ingest_headers_overlay_has_no_scene(self):
        """任务级 overlay 不再输出 scene，避免覆盖模型级 scene。"""
        from raganything.service.jonex_metering_ctx import (
            build_ingest_headers,
            reset_ingest_ctx,
            set_ingest_ctx,
        )

        token = set_ingest_ctx(tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1")
        try:
            headers = build_ingest_headers()
        finally:
            reset_ingest_ctx(token)

        assert "X-Jonex-Scene" not in headers
        assert headers["X-Jonex-Tenant-Id"] == "t1"
        assert headers["X-Jonex-Doc-Id"] == "d1"
        assert headers["X-Jonex-Trace-Id"] == "tr1"

    async def test_driver_request_keeps_model_scene(self, registry, profile_dir, monkeypatch):
        """端到端：driver 实际发出的请求头里 scene 是模型级值，不被 overlay 顶掉。"""
        import httpx

        from raganything.service.jonex_metering_ctx import reset_ingest_ctx, set_ingest_ctx

        monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)
        registry.load_profile("dev")
        bound = self._bind(registry, profile_dir, "qwen2.5-vl-7b", "vlm")

        token = set_ingest_ctx(tenant_id="t1", kb_id="kb1", doc_id="d1", trace_id="tr1")
        try:
            await bound.complete([{"role": "user", "content": "hi"}])
        finally:
            reset_ingest_ctx(token)

        headers = _CapturingClient.last_headers
        assert headers["X-Jonex-Scene"] == "raganything_vlm"
        assert headers["X-Jonex-Doc-Id"] == "d1"  # 任务级维度仍然生效
