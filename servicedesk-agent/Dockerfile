# file: Dockerfile
# MCP Streamable HTTP Server —— 无状态，可直接上 Serverless / 容器平台
FROM python:3.12-slim

WORKDIR /app

# 先装依赖（利用镜像层缓存：改代码不必重装依赖）
RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev

# 无状态：所有状态都在请求里或外部存储（句柄 store），容器本身不持有会话
ENV PORT=8080
EXPOSE 8080

# 平台会并发拉起多个实例；任意请求可落到任意实例
CMD ["uv", "run", "--no-dev", "python", "-m", "servicedesk.mcp.http_app"]
