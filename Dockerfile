FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV AI_SWITCH_PORT=8787
# The container must listen on every interface for the published port to work.
# The host-side binding is what keeps it private (see docker-compose.yml); the
# non-loopback bind also makes AI Switch print an access token on startup.
ENV AI_SWITCH_HOST=0.0.0.0

EXPOSE 8787

CMD ["python3", "app.py"]
