FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

WORKDIR /app

COPY Scraper/requirements-image-scraper.txt /app/
RUN pip install --no-cache-dir -r requirements-image-scraper.txt

COPY credentials.py /app/
COPY Scraper/__init__.py Scraper/image_scraper.py /app/Scraper/

ENTRYPOINT ["python", "-u", "Scraper/image_scraper.py"]
