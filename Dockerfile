# ── Base image ────────────────────────────────────────────────
FROM python:3.11-slim

# ── Working directory ──────────────────────────────────────────
WORKDIR /app

# ── Dependencies install (cache layer) ────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── App files copy ─────────────────────────────────────────────
COPY bot.py .
COPY health_check.py .

# ── Koyeb health check port ────────────────────────────────────
EXPOSE 8000

# ── Start bot ─────────────────────────────────────────────────
CMD ["python", "bot.py"]
