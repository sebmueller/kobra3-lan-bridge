FROM python:3.12-alpine
RUN pip install --no-cache-dir "paho-mqtt==1.6.1"
COPY kobra_bridge.py /app/kobra_bridge.py
WORKDIR /app
CMD ["python", "-u", "/app/kobra_bridge.py"]
