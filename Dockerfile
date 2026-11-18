FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY transit_od ./transit_od
COPY run.py .

ENV APP_DB=/data/app.db
VOLUME ["/data"]
EXPOSE 5000

CMD ["python", "run.py"]
