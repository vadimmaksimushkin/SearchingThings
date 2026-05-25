FROM docker.io/library/python:3.13-slim

WORKDIR /app

COPY Scraper/requirements-extractor.txt /app/
RUN pip install --no-cache-dir -r requirements-extractor.txt

COPY credentials.py /app/
COPY Scraper/__init__.py Scraper/extractor.py /app/Scraper/

ENTRYPOINT ["python", "-u", "Scraper/extractor.py"]