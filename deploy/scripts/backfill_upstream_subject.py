#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""存量账号「上游主体标识」回填脚本（一次性运维动作）。

背景：本次切键（同步 / 首登改为按上游主体标识认人）新增
``platform.users.upstream_subject`` 列。切键**之前**建好的存量账号该列为 ``NULL``：

- 它们不参与全量缺失核对（``_apply_full_missing`` 会排除锚点为 NULL 的行），
  故漏跑回填**不会**产出假失败 —— 上线顺序是软约束；
- 但它们也认不出上游的人（同步/首登只能退回 username 兜底），且停用路径要多一次反查。

本脚本是那个「尽早执行」的回填动作的入口。

CLI 用法::

    # 预演（只读上游、不落库）
    python deploy/scripts/backfill_upstream_subject.py --tenant-id tenant_jonex_demo --dry-run

    # 真跑
    python deploy/scripts/backfill_upstream_subject.py --tenant-id tenant_jonex_demo

前置：``KEYCLOAK_SERVER_URL`` 与 realm-admin 凭据（``KEYCLOAK_REALM_ADMIN_CLIENT_ID`` /
``KEYCLOAK_REALM_ADMIN_CLIENT_SECRET``）已注入 —— 缺凭据时出站会全部降级为 ``skipped``
（安全 fail-closed，不会写入猜测值），脚本会如实打印。

退出码：0 = 执行完毕（含部分跳过）；1 = 参数非法 / 执行异常。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 允许直接以 `python deploy/scripts/xxx.py` 方式运行（把仓库根挂进 sys.path）。
# ⛔ 不能写定长 `parents[N]`：脚本被 `docker cp` 到镜像内扁平路径（如 /app/xxx.py）时
#    层级会变，`parents[2]` 直接 `IndexError`（运维实测踩到）。改为向上找第一个含
#    `jonex_core` 的祖先目录 —— 仓库内运行（…/deploy/scripts/x.py）与容器内扁平运行
#    （/app/x.py）都能定位到根。
for _ancestor in Path(__file__).resolve().parents:
    if (_ancestor / "jonex_core").is_dir():
        sys.path.insert(0, str(_ancestor))
        break

from capabilities.platform.services.upstream_subject_backfill import (  # noqa: E402
    backfill_upstream_subject,
)
from jonex_core.common import get_logger  # noqa: E402
from jonex_core.common.database import AsyncSessionLocal, close_database  # noqa: E402

logger = get_logger("deploy.scripts.backfill_upstream_subject")


async def run(tenant_id: str, *, dry_run: bool) -> int:
    """执行一次回填；返回进程退出码。"""
    session = AsyncSessionLocal()
    try:
        outcome = await backfill_upstream_subject(session, tenant_id)
        if dry_run:
            # ★ 预演：服务层只把值写在 ORM 对象上、不提交，这里回滚即可整批还原。
            await session.rollback()
        else:
            await session.commit()
        print(
            "[backfill_upstream_subject] tenant=%s scanned=%d backfilled=%d skipped=%d%s"
            % (
                tenant_id,
                outcome.scanned,
                outcome.backfilled,
                outcome.skipped,
                "  （--dry-run：已回滚，未落库）" if dry_run else "",
            )
        )
        if outcome.skipped:
            print(
                "[提示] 有 %d 个账号未回填：多为「上游查不到」或「该标识已被别的账号占用」，"
                "详见上面的 warning 日志。" % outcome.skipped
            )
        return 0
    except Exception as e:  # noqa: BLE001 —— CLI 边界：把异常转成可读信息 + 非零退出码
        logger.error("回填执行失败：%s", e)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 1
    finally:
        await session.close()
        await close_database()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="为存量账号回填上游主体标识（platform.users.upstream_subject）"
    )
    parser.add_argument(
        "--tenant-id",
        required=True,
        help="目标租户 ID（如 tenant_jonex_demo）；禁止 default / system 等业务占位租户",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预演：照常读上游，但结束时回滚、不落库",
    )
    args = parser.parse_args(argv)
    return asyncio.run(run(args.tenant_id, dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
