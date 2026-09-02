#!/usr/bin/env bash
#
# Отдельный SOCKS5-прокси (Xray) персонально для нашего бота.
#
# Ставит СВОЙ экземпляр Xray со своим конфигом и своим systemd-сервисом:
#     конфиг:  /usr/local/etc/xray/<APP_NAME>.json
#     сервис:  xray-<APP_NAME>.service
#     порт:    127.0.0.1:1082  (только локально, наружу не торчит)
#
# Существующий Xray (тот, через который работают другие ваши боты) и его
# config.json НЕ ТРОГАЮТСЯ — ни файл, ни сервис xray.service.
# Системная маршрутизация не меняется: SSH и соседние сервисы не затронуты.
#
# Примеры:
#     sudo bash deploy/vpn_setup.sh 'https://vlv.one/xxxx'                  # порт 1082
#     sudo bash deploy/vpn_setup.sh 'https://vlv.one/xxxx' --port 1085
#     sudo bash deploy/vpn_setup.sh 'https://vlv.one/xxxx' --list           # список серверов
#     sudo bash deploy/vpn_setup.sh 'https://vlv.one/xxxx' --name Nether    # выбрать сервер
#     sudo bash deploy/vpn_setup.sh --reuse                                 # взять чужой готовый прокси
#     bash deploy/vpn_setup.sh 'https://vlv.one/xxxx' --dry-run             # ничего не менять
#
set -euo pipefail

SUBSCRIPTION=""
SOCKS_PORT=""
NODE_INDEX=""
NODE_NAME=""
LIST_ONLY=0
DRY_RUN=0
REUSE=0
APP_NAME="${APP_NAME:-axiom-bot}"
TEST_URL="${TEST_URL:-https://api.telegram.org/}"
DEFAULT_PORT=1082

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VPN_TOOL="${SRC_DIR}/tools/vpn_proxy.py"
UNIT_TEMPLATE="${SRC_DIR}/deploy/xray-instance.service.template"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
info() { echo "${GREEN}==>${OFF} $*"; }
warn() { echo "${YELLOW}[!]${OFF} $*"; }
die()  { echo "${RED}[ОШИБКА]${OFF} $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --port)    SOCKS_PORT="$2"; shift 2 ;;
        --node)    NODE_INDEX="$2"; shift 2 ;;
        --name)    NODE_NAME="$2"; shift 2 ;;
        --app)     APP_NAME="$2"; shift 2 ;;
        --list)    LIST_ONLY=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --reuse)   REUSE=1; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *)         SUBSCRIPTION="$1"; shift ;;
    esac
done

SOCKS_PORT="${SOCKS_PORT:-${DEFAULT_PORT}}"
APP_DIR="${APP_DIR:-/opt/${APP_NAME}}"
ENV_FILE="${APP_DIR}/.env"
XRAY_SERVICE="xray-${APP_NAME}"
XRAY_USER="$(printf 'xray-%s' "${APP_NAME}" | cut -c1-31)"
XRAY_CONFIG="${XRAY_CONFIG:-/usr/local/etc/xray/${APP_NAME}.json}"
UNIT_PATH="/etc/systemd/system/${XRAY_SERVICE}.service"

command -v python3 >/dev/null 2>&1 || die "Нужен python3: apt install python3"

# Для разбора подписок в формате Clash нужен PyYAML: он уже есть в venv бота,
# поэтому предпочитаем его системному python3.
PYTHON="python3"
if [ -x "${APP_DIR}/.venv/bin/python" ]; then
    PYTHON="${APP_DIR}/.venv/bin/python"
fi

# curl нужен для проверки доступа к Telegram через прокси
if ! command -v curl >/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ] && command -v apt-get >/dev/null 2>&1; then
        info "Ставлю curl"
        apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null
    else
        die "Нужен curl: apt install curl"
    fi
fi

# --------------------------------------------------------------------------- #
#  Вспомогательное
# --------------------------------------------------------------------------- #
proxy_works() {
    local port="$1" code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 \
            --socks5-hostname "127.0.0.1:${port}" "${TEST_URL}" 2>/dev/null || echo 000)"
    [ "${code}" != "000" ]
}

port_busy() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$"
    else
        return 1
    fi
}

# --------------------------------------------------------------------------- #
#  Режим --list
# --------------------------------------------------------------------------- #
if [ "${LIST_ONLY}" -eq 1 ]; then
    [ -n "${SUBSCRIPTION}" ] || die "Для --list нужна ссылка на подписку"
    "${PYTHON}" "${VPN_TOOL}" --subscription "${SUBSCRIPTION}" --list
    exit 0
fi

# --------------------------------------------------------------------------- #
#  Режим --reuse: взять уже работающий на сервере прокси
# --------------------------------------------------------------------------- #
if [ "${REUSE}" -eq 1 ]; then
    info "Ищу работающий SOCKS5-прокси"
    found=""
    for port in "${SOCKS_PORT}" 1081 1080 10808 10809 1082; do
        printf '    порт %-6s ' "${port}"
        if proxy_works "${port}"; then echo "${GREEN}Telegram доступен${OFF}"; found="${port}"; break; fi
        echo "нет"
    done
    [ -n "${found}" ] || die "Готовый прокси не найден. Запустите со ссылкой на подписку."
    SOCKS_PORT="${found}"
    info "Использую существующий прокси, ничего не устанавливаю"
else
    # ----------------------------------------------------------------------- #
    #  Свой экземпляр Xray
    # ----------------------------------------------------------------------- #
    [ -n "${SUBSCRIPTION}" ] || die \
        "Укажите ссылку на подписку:
    sudo bash deploy/vpn_setup.sh 'https://ваша-ссылка'
Либо возьмите уже работающий на сервере прокси:
    sudo bash deploy/vpn_setup.sh --reuse"
    [ -f "${VPN_TOOL}" ] || die "Не найден ${VPN_TOOL} — запускайте из папки проекта"
    [ -f "${UNIT_TEMPLATE}" ] || die "Не найден ${UNIT_TEMPLATE}"

    echo "${BOLD}Отдельный прокси для бота ${APP_NAME}${OFF}"
    echo "  порт:    127.0.0.1:${SOCKS_PORT}"
    echo "  конфиг:  ${XRAY_CONFIG}"
    echo "  сервис:  ${XRAY_SERVICE}.service"
    echo "  чужой Xray (xray.service и config.json) не затрагивается"
    echo

    if [ "${DRY_RUN}" -eq 1 ]; then
        XRAY_CONFIG="$(mktemp /tmp/xray-${APP_NAME}-XXXX.json)"
        info "Пробный запуск, конфиг: ${XRAY_CONFIG}"
    else
        [ "$(id -u)" -eq 0 ] || die "Нужны права root: sudo bash deploy/vpn_setup.sh '<ссылка>'"

        # Порт не должен быть занят чужим процессом
        if port_busy "${SOCKS_PORT}" && ! systemctl is-active --quiet "${XRAY_SERVICE}" 2>/dev/null; then
            die "Порт ${SOCKS_PORT} уже занят другим процессом.
Посмотрите кем:   ss -ltnp | grep ${SOCKS_PORT}
и возьмите свободный:  sudo bash deploy/vpn_setup.sh '<ссылка>' --port 1085"
        fi

        # Xray ставим, только если его ещё нет
        if command -v xray >/dev/null 2>&1; then
            info "Xray уже установлен: $(xray version 2>/dev/null | head -1)"
        else
            info "Ставлю Xray-core"
            if command -v apt-get >/dev/null 2>&1; then
                apt-get update -qq
                apt-get install -y -qq curl unzip ca-certificates >/dev/null
            fi
            bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" \
                @ install >/dev/null 2>&1 \
                || die "Не удалось установить Xray. Проверьте доступ в интернет с сервера."
            # Установщик поднимает свой xray.service с дефолтным конфигом — он нам не нужен
            systemctl disable --now xray >/dev/null 2>&1 || true
            info "Xray установлен: $(xray version 2>/dev/null | head -1)"
        fi
        mkdir -p "$(dirname "${XRAY_CONFIG}")"

        # Отдельный пользователь для прокси: не root и не общий nobody
        if ! id -u "${XRAY_USER}" >/dev/null 2>&1; then
            info "Создаю системного пользователя ${XRAY_USER}"
            useradd --system --no-create-home --shell /usr/sbin/nologin "${XRAY_USER}"
        fi
    fi

    info "Читаю подписку"
    node_list="$("${PYTHON}" "${VPN_TOOL}" --subscription "${SUBSCRIPTION}" --list)" \
        || die "Не удалось прочитать подписку (подробности выше)"
    echo "${node_list}" | sed 's/^/    /'
    node_count="$(echo "${node_list}" | grep -c '^[[:space:]]*\[[0-9]\+\]' || true)"
    [ "${node_count}" -gt 0 ] || die "В подписке нет серверов"

    apply_node() {
        "${PYTHON}" "${VPN_TOOL}" --subscription "${SUBSCRIPTION}" \
            --socks-port "${SOCKS_PORT}" --output "${XRAY_CONFIG}" "$@"
    }

    if   [ -n "${NODE_NAME}" ];  then apply_node --name "${NODE_NAME}"
    elif [ -n "${NODE_INDEX}" ]; then apply_node --index "${NODE_INDEX}"
    else apply_node --index 0
    fi

    if [ "${DRY_RUN}" -eq 1 ]; then
        "${PYTHON}" -c "import json,sys; json.load(open(sys.argv[1])); print('    JSON корректен')" "${XRAY_CONFIG}"
        echo
        info "Так будет выглядеть unit-файл ${UNIT_PATH}:"
        sed -e "s|__APP_NAME__|${APP_NAME}|g" -e "s|__PORT__|${SOCKS_PORT}|g" \
            -e "s|__CONFIG__|${XRAY_CONFIG}|g" -e "s|__SERVICE__|${XRAY_SERVICE}|g" \
            -e "s|__RUN_USER__|${XRAY_USER}|g" \
            "${UNIT_TEMPLATE}" | sed 's/^/    /'
        info "Пробный запуск завершён, система не изменена"
        exit 0
    fi

    # Конфиг содержит ключи VPN — читать может только сам процесс прокси
    chown "${XRAY_USER}:${XRAY_USER}" "${XRAY_CONFIG}"
    chmod 600 "${XRAY_CONFIG}"

    info "Ставлю сервис ${XRAY_SERVICE}"
    sed -e "s|__APP_NAME__|${APP_NAME}|g" \
        -e "s|__PORT__|${SOCKS_PORT}|g" \
        -e "s|__CONFIG__|${XRAY_CONFIG}|g" \
        -e "s|__SERVICE__|${XRAY_SERVICE}|g" \
        -e "s|__RUN_USER__|${XRAY_USER}|g" \
        "${UNIT_TEMPLATE}" > "${UNIT_PATH}"
    systemctl daemon-reload
    systemctl enable "${XRAY_SERVICE}" >/dev/null 2>&1 || true

    restart_xray() {
        systemctl restart "${XRAY_SERVICE}"
        sleep 3
        systemctl is-active --quiet "${XRAY_SERVICE}" && return 0
        warn "${XRAY_SERVICE} не запустился:"
        journalctl -u "${XRAY_SERVICE}" -n 15 --no-pager || true
        return 1
    }

    restart_xray || die "Xray не смог стартовать с этим конфигом"

    info "Проверяю доступ к Telegram через прокси"
    if proxy_works "${SOCKS_PORT}"; then
        info "${GREEN}Telegram доступен${OFF}"
    elif [ -z "${NODE_NAME}" ] && [ -z "${NODE_INDEX}" ] && [ "${node_count}" -gt 1 ]; then
        warn "Первый сервер не отвечает — перебираю остальные"
        ok=0
        for i in $(seq 1 $((node_count - 1))); do
            info "Пробую сервер №${i}"
            apply_node --index "${i}" >/dev/null || continue
            chown "${XRAY_USER}:${XRAY_USER}" "${XRAY_CONFIG}"; chmod 600 "${XRAY_CONFIG}"
            restart_xray || continue
            if proxy_works "${SOCKS_PORT}"; then
                info "${GREEN}Заработало на сервере №${i}${OFF}"; ok=1; break
            fi
        done
        [ "${ok}" -eq 1 ] || die "Ни один сервер подписки не даёт доступ к Telegram"
    else
        die "Telegram недоступен через выбранный сервер. Другой: --node 1 (список: --list)"
    fi
fi

# --------------------------------------------------------------------------- #
#  Прописываем прокси боту
# --------------------------------------------------------------------------- #
proxy_url="socks5://127.0.0.1:${SOCKS_PORT}"

if [ -f "${ENV_FILE}" ]; then
    if grep -q '^[#[:space:]]*PROXY_URL=' "${ENV_FILE}"; then
        sed -i "s|^[#[:space:]]*PROXY_URL=.*|PROXY_URL=${proxy_url}|" "${ENV_FILE}"
    else
        printf '\nPROXY_URL=%s\n' "${proxy_url}" >> "${ENV_FILE}"
    fi
    info "В ${ENV_FILE} прописан PROXY_URL=${proxy_url}"

    if systemctl list-unit-files 2>/dev/null | grep -q "^${APP_NAME}.service"; then
        systemctl restart "${APP_NAME}"
        sleep 4
        if systemctl is-active --quiet "${APP_NAME}"; then
            info "${GREEN}Бот ${APP_NAME} перезапущен и работает${OFF}"
        else
            warn "Бот не поднялся: journalctl -u ${APP_NAME} -n 30"
        fi
    fi
else
    warn "Файл ${ENV_FILE} не найден — бот ещё не установлен."
    warn "После деплоя добавьте в него строку:"
    echo "    PROXY_URL=${proxy_url}"
fi

cat <<EOF

${BOLD}Готово.${OFF}

Прокси бота:     ${proxy_url}
Сервис прокси:   systemctl status ${XRAY_SERVICE}
Конфиг прокси:   ${XRAY_CONFIG}
Чужой Xray:      не изменялся (xray.service и его config.json нетронуты)

Проверить прокси:  curl --socks5-hostname 127.0.0.1:${SOCKS_PORT} -I https://api.telegram.org
Проверить бота:    cd ${APP_DIR} && .venv/bin/python tools/preflight.py
Логи бота:         journalctl -u ${APP_NAME} -f
Сменить сервер:    sudo bash deploy/vpn_setup.sh '<ссылка>' --list
                   sudo bash deploy/vpn_setup.sh '<ссылка>' --node 2

EOF
