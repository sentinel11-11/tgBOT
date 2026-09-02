"""Превращает ссылку-подписку VPN в конфиг Xray с локальным SOCKS5-прокси.

Зачем: в некоторых сетях Telegram недоступен. Вместо того чтобы заворачивать
в VPN весь сервер (это заденет соседние сервисы и может отрезать SSH),
поднимаем локальный SOCKS5 на 127.0.0.1 и указываем его боту в PROXY_URL.

    # посмотреть список серверов в подписке
    python tools/vpn_proxy.py --subscription https://example.com/abcdef --list

    # сгенерировать конфиг Xray с прокси на 127.0.0.1:1081
    sudo python tools/vpn_proxy.py --subscription https://example.com/abcdef \
        --output /usr/local/etc/xray/config.json

Поддерживаются ссылки vless:// (в т.ч. Reality), vmess://, trojan:// и ss://.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlparse

# Панели отдают разный формат в зависимости от того, каким клиентом
# представиться: кому список ссылок, кому Clash YAML, кому sing-box JSON.
# Перебираем по очереди, пока не найдём то, что умеем разобрать.
USER_AGENTS = [
    "v2rayNG/1.9.5",
    "v2rayN/6.45",
    "sing-box/1.9.0",
    "clash-verge/1.5.11",
    "Clash/1.18.0",
    "curl/8.5.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
]
USER_AGENT = USER_AGENTS[0]

SUPPORTED = ("vless://", "vmess://", "trojan://", "ss://")


class VpnError(Exception):
    """Понятная пользователю ошибка настройки VPN."""


# --------------------------------------------------------------------------- #
#  Загрузка и декодирование подписки
# --------------------------------------------------------------------------- #


def _b64decode(data: str) -> bytes:
    """base64 с добитыми '=' и поддержкой url-safe алфавита."""
    cleaned = "".join(data.split())
    cleaned = cleaned.replace("-", "+").replace("_", "/")
    padding = (-len(cleaned)) % 4
    return base64.b64decode(cleaned + "=" * padding)


def fetch_once(url: str, user_agent: str, timeout: int = 30) -> tuple[str, str]:
    """Одна попытка скачивания. Возвращает (тело, content-type)."""
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        return response.read().decode("utf-8", errors="replace"), content_type


def fetch_subscription(url: str, timeout: int = 30) -> str:
    """Скачивает подписку, перебирая User-Agent, пока формат не окажется понятным."""
    last_payload = ""
    last_error: Exception | None = None

    for user_agent in USER_AGENTS:
        try:
            payload, _ = fetch_once(url, user_agent, timeout)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue

        last_payload = last_payload or payload
        try:
            if collect_nodes(payload):
                return payload
        except VpnError:
            continue

    if last_payload:
        return last_payload  # пусть разбирается вызывающий — он покажет диагностику
    raise VpnError(f"Не удалось скачать подписку {url}: {last_error}")


def parse_subscription(payload: str) -> list[str]:
    """Достаёт ссылки на серверы: подписка бывает и base64, и обычным текстом."""
    payload = payload.strip()
    if not payload:
        raise VpnError("Подписка пустая")

    candidates = [payload]
    if not any(marker in payload for marker in SUPPORTED):
        try:
            candidates.append(_b64decode(payload).decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            pass

    for text in candidates:
        links = [
            line.strip()
            for line in text.replace("\r", "\n").split("\n")
            if line.strip().startswith(SUPPORTED)
        ]
        if links:
            return links

    if payload.lstrip().startswith("<"):
        raise VpnError(
            "По ссылке вернулась веб-страница, а не список серверов.\n"
            "Откройте ссылку в браузере, найдите «ключ подписки» (subscription key) "
            "и передайте именно его."
        )
    raise VpnError("В подписке не нашлось ссылок вида vless:// / vmess:// / trojan:// / ss://")


# --------------------------------------------------------------------------- #
#  Разбор ссылок в outbound-конфиг Xray
# --------------------------------------------------------------------------- #


def _stream_settings(params: dict[str, list[str]], host: str) -> dict[str, Any]:
    """Транспорт и шифрование (общая часть для vless/trojan)."""
    def first(key: str, default: str = "") -> str:
        return params.get(key, [default])[0]

    network = first("type", "tcp") or "tcp"
    security = first("security", "none") or "none"
    sni = first("sni") or first("host") or host

    stream: dict[str, Any] = {"network": network, "security": security}

    if security == "reality":
        stream["realitySettings"] = {
            "serverName": sni,
            "fingerprint": first("fp", "chrome"),
            "publicKey": first("pbk"),
            "shortId": first("sid"),
            "spiderX": first("spx", "/"),
        }
    elif security in {"tls", "xtls"}:
        tls: dict[str, Any] = {"serverName": sni, "fingerprint": first("fp", "chrome")}
        if first("alpn"):
            tls["alpn"] = unquote(first("alpn")).split(",")
        if first("allowInsecure") in {"1", "true"}:
            tls["allowInsecure"] = True
        stream["tlsSettings"] = tls

    if network == "ws":
        headers = {"Host": first("host") or sni} if (first("host") or sni) else {}
        stream["wsSettings"] = {"path": unquote(first("path", "/")) or "/", "headers": headers}
    elif network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": unquote(first("serviceName")),
            "multiMode": first("mode") == "multi",
        }
    elif network == "http" or network == "h2":
        stream["network"] = "http"
        stream["httpSettings"] = {
            "path": unquote(first("path", "/")) or "/",
            "host": [h for h in (first("host") or sni).split(",") if h],
        }
    elif network == "tcp" and first("headerType") == "http":
        stream["tcpSettings"] = {
            "header": {
                "type": "http",
                "request": {"path": [unquote(first("path", "/")) or "/"]},
            }
        }

    return stream


def parse_link(link: str) -> dict[str, Any]:
    """Ссылка на сервер -> outbound Xray. Возвращает {'name':..., 'outbound':...}."""
    link = link.strip()

    # ---------------- VLESS ----------------
    if link.startswith("vless://"):
        url = urlparse(link)
        if not url.hostname or not url.port or not url.username:
            raise VpnError(f"Не разобрать vless-ссылку: {link[:60]}...")
        params = parse_qs(url.query)
        user: dict[str, Any] = {
            "id": url.username,
            "encryption": params.get("encryption", ["none"])[0] or "none",
        }
        if params.get("flow", [""])[0]:
            user["flow"] = params["flow"][0]

        return {
            "name": unquote(url.fragment) or f"{url.hostname}:{url.port}",
            "outbound": {
                "tag": "proxy",
                "protocol": "vless",
                "settings": {
                    "vnext": [{"address": url.hostname, "port": url.port, "users": [user]}]
                },
                "streamSettings": _stream_settings(params, url.hostname),
            },
        }

    # ---------------- VMess ----------------
    if link.startswith("vmess://"):
        try:
            data = json.loads(_b64decode(link[len("vmess://"):]).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise VpnError(f"Не разобрать vmess-ссылку: {exc}") from exc

        host = str(data.get("add", ""))
        params = {
            "type": [str(data.get("net", "tcp"))],
            "security": [str(data.get("tls", "") or "none")],
            "sni": [str(data.get("sni", "") or data.get("host", "") or host)],
            "host": [str(data.get("host", ""))],
            "path": [str(data.get("path", "/"))],
            "headerType": [str(data.get("type", ""))],
            "serviceName": [str(data.get("path", ""))],
            "alpn": [str(data.get("alpn", ""))],
            "fp": [str(data.get("fp", "") or "chrome")],
        }
        return {
            "name": str(data.get("ps") or f"{host}:{data.get('port')}"),
            "outbound": {
                "tag": "proxy",
                "protocol": "vmess",
                "settings": {
                    "vnext": [
                        {
                            "address": host,
                            "port": int(data.get("port", 443)),
                            "users": [
                                {
                                    "id": str(data.get("id", "")),
                                    "alterId": int(data.get("aid", 0) or 0),
                                    "security": str(data.get("scy", "auto") or "auto"),
                                }
                            ],
                        }
                    ]
                },
                "streamSettings": _stream_settings(params, host),
            },
        }

    # ---------------- Trojan ----------------
    if link.startswith("trojan://"):
        url = urlparse(link)
        if not url.hostname or not url.port:
            raise VpnError(f"Не разобрать trojan-ссылку: {link[:60]}...")
        params = parse_qs(url.query)
        params.setdefault("security", ["tls"])
        return {
            "name": unquote(url.fragment) or f"{url.hostname}:{url.port}",
            "outbound": {
                "tag": "proxy",
                "protocol": "trojan",
                "settings": {
                    "servers": [
                        {
                            "address": url.hostname,
                            "port": url.port,
                            "password": unquote(url.username or ""),
                        }
                    ]
                },
                "streamSettings": _stream_settings(params, url.hostname),
            },
        }

    # ---------------- Shadowsocks ----------------
    if link.startswith("ss://"):
        body = link[len("ss://"):]
        fragment = ""
        if "#" in body:
            body, fragment = body.split("#", 1)
        body = body.split("?", 1)[0]

        if "@" in body:
            userinfo, hostport = body.rsplit("@", 1)
            try:
                method, password = _b64decode(userinfo).decode("utf-8").split(":", 1)
            except Exception:  # noqa: BLE001
                method, _, password = unquote(userinfo).partition(":")
        else:
            decoded = _b64decode(body).decode("utf-8")
            userinfo, _, hostport = decoded.rpartition("@")
            method, _, password = userinfo.partition(":")

        host, _, port = hostport.rpartition(":")
        if not host or not port.isdigit():
            raise VpnError(f"Не разобрать ss-ссылку: {link[:60]}...")

        return {
            "name": unquote(fragment) or f"{host}:{port}",
            "outbound": {
                "tag": "proxy",
                "protocol": "shadowsocks",
                "settings": {
                    "servers": [
                        {
                            "address": host,
                            "port": int(port),
                            "method": method,
                            "password": password,
                        }
                    ]
                },
            },
        }

    raise VpnError(f"Неизвестный тип ссылки: {link[:30]}...")


# --------------------------------------------------------------------------- #
#  Другие форматы подписки: Clash YAML и sing-box JSON
# --------------------------------------------------------------------------- #


def _clash_stream(proxy: Mapping[str, Any]) -> dict[str, Any]:
    """Транспорт из записи Clash в формат Xray."""
    network = str(proxy.get("network") or "tcp")
    reality = proxy.get("reality-opts") or {}
    tls_on = bool(proxy.get("tls")) or bool(reality)
    sni = str(proxy.get("servername") or proxy.get("sni") or proxy.get("server") or "")

    security = "reality" if reality else ("tls" if tls_on else "none")
    stream: dict[str, Any] = {"network": network, "security": security}

    if security == "reality":
        stream["realitySettings"] = {
            "serverName": sni,
            "fingerprint": str(proxy.get("client-fingerprint") or "chrome"),
            "publicKey": str(reality.get("public-key") or ""),
            "shortId": str(reality.get("short-id") or ""),
            "spiderX": "/",
        }
    elif security == "tls":
        tls: dict[str, Any] = {
            "serverName": sni,
            "fingerprint": str(proxy.get("client-fingerprint") or "chrome"),
        }
        if proxy.get("skip-cert-verify"):
            tls["allowInsecure"] = True
        stream["tlsSettings"] = tls

    if network == "ws":
        ws_opts = proxy.get("ws-opts") or {}
        headers = dict(ws_opts.get("headers") or {})
        stream["wsSettings"] = {
            "path": str(ws_opts.get("path") or proxy.get("ws-path") or "/"),
            "headers": headers or ({"Host": sni} if sni else {}),
        }
    elif network == "grpc":
        grpc_opts = proxy.get("grpc-opts") or {}
        stream["grpcSettings"] = {
            "serviceName": str(grpc_opts.get("grpc-service-name") or "")
        }
    return stream


def parse_clash(payload: str) -> list[dict[str, Any]]:
    """Подписка в формате Clash: ключ proxies со списком серверов."""
    try:
        import yaml  # PyYAML уже в зависимостях бота
    except ImportError as exc:  # pragma: no cover
        if "proxies:" in payload[:4000]:
            raise VpnError(
                "Подписка в формате Clash, но не установлен PyYAML. "
                "Запустите скрипт через python из venv бота "
                "(/opt/<бот>/.venv/bin/python) или поставьте: apt install python3-yaml"
            ) from exc
        return []

    try:
        data = yaml.safe_load(payload)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
        return []

    nodes: list[dict[str, Any]] = []
    for proxy in data["proxies"]:
        if not isinstance(proxy, dict):
            continue
        kind = str(proxy.get("type") or "").lower()
        name = str(proxy.get("name") or proxy.get("server") or "сервер")
        host, port = str(proxy.get("server") or ""), int(proxy.get("port") or 0)
        if not host or not port:
            continue

        if kind == "vless":
            user: dict[str, Any] = {"id": str(proxy.get("uuid") or ""), "encryption": "none"}
            if proxy.get("flow"):
                user["flow"] = str(proxy["flow"])
            outbound = {
                "tag": "proxy", "protocol": "vless",
                "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
                "streamSettings": _clash_stream(proxy),
            }
        elif kind == "vmess":
            outbound = {
                "tag": "proxy", "protocol": "vmess",
                "settings": {"vnext": [{"address": host, "port": port, "users": [{
                    "id": str(proxy.get("uuid") or ""),
                    "alterId": int(proxy.get("alterId") or 0),
                    "security": str(proxy.get("cipher") or "auto"),
                }]}]},
                "streamSettings": _clash_stream(proxy),
            }
        elif kind == "trojan":
            outbound = {
                "tag": "proxy", "protocol": "trojan",
                "settings": {"servers": [{
                    "address": host, "port": port, "password": str(proxy.get("password") or ""),
                }]},
                "streamSettings": _clash_stream({**proxy, "tls": True}),
            }
        elif kind in {"ss", "shadowsocks"}:
            outbound = {
                "tag": "proxy", "protocol": "shadowsocks",
                "settings": {"servers": [{
                    "address": host, "port": port,
                    "method": str(proxy.get("cipher") or "aes-256-gcm"),
                    "password": str(proxy.get("password") or ""),
                }]},
            }
        else:
            continue

        nodes.append({"name": name, "outbound": outbound})
    return nodes


def parse_singbox(payload: str) -> list[dict[str, Any]]:
    """Подписка в формате sing-box: массив outbounds."""
    try:
        data = json.loads(payload)
    except ValueError:
        return []
    if not isinstance(data, dict) or not isinstance(data.get("outbounds"), list):
        return []

    nodes: list[dict[str, Any]] = []
    for outbound in data["outbounds"]:
        if not isinstance(outbound, dict):
            continue
        kind = str(outbound.get("type") or "").lower()
        host = str(outbound.get("server") or "")
        port = int(outbound.get("server_port") or 0)
        name = str(outbound.get("tag") or host)
        if kind not in {"vless", "vmess", "trojan", "shadowsocks"} or not host or not port:
            continue

        tls = outbound.get("tls") or {}
        reality = tls.get("reality") or {}
        transport = outbound.get("transport") or {}
        network = str(transport.get("type") or "tcp")

        if reality.get("enabled"):
            security = "reality"
        elif tls.get("enabled"):
            security = "tls"
        else:
            security = "none"

        stream: dict[str, Any] = {"network": network, "security": security}
        sni = str(tls.get("server_name") or host)
        fingerprint = str((tls.get("utls") or {}).get("fingerprint") or "chrome")

        if security == "reality":
            stream["realitySettings"] = {
                "serverName": sni, "fingerprint": fingerprint,
                "publicKey": str(reality.get("public_key") or ""),
                "shortId": str(reality.get("short_id") or ""), "spiderX": "/",
            }
        elif security == "tls":
            stream["tlsSettings"] = {"serverName": sni, "fingerprint": fingerprint}

        if network == "ws":
            headers = dict(transport.get("headers") or {})
            stream["wsSettings"] = {
                "path": str(transport.get("path") or "/"),
                "headers": headers or {"Host": sni},
            }
        elif network == "grpc":
            stream["grpcSettings"] = {"serviceName": str(transport.get("service_name") or "")}

        if kind == "vless":
            user: dict[str, Any] = {"id": str(outbound.get("uuid") or ""), "encryption": "none"}
            if outbound.get("flow"):
                user["flow"] = str(outbound["flow"])
            body = {
                "protocol": "vless",
                "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
            }
        elif kind == "vmess":
            body = {
                "protocol": "vmess",
                "settings": {"vnext": [{"address": host, "port": port, "users": [{
                    "id": str(outbound.get("uuid") or ""),
                    "alterId": int(outbound.get("alter_id") or 0),
                    "security": str(outbound.get("security") or "auto"),
                }]}]},
            }
        elif kind == "trojan":
            body = {
                "protocol": "trojan",
                "settings": {"servers": [{
                    "address": host, "port": port,
                    "password": str(outbound.get("password") or ""),
                }]},
            }
        else:
            body = {
                "protocol": "shadowsocks",
                "settings": {"servers": [{
                    "address": host, "port": port,
                    "method": str(outbound.get("method") or "aes-256-gcm"),
                    "password": str(outbound.get("password") or ""),
                }]},
            }

        nodes.append({"name": name, "outbound": {"tag": "proxy", **body, "streamSettings": stream}})
    return nodes


def collect_nodes(payload: str) -> list[dict[str, Any]]:
    """Разбирает подписку в любом из поддерживаемых форматов."""
    # 1) список ссылок (обычным текстом или base64)
    try:
        links = parse_subscription(payload)
    except VpnError:
        links = []
    nodes = []
    for link in links:
        try:
            nodes.append(parse_link(link))
        except VpnError:
            continue
    if nodes:
        return nodes

    # 2) Clash YAML, 3) sing-box JSON
    return parse_clash(payload) or parse_singbox(payload)


def describe_payload(payload: str, limit: int = 400) -> str:
    """Диагностика: что же всё-таки вернул сервер."""
    text = payload.strip()
    if not text:
        return "пустой ответ"
    head = text[:limit].replace("\n", "\n    ")
    kind = "неизвестный формат"
    if text.startswith("<"):
        kind = "HTML-страница (нужен другой адрес подписки)"
    elif text.startswith("{"):
        kind = "JSON"
    elif "proxies:" in text[:2000]:
        kind = "Clash YAML"
    return f"{kind}, {len(text)} символов. Начало ответа:\n    {head}"


# --------------------------------------------------------------------------- #
#  Конфиг Xray
# --------------------------------------------------------------------------- #


def build_config(
    outbound: dict[str, Any],
    *,
    socks_port: int = 1081,
    listen: str = "127.0.0.1",
) -> dict[str, Any]:
    """Готовый конфиг: SOCKS5 внутрь, выбранный сервер наружу."""
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "socks-in",
                "listen": listen,
                "port": socks_port,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
                "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
            }
        ],
        "outbounds": [
            outbound,
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                # Локальные адреса мимо VPN — иначе можно потерять доступ к самому серверу.
                {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"}
            ],
        },
    }


def select_node(nodes: list[dict[str, Any]], index: int | None, name: str | None) -> dict[str, Any]:
    """Выбор сервера по номеру или по части названия."""
    if not nodes:
        raise VpnError("В подписке нет ни одного сервера")
    if name:
        matches = [n for n in nodes if name.lower() in n["name"].lower()]
        if not matches:
            available = ", ".join(n["name"] for n in nodes[:10])
            raise VpnError(f"Сервер с названием «{name}» не найден. Доступны: {available}")
        return matches[0]
    position = index or 0
    if position < 0 or position >= len(nodes):
        raise VpnError(f"Нет сервера №{position}: в подписке их {len(nodes)} (нумерация с 0)")
    return nodes[position]


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Подписка VPN -> конфиг Xray с локальным SOCKS5-прокси"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--subscription", help="ссылка на подписку")
    source.add_argument("--link", help="одна ссылка vless:// / vmess:// / trojan:// / ss://")
    source.add_argument("--file", help="файл со списком ссылок")

    parser.add_argument("--list", action="store_true", help="показать серверы и выйти")
    parser.add_argument(
        "--dump", action="store_true", help="показать, что вернула подписка (диагностика)"
    )
    parser.add_argument("--index", type=int, help="номер сервера (с 0, по умолчанию 0)")
    parser.add_argument("--name", help="выбрать сервер по части названия")
    parser.add_argument("--socks-port", type=int, default=1081, help="порт SOCKS5 (1081)")
    parser.add_argument("--listen", default="127.0.0.1", help="адрес прослушивания")
    parser.add_argument("--output", help="куда записать конфиг Xray")
    args = parser.parse_args()

    try:
        if args.link:
            payload = args.link
        elif args.file:
            payload = Path(args.file).read_text(encoding="utf-8")
        else:
            payload = fetch_subscription(args.subscription)

        if args.dump:
            print(describe_payload(payload, limit=2000))
            return 0

        nodes = collect_nodes(payload)
        if not nodes:
            raise VpnError(
                "В подписке не нашлось серверов в понятном формате "
                "(ссылки vless/vmess/trojan/ss, Clash YAML или sing-box JSON).\n"
                f"Что вернул сервер: {describe_payload(payload)}"
            )
        errors: list[str] = []

        if args.list:
            print(f"Серверов в подписке: {len(nodes)}\n")
            for number, node in enumerate(nodes):
                protocol = node["outbound"]["protocol"]
                print(f"  [{number}] {node['name']}  ({protocol})")
            if errors:
                print(f"\nНе разобрано ссылок: {len(errors)}")
            return 0

        node = select_node(nodes, args.index, args.name)
        config = build_config(
            node["outbound"], socks_port=args.socks_port, listen=args.listen
        )

        rendered = json.dumps(config, ensure_ascii=False, indent=2)
        if args.output:
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered + "\n", encoding="utf-8")
            print(f"Выбран сервер: {node['name']} ({node['outbound']['protocol']})")
            print(f"Конфиг записан: {path}")
            print(f"Прокси будет доступен на socks5://{args.listen}:{args.socks_port}")
        else:
            print(rendered)
        return 0

    except VpnError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
