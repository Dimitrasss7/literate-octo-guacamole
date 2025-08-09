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

# Importing necessary components from telethon
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError, 
    FloodWaitError, 
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeEmptyError,
    PhoneNumberInvalidError
)
from telethon.sessions import StringSession

class TelegramManager:
    def __init__(self):
        self.clients: Dict[int, Client] = {}
        self.pending_clients: Dict[str, Client] = {}  # Для хранения клиентов ожидающих код
        self.cipher = Fernet(ENCRYPTION_KEY)

    def encrypt_session(self, session_data: str) -> str:
        return self.cipher.encrypt(session_data.encode()).decode()

    def decrypt_session(self, encrypted_data: str) -> str:
        return self.cipher.decrypt(encrypted_data.encode()).decode()

    async def add_account(self, phone: str, proxy: Optional[str] = None) -> Dict:
        """Добавление нового аккаунта"""
        try:
            session_name = f"session_{phone.replace('+', '').replace(' ', '').replace('(', '').replace(')', '').replace('-', '')}"
            session_path = os.path.join(SESSIONS_DIR, session_name)

            # Создаем клиент с теми же настройками
            client = Client(
                session_path,
                api_id=API_ID,
                api_hash=API_HASH,
                phone_number=phone,
                proxy=self._parse_proxy(proxy) if proxy else None,
                sleep_threshold=60,  # Увеличиваем время ожидания
                max_concurrent_transmissions=1
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
                try:
                    sent_code = await client.send_code(phone)
                    # Сохраняем клиент для использования при подтверждении кода
                    self.pending_clients[session_name] = client
                    return {
                        "status": "code_required",
                        "phone_code_hash": sent_code.phone_code_hash,
                        "session_name": session_name
                    }
                except Exception as send_error:
                    await client.disconnect()
                    return {"status": "error", "message": f"Ошибка отправки кода: {str(send_error)}"}

        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def verify_code(self, phone: str, code: str, phone_code_hash: str, session_name: str, proxy: Optional[str] = None):
        """Подтверждение кода из SMS"""
        try:
            proxy_dict = None
            if proxy:
                proxy_parts = proxy.split(':')
                if len(proxy_parts) >= 4:
                    proxy_dict = {
                        'proxy_type': 'socks5',
                        'addr': proxy_parts[0],
                        'port': int(proxy_parts[1]),
                        'username': proxy_parts[2],
                        'password': proxy_parts[3]
                    }

            client = TelegramClient(
                session_name,
                API_ID, # Use API_ID from config
                API_HASH, # Use API_HASH from config
                proxy=proxy_dict if proxy else None
            )

            await client.connect()

            # Проверяем код
            signed_in = await client.sign_in(phone, code, phone_code_hash=phone_code_hash)

            if signed_in:
                # Проверяем, требуется ли 2FA
                if hasattr(signed_in, 'password'):
                    await client.disconnect()
                    return {
                        "status": "password_required", 
                        "message": "Требуется пароль двухфакторной аутентификации",
                        "session_name": session_name
                    }

                # Успешная авторизация
                await client.disconnect()

                # Сохраняем аккаунт в базе данных
                # Calling the correct method: _save_account
                session_path = os.path.join(SESSIONS_DIR, session_name)
                await self._save_account(phone, session_path, signed_in.user.first_name, proxy)

                return {"status": "success", "name": signed_in.user.first_name}
            else:
                await client.disconnect()
                return {"status": "error", "message": "Неверный код"}

        except PhoneCodeInvalidError:
            return {"status": "error", "message": "Неверный код подтверждения"}
        except PhoneCodeExpiredError:
            return {"status": "code_expired", "message": "Код истёк. Запросите новый код"}
        except PhoneCodeEmptyError:
            return {"status": "error", "message": "Пустой код подтверждения"}
        except SessionPasswordNeededError:
            return {
                "status": "password_required", 
                "message": "Требуется пароль двухфакторной аутентификации",
                "session_name": session_name
            }
        except FloodWaitError as e:
            return {"status": "error", "message": f"Слишком много попыток. Подождите {e.seconds} секунд"}
        except PhoneNumberInvalidError:
            return {"status": "error", "message": "Неверный номер телефона"}
        except Exception as e:
            error_msg = str(e)
            print(f"Ошибка при верификации кода: {error_msg}")

            # Логируем подробную ошибку
            with open("unknown_errors.txt", "a", encoding="utf-8") as f:
                f.write(f"Verify code error: {error_msg}\n")
                f.write(f"Phone: {phone}\n")
                f.write(f"Code: {code[:2]}***\n")
                f.write(f"Exception type: {type(e).__name__}\n")
                f.write("---\n")

            return {"status": "error", "message": f"Ошибка при подтверждении кода: {error_msg}"}


    async def verify_password(self, phone: str, password: str, session_name: str, proxy: Optional[str] = None) -> Dict:
        """Подтверждение двухфакторной аутентификации"""
        try:
            # Используем существующий клиент из pending_clients
            client = self.pending_clients.get(session_name)

            if not client:
                # Если клиента нет, создаем новый
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
            session_path = os.path.join(SESSIONS_DIR, session_name)
            await self._save_account(phone, session_path, me.first_name, proxy)
            await client.disconnect()

            # Удаляем из pending_clients
            if session_name in self.pending_clients:
                del self.pending_clients[session_name]

            return {"status": "success", "name": me.first_name}

        except Exception as e:
            # Удаляем из pending_clients при ошибке
            if session_name in self.pending_clients:
                del self.pending_clients[session_name]
            return {"status": "error", "message": str(e)}

    async def _save_account(self, phone: str, session_path: str, name: str, proxy: Optional[str]):
        """Сохранение аккаунта в базу данных"""
        db = next(get_db())
        try:
            session_file_path = f"{session_path}.session"

            if not os.path.exists(session_file_path):
                raise Exception(f"Session file not found: {session_file_path}")

            # Читаем файл сессии
            with open(session_file_path, "rb") as f:
                session_data = f.read()

            print(f"Read session data, size: {len(session_data)} bytes")

            # Пробуем зашифровать сессию
            try:
                encrypted_session = self.cipher.encrypt(session_data).decode()
                print("Session encrypted successfully")
            except Exception as encrypt_error:
                print(f"Failed to encrypt session: {str(encrypt_error)}")
                # Сохраняем как base64 если шифрование не работает
                import base64
                encrypted_session = base64.b64encode(session_data).decode()
                print("Session saved as base64")

            # Проверяем, есть ли уже аккаунт с таким номером
            existing_account = db.query(Account).filter(Account.phone == phone).first()
            if existing_account:
                # Обновляем существующий
                existing_account.name = name
                existing_account.session_data = encrypted_session
                existing_account.proxy = proxy
                existing_account.status = "online"
                existing_account.is_active = True
                print(f"Updated existing account: {phone}")
            else:
                # Создаем новый
                account = Account(
                    phone=phone,
                    name=name,
                    session_data=encrypted_session,
                    proxy=proxy,
                    status="online"
                )
                db.add(account)
                print(f"Added new account: {phone}")

            db.commit()
            print("Account saved to database successfully")

        except Exception as save_error:
            print(f"Error saving account: {str(save_error)}")
            db.rollback()
            raise save_error
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
            client = self.clients[account_id]
            try:
                # Проверяем, что клиент все еще подключен
                await client.get_me()
                return client
            except:
                # Если клиент не работает, удаляем его
                del self.clients[account_id]

        db = next(get_db())
        account = None
        try:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account or not account.is_active:
                print(f"Account {account_id} not found or not active")
                return None

            print(f"Creating client for account {account_id}")

            try:
                # Пробуем расшифровать сессию
                try:
                    session_data = self.cipher.decrypt(account.session_data.encode())
                    print(f"Session data decrypted successfully, size: {len(session_data)} bytes")
                except Exception as decrypt_error:
                    print(f"Failed to decrypt session with current cipher: {str(decrypt_error)}")
                    # Пробуем использовать данные как есть (если они не зашифрованы)
                    try:
                        import base64
                        session_data = base64.b64decode(account.session_data.encode())
                        print(f"Session data decoded from base64, size: {len(session_data)} bytes")
                    except:
                        # Если это текстовые данные, конвертируем в байты
                        session_data = account.session_data.encode()
                        print(f"Using session data as text bytes, size: {len(session_data)} bytes")

                # Создаем временный файл сессии
                session_name = f"temp_session_{account_id}"
                session_path = os.path.join(SESSIONS_DIR, session_name)

                with open(f"{session_path}.session", "wb") as f:
                    f.write(session_data)

                print(f"Session file created: {session_path}.session")

            except Exception as session_error:
                print(f"Failed to create session file: {str(session_error)}")
                # Пробуем использовать оригинальные файлы сессий
                original_session_file = None
                phone_clean = account.phone.replace('+', '').replace(' ', '').replace('(', '').replace(')', '').replace('-', '')

                for file in os.listdir(SESSIONS_DIR):
                    if file.startswith(f"session_{phone_clean}") and file.endswith('.session'):
                        original_session_file = os.path.join(SESSIONS_DIR, file)
                        break

                if original_session_file and os.path.exists(original_session_file):
                    print(f"Using original session file: {original_session_file}")
                    session_name = f"temp_session_{account_id}"
                    session_path = os.path.join(SESSIONS_DIR, session_name)

                    import shutil
                    shutil.copy2(original_session_file, f"{session_path}.session")
                    print(f"Copied original session to: {session_path}.session")
                else:
                    raise Exception(f"Не удалось создать файл сессии и не найден оригинальный файл")

            # Создаем клиент
            client = Client(
                session_path,
                api_id=API_ID,
                api_hash=API_HASH,
                proxy=self._parse_proxy(account.proxy) if account.proxy else None,
                sleep_threshold=60
            )

            try:
                await client.start()
                print(f"Client started for account {account_id}")

                # Проверяем, что клиент работает
                me = await client.get_me()
                print(f"Client verified for {me.first_name}")

                self.clients[account_id] = client

                # Обновляем статус
                account.status = "online"
                account.last_activity = datetime.utcnow()
                db.commit()

                return client

            except Exception as start_error:
                print(f"Error starting client: {str(start_error)}")
                # Пробуем удалить временный файл сессии и создать заново
                try:
                    temp_session_file = f"{session_path}.session"
                    if os.path.exists(temp_session_file):
                        os.remove(temp_session_file)
                    # Создаем заново из исходной сессии
                    original_session_file = None
                    for file in os.listdir(SESSIONS_DIR):
                        if file.startswith(f"session_{account.phone.replace('+', '').replace(' ', '').replace('(', '').replace(')', '').replace('-', '')}"):
                            original_session_file = os.path.join(SESSIONS_DIR, file)
                            break

                    if original_session_file and os.path.exists(original_session_file):
                        print(f"Trying to use original session file: {original_session_file}")
                        import shutil
                        shutil.copy2(original_session_file, temp_session_file)
                        await client.start()
                        me = await client.get_me()
                        print(f"Client started with original session for {me.first_name}")
                        self.clients[account_id] = client
                        account.status = "online"
                        account.last_activity = datetime.utcnow()
                        db.commit()
                        return client
                except Exception as fallback_error:
                    print(f"Fallback also failed: {str(fallback_error)}")

                raise start_error

        except Exception as e:
            print(f"Error getting client for account {account_id}: {str(e)}")
            if account:
                account.status = "error"
                db.commit()
            return None
        finally:
            db.close()

    async def get_user_contacts(self, account_id: int) -> Dict:
        """Получение всех контактов пользователя"""
        try:
            client = await self.get_client(account_id)
            if not client:
                return {"status": "error", "message": "Не удалось подключиться к аккаунту"}

            contacts = []
            
            try:
                # Получаем контакты через метод get_contacts
                async for contact in client.get_contacts():
                    contact_info = {
                        "id": contact.id,
                        "username": contact.username,
                        "first_name": contact.first_name,
                        "last_name": contact.last_name,
                        "phone": getattr(contact, 'phone_number', None)
                    }
                    contacts.append(contact_info)
                    
                print(f"Found {len(contacts)} contacts for account {account_id}")
                
            except Exception as contacts_error:
                print(f"Error getting contacts via get_contacts: {contacts_error}")
                # Fallback: получаем приватные диалоги
                async for dialog in client.get_dialogs():
                    if dialog.chat.type in ["private"] and not dialog.chat.is_self:
                        contact_info = {
                            "id": dialog.chat.id,
                            "username": dialog.chat.username,
                            "first_name": dialog.chat.first_name,
                            "last_name": dialog.chat.last_name,
                            "phone": getattr(dialog.chat, 'phone_number', None)
                        }
                        contacts.append(contact_info)
                        
                print(f"Found {len(contacts)} private dialogs for account {account_id}")

            return {"status": "success", "contacts": contacts}

        except Exception as e:
            print(f"Error getting contacts: {str(e)}")
            return {"status": "error", "message": str(e)}

    async def get_user_chats(self, account_id: int) -> Dict:
        """Получение всех чатов и каналов пользователя"""
        try:
            client = await self.get_client(account_id)
            if not client:
                return {"status": "error", "message": "Не удалось подключиться к аккаунту"}

            chats = {"groups": [], "channels": [], "private": []}
            
            # Получаем все диалоги пользователя
            async for dialog in client.get_dialogs():
                chat_info = {
                    "id": dialog.chat.id,
                    "title": dialog.chat.title or f"{dialog.chat.first_name or ''} {dialog.chat.last_name or ''}".strip(),
                    "username": dialog.chat.username,
                    "type": dialog.chat.type
                }
                
                if dialog.chat.type == "private":
                    chats["private"].append(chat_info)
                elif dialog.chat.type == "group" or dialog.chat.type == "supergroup":
                    chats["groups"].append(chat_info)
                elif dialog.chat.type == "channel":
                    chats["channels"].append(chat_info)

            return {"status": "success", "chats": chats}

        except Exception as e:
            print(f"Error getting chats: {str(e)}")
            return {"status": "error", "message": str(e)}

    async def send_message(self, account_id: int, chat_id: str, message: str, file_path: Optional[str] = None) -> Dict:
        """Отправка сообщения"""
        try:
            print(f"Attempting to send message to {chat_id} from account {account_id}")

            client = await self.get_client(account_id)
            if not client:
                print(f"Failed to get client for account {account_id}")
                return {"status": "error", "message": "Не удалось подключиться к аккаунту"}

            # Проверяем лимиты
            db = next(get_db())
            try:
                account = db.query(Account).filter(Account.id == account_id).first()
                if not account:
                    return {"status": "error", "message": "Аккаунт не найден"}

                now = datetime.utcnow()
                if account.last_message_time and (now - account.last_message_time).seconds < 1:
                    await asyncio.sleep(1)

                # Очищаем chat_id от лишних символов
                clean_chat_id = chat_id.strip()

                # Определяем тип чата и подготавливаем target_chat
                if clean_chat_id.startswith('+'):
                    # Приватная ссылка - используем invite link напрямую
                    try:
                        # Сначала присоединяемся к чату по ссылке
                        invite_link = f"https://t.me/{clean_chat_id}"
                        print(f"Joining chat via invite link: {invite_link}")
                        chat_info = await client.join_chat(invite_link)
                        target_chat = chat_info.id
                        print(f"Joined chat, ID: {target_chat}")
                    except Exception as join_error:
                        print(f"Failed to join chat: {join_error}")
                        # Пробуем использовать ссылку напрямую
                        target_chat = clean_chat_id
                elif clean_chat_id.startswith('@'):
                    target_chat = clean_chat_id  # оставляем @ для групп/каналов
                elif clean_chat_id.isdigit() or clean_chat_id.startswith('-'):
                    # Это ID чата
                    target_chat = int(clean_chat_id)
                else:
                    # Обычный username без @
                    target_chat = f"@{clean_chat_id}"

                print(f"Sending message to target_chat: {target_chat}")

                # Отправляем сообщение
                try:
                    if file_path and os.path.exists(file_path):
                        result = await client.send_document(target_chat, file_path, caption=message)
                    else:
                        result = await client.send_message(target_chat, message)
                except Exception as send_error:
                    # Если не удалось отправить, пробуем альтернативные методы
                    print(f"First attempt failed: {send_error}")

                    if clean_chat_id.startswith('+'):
                        # Для приватных ссылок пробуем другой подход
                        try:
                            # Получаем информацию о чате
                            chat = await client.get_chat(f"https://t.me/{clean_chat_id}")
                            if file_path and os.path.exists(file_path):
                                result = await client.send_document(chat.id, file_path, caption=message)
                            else:
                                result = await client.send_message(chat.id, message)
                        except Exception as alt_error:
                            raise send_error
                    else:
                        raise send_error

                print(f"Message sent successfully: {result}")

                # Обновляем статистику
                account.messages_sent_today += 1
                account.messages_sent_hour += 1
                account.last_message_time = now
                db.commit()

                return {"status": "success", "message_id": result.id if hasattr(result, 'id') else None}

            finally:
                db.close()

        except FloodWait as e:
            print(f"FloodWait error: {e.value} seconds")
            await asyncio.sleep(e.value)
            return {"status": "flood_wait", "seconds": e.value}
        except Exception as e:
            print(f"Error sending message: {str(e)}")
            return {"status": "error", "message": str(e)}

# Глобальный экземпляр менеджера
telegram_manager = TelegramManager()