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
        self.pending_clients: Dict[str, Client] = {}
        self.cipher = Fernet(ENCRYPTION_KEY)
        self._cleanup_temp_sessions()

    def _cleanup_temp_sessions(self):
        """Очистка временных файлов сессий"""
        try:
            if not os.path.exists(SESSIONS_DIR):
                return
            for filename in os.listdir(SESSIONS_DIR):
                if filename.startswith('temp_client_') and filename.endswith('.session'):
                    temp_path = os.path.join(SESSIONS_DIR, filename)
                    try:
                        os.remove(temp_path)
                    except:
                        pass
        except Exception as e:
            print(f"Error cleaning temp sessions: {e}")

    def encrypt_session(self, session_data: str) -> str:
        return self.cipher.encrypt(session_data.encode()).decode()

    def decrypt_session(self, encrypted_data: str) -> str:
        return self.cipher.decrypt(encrypted_data.encode()).decode()

    async def add_account(self, phone: str, proxy: Optional[str] = None) -> Dict:
        """Добавление нового аккаунта"""
        try:
            # Очищаем номер телефона
            clean_phone = phone.replace('+', '').replace(' ', '').replace('(', '').replace(')', '').replace('-', '')
            session_name = f"session_{clean_phone}"
            session_path = os.path.join(SESSIONS_DIR, session_name)

            # Удаляем старую сессию если есть
            old_session_file = f"{session_path}.session"
            if os.path.exists(old_session_file):
                try:
                    os.remove(old_session_file)
                except:
                    pass

            client = Client(
                session_path,
                api_id=API_ID,
                api_hash=API_HASH,
                phone_number=phone,
                proxy=self._parse_proxy(proxy) if proxy else None,
                sleep_threshold=30,
                max_concurrent_transmissions=1,
                no_updates=True
            )

            await client.connect()

            try:
                me = await client.get_me()
                await self._save_account(phone, session_path, me.first_name, proxy)
                await client.disconnect()
                return {"status": "success", "name": me.first_name}
            except:
                try:
                    # Отправляем код с задержкой
                    await asyncio.sleep(1)
                    sent_code = await client.send_code(phone)
                    self.pending_clients[session_name] = client

                    print(f"Код отправлен на {phone}, hash: {sent_code.phone_code_hash}")

                    return {
                        "status": "code_required",
                        "phone_code_hash": sent_code.phone_code_hash,
                        "session_name": session_name
                    }
                except Exception as send_error:
                    await client.disconnect()
                    error_msg = str(send_error)
                    if "flood" in error_msg.lower():
                        return {"status": "error", "message": "Слишком много попыток. Попробуйте позже"}
                    return {"status": "error", "message": f"Ошибка отправки кода: {error_msg}"}

        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def verify_code(self, phone: str, code: str, phone_code_hash: str, session_name: str, proxy: Optional[str] = None):
        """Подтверждение кода из SMS"""
        try:
            # Очищаем код от лишних символов и пробелов
            clean_code = ''.join(filter(str.isdigit, code.strip()))

            if len(clean_code) != 5:
                return {"status": "error", "message": "Код должен содержать ровно 5 цифр"}

            client = self.pending_clients.get(session_name)

            if not client:
                session_path = os.path.join(SESSIONS_DIR, session_name)
                client = Client(
                    session_path,
                    api_id=API_ID,
                    api_hash=API_HASH,
                    proxy=self._parse_proxy(proxy) if proxy else None,
                    no_updates=True,
                    takeout=False
                )
                await client.connect()

            # Дополнительная задержка перед попыткой входа
            await asyncio.sleep(1)

            try:
                await client.sign_in(phone, phone_code_hash, clean_code)
            except Exception as sign_in_error:
                # Если первая попытка не удалась, попробуем еще раз через несколько секунд
                await asyncio.sleep(3)
                await client.sign_in(phone, phone_code_hash, clean_code)

            me = await client.get_me()

            session_path = os.path.join(SESSIONS_DIR, session_name)
            await self._save_account(phone, session_path, me.first_name, proxy)

            await client.disconnect()

            if session_name in self.pending_clients:
                del self.pending_clients[session_name]

            return {"status": "success", "name": me.first_name}

        except Exception as e:
            error_msg = str(e).lower()
            print(f"Ошибка при верификации кода: {str(e)}")

            if "phone_code_invalid" in error_msg or "invalid code" in error_msg:
                return {"status": "error", "message": "Неверный код или код истёк. Попробуйте запросить новый код"}
            elif "phone_code_expired" in error_msg or "expired" in error_msg:
                return {"status": "error", "message": "Код истёк. Запросите новый код через форму добавления аккаунта"}
            elif "phone_code_empty" in error_msg or "empty" in error_msg:
                return {"status": "error", "message": "Код не может быть пустым"}
            elif "session_password_needed" in error_msg or "password" in error_msg or "2fa" in error_msg:
                return {
                    "status": "password_required",
                    "message": "Требуется пароль двухфакторной аутентификации",
                    "session_name": session_name
                }
            elif "flood" in error_msg:
                return {"status": "error", "message": "Слишком много попыток. Попробуйте позже"}
            else:
                return {"status": "error", "message": f"Попробуйте запросить новый код. Детали: {str(e)}"}

    async def verify_password(self, phone: str, password: str, session_name: str, proxy: Optional[str] = None) -> Dict:
        """Подтверждение двухфакторной аутентификации"""
        try:
            client = self.pending_clients.get(session_name)

            if not client:
                session_path = os.path.join(SESSIONS_DIR, session_name)
                client = Client(
                    session_path,
                    api_id=API_ID,
                    api_hash=API_HASH,
                    proxy=self._parse_proxy(proxy) if proxy else None,
                    no_updates=True,
                    takeout=False
                )
                await client.connect()

            await client.check_password(password)
            me = await client.get_me()
            session_path = os.path.join(SESSIONS_DIR, session_name)
            await self._save_account(phone, session_path, me.first_name, proxy)
            await client.disconnect()

            if session_name in self.pending_clients:
                del self.pending_clients[session_name]

            return {"status": "success", "name": me.first_name}

        except Exception as e:
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

            with open(session_file_path, "rb") as f:
                session_data = f.read()

            try:
                encrypted_session = self.cipher.encrypt(session_data).decode()
            except Exception:
                import base64
                encrypted_session = base64.b64encode(session_data).decode()

            existing_account = db.query(Account).filter(Account.phone == phone).first()
            if existing_account:
                existing_account.name = name
                existing_account.session_data = encrypted_session
                existing_account.proxy = proxy
                existing_account.status = "online"
                existing_account.is_active = True
            else:
                account = Account(
                    phone=phone,
                    name=name,
                    session_data=encrypted_session,
                    proxy=proxy,
                    status="online"
                )
                db.add(account)

            db.commit()

        except Exception as save_error:
            db.rollback()
            raise save_error
        finally:
            db.close()

    def _parse_proxy(self, proxy_string: str) -> Dict:
        """Парсинг строки прокси"""
        if not proxy_string:
            return None

        parts = proxy_string.split("://")
        if len(parts) != 2:
            return None

        scheme = parts[0].lower()
        rest = parts[1]

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

    async def _get_client_for_account(self, account_id: int) -> Optional[Client]:
        """Получение или создание клиента для аккаунта"""
        if account_id in self.clients and self.clients[account_id].is_connected:
            return self.clients[account_id]

        # Получаем данные аккаунта
        db = next(get_db())
        try:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account or not account.is_active:
                print(f"Аккаунт {account_id} неактивен или не найден")
                return None

            # Ищем файл сессии
            phone_clean = account.phone.replace('+', '').replace(' ', '').replace('(', '').replace(')', '').replace('-', '')

            # Список возможных имен сессий
            possible_names = [
                f"session_{phone_clean}",
                f"session_{account.phone}",
                phone_clean
            ]

            session_file = None
            for name in possible_names:
                path = os.path.join(SESSIONS_DIR, f"{name}.session")
                if os.path.exists(path):
                    session_file = os.path.join(SESSIONS_DIR, name)
                    print(f"Найден файл сессии: {session_file}.session")
                    break

            if not session_file:
                print(f"Файл сессии не найден для аккаунта {account_id}, проверенные пути:")
                for name in possible_names:
                    print(f"  - {os.path.join(SESSIONS_DIR, name)}.session")
                return None

            # Создаем клиент
            client = Client(
                session_file,
                api_id=API_ID,
                api_hash=API_HASH,
                proxy=self._parse_proxy(account.proxy) if account.proxy else None,
                sleep_threshold=30,
                no_updates=True
            )

            # Проверяем подключение и авторизацию
            try:
                if not client.is_connected:
                    await client.connect()

                me = await client.get_me()
                print(f"✓ Клиент для аккаунта {account_id} успешно подключен: {me.first_name}")

                # Обновляем статус в БД
                account.status = "online"
                account.last_activity = datetime.utcnow()
                db.commit()

                self.clients[account_id] = client
                return client

            except Exception as auth_error:
                print(f"Ошибка авторизации клиента {account_id}: {auth_error}")
                try:
                    if client.is_connected:
                        await client.disconnect()
                except:
                    pass
                return None

        except Exception as e:
            print(f"Общая ошибка создания клиента для аккаунта {account_id}: {str(e)}")
            return None
        finally:
            db.close()

    async def get_user_contacts(self, account_id: int) -> Dict:
        """Получение контактов пользователя"""
        try:
            print(f"Получение контактов для аккаунта {account_id}")

            client = await self._get_client_for_account(account_id)
            if not client:
                return {"status": "error", "message": "Не удалось подключиться к аккаунту"}

            try:
                # Проверяем, что клиент подключен
                if not client.is_connected:
                    await client.connect()

                # Получаем контакты через Pyrogram
                contacts_list = []
                contacts = await client.get_contacts()

                for contact in contacts:
                    # Безопасное получение атрибутов
                    first_name = getattr(contact, 'first_name', '') or ""
                    last_name = getattr(contact, 'last_name', '') or ""
                    username = getattr(contact, 'username', '') or ""
                    
                    contact_data = {
                        "id": contact.id,
                        "first_name": first_name,
                        "last_name": last_name,
                        "username": username,
                        "phone": getattr(contact, 'phone_number', '') or "",
                        "is_bot": getattr(contact, 'is_bot', False),
                        "is_verified": getattr(contact, 'is_verified', False),
                        "is_premium": getattr(contact, 'is_premium', False),
                        "display_name": f"{first_name} {last_name}".strip() or username or f"User {contact.id}"
                    }
                    contacts_list.append(contact_data)

                print(f"Найдено {len(contacts_list)} контактов для аккаунта {account_id}")
                return {
                    "status": "success",
                    "contacts": contacts_list,
                    "count": len(contacts_list)
                }

            except Exception as e:
                print(f"Ошибка получения контактов: {e}")
                return {"status": "error", "message": f"Ошибка получения контактов: {str(e)}"}

        except Exception as e:
            print(f"Общая ошибка при получении контактов: {e}")
            return {"status": "error", "message": f"Не удалось получить контакты: {str(e)}"}

    async def get_user_dialogs(self, account_id: int) -> Dict:
        """Получение контактов из диалогов (старый метод)"""
        try:
            print(f"=== Получение диалогов для аккаунта {account_id} ===")

            client = await self._get_client_for_account(account_id)
            if not client:
                return {"status": "error", "message": "Не удалось подключиться к аккаунту"}

            contacts = []

            try:
                # Получаем информацию о себе
                me = await client.get_me()
                print(f"Получаем диалоги для: {me.first_name}")

                # Получаем диалоги с таймаутом
                dialog_count = 0
                async for dialog in client.get_dialogs(limit=50):
                    dialog_count += 1
                    chat = dialog.chat

                    # Пропускаем системные чаты и самого себя
                    if chat.id == me.id or chat.id == 777000:
                        continue

                    # Обрабатываем только приватные чаты
                    if hasattr(chat, 'type') and 'PRIVATE' in str(chat.type):
                        # Получаем данные контакта
                        first_name = getattr(chat, 'first_name', '') or ''
                        last_name = getattr(chat, 'last_name', '') or ''
                        username = getattr(chat, 'username', '') or ''

                        # Формируем имя для отображения
                        display_name = f"{first_name} {last_name}".strip()
                        if not display_name and username:
                            display_name = f"@{username}"
                        elif not display_name:
                            display_name = f"Пользователь {chat.id}"

                        contact_info = {
                            "id": chat.id,
                            "first_name": first_name,
                            "last_name": last_name,
                            "username": username,
                            "display_name": display_name
                        }

                        contacts.append(contact_info)
                        print(f"✓ Контакт: {display_name}")

                    # Ограничиваем количество для быстрой загрузки
                    if dialog_count >= 30:
                        break

                print(f"✓ Найдено {len(contacts)} контактов из {dialog_count} диалогов")

                # Закрываем клиент
                await client.disconnect()

                return {
                    "status": "success",
                    "contacts": contacts,
                    "total": len(contacts)
                }

            except Exception as e:
                print(f"Ошибка получения диалогов: {str(e)}")
                await client.disconnect()
                return {"status": "error", "message": f"Ошибка получения диалогов: {str(e)}"}

        except Exception as e:
            print(f"Общая ошибка получения контактов: {str(e)}")
            return {"status": "error", "message": str(e)}

    async def get_user_chats(self, account_id: int) -> Dict:
        """Получение чатов и каналов"""
        try:
            print(f"=== Получение чатов для аккаунта {account_id} ===")

            client = await self._get_client_for_account(account_id)
            if not client:
                return {"status": "error", "message": "Не удалось подключиться к аккаунту"}

            chats = {"groups": [], "channels": [], "private": []}

            try:
                dialog_count = 0
                async for dialog in client.get_dialogs(limit=30):
                    dialog_count += 1
                    chat = dialog.chat

                    if hasattr(chat, 'type'):
                        chat_type = str(chat.type)

                        # Получаем название
                        if hasattr(chat, 'title'):
                            title = chat.title
                        else:
                            first_name = getattr(chat, 'first_name', '') or ''
                            last_name = getattr(chat, 'last_name', '') or ''
                            title = f"{first_name} {last_name}".strip() or f"Chat {chat.id}"

                        chat_data = {
                            "id": chat.id,
                            "title": title,
                            "username": getattr(chat, 'username', '') or ''
                        }

                        # Распределяем по типам
                        if 'PRIVATE' in chat_type:
                            chats["private"].append(chat_data)
                        elif 'GROUP' in chat_type:
                            chats["groups"].append(chat_data)
                        elif 'CHANNEL' in chat_type:
                            chats["channels"].append(chat_data)

                print(f"✓ Найдено: {len(chats['private'])} приватных, {len(chats['groups'])} групп, {len(chats['channels'])} каналов")

                # Закрываем клиент
                await client.disconnect()

                return {"status": "success", "chats": chats}

            except Exception as e:
                print(f"Ошибка получения чатов: {str(e)}")
                await client.disconnect()
                return {"status": "error", "message": str(e)}

        except Exception as e:
            print(f"Общая ошибка получения чатов: {str(e)}")
            return {"status": "error", "message": str(e)}

    async def cleanup_client(self, account_id: int):
        """Очистка клиента"""
        if account_id in self.clients:
            client = self.clients[account_id]
            try:
                await client.stop()
            except:
                pass
            del self.clients[account_id]

    async def disconnect_client(self, account_id: int) -> bool:
        """Отключение клиента"""
        try:
            if account_id in self.clients:
                client = self.clients[account_id]
                try:
                    if hasattr(client, 'is_connected') and client.is_connected:
                        await client.disconnect()
                except Exception as disconnect_error:
                    print(f"Error during disconnect for client {account_id}: {disconnect_error}")
                    # Продолжаем удаление из словаря даже если disconnect не удался

                del self.clients[account_id]
                return True
        except Exception as e:
            print(f"Error disconnecting client {account_id}: {e}")
        return False

    async def send_message(self, account_id: int, recipient: str, message: str, file_path: Optional[str] = None, 
                          schedule_seconds: int = 0) -> Dict:
        """Отправка сообщения через указанный аккаунт с использованием встроенного планировщика Telegram"""
        print(f"Планирование сообщения в {recipient} от аккаунта {account_id}")
        
        if schedule_seconds > 0:
            print(f"Сообщение будет запланировано в Telegram на отправку через {schedule_seconds} секунд")
        else:
            print(f"Сообщение будет отправлено немедленно")

        try:
            # Получаем клиент для аккаунта
            client = await self._get_client_for_account(account_id)
            if not client:
                return {"status": "error", "message": "Клиент не найден"}

            # Проверяем подключение клиента
            try:
                if not client.is_connected:
                    await client.connect()
                
                # Проверяем, что клиент действительно авторизован
                me = await client.get_me()
                print(f"Отправка от: {me.first_name}")
            except Exception as conn_error:
                print(f"Ошибка подключения клиента: {conn_error}")
                return {"status": "error", "message": f"Ошибка подключения: {conn_error}"}

            try:
                # Если это username, добавляем @ если его нет
                if not recipient.startswith('@') and not recipient.startswith('+') and not recipient.isdigit() and not recipient.startswith('-'):
                    recipient = f"@{recipient}"

                # Всегда используем встроенный планировщик Telegram
                from datetime import datetime, timedelta
                
                # Рассчитываем время отправки через Telegram планировщик
                if schedule_seconds > 0:
                    schedule_date = datetime.utcnow() + timedelta(seconds=schedule_seconds)
                    print(f"Сообщение будет запланировано в Telegram на: {schedule_date}")
                else:
                    # Для немедленной отправки не используем планировщик
                    schedule_date = None
                    print(f"Сообщение будет отправлено немедленно")

                # Отправляем сообщение через встроенный планировщик Telegram
                sent_message = None
                try:
                    if file_path and os.path.exists(file_path):
                        if schedule_date:
                            print(f"Планирование отправки файла: {file_path}")
                            # Отправляем с файлом через планировщик
                            sent_message = await client.send_document(
                                chat_id=recipient,
                                document=file_path,
                                caption=message if message else None,
                                schedule_date=schedule_date
                            )
                            print(f"Файл успешно запланирован к отправке через Telegram на {schedule_date}")
                        else:
                            print(f"Отправка файла немедленно: {file_path}")
                            # Отправляем файл немедленно
                            sent_message = await client.send_document(
                                chat_id=recipient,
                                document=file_path,
                                caption=message if message else None
                            )
                            print(f"Файл успешно отправлен немедленно")
                    else:
                        if schedule_date:
                            # Отправляем только текст через планировщик
                            sent_message = await client.send_message(
                                chat_id=recipient,
                                text=message,
                                schedule_date=schedule_date
                            )
                            print(f"Текстовое сообщение успешно запланировано через Telegram на {schedule_date}")
                        else:
                            # Отправляем текст немедленно
                            sent_message = await client.send_message(
                                chat_id=recipient,
                                text=message
                            )
                            print(f"Текстовое сообщение отправлено немедленно")
                except Exception as send_error:
                    print(f"Ошибка при отправке: {send_error}")
                    raise send_error

                # Обновляем статистику аккаунта
                await self._update_account_stats(account_id)

                # Безопасное получение информации о сообщении
                message_id = None
                chat_id = None
                
                try:
                    if sent_message:
                        # Получаем ID сообщения
                        message_id = getattr(sent_message, 'id', None)
                        print(f"Message ID: {message_id}")
                        
                        # Получаем chat_id из recipient, так как это самый надежный способ
                        try:
                            if recipient.isdigit():
                                chat_id = int(recipient)
                            elif recipient.startswith('-') and recipient[1:].isdigit():
                                chat_id = int(recipient)
                            else:
                                chat_id = recipient
                            print(f"Chat ID from recipient: {chat_id}")
                        except:
                            chat_id = recipient
                        
                        print(f"Final message_id: {message_id}, chat_id: {chat_id}")
                
                except Exception as parse_error:
                    print(f"Ошибка при парсинге результата сообщения: {parse_error}")
                    # Продолжаем выполнение, даже если не удалось получить детали
                    message_id = None
                    chat_id = recipient

                return {
                    "status": "success",
                    "message_id": message_id,
                    "chat_id": chat_id
                }

            except Exception as e:
                error_msg = str(e)
                print(f"Ошибка отправки сообщения: {error_msg}")
                
                # Обработка специфических ошибок
                if "PEER_ID_INVALID" in error_msg:
                    return {"status": "error", "message": f"Пользователь {recipient} не найден или недоступен"}
                elif "USER_IS_BLOCKED" in error_msg:
                    return {"status": "error", "message": f"Пользователь {recipient} заблокировал бота"}
                elif "CHAT_WRITE_FORBIDDEN" in error_msg:
                    return {"status": "error", "message": f"Нет прав для записи в чат {recipient}"}
                else:
                    return {"status": "error", "message": error_msg}

        except Exception as e:
            error_msg = str(e)
            print(f"Общая ошибка отправки сообщения: {error_msg}")
            return {"status": "error", "message": error_msg}

    async def _update_account_stats(self, account_id: int):
        """Обновление статистики аккаунта"""
        db = next(get_db())
        try:
            account = db.query(Account).filter(Account.id == account_id).first()
            if account:
                now = datetime.utcnow()
                # Ограничение по времени между сообщениями
                if account.last_message_time and (now - account.last_message_time).total_seconds() < 1:
                    await asyncio.sleep(1 - (now - account.last_message_time).total_seconds())

                account.messages_sent_today += 1
                account.messages_sent_hour += 1
                account.last_message_time = datetime.utcnow()
                db.commit()
        except Exception as e:
            db.rollback()
            print(f"Ошибка обновления статистики аккаунта {account_id}: {e}")
        finally:
            db.close()

    async def get_client(self, account_id: int) -> Optional[Client]:
        """Вспомогательная функция для получения клиента (переименована для соответствия изменениям)"""
        return await self._get_client_for_account(account_id)

# Глобальный экземпляр менеджера
telegram_manager = TelegramManager()