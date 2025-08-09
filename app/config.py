
import os
from cryptography.fernet import Fernet
from dotenv import load_dotenv

# Загружаем переменные из .env файла
load_dotenv()

# Настройки приложения
DATABASE_URL = "sqlite:///./telegram_sender.db"

# Генерируем правильные ключи если они не указаны
def get_or_generate_key(env_name: str) -> bytes:
    key_str = os.getenv(env_name)
    if key_str:
        try:
            # Пробуем использовать как base64 ключ
            from base64 import urlsafe_b64decode
            key = urlsafe_b64decode(key_str + '==')  # Добавляем padding если нужно
            if len(key) == 32:
                return key_str.encode()
            else:
                print(f"Warning: {env_name} is not 32 bytes, generating new key")
                return Fernet.generate_key()
        except:
            print(f"Warning: Invalid {env_name}, generating new key")
            return Fernet.generate_key()
    else:
        return Fernet.generate_key()

SECRET_KEY = os.getenv("SECRET_KEY", "your_secret_key_here")
ENCRYPTION_KEY = get_or_generate_key("ENCRYPTION_KEY")

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
