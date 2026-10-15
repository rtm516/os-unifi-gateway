FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collectors/ collectors/
COPY *.py ./

ENV UNIFI_GW_CONFIG=/app/conf/unifi-gateway.conf
ENV UNIFI_GW_LOG_LEVEL=INFO
ENV PYTHONUNBUFFERED=1

VOLUME ["/app/conf"]

ENTRYPOINT ["python", "unifi_gateway.py"]
CMD ["run"]
