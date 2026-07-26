# Образ бота учёта трат для Fly.io
FROM python:3.11-slim

WORKDIR /app

# Зависимости — отдельным слоем (быстрее пересборка при изменении кода)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код бота
COPY . .

# База лежит на постоянном томе Fly (см. [mounts] в fly.toml).
# Секреты (BOT_TOKEN, DB_KEY) задаются через `fly secrets set`, не в образе.
ENV DB_PATH=/data/data.db \
    PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
