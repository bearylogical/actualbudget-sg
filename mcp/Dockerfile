FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py oauth.py ./
RUN useradd -r -u 10001 mcp && mkdir -p /data && chown mcp /data
USER mcp
EXPOSE 8765
CMD ["python", "server.py", "--http", "--host", "0.0.0.0", "--port", "8765"]
