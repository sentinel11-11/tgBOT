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
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

# Панели отдают браузеру HTML, а VPN-клиентам — сам список серверов,
# поэтому представляемся клиентом.
USER_AGENT = "v2rayNG/1.9.5"

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


def fetch_subscription(url: str, timeout: int = 30) -> str:
    """Скачивает подписку по ссылке."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        raise VpnError(f"Не удалось скачать подписку {url}: {exc}") from exc


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
    parser.add_argument("--index", type=int, help="номер сервера (с 0, по умолчанию 0)")
    parser.add_argument("--name", help="выбрать сервер по части названия")
    parser.add_argument("--socks-port", type=int, default=1081, help="порт SOCKS5 (1081)")
    parser.add_argument("--listen", default="127.0.0.1", help="адрес прослушивания")
    parser.add_argument("--output", help="куда записать конфиг Xray")
    args = parser.parse_args()

    try:
        if args.link:
            links = [args.link]
        elif args.file:
            links = parse_subscription(Path(args.file).read_text(encoding="utf-8"))
        else:
            links = parse_subscription(fetch_subscription(args.subscription))

        nodes: list[dict[str, Any]] = []
        errors: list[str] = []
        for link in links:
            try:
                nodes.append(parse_link(link))
            except VpnError as exc:
                errors.append(str(exc))

        if not nodes:
            raise VpnError(
                "Ни одну ссылку не удалось разобрать:\n  " + "\n  ".join(errors[:5])
            )

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
