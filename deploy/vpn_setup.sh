#!/usr/bin/env bash
#
# Локальный SOCKS5-прокси через VPN-подписку — чтобы бот достучался до Telegram
# там, где он заблокирован.
#
# ВАЖНО: через VPN пойдёт ТОЛЬКО наш бот. Весь остальной трафик сервера
# (SSH, соседние боты, обновления) остаётся как был: Xray слушает SOCKS5
# на 127.0.0.1 и никуда не вмешивается.
#
# Запуск на сервере:
#     sudo bash deploy/vpn_setup.sh 'https://ваша-ссылка-подписки'
#
# Полезные ключи:
#     --port 1081          порт SOCKS5 (по умолчанию 1081)
#     --node 2             взять конкретный сервер из подписки (нумерация с 0)
#     --name Netherlands   выбрать сервер по названию
#     --list               показать серверы подписки и выйти
#     --app axiom-bot      имя сервиса бота, которому прописать прокси
#     --dry-run            только сгенерировать конфиг, ничего не устанавливать
#
set -euo pipefail

SUBSCRIPTION=""
SOCKS_PORT=1081
NODE_INDEX=""
NODE_NAME=""
LIST_ONLY=0
DRY_RUN=0
APP_NAME="${APP_NAME:-axiom-bot}"
XRAY_CONFIG="/usr/local/etc/xray/config.json"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VPN_TOOL="${SRC_DIR}/tools/vpn_proxy.py"

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
        --config)  XRAY_CONFIG="$2"; shift 2 ;;
        --list)    LIST_ONLY=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *)         SUBSCRIPTION="$1"; shift ;;
    esac
done

APP_DIR="${APP_DIR:-/opt/${APP_NAME}}"

[ -n "${SUBSCRIPTION}" ] || die "Укажите ссылку на подписку: sudo bash deploy/vpn_setup.sh 'https://...'"
[ -f "${VPN_TOOL}" ] || die "Не найден ${VPN_TOOL} — запускайте скрипт из папки проекта"
command -v python3 >/dev/null 2>&1 || die "Нужен python3: apt install python3"

# --------------------------------------------------------------------------- #
# Список серверов
# --------------------------------------------------------------------------- #
if [ "${LIST_ONLY}" -eq 1 ]; then
    python3 "${VPN_TOOL}" --subscription "${SUBSCRIPTION}" --list
    exit 0
fi

echo "${BOLD}Настройка VPN-прокси для бота${OFF}"
echo "  подписка:  ${SUBSCRIPTION:0:40}..."
echo "  прокси:    socks5://127.0.0.1:${SOCKS_PORT}"
echo "  бот:       ${APP_NAME} (${APP_DIR})"
echo

if [ "${DRY_RUN}" -eq 1 ]; then
    XRAY_CONFIG="$(mktemp /tmp/xray-config-XXXX.json)"
    info "Пробный запуск: конфиг будет записан в ${XRAY_CONFIG}"
else
    [ "$(id -u)" -eq 0 ] || die "Нужны права root: sudo bash deploy/vpn_setup.sh '<ссылка>'"
fi

# --------------------------------------------------------------------------- #
# Установка Xray
# --------------------------------------------------------------------------- #
if [ "${DRY_RUN}" -eq 0 ] && ! command -v xray >/dev/null 2>&1; then
    info "Ставлю Xray-core"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq
        apt-get install -y -qq curl unzip ca-certificates >/dev/null
    fi
    bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" \
        @ install >/dev/null 2>&1 \
        || die "Не удалось установить Xray. Проверьте доступ в интернет с сервера."
    info "Xray установлен: $(xray version 2>/dev/null | head -1)"
elif [ "${DRY_RUN}" -eq 0 ]; then
    info "Xray уже установлен: $(xray version 2>/dev/null | head -1)"
fi

# --------------------------------------------------------------------------- #
# Сколько серверов в подписке
# --------------------------------------------------------------------------- #
info "Читаю подписку"
node_list="$(python3 "${VPN_TOOL}" --subscription "${SUBSCRIPTION}" --list)" \
    || die "Не удалось прочитать подписку (подробности выше)"
echo "${node_list}" | sed 's/^/    /'
node_count="$(echo "${node_list}" | grep -c '^\s*\[[0-9]\+\]' || true)"
[ "${node_count}" -gt 0 ] || die "В подписке нет серверов"

# --------------------------------------------------------------------------- #
# Генерация конфига + проверка доступа к Telegram
# --------------------------------------------------------------------------- #
apply_node() {
    local selector=("$@")
    python3 "${VPN_TOOL}" --subscription "${SUBSCRIPTION}" \
        --socks-port "${SOCKS_PORT}" --output "${XRAY_CONFIG}" "${selector[@]}"
}

telegram_reachable() {
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
            --socks5-hostname "127.0.0.1:${SOCKS_PORT}" https://api.telegram.org/ 2>/dev/null || echo 000)"
    [ "${code}" != "000" ]
}

restart_xray() {
    systemctl restart xray
    sleep 3
    systemctl is-active --quiet xray || {
        warn "Xray не запустился:"
        journalctl -u xray -n 15 --no-pager || true
        return 1
    }
    return 0
}

if [ -n "${NODE_NAME}" ]; then
    apply_node --name "${NODE_NAME}"
elif [ -n "${NODE_INDEX}" ]; then
    apply_node --index "${NODE_INDEX}"
else
    apply_node --index 0
fi

if [ "${DRY_RUN}" -eq 1 ]; then
    echo
    info "Конфиг готов: ${XRAY_CONFIG}"
    python3 -c "import json,sys; json.load(open(sys.argv[1])); print('    JSON корректен')" "${XRAY_CONFIG}"
    exit 0
fi

restart_xray || die "Xray не смог стартовать с этим конфигом"
systemctl enable xray >/dev/null 2>&1 || true

info "Проверяю доступ к Telegram через прокси"
if telegram_reachable; then
    info "${GREEN}Telegram доступен через VPN${OFF}"
elif [ -z "${NODE_NAME}" ] && [ -z "${NODE_INDEX}" ] && [ "${node_count}" -gt 1 ]; then
    warn "Первый сервер не отвечает — перебираю остальные"
    ok=0
    for i in $(seq 1 $((node_count - 1))); do
        info "Пробую сервер №${i}"
        apply_node --index "${i}" >/dev/null || continue
        restart_xray || continue
        if telegram_reachable; then
            info "${GREEN}Заработало на сервере №${i}${OFF}"
            ok=1
            break
        fi
    done
    [ "${ok}" -eq 1 ] || die "Ни один сервер подписки не даёт доступ к Telegram. Проверьте подписку."
else
    die "Telegram недоступен через выбранный сервер. Попробуйте другой: --node 1 или --list"
fi

# --------------------------------------------------------------------------- #
# Прописываем прокси боту
# --------------------------------------------------------------------------- #
env_file="${APP_DIR}/.env"
if [ -f "${env_file}" ]; then
    if grep -q '^[#[:space:]]*PROXY_URL=' "${env_file}"; then
        sed -i "s|^[#[:space:]]*PROXY_URL=.*|PROXY_URL=socks5://127.0.0.1:${SOCKS_PORT}|" "${env_file}"
    else
        printf '\nPROXY_URL=socks5://127.0.0.1:%s\n' "${SOCKS_PORT}" >> "${env_file}"
    fi
    info "В ${env_file} прописан PROXY_URL"

    if systemctl list-unit-files | grep -q "^${APP_NAME}.service"; then
        systemctl restart "${APP_NAME}"
        sleep 4
        if systemctl is-active --quiet "${APP_NAME}"; then
            info "${GREEN}Бот ${APP_NAME} перезапущен и работает${OFF}"
        else
            warn "Бот не поднялся, смотрите: journalctl -u ${APP_NAME} -n 30"
        fi
    fi
else
    warn "Файл ${env_file} не найден — бот ещё не установлен."
    warn "После деплоя добавьте в него строку:"
    echo "    PROXY_URL=socks5://127.0.0.1:${SOCKS_PORT}"
fi

cat <<EOF

${BOLD}Готово.${OFF}

Прокси:        socks5://127.0.0.1:${SOCKS_PORT}  (только для локальных процессов)
Конфиг Xray:   ${XRAY_CONFIG}
Сервис Xray:   systemctl status xray

Сменить сервер:   sudo bash deploy/vpn_setup.sh '<ссылка>' --list
                  sudo bash deploy/vpn_setup.sh '<ссылка>' --node 2
Проверить бота:   cd ${APP_DIR} && .venv/bin/python tools/preflight.py
Логи бота:        journalctl -u ${APP_NAME} -f

EOF
