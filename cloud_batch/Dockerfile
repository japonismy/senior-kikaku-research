FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY daily_youtube_metadata_update.py .

CMD ["python", "daily_youtube_metadata_update.py"]
