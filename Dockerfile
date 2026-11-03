FROM python:3.12-slim

# 全站只依赖标准库，无需 pip install：镜像小、构建快、没有供应链风险
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=80

WORKDIR /app

COPY app.py billing.py payments.py tools.py ./
COPY config ./config
COPY web ./web
COPY inventory ./inventory

# 运行时数据目录。
# 注意：容器文件系统是临时的，重新发版会重置。
# 必须在云托管控制台把「文件存储(CFS)」挂载到 /app/data，
# 否则租户、余额、充值单会随每次发版全部丢失。
RUN mkdir -p /app/data

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ.get('PORT','80'),timeout=4)"

CMD ["python", "app.py"]
