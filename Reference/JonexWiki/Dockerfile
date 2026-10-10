FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY alembic.ini ./
COPY src ./src
RUN pip install --no-cache-dir .
# [jonex] main.py:run() 默认 JONEX_WIKI_API_PORT=8007（引擎实际监听端口）
EXPOSE 8007
CMD ["sh", "-c", "alembic upgrade head && jonex-wiki-api"]
