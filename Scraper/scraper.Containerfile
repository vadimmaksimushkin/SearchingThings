FROM mcr.microsoft.com/playwright/python:latest

WORKDIR /app

COPY Scraper/requirements-scraper.txt /app/
RUN pip install --no-cache-dir -r requirements-scraper.txt

COPY credentials.py constants.py /app/
COPY Scraper/__init__.py Scraper/scraper.py /app/Scraper/

ENTRYPOINT ["python", "-u", "Scraper/scraper.py"]
