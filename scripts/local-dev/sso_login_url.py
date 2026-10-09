#!/usr/bin/env python3
"""生成「模拟单点登录网址」——向本机 Gateway 的 SSO 发起端点要一条全新链接。

为什么必须向后端要、不能本地拼：
    Keycloak 的 ``state`` 是一次性票据，由后端发起时生成并写入 Redis（TTL 默认
    300s），回调侧 ``getdel`` 一次性消费。本地拼出来的 authorize 网址 state 不在
    Redis 里，回跳必然被拒（``OidcStateMismatchError``，错误码 3015）。
    所以本脚本只做一件事：请求发起端点、取回后端现签的那条网址。

为什么脚本里没有 realm / client_id：
    这两者都从后端 302 响应的 ``Location`` 里解析得到，无需也不该在脚本里写死。
    脚本唯一硬编码的只有「授权端点路径」这一路由约定。

为什么默认落点不是白名单第一项：
    白名单是 ``{应用: [落点, ...]}``。**同一个落点被多个应用声明 = 宿主入口**（所有子
    应用共用的那个 shell 地址）；**只被一个应用声明 = 该应用专属的 standalone 调试
    入口**（如 core-business 的 5175）。
    SSO 回调只下发 httpOnly refresh cookie、不下发 access token（见 backend
    ``capabilities/platform/api/auth.py`` 的 callback：set_cookie 后直接 302）。宿主 shell
    启动时会拿这枚 cookie 走 ``/auth/refresh`` 换 access（``frontends/shell/src/App.tsx``），
    而子应用 standalone 的启动流程（``frontends/shared/shell-sdk/src/authBootstrap.ts``）
    **不读 cookie**，localStorage 里没有 token 就直接跳密码登录页 —— 落在 standalone
    入口上等于把刚到手的登录态丢掉。
    所以默认落点取「被最多应用共用」的那个（即宿主入口）；并列时探测本机哪个真在跑，
    取第一个应答的。

用法：
    .venv/bin/python scripts/local-dev/sso_login_url.py            # 零参数即用
    .venv/bin/python scripts/local-dev/sso_login_url.py --list     # 列出可用落点
    .venv/bin/python scripts/local-dev/sso_login_url.py -t tenant_x -r http://localhost:8080/
    .venv/bin/python scripts/local-dev/sso_login_url.py --idp casdoor   # 直接跳外部身份源
    .venv/bin/python scripts/local-dev/sso_login_url.py -g http://172.30.2.57:8000

常见报错排查：
    3006 / 403   登录后落点不在 ``AUTH_ALLOWED_REDIRECT_URIS`` 白名单 → 跑 ``--list`` 挑一个
    3005 / 403   租户非法（空 / default / default_tenant / system 被禁）
    1001 / 422   缺 query 参数（tenant_id 或 redirect_uri 没传全）
    3015         打开时 state 已过期或被消费过 → 重跑本脚本拿新链接（TTL 默认 300s）
    3022 / 403   走完外部身份源登录后被拒——``login_policy=synced_only`` 且该用户未同步，
                 属业务预期，不是网址生成错误
    落回登录页   全程没有报错，但登录完停在「密码登录页」——落点是 standalone 调试入口，
                 SSO 登录态被丢弃；改用 ``--list`` 里标 [宿主入口] 的落点
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 仓库根 = 本文件的上两层（scripts/local-dev/ → 仓库根），与 launch.py 同口径
WS = Path(__file__).resolve().parents[2]
ENV_CANDIDATES = ("deploy/.env", ".env.local")

AUTHORIZE_PATH = "/api/v1/auth/oidc/authorize"
DEFAULT_GATEWAY = "http://localhost:8000"
DEFAULT_TENANT = "tenant_jonex_demo"
DEFAULT_STATE_TTL = 300

ERROR_HINTS = {
    3006: "落点不在 AUTH_ALLOWED_REDIRECT_URIS 白名单 → 跑 --list 挑一个",
    3005: "租户非法（空 / default / default_tenant / system 被禁）",
    1001: "缺 query 参数（tenant_id 或 redirect_uri）",
    3015: "state 已过期或被消费 → 重跑本脚本",
    3022: "login_policy=synced_only 且该用户未同步（业务预期，非生成错误）",
}


def load_env_file(path: Path) -> dict:
    """读单个 env 文件为 dict；优先 dotenv（正确解析引号/转义），缺则内置降级解析。"""
    try:
        from dotenv import dotenv_values
    except ImportError:
        if not path.is_file():
            return {}
        out = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
        return out
    if not path.is_file():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def resolve_whitelist(explicit_env: str = "") -> tuple:
    """定位并解析 ``AUTH_ALLOWED_REDIRECT_URIS``。

    返回 ``(env 文件路径或 None, {app 名: [uri, ...]})``。app 名与 URI 一律原样透出，
    脚本不翻译、不推断「哪个端口对应哪个应用」——白名单增删应用或改端口时自动跟随。
    """
    paths = [WS / explicit_env] if explicit_env else [WS / p for p in ENV_CANDIDATES]
    for path in paths:
        if not path.is_file():
            continue
        raw = load_env_file(path).get("AUTH_ALLOWED_REDIRECT_URIS", "")
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            print(f"⚠️  {path} 里的 AUTH_ALLOWED_REDIRECT_URIS 不是合法 JSON，已跳过", file=sys.stderr)
            continue
        if isinstance(parsed, dict):
            return path, parsed
    return None, {}


def group_by_uri(whitelist: dict) -> dict:
    """把 ``{app: [uri, ...]}`` 归并成 ``{uri: [app, ...]}``，保持 URI 首次出现顺序。

    只做归并，不解释 URI 的含义：一个 URI 出现在几个 app 名下由配置说了算。
    """
    groups: dict = {}
    for app, uris in whitelist.items():
        if not isinstance(uris, list):
            continue
        for uri in uris:
            if isinstance(uri, str):
                groups.setdefault(uri, []).append(app)
    return groups


def hosted_order(whitelist: dict) -> list:
    """按「被应用的共用数降序 → 白名单内首次出现顺序」排序，返回 ``[(uri, [app, ...])]``。

    并列时依赖 dict 保序 + sorted 稳定排序，所以顺序完全由白名单自身决定，
    脚本不引入任何额外偏好。
    """
    return sorted(group_by_uri(whitelist).items(), key=lambda kv: -len(kv[1]))


def _same_origin_with_path_prefix(entry: str, uri: str) -> bool:
    """落点命中判定，与后端 ``_redirect_uri_allowed`` 同口径：scheme+host+port 相等 + path 前缀。"""
    a, t = urllib.parse.urlparse(entry), urllib.parse.urlparse(uri)
    try:
        if (a.scheme, a.hostname, a.port) != (t.scheme, t.hostname, t.port):
            return False
    except ValueError:
        return False
    if a.path in ("", "/"):
        return True
    return t.path.startswith(a.path)


def classify_landing(uri: str, order: list) -> tuple:
    """判断一个落点属于宿主入口还是 standalone 调试入口。

    返回 ``(类型, 命中的 app 列表)``，类型为 ``'hosted'`` / ``'standalone'`` / ``'unknown'``。
    命中多个白名单条目时「宿主」优先——只要有一条是共用的，这个落点就是可用的。
    """
    hits = [(u, apps) for u, apps in order if _same_origin_with_path_prefix(u, uri)]
    if not hits:
        return "unknown", []
    for _uri, apps in hits:
        if len(apps) > 1:
            return "hosted", apps
    return "standalone", hits[0][1]


def probe_uri(uri: str, timeout: float) -> tuple:
    """探测落点是否有服务应答，返回 ``(是否可达, 说明)``。

    任何 HTTP 状态码（含 404/302）都算「可达」——重点是那个地址上有没有前端在跑。
    """
    req = urllib.request.Request(uri, headers={"Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"可达（HTTP {resp.status}）"
    except urllib.error.HTTPError as exc:
        return True, f"可达（HTTP {exc.code}）"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"不可达（{exc}）"


def resolve_default_landing(order: list, timeout: float, do_probe: bool) -> tuple:
    """自动挑默认落点，返回 ``(uri, 来源说明, [探测说明行])``。

    优先宿主入口；多个宿主入口并列时（如 :8080 与 :80 同时配着）探测本机哪个在跑，
    取第一个应答的。没有宿主入口、或全都探不到时退回首位并如实说明，不假装成功。
    """
    hosted = [(uri, apps) for uri, apps in order if len(apps) > 1]
    notes: list = []
    if not hosted:
        uri, apps = order[0]
        return uri, f"自动选用（{uri} 仅被 {', '.join(apps)} 声明，白名单里没有共用宿主入口）", notes

    if len(hosted) == 1 or not do_probe:
        uri, apps = hosted[0]
        return uri, f"自动选用宿主入口（{len(apps)} 个应用共用）", notes

    for uri, apps in hosted:
        ok, detail = probe_uri(uri, timeout)
        notes.append(f"探测 {uri} → {detail}")
        if ok:
            return uri, f"自动选用宿主入口（{len(apps)} 个应用共用，本机可达）", notes

    notes.append("⚠️  宿主入口都探测不到（前端没起？）已按白名单顺序返回首个")
    uri, apps = hosted[0]
    return uri, f"自动选用宿主入口（{len(apps)} 个应用共用，可达性未确认）", notes


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止自动跟随 302——我们要的就是那枚 Location 本身。"""

    def redirect_request(self, *_args, **_kwargs):
        return None


def request_authorize(url: str, timeout: float) -> tuple:
    """请求发起端点，返回 ``(状态码, headers, 响应体文本)``；302 不跟随。"""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # 302 与 4xx 都走这里：HTTPError 自身就是 response，body/headers 都在
        return exc.code, exc.headers, exc.read().decode("utf-8", "replace")


def extract_realm(location: str) -> str:
    """从 authorize 网址的 ``/realms/{realm}/`` 段取出 realm，取不到返回空串。"""
    marker = "/realms/"
    path = urllib.parse.urlparse(location).path
    idx = path.find(marker)
    if idx == -1:
        return ""
    return path[idx + len(marker):].split("/", 1)[0]


def with_idp_hint(location: str, alias: str) -> str:
    """给 Keycloak authorize 网址追加 ``kc_idp_hint``（state 不变，仍是一次性票据）。"""
    parts = urllib.parse.urlsplit(location)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k != "kc_idp_hint"]
    query.append(("kc_idp_hint", alias))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def report_error(status: int, body: str) -> None:
    """把后端错误原样透出（不吞错误码），并附排查提示。"""
    print(f"\n❌ 发起失败：HTTP {status}", file=sys.stderr)
    code = None
    try:
        payload = json.loads(body)
        code = payload.get("code")
        print(f"   code    = {code}", file=sys.stderr)
        print(f"   message = {payload.get('message', '')}", file=sys.stderr)
        details = payload.get("error_details") or {}
        for item in details.get("validation_errors") or []:
            print(f"   参数缺失: {'.'.join(str(x) for x in item.get('loc', []))}", file=sys.stderr)
    except json.JSONDecodeError:
        print(f"   body    = {body.strip()[:500]}", file=sys.stderr)
    hint = ERROR_HINTS.get(code)
    if hint:
        print(f"   排查    = {hint}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成「模拟单点登录网址」（向本机 Gateway 的 SSO 发起端点现取）",
        epilog="提示：产出的网址含一次性 state，拿到后请在 TTL 内打开，过期重跑即可。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-g", "--gateway", default=os.environ.get("JONEX_GATEWAY_URL", DEFAULT_GATEWAY),
                        help=f"Gateway 基地址（默认 {DEFAULT_GATEWAY}，可用环境变量 JONEX_GATEWAY_URL 覆盖）")
    parser.add_argument("-t", "--tenant", default=os.environ.get("JONEX_SSO_TENANT", DEFAULT_TENANT),
                        help=f"租户 ID（默认 {DEFAULT_TENANT}）")
    parser.add_argument("-r", "--redirect", default="",
                        help="登录成功后的前端落点；省略则自动选宿主入口（用 --list 看全部候选）")
    parser.add_argument("--idp", default="",
                        help="追加 kc_idp_hint 直接跳指定外部身份源，例如 --idp casdoor")
    parser.add_argument("--env-file", default="",
                        help="指定白名单来源 env 文件（默认依次找 deploy/.env、.env.local）")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP 超时秒数（默认 10）")
    parser.add_argument("--list", action="store_true", help="只列出白名单里的可用落点，不发起请求")
    parser.add_argument("--no-probe", action="store_true",
                        help="自动选落点时跳过可达性探测（前端不在本机跑时用）")
    args = parser.parse_args()

    env_path, whitelist = resolve_whitelist(args.env_file)
    order = hosted_order(whitelist)

    if args.list:
        if not order:
            print("⚠️  没找到 AUTH_ALLOWED_REDIRECT_URIS 白名单配置", file=sys.stderr)
            return 1
        print(f"可用落点（原样读自 {env_path.relative_to(WS)}；宿主入口在前）：")
        width = max(len(uri) for uri, _ in order)
        for uri, apps in order:
            if len(apps) > 1:
                tag, desc = "宿主入口", f"共用 {len(apps)} 个应用：{', '.join(apps)}"
            else:
                tag, desc = "调试入口", f"仅 {apps[0]}（standalone，不能完成 SSO 落地）"
            print(f"  {tag}  {uri:<{width}}  {desc}")
        print("\n  宿主入口 = 所有子应用共用的 shell 地址，SSO 回调后能直接换出登录态")
        print("  调试入口 = 子应用 standalone 调试端口，只用于「宿主登录后换 ticket 跳转」那条链路")
        print("  默认落点 = 从宿主入口起按序探测可达性，取第一个应答的；可用 -r 指定落点")
        return 0

    notes: list = []
    if args.redirect:
        redirect_uri, source = args.redirect, "命令行 --redirect"
        kind, apps = classify_landing(redirect_uri, order)
        if kind == "standalone":
            notes.append(f"⚠️  该落点只被 {apps[0]} 声明，是 standalone 调试入口：SSO 回调只下发 "
                         f"httpOnly refresh cookie，standalone 启动流程不读它 → 会弹回密码登录页")
            shared = [u for u, a in order if len(a) > 1]
            if shared:
                notes.append(f"   要落宿主入口请改用：{' / '.join(shared)}")
        # 不在白名单的落点不在这里提示：后端会先以 3006 拒绝，report_error 已给出排查指引
    elif order:
        redirect_uri, source, probed = resolve_default_landing(
            order, min(args.timeout, 3.0), not args.no_probe)
        notes.extend(probed)
    else:
        print("❌ 未指定 --redirect，且没找到白名单配置可用来自动选取", file=sys.stderr)
        print("   仓库根下依次找过 deploy/.env、.env.local 的 AUTH_ALLOWED_REDIRECT_URIS", file=sys.stderr)
        return 1

    gateway = args.gateway.rstrip("/")
    query = urllib.parse.urlencode({"tenant_id": args.tenant, "redirect_uri": redirect_uri})
    initiate_url = f"{gateway}{AUTHORIZE_PATH}?{query}"

    try:
        status, headers, body = request_authorize(initiate_url, args.timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"❌ 连不上 Gateway（{gateway}）：{exc}", file=sys.stderr)
        print("   确认 Gateway 已启动，或用 -g 指定正确地址（测试服见 --help 示例）", file=sys.stderr)
        return 1

    if status != 302:
        report_error(status, body)
        return 1

    location = headers.get("Location", "")
    if not location:
        print(f"❌ 后端返回 {status} 但 Location 为空，无法解析 authorize 网址", file=sys.stderr)
        return 1

    ttl = load_env_file(env_path).get("KEYCLOAK_STATE_TTL_SECONDS") if env_path else None
    ttl = ttl or DEFAULT_STATE_TTL
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(location).query)).get("state", "?")
    realm = extract_realm(location)

    print("\n" + "═" * 72)
    print("① jonex 发起端点（浏览器直接打开，走全链路）")
    print(f"   {initiate_url}")
    print()
    print("② Keycloak authorize（后端 302 的 Location——同样可直接打开，也便于排查/追加参数）")
    print(f"   {location}")
    if args.idp:
        print()
        print(f"③ 同上 + kc_idp_hint={args.idp}（直接跳外部身份源，免点按钮）")
        print(f"   {with_idp_hint(location, args.idp)}")
    print("═" * 72)
    print(f"state      = {state}")
    print(f"有效期     = {ttl}s（即取即用；登录回调成功一次即被消费，仅重复打开登录页不受影响；过期重跑本脚本）")
    print(f"租户       = {args.tenant}" + (f"  →  realm {realm}" if realm else ""))
    print(f"登录后落点 = {redirect_uri}")
    print(f"             来源：{source}")
    for note in notes:
        print(f"             {note}")
    print("═" * 72)
    print("落点不对会白登录一场：standalone 调试入口（5175/5176/5177 这类子应用端口）接不住")
    print("SSO 登录态，会弹回密码登录页。用 --list 认准「宿主入口」。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
