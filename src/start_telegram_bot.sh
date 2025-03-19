#!/bin/bash

# Проверка наличия Python 3
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 не установлен"
    exit 1
fi

# Проверка версии Python
python_version=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
if (( $(echo "$python_version < 3.7" | bc -l) )); then
    echo "❌ Требуется Python 3.7 или выше (установлена версия $python_version)"
    exit 1
fi

# Проверка и создание виртуального окружения
if [ ! -d "../venv_new" ]; then
    echo "🔄 Создаю виртуальное окружение..."
    python3 -m venv ../venv_new
fi

# Активация виртуального окружения
source ../venv_new/bin/activate

# Проверка и создание необходимых директорий
for dir in "logs" "sessions" "captcha_screenshots"; do
    if [ ! -d "$dir" ]; then
        echo "📁 Создаю директорию $dir..."
        mkdir -p "$dir"
    fi
done

# Проверка наличия .env файла
if [ ! -f "../.env" ]; then
    echo "❌ Файл .env не найден"
    exit 1
fi

# Проверка наличия необходимых переменных в .env
if ! grep -q "API_ID=" "../.env" || ! grep -q "API_HASH=" "../.env" || ! grep -q "BOT_TOKEN=" "../.env"; then
    echo "❌ В файле .env отсутствуют необходимые переменные (API_ID, API_HASH, BOT_TOKEN)"
    exit 1
fi

# Установка зависимостей
echo "📦 Устанавливаю зависимости..."
pip install -r ../requirements.txt

# Очистка старых lock-файлов
find sessions -name "*.lock" -type f -delete

# Запуск бота
echo "🚀 Запускаю бота..."
python3 "$(pwd)/bot.py" 