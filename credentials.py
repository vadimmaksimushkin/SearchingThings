import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

GOOGLE_MAPS_API_KEY: str = os.environ["GOOGLE_MAPS_API_KEY"]
PLACES_DB_URL:       str = os.environ["PLACES_DB_URL"]
QUEUE_DB_URL:        str = os.environ["QUEUE_DB_URL"]
