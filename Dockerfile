FROM python:3.12-alpine

RUN apk add --no-cache git

RUN adduser -D gcm
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gcm.py .

ENTRYPOINT ["python", "/app/gcm.py"]
