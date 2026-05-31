FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

ENV PORT=8080
ENV DATABASE_PATH=/data/auth.db

VOLUME /data
EXPOSE 8080

CMD ["python", "bot.py"]
