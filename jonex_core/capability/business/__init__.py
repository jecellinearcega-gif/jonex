#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""业务能力 Client 包。

这里放「业务能力对外的调用契约」——接入方（知识库等能力、宿主功能模块）通过
`jonex_core.capability.business.<capability>.client` 里的工厂拿 Client，
不直接依赖某个业务能力的内部实现。

跟 `atomic/` 下的 client 是同一套写法：能力清单（`capability_runtime.yaml`）
决定这次是进程内直调还是经 Sidecar 反代到独立进程。
"""

__all__ = []
