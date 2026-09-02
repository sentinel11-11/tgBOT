"""Тесты разбора VPN-подписки и генерации конфига Xray."""

from __future__ import annotations

import base64
import json

import pytest

from tools.vpn_proxy import (
    VpnError,
    build_config,
    parse_link,
    parse_subscription,
    select_node,
)

VLESS_REALITY = (
    "vless://11111111-2222-3333-4444-555555555555@1.2.3.4:443"
    "?type=tcp&security=reality&pbk=PUBLICKEY123&fp=chrome&sni=www.microsoft.com"
    "&sid=ab12&spx=%2F&flow=xtls-rprx-vision#Netherlands%20Smart"
)
VLESS_WS_TLS = (
    "vless://aaaa-bbbb@example.com:8443"
    "?type=ws&security=tls&path=%2Fwspath&host=cdn.example.com&sni=cdn.example.com#WS%20Node"
)
TROJAN = "trojan://secretpass@trojan.example.com:443?security=tls&sni=trojan.example.com#Trojan%20RU"
SS = "ss://" + base64.b64encode(b"aes-256-gcm:password123").decode() + "@ss.example.com:8388#SS%20Node"

VMESS_PAYLOAD = {
    "v": "2", "ps": "VMess Node", "add": "vmess.example.com", "port": "443",
    "id": "99999999-8888-7777-6666-555555555555", "aid": "0", "scy": "auto",
    "net": "ws", "type": "none", "host": "vmess.example.com",
    "path": "/vmesspath", "tls": "tls", "sni": "vmess.example.com",
}
VMESS = "vmess://" + base64.b64encode(json.dumps(VMESS_PAYLOAD).encode()).decode()


# --------------------------------------------------------------------------- #
#  Разбор ссылок
# --------------------------------------------------------------------------- #

def test_vless_reality():
    node = parse_link(VLESS_REALITY)
    assert node["name"] == "Netherlands Smart"

    outbound = node["outbound"]
    assert outbound["protocol"] == "vless"

    vnext = outbound["settings"]["vnext"][0]
    assert vnext["address"] == "1.2.3.4" and vnext["port"] == 443
    assert vnext["users"][0]["id"] == "11111111-2222-3333-4444-555555555555"
    assert vnext["users"][0]["flow"] == "xtls-rprx-vision"

    stream = outbound["streamSettings"]
    assert stream["security"] == "reality"
    assert stream["realitySettings"]["publicKey"] == "PUBLICKEY123"
    assert stream["realitySettings"]["shortId"] == "ab12"
    assert stream["realitySettings"]["serverName"] == "www.microsoft.com"


def test_vless_websocket_tls():
    stream = parse_link(VLESS_WS_TLS)["outbound"]["streamSettings"]
    assert stream["network"] == "ws"
    assert stream["security"] == "tls"
    assert stream["wsSettings"]["path"] == "/wspath"
    assert stream["wsSettings"]["headers"]["Host"] == "cdn.example.com"
    assert stream["tlsSettings"]["serverName"] == "cdn.example.com"


def test_vmess():
    node = parse_link(VMESS)
    assert node["name"] == "VMess Node"

    outbound = node["outbound"]
    assert outbound["protocol"] == "vmess"
    vnext = outbound["settings"]["vnext"][0]
    assert vnext["address"] == "vmess.example.com" and vnext["port"] == 443
    assert vnext["users"][0]["alterId"] == 0
    assert outbound["streamSettings"]["wsSettings"]["path"] == "/vmesspath"


def test_trojan():
    outbound = parse_link(TROJAN)["outbound"]
    assert outbound["protocol"] == "trojan"
    server = outbound["settings"]["servers"][0]
    assert server["password"] == "secretpass"
    assert server["address"] == "trojan.example.com"
    assert outbound["streamSettings"]["security"] == "tls"


def test_shadowsocks():
    outbound = parse_link(SS)["outbound"]
    assert outbound["protocol"] == "shadowsocks"
    server = outbound["settings"]["servers"][0]
    assert server["method"] == "aes-256-gcm"
    assert server["password"] == "password123"
    assert server["port"] == 8388


def test_unknown_scheme_raises():
    with pytest.raises(VpnError, match="Неизвестный тип"):
        parse_link("wireguard://whatever")


# --------------------------------------------------------------------------- #
#  Подписка
# --------------------------------------------------------------------------- #

def test_subscription_base64():
    payload = base64.b64encode(f"{VLESS_REALITY}\n{TROJAN}".encode()).decode()
    assert parse_subscription(payload) == [VLESS_REALITY, TROJAN]


def test_subscription_plain_text():
    assert parse_subscription(f"{VLESS_REALITY}\r\n{VMESS}\n") == [VLESS_REALITY, VMESS]


def test_subscription_html_gives_clear_error():
    with pytest.raises(VpnError, match="веб-страница"):
        parse_subscription("<!DOCTYPE html><html><body>Subscription</body></html>")


def test_subscription_empty():
    with pytest.raises(VpnError, match="пустая"):
        parse_subscription("   ")


# --------------------------------------------------------------------------- #
#  Выбор сервера и конфиг
# --------------------------------------------------------------------------- #

def test_select_by_name_and_index():
    nodes = [parse_link(VLESS_REALITY), parse_link(TROJAN)]
    assert select_node(nodes, None, "trojan")["name"] == "Trojan RU"
    assert select_node(nodes, 0, None)["name"] == "Netherlands Smart"
    assert select_node(nodes, None, None)["name"] == "Netherlands Smart"


def test_select_missing_name_lists_available():
    nodes = [parse_link(VLESS_REALITY)]
    with pytest.raises(VpnError, match="Netherlands Smart"):
        select_node(nodes, None, "Japan")


def test_select_index_out_of_range():
    with pytest.raises(VpnError, match="в подписке их 1"):
        select_node([parse_link(TROJAN)], 5, None)


def test_config_exposes_local_socks_only():
    config = build_config(parse_link(VLESS_REALITY)["outbound"], socks_port=1081)

    inbound = config["inbounds"][0]
    assert inbound["protocol"] == "socks"
    assert inbound["port"] == 1081
    assert inbound["listen"] == "127.0.0.1", "прокси не должен торчать наружу"

    tags = [o["tag"] for o in config["outbounds"]]
    assert tags == ["proxy", "direct", "block"]

    # Локальные сети идут мимо VPN — иначе можно потерять доступ к серверу
    rule = config["routing"]["rules"][0]
    assert rule["ip"] == ["geoip:private"] and rule["outboundTag"] == "direct"


def test_config_is_valid_json():
    config = build_config(parse_link(VMESS)["outbound"])
    assert json.loads(json.dumps(config))["outbounds"][0]["protocol"] == "vmess"
