"""[jonex] 把 `capabilities.knowledge_base` 的「短名身份」钉到规范身份上。

pytest 收集 `capabilities/...` 下的用例时会把 `capabilities/` 放进 `sys.path`，
于是 `import knowledge_base` 会**再执行一遍** `capabilities/knowledge_base/**`：
同一份 `models/document.py` 被注册成第二个模块对象，而它声明进的是**同一个**
`Base.metadata` → `Table 'knowledge_base.knowledge_documents' is already defined`。

本 conftest 在收集前先把规范名导入，再把短名指向同一个模块对象 ——
后续那次 `import knowledge_base` 命中 `sys.modules` 缓存，不再重复执行。
"""
import sys

import capabilities.knowledge_base  # noqa: F401  （先建立规范身份）

for _short, _canonical in (("knowledge_base", "capabilities.knowledge_base"),):
    sys.modules.setdefault(_short, sys.modules[_canonical])
