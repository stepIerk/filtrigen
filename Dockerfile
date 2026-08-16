FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY db.py userbot.py controlbot.py ./

# По умолчанию контейнер запускает controlbot.py, который сам поднимает и userbot
CMD ["python", "controlbot.py"]
