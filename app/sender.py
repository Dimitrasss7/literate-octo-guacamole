import asyncio
import json
import csv
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from sqlalchemy.orm import Session
from app.database import Account, Campaign, SendLog, get_db
from app.telegram_client import telegram_manager

class MessageSender:
    def __init__(self):
        self.active_campaigns: Dict[int, bool] = {}
        self.scheduled_campaigns: Dict[int, asyncio.Task] = {}

    async def start_campaign(self, campaign_id: int) -> Dict:
        """Запуск кампании рассылки"""
        if campaign_id in self.active_campaigns:
            return {"status": "error", "message": "Кампания уже запущена"}

        db = next(get_db())
        try:
            campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
            if not campaign:
                return {"status": "error", "message": "Кампания не найдена"}

            campaign.status = "running"
            db.commit()

            self.active_campaigns[campaign_id] = True

            # Запускаем отправку в фоне
            asyncio.create_task(self._run_campaign(campaign_id))

            return {"status": "success", "message": "Кампания запущена"}
        finally:
            db.close()

    async def create_auto_campaign(self, account_id: int, message: str, delay_seconds: int = 5, target_types: List[str] = None) -> Dict:
        """Создание автоматической кампании для всех контактов пользователя"""
        if target_types is None:
            target_types = ["private"]  # По умолчанию только приватные сообщения

        try:
            from app.telegram_client import telegram_manager

            # Получаем все чаты пользователя
            chats_result = await telegram_manager.get_user_chats(account_id)
            if chats_result["status"] != "success":
                return {"status": "error", "message": "Не удалось получить список чатов"}

            chats = chats_result["chats"]
            recipients = {"private": [], "groups": [], "channels": []}

            # Формируем списки получателей
            for chat_type in target_types:
                if chat_type in chats:
                    for chat in chats[chat_type]:
                        if chat["username"]:
                            recipients[chat_type].append(f"@{chat['username']}")
                        else:
                            recipients[chat_type].append(str(chat["id"]))

            # Создаем кампанию в базе данных
            db = next(get_db())
            try:
                campaign = Campaign(
                    name=f"Автоматическая рассылка {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                    delay_seconds=delay_seconds,
                    private_message=message if "private" in target_types else None,
                    group_message=message if "groups" in target_types else None,
                    channel_message=message if "channels" in target_types else None,
                    private_list="\n".join(recipients["private"]) if recipients["private"] else None,
                    groups_list="\n".join(recipients["groups"]) if recipients["groups"] else None,
                    channels_list="\n".join(recipients["channels"]) if recipients["channels"] else None,
                    status="created"
                )

                db.add(campaign)
                db.commit()
                db.refresh(campaign)

                return {
                    "status": "success", 
                    "campaign_id": campaign.id,
                    "recipients_count": sum(len(recipients[t]) for t in recipients),
                    "message": f"Создана автоматическая кампания с {sum(len(recipients[t]) for t in recipients)} получателями"
                }

            finally:
                db.close()

        except Exception as e:
            print(f"Error creating auto campaign: {str(e)}")
            return {"status": "error", "message": str(e)}

    async def start_auto_campaign(self, account_id: int, message: str, delay_seconds: int = 5, target_types: List[str] = None) -> Dict:
        """Создание и запуск автоматической кампании"""
        # Создаем кампанию
        result = await self.create_auto_campaign(account_id, message, delay_seconds, target_types)
        if result["status"] != "success":
            return result

        # Запускаем кампанию
        campaign_id = result["campaign_id"]
        start_result = await self.start_campaign(campaign_id)

        if start_result["status"] == "success":
            return {
                "status": "success",
                "campaign_id": campaign_id,
                "recipients_count": result["recipients_count"],
                "message": f"Автоматическая рассылка запущена для {result['recipients_count']} получателей"
            }
        else:
            return start_result

    async def stop_campaign(self, campaign_id: int) -> Dict:
        """Остановка кампании"""
        if campaign_id in self.active_campaigns:
            self.active_campaigns[campaign_id] = False

            db = next(get_db())
            campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
            if campaign:
                campaign.status = "paused"
                db.commit()
            db.close()

            return {"status": "success", "message": "Кампания остановлена"}

        return {"status": "error", "message": "Кампания не активна"}

    async def _run_campaign(self, campaign_id: int):
        """Выполнение кампании рассылки"""
        db = next(get_db())
        try:
            campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
            if not campaign:
                return

            # Для кампаний по контактам используем только тот аккаунт, который был указан
            if hasattr(campaign, 'account_id') and campaign.account_id:
                # Используем конкретный аккаунт для рассылки по его контактам
                account = db.query(Account).filter(
                    Account.id == campaign.account_id,
                    Account.is_active == True
                ).first()
                
                if not account:
                    campaign.status = "completed"
                    db.commit()
                    return
                
                accounts = [account]
                print(f"Using specific account {account.id} ({account.name}) for contacts campaign")
            else:
                # Для обычных кампаний используем все активные аккаунты
                accounts = db.query(Account).filter(Account.is_active == True).all()
                if not accounts:
                    campaign.status = "completed"
                    db.commit()
                    return

            # Парсим списки получателей
            recipients = self._parse_recipients(campaign)

            account_index = 0
            total_sent = 0

            # Счетчик для расчета задержки для всех сообщений
            total_message_count = 0
            
            for recipient_type, recipient_list in recipients.items():
                if not self.active_campaigns.get(campaign_id, False):
                    break

                message = self._get_message_for_type(campaign, recipient_type)
                if not message:
                    continue

                # Отправляем все сообщения с использованием Telegram scheduling
                for recipient in recipient_list:
                    if not self.active_campaigns.get(campaign_id, False):
                        break

                    # Выбираем аккаунт
                    if len(accounts) == 1:
                        # Используем единственный аккаунт
                        account = accounts[0]
                    else:
                        # Выбираем аккаунт по ротации
                        account = accounts[account_index % len(accounts)]
                        account_index += 1

                    # Проверяем лимиты аккаунта
                    if not self._check_account_limits(account):
                        continue

                    print(f"Scheduling message to {recipient} via account {account.id}")

                    # Рассчитываем задержку для текущего сообщения (увеличиваем для каждого сообщения)
                    current_delay = total_message_count * campaign.delay_seconds
                    total_message_count += 1
                    
                    # Отправляем сообщение с отложенной отправкой через Telegram
                    result = await telegram_manager.send_message(
                        account.id,
                        recipient,
                        message,
                        campaign.attachment_path,
                        schedule_seconds=current_delay
                    )

                    print(f"Schedule result for {recipient}: {result}")

                    # Логируем результат
                    self._log_send_result(
                        campaign_id, account.id, recipient, 
                        recipient_type, result
                    )

                    if result["status"] == "success":
                        total_sent += 1
                        if current_delay > 0:
                            print(f"Message scheduled successfully to {recipient} (will be sent in {current_delay} seconds by Telegram)")
                        else:
                            print(f"Message sent immediately to {recipient}")
                    else:
                        print(f"Failed to schedule message to {recipient}: {result.get('message', 'Unknown error')}")

            # Завершаем кампанию
            campaign.status = "completed"
            db.commit()

            if campaign_id in self.active_campaigns:
                del self.active_campaigns[campaign_id]

        finally:
            db.close()

    def _parse_recipients(self, campaign: Campaign) -> Dict[str, List[str]]:
        """Парсинг списков получателей"""
        recipients = {}

        if campaign.channels_list:
            try:
                recipients["channel"] = json.loads(campaign.channels_list)
            except:
                recipients["channel"] = [line.strip() for line in campaign.channels_list.split("\n") if line.strip()]

        if campaign.groups_list:
            try:
                recipients["group"] = json.loads(campaign.groups_list)
            except:
                recipients["group"] = [line.strip() for line in campaign.groups_list.split("\n") if line.strip()]

        if campaign.private_list:
            try:
                recipients["private"] = json.loads(campaign.private_list)
            except:
                recipients["private"] = [line.strip() for line in campaign.private_list.split("\n") if line.strip()]

        # Убираем пустые строки и очищаем от лишних символов
        for key in recipients:
            cleaned_recipients = []
            for r in recipients[key]:
                if r.strip():
                    clean_r = r.strip()

                    # Обрабатываем ссылки Telegram
                    if 't.me/' in clean_r:
                        if 't.me/joinchat/' in clean_r:
                            # Старый формат приватных ссылок
                            clean_r = clean_r.split('t.me/joinchat/')[1]
                            clean_r = f"+{clean_r}"
                        elif 't.me/+' in clean_r:
                            # Новый формат приватных ссылок
                            clean_r = clean_r.split('t.me/')[1]
                        else:
                            # Это обычный username
                            clean_r = clean_r.split('t.me/')[1].split('?')[0]  # убираем параметры
                            # Для обычных username не убираем @, оставляем как есть
                            if not clean_r.startswith('@') and not clean_r.startswith('+'):
                                clean_r = f"@{clean_r}"
                    else:
                        # Если это просто username или ID
                        if clean_r.startswith('@'):
                            # Оставляем @ для групп и каналов
                            pass
                        elif clean_r.startswith('+'):
                            # Приватная ссылка без t.me
                            pass  
                        elif clean_r.isdigit() or clean_r.startswith('-'):
                            # Это ID чата
                            pass
                        else:
                            # Обычный username без @ - добавляем @
                            clean_r = f"@{clean_r}"

                    if clean_r:
                        cleaned_recipients.append(clean_r)
            recipients[key] = cleaned_recipients

        print(f"Parsed recipients: {recipients}")
        return recipients

    def _get_message_for_type(self, campaign: Campaign, recipient_type: str) -> str:
        """Получение сообщения для типа получателя"""
        if recipient_type == "channel":
            return campaign.channel_message
        elif recipient_type == "group":
            return campaign.group_message
        elif recipient_type == "private":
            return campaign.private_message
        return None

    def _check_account_limits(self, account: Account) -> bool:
        """Проверка лимитов аккаунта"""
        from app.config import MAX_MESSAGES_PER_HOUR, MAX_MESSAGES_PER_DAY

        if account.messages_sent_today >= MAX_MESSAGES_PER_DAY:
            return False

        if account.messages_sent_hour >= MAX_MESSAGES_PER_HOUR:
            return False

        return True

    def _log_send_result(self, campaign_id: int, account_id: int, 
                        recipient: str, recipient_type: str, result: Dict):
        """Логирование результата отправки"""
        db = next(get_db())
        try:
            log = SendLog(
                campaign_id=campaign_id,
                account_id=account_id,
                recipient=recipient,
                recipient_type=recipient_type,
                status=result["status"],
                error_message=result.get("message") if result["status"] != "success" else None
            )
            db.add(log)
            db.commit()
        finally:
            db.close()

    async def create_and_start_auto_campaign(self, account_id: int, message: str, 
                                          delay_seconds: int, unique_targets: bool = True) -> Dict:
        """Создание и запуск автоматической кампании"""
        try:
            # Получаем контакты пользователя
            contacts_result = await telegram_manager.get_user_contacts(account_id)
            if contacts_result["status"] != "success":
                return {"status": "error", "message": f"Не удалось получить контакты: {contacts_result.get('message', 'Unknown error')}"}

            contacts = contacts_result.get("contacts", [])
            if not contacts:
                return {"status": "error", "message": "У аккаунта нет контактов для рассылки"}

            # Создаем список целей из контактов
            targets = []
            for contact in contacts:
                if contact.get("username"):
                    targets.append(f"@{contact['username']}")
                elif contact.get("id"):
                    targets.append(str(contact["id"]))

            if not targets:
                return {"status": "error", "message": "Не найдено целей для рассылки"}

            # Создаем кампанию
            campaign_name = f"Auto Campaign {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            campaign_result = await self.create_campaign(
                name=campaign_name,
                message=message,
                targets=targets,
                account_id=account_id,
                delay_seconds=delay_seconds
            )

            if campaign_result["status"] != "success":
                return campaign_result

            campaign_id = campaign_result["campaign_id"]

            # Запускаем кампанию
            start_result = await self.start_campaign(campaign_id)

            return {
                "status": "success",
                "campaign_id": campaign_id,
                "targets_count": len(targets),
                "message": "Автоматическая кампания создана и запущена"
            }

        except Exception as e:
            print(f"Error in create_and_start_auto_campaign: {str(e)}")
            return {"status": "error", "message": str(e)}

    async def create_campaign(self, name: str, message: str, targets: List[str], 
                              account_id: int, file_path: Optional[str] = None, 
                              delay_seconds: int = 1) -> Dict:
        """Создание кампании рассылки"""
        db = next(get_db())
        try:
            campaign = Campaign(
                name=name,
                private_message=message,
                private_list="\n".join(targets),
                account_id=account_id,
                attachment_path=file_path,
                delay_seconds=delay_seconds,
                status="created"
            )
            db.add(campaign)
            db.commit()
            db.refresh(campaign)
            return {"status": "success", "campaign_id": campaign.id}
        finally:
            db.close()

    async def create_contacts_campaign(self, account_id: int, message: str, delay_seconds: int = 5, 
                                     start_in_minutes: Optional[int] = None, attachment_path: Optional[str] = None) -> Dict:
        """Создание кампании рассылки только по контактам из адресной книги"""
        try:
            # Получаем контакты пользователя из адресной книги
            contacts_result = await telegram_manager.get_user_contacts(account_id)
            if contacts_result["status"] != "success":
                return {"status": "error", "message": f"Не удалось получить контакты: {contacts_result.get('message', 'Unknown error')}"}

            contacts = contacts_result.get("contacts", [])
            if not contacts:
                return {"status": "error", "message": "У аккаунта нет контактов для рассылки"}

            # Формируем список получателей
            targets = []
            for contact in contacts:
                if contact.get("username"):
                    targets.append(f"@{contact['username']}")
                elif contact.get("id"):
                    targets.append(str(contact["id"]))

            if not targets:
                return {"status": "error", "message": "Не найдено целей для рассылки среди контактов"}

            # Создаем кампанию в базе данных
            db = next(get_db())
            try:
                # Вычисляем время запуска
                start_time = datetime.utcnow()
                if start_in_minutes:
                    start_time = start_time + timedelta(minutes=start_in_minutes)

                campaign = Campaign(
                    name=f"Рассылка по контактам {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                    delay_seconds=delay_seconds,
                    private_message=message,
                    private_list="\n".join(targets),
                    attachment_path=attachment_path,
                    account_id=account_id,
                    status="scheduled" if start_in_minutes else "created"
                )

                db.add(campaign)
                db.commit()
                db.refresh(campaign)

                # Если задана задержка - планируем запуск
                if start_in_minutes:
                    task = asyncio.create_task(self._schedule_campaign_start(campaign.id, start_in_minutes * 60))
                    self.scheduled_campaigns[campaign.id] = task
                    
                    return {
                        "status": "success",
                        "campaign_id": campaign.id,
                        "contacts_count": len(targets),
                        "scheduled_start": start_time.strftime('%Y-%m-%d %H:%M:%S'),
                        "message": f"Кампания создана и запланирована на {start_time.strftime('%H:%M')}. Рассылка по {len(targets)} контактам"
                    }
                else:
                    return {
                        "status": "success",
                        "campaign_id": campaign.id,
                        "contacts_count": len(targets),
                        "message": f"Кампания создана с {len(targets)} контактами. Готова к запуску"
                    }

            finally:
                db.close()

        except Exception as e:
            print(f"Error creating contacts campaign: {str(e)}")
            return {"status": "error", "message": str(e)}

    async def start_contacts_campaign(self, account_id: int, message: str, delay_seconds: int = 5, 
                                    start_in_minutes: Optional[int] = None, attachment_path: Optional[str] = None) -> Dict:
        """Создание и запуск кампании рассылки по контактам"""
        # Создаем кампанию
        result = await self.create_contacts_campaign(account_id, message, delay_seconds, start_in_minutes, attachment_path)
        if result["status"] != "success":
            return result

        campaign_id = result["campaign_id"]

        # Если задержка не указана - запускаем сразу
        if start_in_minutes is None:
            start_result = await self.start_campaign(campaign_id)
            if start_result["status"] == "success":
                return {
                    "status": "success",
                    "campaign_id": campaign_id,
                    "contacts_count": result["contacts_count"],
                    "message": f"Рассылка запущена по {result['contacts_count']} контактам"
                }
            else:
                return start_result
        else:
            return result

    async def _schedule_campaign_start(self, campaign_id: int, delay_seconds: int):
        """Планировщик запуска кампании с задержкой"""
        try:
            print(f"Кампания {campaign_id} запланирована на запуск через {delay_seconds} секунд")
            
            # Ждем указанное время
            await asyncio.sleep(delay_seconds)
            
            # Запускаем кампанию
            result = await self.start_campaign(campaign_id)
            
            # Удаляем из планировщика
            if campaign_id in self.scheduled_campaigns:
                del self.scheduled_campaigns[campaign_id]
            
            print(f"Запланированная кампания {campaign_id} запущена: {result}")
            
        except asyncio.CancelledError:
            print(f"Запланированная кампания {campaign_id} была отменена")
        except Exception as e:
            print(f"Ошибка запуска запланированной кампании {campaign_id}: {str(e)}")

    async def cancel_scheduled_campaign(self, campaign_id: int) -> Dict:
        """Отмена запланированной кампании"""
        if campaign_id in self.scheduled_campaigns:
            task = self.scheduled_campaigns[campaign_id]
            task.cancel()
            del self.scheduled_campaigns[campaign_id]
            
            # Обновляем статус в БД
            db = next(get_db())
            try:
                campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
                if campaign:
                    campaign.status = "cancelled"
                    db.commit()
            finally:
                db.close()
            
            return {"status": "success", "message": "Запланированная кампания отменена"}
        
        return {"status": "error", "message": "Кампания не найдена в планировщике"}

    def get_scheduled_campaigns(self) -> List[int]:
        """Получение списка запланированных кампаний"""
        return list(self.scheduled_campaigns.keys())


# Глобальный экземпляр отправителя
message_sender = MessageSender()