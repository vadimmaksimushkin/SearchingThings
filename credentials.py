import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

GOOGLE_MAPS_API_KEY:      str = os.environ.get("GOOGLE_MAPS_API_KEY", "")
PLACES_DB_URL:            str = os.environ.get("PLACES_DB_URL", "")
QUEUE_DB_URL:             str = os.environ.get("QUEUE_DB_URL", "")
IMAGE_QUEUE_DB_URL:       str = os.environ.get("IMAGE_QUEUE_DB_URL", "")
R2_USER_TOKEN:            str = os.environ.get("R2_USER_TOKEN", "")
R2_ACCOUNT_ID:            str = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY:            str = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_ACCESS_KEY:     str = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET:                str = os.environ.get("R2_BUCKET", "")
R2_PAGES_BUCKET:          str = os.environ.get("R2_PAGES_BUCKET", "")
R2_PUBLIC_URL:            str = os.environ.get("R2_PUBLIC_URL", "")
