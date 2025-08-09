
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

            # Создаем клиент с настройками для избежания проблем с базой данных
            client = Client(
                session_path,
                api_id=API_ID,
                api_hash=API_HASH,
                phone_number=phone,
                proxy=self._parse_proxy(proxy) if proxy else None,
                sleep_threshold=60,
                max_concurrent_transmissions=1,
                in_memory=True  # Используем in-memory storage
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
            # Используем существующий клиент если есть
            client = self.pending_clients.get(session_name)
            
            if not client:
                # Создаем новый клиент если нет существующего
                session_path = os.path.join(SESSIONS_DIR, session_name)
                client = Client(
                    session_path,
                    api_id=API_ID,
                    api_hash=API_HASH,
                    phone_number=phone,
                    proxy=self._parse_proxy(proxy) if proxy else None
                )
                await client.connect()

            # Подтверждаем код
            await client.sign_in(phone, phone_code_hash, code)

            # Получаем информацию о пользователе
            me = await client.get_me()
            
            # Сохраняем аккаунт
            session_path = os.path.join(SESSIONS_DIR, session_name)
            await self._save_account(phone, session_path, me.first_name, proxy)
            
            await client.disconnect()

            # Удаляем из pending_clients
            if session_name in self.pending_clients:
                del self.pending_clients[session_name]

            return {"status": "success", "name": me.first_name}

        except Exception as e:
            error_msg = str(e).lower()
            print(f"Ошибка при верификации кода: {str(e)}")

            # Обработка различных типов ошибок
            if "phone_code_invalid" in error_msg or "invalid code" in error_msg:
                return {"status": "error", "message": "Неверный код подтверждения"}
            elif "phone_code_expired" in error_msg or "expired" in error_msg:
                return {"status": "error", "message": "Код истёк. Запросите новый код через форму добавления аккаунта"}
            elif "phone_code_empty" in error_msg or "empty" in error_msg:
                return {"status": "error", "message": "Код не может быть пустым"}
            elif "password" in error_msg or "2fa" in error_msg:
                return {
                    "status": "password_required", 
                    "message": "Требуется пароль двухфакторной аутентификации",
                    "session_name": session_name
                }
            elif "flood" in error_msg:
                return {"status": "error", "message": "Слишком много попыток. Попробуйте позже"}
            else:
                return {"status": "error", "message": f"Ошибка при подтверждении: {str(e)}"}


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
        """Получение клиента для аккаунта - исправленная версия"""
        # Сначала проверяем кэш
        if account_id in self.clients:
            client = self.clients[account_id]
            try:
                # Быстрая проверка что клиент работает
                if hasattr(client, 'is_connected') and client.is_connected:
                    return client
            except:
                pass
            # Удаляем неработающий клиент из кэша
            try:
                await client.stop()
            except:
                pass
            del self.clients[account_id]

        db = next(get_db())
        account = None
        try:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account or not account.is_active:
                print(f"Account {account_id} not found or inactive")
                return None

            # Поиск файла сессии - проверяем все возможные варианты
            phone_clean = account.phone.replace('+', '').replace(' ', '').replace('(', '').replace(')', '').replace('-', '')
            session_file = None
            
            # Список всех возможных имен файлов сессий
            possible_names = [
                f"session_{phone_clean}.session",
                f"session_{account.phone}.session", 
                f"{account.phone}.session",
                f"{phone_clean}.session"
            ]
            
            print(f"Looking for session file for account {account_id}, phone: {account.phone}")
            print(f"Checking files: {possible_names}")
            
            for name in possible_names:
                path = os.path.join(SESSIONS_DIR, name)
                print(f"Checking: {path}")
                if os.path.exists(path):
                    session_file = path
                    print(f"Found session file: {path}")
                    break
            
            if not session_file:
                print(f"No session file found for account {account_id}")
                # Показываем что есть в папке
                session_files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.session')]
                print(f"Available session files: {session_files}")
                return None

            # Убираем .session из пути для Pyrogram
            session_name = session_file.replace('.session', '')
            
            print(f"Creating client with session: {session_name}")
            
            # Создаем клиент с правильными настройками
            try:
                client = Client(
                    name=session_name,
                    api_id=API_ID,
                    api_hash=API_HASH,
                    proxy=self._parse_proxy(account.proxy) if account.proxy else None,
                    no_updates=True,
                    takeout=False,
                    workdir=SESSIONS_DIR  # Указываем рабочую папку
                )

                print(f"Starting client for account {account_id}...")
                
                # Подключаемся без интерактивного ввода
                await client.connect()
                
                # Проверяем авторизацию
                try:
                    me = await client.get_me()
                    print(f"Client authorized as: {me.first_name} (ID: {me.id})")
                    
                    # Кэшируем клиент
                    self.clients[account_id] = client
                    
                    # Обновляем статус
                    account.status = "online"
                    account.last_activity = datetime.utcnow()
                    db.commit()
                    
                    return client
                    
                except Exception as auth_error:
                    print(f"Client not authorized: {auth_error}")
                    await client.disconnect()
                    return None
                    
            except Exception as client_error:
                print(f"Error creating client: {client_error}")
                return None

        except Exception as e:
            print(f"Error in get_client for account {account_id}: {str(e)}")
            if account:
                account.status = "error"
                try:
                    db.commit()
                except:
                    pass
            return None
        finally:
            try:
                db.close()
            except:
                pass

    async def get_user_contacts(self, account_id: int) -> Dict:
        """Быстрое получение контактов пользователя с тайм-аутом"""
        try:
            print(f"=== Быстрая загрузка контактов для аккаунта {account_id} ===")
            
            # Устанавливаем тайм-аут на получение клиента
            try:
                client = await asyncio.wait_for(self.get_client(account_id), timeout=10.0)
            except asyncio.TimeoutError:
                print(f"Тайм-аут при подключении к аккаунту {account_id}")
                return {"status": "error", "message": "Тайм-аут подключения к аккаунту"}
            
            if not client:
                return {"status": "error", "message": "Не удалось подключиться к аккаунту"}

            contacts = []
            
            try:
                # Получаем информацию о себе с тайм-аутом
                me = await asyncio.wait_for(client.get_me(), timeout=5.0)
                print(f"Пользователь: {me.first_name} (ID: {me.id})")
                
                # Быстро получаем только первые 15 диалогов с тайм-аутом
                dialog_count = 0
                print("Быстро сканируем диалоги...")
                
                try:
                    async with asyncio.timeout(15.0):  # Общий тайм-аут на получение диалогов
                        async for dialog in client.get_dialogs(limit=15):
                            try:
                                dialog_count += 1
                                chat = dialog.chat
                                
                                # Проверяем только приватные чаты (не группы/каналы)
                                if (hasattr(chat, 'type') and 
                                    str(chat.type) == 'ChatType.PRIVATE' and
                                    chat.id != me.id and chat.id != 777000 and
                                    not getattr(chat, 'is_bot', False) and
                                    not getattr(chat, 'is_deleted', False)):
                                    
                                    # Быстро формируем данные контакта
                                    first_name = getattr(chat, 'first_name', '') or ''
                                    last_name = getattr(chat, 'last_name', '') or ''
                                    username = getattr(chat, 'username', '') or ''
                                    
                                    # Формируем имя для отображения
                                    if first_name or last_name:
                                        display_name = f"{first_name} {last_name}".strip()
                                    elif username:
                                        display_name = f"@{username}"
                                    else:
                                        display_name = f"User {chat.id}"
                                    
                                    contact_data = {
                                        "id": chat.id,
                                        "first_name": first_name,
                                        "last_name": last_name, 
                                        "username": username,
                                        "phone": getattr(chat, 'phone_number', '') or '',
                                        "display_name": display_name
                                    }
                                    
                                    contacts.append(contact_data)
                                    
                                    # Прерываем если нашли достаточно контактов
                                    if len(contacts) >= 10:
                                        break
                                
                            except Exception as dialog_error:
                                print(f"Пропуск диалога: {dialog_error}")
                                continue
                except asyncio.TimeoutError:
                    print("Тайм-аут при получении диалогов")

                print(f"✓ Найдено {len(contacts)} контактов из {dialog_count} диалогов")
                
                return {
                    "status": "success", 
                    "contacts": contacts,
                    "total_found": len(contacts)
                }
                
            except asyncio.TimeoutError:
                print("Тайм-аут при получении информации о пользователе")
                return {"status": "error", "message": "Тайм-аут получения данных пользователя"}
            except Exception as get_error:
                print(f"Ошибка получения диалогов: {get_error}")
                return {"status": "error", "message": f"Ошибка получения диалогов: {str(get_error)}"}

        except Exception as e:
            print(f"Ошибка получения контактов: {str(e)}")
            return {"status": "error", "message": str(e)}

    async def get_user_chats(self, account_id: int) -> Dict:
        """Быстрое получение чатов и каналов с тайм-аутом"""
        try:
            print(f"=== Быстрая загрузка чатов для аккаунта {account_id} ===")
            
            # Получаем клиент с тайм-аутом
            try:
                client = await asyncio.wait_for(self.get_client(account_id), timeout=10.0)
            except asyncio.TimeoutError:
                return {"status": "error", "message": "Тайм-аут подключения к аккаунту"}
            
            if not client:
                return {"status": "error", "message": "Не удалось подключиться к аккаунту"}

            chats = {"groups": [], "channels": [], "private": []}
            
            try:
                # Быстро получаем только первые 15 диалогов с тайм-аутом
                dialog_count = 0
                
                try:
                    async with asyncio.timeout(15.0):  # Общий тайм-аут на получение диалогов
                        async for dialog in client.get_dialogs(limit=15):
                            try:
                                dialog_count += 1
                                chat = dialog.chat
                                
                                if hasattr(chat, 'type'):
                                    chat_type = str(chat.type).replace('ChatType.', '')
                                    
                                    # Формируем название чата
                                    if hasattr(chat, 'title') and chat.title:
                                        title = chat.title
                                    else:
                                        first_name = getattr(chat, 'first_name', '') or ''
                                        last_name = getattr(chat, 'last_name', '') or ''
                                        title = f"{first_name} {last_name}".strip() or f"Chat {chat.id}"
                                    
                                    chat_info = {
                                        "id": chat.id,
                                        "title": title,
                                        "username": getattr(chat, 'username', '') or '',
                                        "type": chat_type
                                    }
                                    
                                    # Распределяем по типам
                                    if chat_type == 'PRIVATE':
                                        chats["private"].append(chat_info)
                                    elif chat_type in ['GROUP', 'SUPERGROUP']:
                                        chats["groups"].append(chat_info)
                                    elif chat_type == 'CHANNEL':
                                        chats["channels"].append(chat_info)
                                        
                            except Exception as chat_error:
                                continue
                except asyncio.TimeoutError:
                    print("Тайм-аут при получении диалогов")

                total_chats = len(chats['private']) + len(chats['groups']) + len(chats['channels'])
                print(f"✓ Найдено чатов: {total_chats} (групп: {len(chats['groups'])}, каналов: {len(chats['channels'])})")
                
                return {"status": "success", "chats": chats}
                
            except Exception as chats_error:
                print(f"Ошибка получения чатов: {chats_error}")
                return {"status": "error", "message": f"Ошибка получения чатов: {str(chats_error)}"}

        except Exception as e:
            print(f"Ошибка получения чатов: {str(e)}")
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
