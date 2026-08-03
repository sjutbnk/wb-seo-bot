# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Устанавливаем зависимости для сборки
RUN pip install --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Создаём непривилегированного пользователя
RUN groupadd -r botuser && useradd -r -g botuser botuser

WORKDIR /app

# Копируем установленные пакеты из builder
COPY --from=builder /install /usr/local

# Копируем исходный код
COPY --chown=botuser:botuser . .

# Переключаемся на непривилегированного пользователя
USER botuser

# Healthcheck — проверяем что процесс жив
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python3 -c "import sys; sys.exit(0)"

CMD ["python3", "main.py"]
