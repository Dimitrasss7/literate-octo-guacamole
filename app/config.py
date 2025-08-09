
import os
from cryptography.fernet import Fernet

# Настройки приложения
DATABASE_URL = "sqlite:///./telegram_sender.db"
SECRET_KEY = os.getenv("SECRET_KEY", Fernet.generate_key().decode())
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", Fernet.generate_key())

# Настройки Telegram API
API_ID = os.getenv("API_ID", "")
API_HASH = os.getenv("API_HASH", "")

# Настройки рассылки
DEFAULT_DELAY_SECONDS = 3
MAX_MESSAGES_PER_HOUR = 30
MAX_MESSAGES_PER_DAY = 200

# Папки для хранения данных
SESSIONS_DIR = "sessions"
UPLOADS_DIR = "uploads"
LOGS_DIR = "logs"

# Создаем необходимые папки
for directory in [SESSIONS_DIR, UPLOADS_DIR, LOGS_DIR]:
    os.makedirs(directory, exist_ok=True)
