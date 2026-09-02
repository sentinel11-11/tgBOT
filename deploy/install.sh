#!/usr/bin/env bash
#
# Установка бота на Linux-сервер как systemd-сервиса.
#
# Запускать НА СЕРВЕРЕ из распакованной папки проекта:
#     sudo bash deploy/install.sh
#
# Скрипт безопасен для повторного запуска (обновление = запустить снова):
#   • не трогает уже лежащие на сервере .env, config.yaml и credentials.json;
#   • не трогает базу data/candidates.db;
#   • каждый бот живёт в своей папке под своим пользователем и именем сервиса,
#     поэтому другие боты на этом же сервере не задеваются.
#
# Настройки через переменные окружения:
#     APP_NAME=my-bot sudo -E bash deploy/install.sh     # имя сервиса и папки
#     APP_DIR=/srv/my-bot ...                            # куда ставить
#     RUN_USER=botuser ...                               # от кого запускать
#     NO_SYSTEMD=1 ...                                   # только файлы, без сервиса
#
set -euo pipefail

APP_NAME="${APP_NAME:-axiom-bot}"
APP_DIR="${APP_DIR:-/opt/${APP_NAME}}"
RUN_USER="${RUN_USER:-${APP_NAME}}"
SERVICE="${SERVICE:-${APP_NAME}}"
DESCRIPTION="${DESCRIPTION:-Telegram bot for candidate screening (${APP_NAME})}"
NO_SYSTEMD="${NO_SYSTEMD:-0}"
PYTHON="${PYTHON:-python3}"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
info()  { echo "${GREEN}==>${OFF} $*"; }
warn()  { echo "${YELLOW}[!]${OFF} $*"; }
die()   { echo "${RED}[ОШИБКА]${OFF} $*" >&2; exit 1; }

echo "${BOLD}Установка ${APP_NAME}${OFF}"
echo "  исходники: ${SRC_DIR}"
echo "  каталог:   ${APP_DIR}"
echo "  сервис:    ${SERVICE}.service"
echo "  польз.:    ${RUN_USER}"
echo

[ -d "${SRC_DIR}/bot" ] || die "В ${SRC_DIR} нет папки bot/ — запускайте скрипт из корня проекта."

use_systemd=0
if [ "${NO_SYSTEMD}" != "1" ] && command -v systemctl >/dev/null 2>&1; then
    use_systemd=1
    [ "$(id -u)" -eq 0 ] || die "Нужны права root: sudo bash deploy/install.sh"
fi

# --------------------------------------------------------------------------- #
# 1. Python
# --------------------------------------------------------------------------- #
command -v "${PYTHON}" >/dev/null 2>&1 || die "${PYTHON} не найден. Установите: apt install python3"

py_version="$(${PYTHON} -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
${PYTHON} -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "Нужен Python 3.9+, найден ${py_version}. Установите новее: apt install python3.11"
info "Python ${py_version}"

if ! ${PYTHON} -c 'import venv' >/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ] && command -v apt-get >/dev/null 2>&1; then
        info "Ставлю python3-venv"
        apt-get update -qq && apt-get install -y -qq "python3-venv" >/dev/null
    else
        die "Нет модуля venv. Установите: apt install python3-venv"
    fi
fi

# --------------------------------------------------------------------------- #
# 2. Пользователь и каталоги
# --------------------------------------------------------------------------- #
if [ "${use_systemd}" -eq 1 ]; then
    if ! id -u "${RUN_USER}" >/dev/null 2>&1; then
        info "Создаю системного пользователя ${RUN_USER}"
        useradd --system --shell /usr/sbin/nologin --home-dir "${APP_DIR}" "${RUN_USER}"
    else
        info "Пользователь ${RUN_USER} уже есть"
    fi
fi

mkdir -p "${APP_DIR}" "${APP_DIR}/data" "${APP_DIR}/logs"

# --------------------------------------------------------------------------- #
# 3. Код (секреты и база не перезаписываются)
# --------------------------------------------------------------------------- #
info "Копирую код"
rm -rf "${APP_DIR}/bot" "${APP_DIR}/tools" "${APP_DIR}/deploy"
cp -r "${SRC_DIR}/bot" "${SRC_DIR}/tools" "${SRC_DIR}/deploy" "${APP_DIR}/"
cp "${SRC_DIR}/requirements.txt" "${SRC_DIR}/config.example.yaml" "${APP_DIR}/"
[ -f "${SRC_DIR}/README.md" ] && cp "${SRC_DIR}/README.md" "${APP_DIR}/"

for secret in config.yaml .env credentials.json; do
    if [ -f "${APP_DIR}/${secret}" ]; then
        info "На сервере уже есть ${secret} — оставляю без изменений"
    elif [ -f "${SRC_DIR}/${secret}" ]; then
        cp "${SRC_DIR}/${secret}" "${APP_DIR}/${secret}"
        chmod 600 "${APP_DIR}/${secret}"
        info "Загружен ${secret}"
    else
        warn "${secret} отсутствует — настройте его на сервере (см. конец вывода)"
    fi
done

if [ ! -f "${APP_DIR}/config.yaml" ] && [ -f "${APP_DIR}/config.example.yaml" ]; then
    cp "${APP_DIR}/config.example.yaml" "${APP_DIR}/config.yaml"
    info "config.yaml создан из шаблона"
fi

# --------------------------------------------------------------------------- #
# 4. Виртуальное окружение
# --------------------------------------------------------------------------- #
if [ ! -x "${APP_DIR}/.venv/bin/python" ]; then
    info "Создаю виртуальное окружение"
    ${PYTHON} -m venv "${APP_DIR}/.venv"
fi
info "Ставлю зависимости (это займёт минуту)"
"${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/.venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

if [ "${use_systemd}" -eq 1 ]; then
    chown -R "${RUN_USER}:${RUN_USER}" "${APP_DIR}"
    chmod 750 "${APP_DIR}"
fi

# --------------------------------------------------------------------------- #
# 5. systemd
# --------------------------------------------------------------------------- #
if [ "${use_systemd}" -eq 1 ]; then
    unit_path="/etc/systemd/system/${SERVICE}.service"
    info "Пишу ${unit_path}"
    sed -e "s|__APP_DIR__|${APP_DIR}|g" \
        -e "s|__USER__|${RUN_USER}|g" \
        -e "s|__SERVICE__|${SERVICE}|g" \
        -e "s|__DESCRIPTION__|${DESCRIPTION}|g" \
        "${SRC_DIR}/deploy/tgbot.service.template" > "${unit_path}"

    # ProtectHome=true не даст боту работать, если его поставили внутрь /home
    case "${APP_DIR}" in
        /home/*|/root/*)
            sed -i 's|^ProtectHome=true|ProtectHome=false|' "${unit_path}"
            warn "APP_DIR внутри домашнего каталога — ProtectHome отключён"
            ;;
    esac

    systemctl daemon-reload
    systemctl enable "${SERVICE}" >/dev/null 2>&1 || true

    if [ -s "${APP_DIR}/.env" ] && grep -q '^BOT_TOKEN=.\+' "${APP_DIR}/.env"; then
        info "Запускаю сервис"
        systemctl restart "${SERVICE}"
        sleep 4
        if systemctl is-active --quiet "${SERVICE}"; then
            info "${GREEN}Сервис ${SERVICE} работает${OFF}"
        else
            warn "Сервис не поднялся. Последние строки лога:"
            journalctl -u "${SERVICE}" -n 20 --no-pager || true
        fi
    else
        warn "В ${APP_DIR}/.env нет BOT_TOKEN — сервис включён, но не запущен"
    fi

    echo
    other=$(systemctl list-units --type=service --state=running --no-legend 2>/dev/null \
            | awk '{print $1}' | grep -i -E 'bot' | grep -v "^${SERVICE}.service$" || true)
    if [ -n "${other}" ]; then
        echo "${BOLD}Другие боты на этом сервере (не тронуты):${OFF}"
        echo "${other}" | sed 's/^/  /'
        echo "  Важно: один и тот же токен нельзя запускать дважды —"
        echo "  Telegram вернёт ошибку Conflict: terminated by other getUpdates request."
    fi
fi

# --------------------------------------------------------------------------- #
# 6. Итог
# --------------------------------------------------------------------------- #
cat <<EOF

${BOLD}Готово.${OFF}

Каталог:   ${APP_DIR}
Настройки: ${APP_DIR}/.env  и  ${APP_DIR}/config.yaml
База:      ${APP_DIR}/data/candidates.db
Логи:      ${APP_DIR}/logs/bot.log  и  journalctl -u ${SERVICE} -f

Полезные команды:
  systemctl status ${SERVICE}
  systemctl restart ${SERVICE}
  systemctl stop ${SERVICE}
  journalctl -u ${SERVICE} -f
  ${APP_DIR}/.venv/bin/python ${APP_DIR}/tools/preflight.py    # проверка настроек

EOF
