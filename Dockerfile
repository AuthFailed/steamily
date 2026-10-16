FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot

ENV PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/matplotlib
USER nobody
CMD ["python", "-m", "bot"]
