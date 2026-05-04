FROM apify/actor-python:3.11

LABEL maintainer="StromAudit Pro"
LABEL version="2.0"
LABEL description="Deutsche Energie-Compliance & ESG Pre-Audit Engine"

WORKDIR /usr/src/app

# Dependencies installeren
COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir --upgrade pip && \
    python3 -m pip install --no-cache-dir -r requirements.txt

# Broncode en data kopiëren
COPY main.py ./
COPY plz_data.json ./
COPY INPUT_SCHEMA.json ./
COPY dataset_schema.json ./

CMD ["python3", "main.py"]
