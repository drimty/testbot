#!/usr/bin/env bash
# =============================================================================
# Установка / обновление Telegram Bug & Feature Tracker Bot
# Ubuntu 20.04 / 22.04 / 24.04
#
# Первый запуск:  sudo bash install.sh   -> установит всё с нуля
# Повторный запуск: sudo bash install.sh -> проверит обновления и применит их
#   (safe: .env и tickets.db никогда не перезаписываются)
# =============================================================================
set -euo pipefail

# --------------------------- НАСТРОЙКИ (отредактируйте) ---------------------
# Ссылка на "raw" файлы вашего репозитория на GitHub, где лежат
# bot.py, requirements.txt, VERSION.
# Пример: репозиторий https://github.com/USERNAME/REPO, файлы в ветке main ->
#   https://raw.githubusercontent.com/USERNAME/REPO/main
REPO_RAW_URL="https://raw.githubusercontent.com/drimty/testbot/main"   # <-- ВСТАВЬТЕ СЮДА ССЫЛКУ
# -----------------------------------------------------------------------------

INSTALL_DIR="/opt/bugtracker-bot"
SERVICE_NAME="bugtracker-bot"
SERVICE_USER="bugtracker"
VENV_DIR="$INSTALL_DIR/venv"

c_green() { echo -e "\e[32m$1\e[0m"; }
c_yellow() { echo -e "\e[33m$1\e[0m"; }
c_red() { echo -e "\e[31m$1\e[0m"; }

if [[ $EUID -ne 0 ]]; then
    c_red "Запустите скрипт с правами root: sudo bash install.sh"
    exit 1
fi

if [[ "$REPO_RAW_URL" == *"ВАШ_ЛОГИН"* ]]; then
    c_red "Сначала отредактируйте REPO_RAW_URL в начале этого скрипта — укажите ссылку на ваш GitHub-репозиторий!"
    exit 1
fi

FIRST_INSTALL=false
if [[ ! -d "$INSTALL_DIR" ]]; then
    FIRST_INSTALL=true
fi

echo "==> Обновление списка пакетов apt..."
apt-get update -y

echo "==> Установка системных зависимостей (python3, venv, curl)..."
apt-get install -y python3 python3-venv python3-pip curl ca-certificates

if ! id "$SERVICE_USER" &>/dev/null; then
    echo "==> Создание системного пользователя $SERVICE_USER..."
    useradd --system --create-home --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER" || true
fi

mkdir -p "$INSTALL_DIR"

echo "==> Скачивание файлов бота из репозитория..."
TMP_DIR="$(mktemp -d)"
if ! curl -fsSL "$REPO_RAW_URL/bot.py" -o "$TMP_DIR/bot.py" \
    || ! curl -fsSL "$REPO_RAW_URL/requirements.txt" -o "$TMP_DIR/requirements.txt" \
    || ! curl -fsSL "$REPO_RAW_URL/VERSION" -o "$TMP_DIR/VERSION"; then
    c_red "Не удалось скачать файлы по адресу: $REPO_RAW_URL"
    c_red "Проверьте, что REPO_RAW_URL указывает на правильный репозиторий/ветку и что там лежат bot.py, requirements.txt, VERSION."
    rm -rf "$TMP_DIR"
    exit 1
fi

NEW_VERSION="$(tr -d '[:space:]' < "$TMP_DIR/VERSION")"
OLD_VERSION="none"
[[ -f "$INSTALL_DIR/VERSION" ]] && OLD_VERSION="$(tr -d '[:space:]' < "$INSTALL_DIR/VERSION")"

# Сравниваем именно содержимое bot.py (по хэшу), а не только номер версии —
# так обновление сработает, даже если забыли поднять номер в файле VERSION.
NEW_HASH="$(sha256sum "$TMP_DIR/bot.py" | awk '{print $1}')"
OLD_HASH="none"
[[ -f "$INSTALL_DIR/bot.py" ]] && OLD_HASH="$(sha256sum "$INSTALL_DIR/bot.py" | awk '{print $1}')"

if [[ "$NEW_HASH" == "$OLD_HASH" && "$FIRST_INSTALL" == false ]]; then
    c_green "Бот уже обновлён до последней версии ($OLD_VERSION). Изменений нет."
    rm -rf "$TMP_DIR"
    exit 0
fi

echo "==> Версия: $OLD_VERSION -> $NEW_VERSION (обнаружены изменения, применяем)"

cp "$TMP_DIR/bot.py" "$INSTALL_DIR/bot.py"
cp "$TMP_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
cp "$TMP_DIR/VERSION" "$INSTALL_DIR/VERSION"
rm -rf "$TMP_DIR"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    echo "==> Первая установка: создаём файл .env"
    read -rp "Введите токен бота (получен от @BotFather): " BOT_TOKEN
    read -rp "Введите ID администраторов через запятую (узнать у @userinfobot): " ADMIN_IDS
    cat > "$INSTALL_DIR/.env" <<EOF
BOT_TOKEN=$BOT_TOKEN
ADMIN_IDS=$ADMIN_IDS
DB_PATH=tickets.db
EOF
    chmod 600 "$INSTALL_DIR/.env"
else
    echo "==> Файл .env уже существует, оставляю без изменений."
fi

echo "==> Настройка виртуального окружения и установка Python-зависимостей..."
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "==> Настройка systemd-сервиса..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Telegram Bug/Feature Tracker Bot
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python $INSTALL_DIR/bot.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

# Сохраняем копию самого install.sh внутри INSTALL_DIR, чтобы обновления
# в будущем (в т.ч. по cron) всегда запускались из одного и того же места.
cp -f "$(readlink -f "$0")" "$INSTALL_DIR/install.sh" 2>/dev/null || true
chmod +x "$INSTALL_DIR/install.sh" 2>/dev/null || true

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    c_green "✅ Бот версии $NEW_VERSION установлен и запущен."
else
    c_red "⚠️ Сервис не запустился. Проверьте логи: journalctl -u $SERVICE_NAME -n 50"
    exit 1
fi

echo ""
echo "Полезные команды:"
echo "  Статус:            systemctl status $SERVICE_NAME"
echo "  Логи (в реальном времени): journalctl -u $SERVICE_NAME -f"
echo "  Проверить/поставить обновление вручную: sudo bash $INSTALL_DIR/install.sh"
echo ""
echo "Чтобы проверять обновления автоматически (например, раз в день), добавьте в cron:"
echo "  sudo crontab -e"
echo "  0 4 * * * bash $INSTALL_DIR/install.sh >> /var/log/${SERVICE_NAME}-update.log 2>&1"
