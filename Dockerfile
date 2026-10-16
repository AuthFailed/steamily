FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot

ENV PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/matplotlib
USER nobody
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD ["python", "-m", "bot.health"]
CMD ["python", "-m", "bot"]
