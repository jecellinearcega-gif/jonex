#!/usr/bin/env python3
"""本机后端服务启动器（dotenv 加载 .env.local，避免 shell 引号移除破坏 JSON 值）。

用法：
    .venv/bin/python scripts/local-dev/launch.py start [name ...]
    .venv/bin/python scripts/local-dev/launch.py stop  [name ...]
    .venv/bin/python scripts/local-dev/launch.py status
"""
import os
import pathlib
import signal
import subprocess
import sys

from dotenv import dotenv_values

# 仓库根 = 本文件的上两层（scripts/local-dev/ → 仓库根）
WS = pathlib.Path(__file__).resolve().parents[2]
LOGS = WS / ".tmp-setup" / "logs"
PIDS = WS / ".tmp-setup" / "pids"

# name -> (解释器, 端口, 额外 env, 参数)
SERVICES = {
    "sidecar": (".venv/bin/python", None, {}, ["main.py"]),
    "gateway": (".venv/bin/python", 8000, {}, ["run_gateway.py"]),
    "knowledge_base": (".venv/bin/python", 8003,
                       {"CAPABILITY_KIND": "business", "CAPABILITY_NAME": "knowledge_base"},
                       ["deploy/start_capability.py"]),
    "business_domain": (".venv/bin/python", 8005,
                        {"CAPABILITY_KIND": "business", "CAPABILITY_NAME": "business_domain"},
                        ["deploy/start_capability.py"]),
    "platform": (".venv/bin/python", 8006,
                 {"CAPABILITY_KIND": "business", "CAPABILITY_NAME": "platform"},
                 ["deploy/start_capability.py"]),
    # B1 单容器：引擎 + jonex_wiki 能力同进程，必须用 pydantic v2 的 .venv-jw；
    # 8007 被另一个 checkout（jonex-wiki-9.8-test）占用，这里用 8017。
    "rag": (".venv-rag/bin/python", 8004,
            {"CAPABILITY_KIND": "atomic", "CAPABILITY_NAME": "rag.lightrag",
             "PYTHONPATH": str(WS)},
            ["deploy/start_capability.py"]),
    "jonex_wiki": (".venv-jw/bin/python", 8017,
                   {"CAPABILITY_KIND": "atomic", "CAPABILITY_NAME": "jonex_wiki",
                    "PYTHONPATH": str(WS)},
                   ["deploy/start_capability.py"]),
}


def base_env() -> dict:
    env = dict(os.environ)
    env.update({k: v for k, v in dotenv_values(WS / ".env.local").items() if v is not None})
    env["PYTHONUNBUFFERED"] = "1"
    env["CAPABILITY_RUNTIME_FILE"] = str(WS / "deploy" / "config" / "capability_runtime.yaml")
    return env


def start(names):
    LOGS.mkdir(parents=True, exist_ok=True)
    PIDS.mkdir(parents=True, exist_ok=True)
    for name in names:
        exe, port, extra, args = SERVICES[name]
        pid_file = PIDS / f"{name}.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), 0)
                print(f"{name}: 已在跑 (pid={pid_file.read_text().strip()})")
                continue
            except (OSError, ValueError):
                pid_file.unlink(missing_ok=True)
        env = {**base_env(), **extra}
        if port:
            env["SERVICE_PORT"] = str(port)
            env["SERVICE_ENDPOINT"] = f"http://127.0.0.1:{port}"
        log = open(LOGS / f"{name}.log", "ab", buffering=0)
        proc = subprocess.Popen(
            [str(WS / exe), *args], cwd=WS, env=env,
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,          # 脱离当前会话，父进程退出后仍存活
        )
        pid_file.write_text(str(proc.pid))
        print(f"{name}: pid={proc.pid} port={port or '-'} log=.tmp-setup/logs/{name}.log")


def stop(names):
    for name in names:
        pid_file = PIDS / f"{name}.pid"
        if not pid_file.exists():
            print(f"{name}: 无 pid 记录")
            continue
        try:
            os.killpg(os.getpgid(int(pid_file.read_text().strip())), signal.SIGTERM)
            print(f"{name}: 已停止")
        except (OSError, ValueError) as exc:
            print(f"{name}: 停止失败 {exc}")
        pid_file.unlink(missing_ok=True)


def status(_picked=None):
    for name, (exe, port, _extra, _args) in SERVICES.items():
        pid_file = PIDS / f"{name}.pid"
        alive = "未启动"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), 0)
                alive = f"运行中 pid={pid_file.read_text().strip()}"
            except (OSError, ValueError):
                alive = "已退出"
        print(f"{name:18} {alive:22} port={port or '-'}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    picked = sys.argv[2:] or list(SERVICES)
    {"start": start, "stop": stop, "status": status}[cmd](picked)
