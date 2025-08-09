
import asyncio
import os
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from pyrogram import Client
from pyrogram.errors import FloodWait, SessionPasswordNeeded, PhoneCodeInvalid
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session
from app.database import Account, Campaign, SendLog, get_db
from app.config import API_ID, API_HASH, SESSIONS_DIR, ENCRYPTION_KEY

class TelegramManager:
    def __init__(self):
        self.clients: Dict[int, Client] = {}
        self.cipher = Fernet(ENCRYPTION_KEY)
    
    def encrypt_session(self, session_data: str) -> str:
        return self.cipher.encrypt(session_data.encode()).decode()
    
    def decrypt_session(self, encrypted_data: str) -> str:
        return self.cipher.decrypt(encrypted_data.encode()).decode()
    
    async def add_account(self, phone: str, proxy: Optional[str] = None) -> Dict:
        """Добавление нового аккаунта"""
        try:
            session_name = f"session_{phone.replace('+', '')}"
            session_path = os.path.join(SESSIONS_DIR, session_name)
            
            # Создаем клиент
            client = Client(
                session_path,
                api_id=API_ID,
                api_hash=API_HASH,
                phone_number=phone,
                proxy=self._parse_proxy(proxy) if proxy else None
            )
            
            # Подключаемся и авторизуемся
            await client.connect()
            
            try:
                # Проверяем авторизацию через получение информации о себе
                me = await client.get_me()
                # Если дошли до сюда - уже авторизован
                await self._save_account(phone, session_path, me.first_name, proxy)
                await client.disconnect()
                return {"status": "success", "name": me.first_name}
            except:
                # Не авторизован - отправляем код
                sent_code = await client.send_code(phone)
                return {
                    "status": "code_required",
                    "phone_code_hash": sent_code.phone_code_hash,
                    "session_name": session_name
                }
                
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    async def verify_code(self, phone: str, code: str, phone_code_hash: str, session_name: str, proxy: Optional[str] = None) -> Dict:
        """Подтверждение кода авторизации"""
        try:
            session_path = os.path.join(SESSIONS_DIR, session_name)
            
            client = Client(
                session_path,
                api_id=API_ID,
                api_hash=API_HASH,
                phone_number=phone,
                proxy=self._parse_proxy(proxy) if proxy else None
            )
            
            await client.connect()
            
            try:
                await client.sign_in(phone, phone_code_hash, code)
            except SessionPasswordNeeded:
                await client.disconnect()
                return {"status": "password_required", "session_name": session_name}
            
            me = await client.get_me()
            await self._save_account(phone, session_path, me.first_name, proxy)
            await client.disconnect()
            
            return {"status": "success", "name": me.first_name}
            
        except PhoneCodeInvalid:
            return {"status": "error", "message": "Неверный код"}
        except Exception as e:
            error_message = str(e)
            # Проверяем на истечение кода
            if "PHONE_CODE_EXPIRED" in error_message:
                return {"status": "code_expired", "message": "Код подтверждения истек. Запросите новый код."}
            elif "PHONE_CODE_INVALID" in error_message:
                return {"status": "error", "message": "Неверный код подтверждения"}
            else:
                return {"status": "error", "message": error_message}
    
    async def verify_password(self, phone: str, password: str, session_name: str, proxy: Optional[str] = None) -> Dict:
        """Подтверждение двухфакторной аутентификации"""
        try:
            session_path = os.path.join(SESSIONS_DIR, session_name)
            
            client = Client(
                session_path,
                api_id=API_ID,
                api_hash=API_HASH,
                phone_number=phone,
                proxy=self._parse_proxy(proxy) if proxy else None
            )
            
            await client.connect()
            await client.check_password(password)
            
            me = await client.get_me()
            await self._save_account(phone, session_path, me.first_name, proxy)
            await client.disconnect()
            
            return {"status": "success", "name": me.first_name}
            
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    async def _save_account(self, phone: str, session_path: str, name: str, proxy: Optional[str]):
        """Сохранение аккаунта в базу данных"""
        db = next(get_db())
        try:
            # Читаем и шифруем файл сессии
            with open(f"{session_path}.session", "rb") as f:
                session_data = f.read()
            
            encrypted_session = self.cipher.encrypt(session_data).decode()
            
            account = Account(
                phone=phone,
                name=name,
                session_data=encrypted_session,
                proxy=proxy,
                status="online"
            )
            
            db.add(account)
            db.commit()
        finally:
            db.close()
    
    def _parse_proxy(self, proxy_string: str) -> Dict:
        """Парсинг строки прокси"""
        if not proxy_string:
            return None
        
        # Формат: type://user:pass@host:port
        parts = proxy_string.split("://")
        if len(parts) != 2:
            return None
        
        scheme = parts[0].lower()
        rest = parts[1]
        
        # Разбираем user:pass@host:port
        if "@" in rest:
            auth, address = rest.split("@", 1)
            username, password = auth.split(":", 1)
        else:
            username = password = None
            address = rest
        
        host, port = address.split(":", 1)
        
        return {
            "scheme": scheme,
            "hostname": host,
            "port": int(port),
            "username": username,
            "password": password
        }
    
    async def get_client(self, account_id: int) -> Optional[Client]:
        """Получение клиента для аккаунта"""
        if account_id in self.clients:
            return self.clients[account_id]
        
        db = next(get_db())
        try:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account or not account.is_active:
                return None
            
            # Расшифровываем сессию
            session_data = self.cipher.decrypt(account.session_data.encode())
            
            # Создаем временный файл сессии
            session_name = f"temp_session_{account_id}"
            session_path = os.path.join(SESSIONS_DIR, session_name)
            
            with open(f"{session_path}.session", "wb") as f:
                f.write(session_data)
            
            # Создаем клиент
            client = Client(
                session_path,
                api_id=API_ID,
                api_hash=API_HASH,
                proxy=self._parse_proxy(account.proxy) if account.proxy else None
            )
            
            await client.start()
            self.clients[account_id] = client
            
            # Обновляем статус
            account.status = "online"
            account.last_activity = datetime.utcnow()
            db.commit()
            
            return client
            
        except Exception as e:
            if account:
                account.status = "error"
                db.commit()
            return None
        finally:
            db.close()
    
    async def send_message(self, account_id: int, chat_id: str, message: str, file_path: Optional[str] = None) -> Dict:
        """Отправка сообщения"""
        try:
            client = await self.get_client(account_id)
            if not client:
                return {"status": "error", "message": "Не удалось подключиться к аккаунту"}
            
            # Проверяем лимиты
            db = next(get_db())
            account = db.query(Account).filter(Account.id == account_id).first()
            
            now = datetime.utcnow()
            if account.last_message_time and (now - account.last_message_time).seconds < 1:
                await asyncio.sleep(1)
            
            # Отправляем сообщение
            if file_path and os.path.exists(file_path):
                await client.send_document(chat_id, file_path, caption=message)
            else:
                await client.send_message(chat_id, message)
            
            # Обновляем статистику
            account.messages_sent_today += 1
            account.messages_sent_hour += 1
            account.last_message_time = now
            db.commit()
            db.close()
            
            return {"status": "success"}
            
        except FloodWait as e:
            await asyncio.sleep(e.value)
            return {"status": "flood_wait", "seconds": e.value}
        except Exception as e:
            return {"status": "error", "message": str(e)}

# Глобальный экземпляр менеджера
telegram_manager = TelegramManager()
