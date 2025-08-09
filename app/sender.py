
import asyncio
import json
import csv
from datetime import datetime
from typing import List, Dict
from sqlalchemy.orm import Session
from app.database import Account, Campaign, SendLog, get_db
from app.telegram_client import telegram_manager

class MessageSender:
    def __init__(self):
        self.active_campaigns: Dict[int, bool] = {}
    
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
            
            # Получаем активные аккаунты
            accounts = db.query(Account).filter(Account.is_active == True).all()
            if not accounts:
                campaign.status = "completed"
                db.commit()
                return
            
            # Парсим списки получателей
            recipients = self._parse_recipients(campaign)
            
            account_index = 0
            total_sent = 0
            
            for recipient_type, recipient_list in recipients.items():
                if not self.active_campaigns.get(campaign_id, False):
                    break
                
                message = self._get_message_for_type(campaign, recipient_type)
                if not message:
                    continue
                
                for recipient in recipient_list:
                    if not self.active_campaigns.get(campaign_id, False):
                        break
                    
                    # Выбираем аккаунт по ротации
                    account = accounts[account_index % len(accounts)]
                    account_index += 1
                    
                    # Проверяем лимиты аккаунта
                    if not self._check_account_limits(account):
                        continue
                    
                    print(f"Sending message to {recipient} via account {account.id}")
                    
                    # Отправляем сообщение
                    result = await telegram_manager.send_message(
                        account.id,
                        recipient,
                        message,
                        campaign.attachment_path
                    )
                    
                    print(f"Send result: {result}")
                    
                    # Логируем результат
                    self._log_send_result(
                        campaign_id, account.id, recipient, 
                        recipient_type, result
                    )
                    
                    if result["status"] == "success":
                        total_sent += 1
                        print(f"Message sent successfully to {recipient}")
                    else:
                        print(f"Failed to send message to {recipient}: {result.get('message', 'Unknown error')}")
                    
                    # Задержка между отправками
                    await asyncio.sleep(campaign.delay_seconds)
            
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

# Глобальный экземпляр отправителя
message_sender = MessageSender()
