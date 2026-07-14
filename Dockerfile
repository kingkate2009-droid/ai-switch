FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV AI_SWITCH_PORT=8787

EXPOSE 8787

CMD ["python3", "app.py"]
