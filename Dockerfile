FROM python:3.13-slim

WORKDIR /app
COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

EXPOSE 8000
# 零第三方依赖，无需 pip install
CMD ["python", "-m", "app.server", "--host", "0.0.0.0", "--port", "8000"]
