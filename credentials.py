import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

GOOGLE_MAPS_API_KEY:      str = os.environ["GOOGLE_MAPS_API_KEY"]
PLACES_DB_URL:            str = os.environ["PLACES_DB_URL"]
QUEUE_DB_URL:             str = os.environ["QUEUE_DB_URL"]
IMAGE_QUEUE_DB_URL:       str = os.environ["IMAGE_QUEUE_DB_URL"]
R2_USER_TOKEN:            str = os.environ["R2_USER_TOKEN"]
R2_ACCOUNT_ID:            str = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY:            str = os.environ["R2_ACCESS_KEY"]
R2_SECRET_ACCESS_KEY:     str = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET:                str = os.environ["R2_BUCKET"]
R2_PUBLIC_URL:            str = os.environ["R2_PUBLIC_URL"]
