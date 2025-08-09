import os
import json
from typing import List, Optional
from datetime import datetime
from fastapi import FastAPI, Request, Form, File, UploadFile, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import Account, Campaign, SendLog, get_db
from app.telegram_client import telegram_manager
from app.sender import message_sender
from app.proxy_manager import proxy_manager
from app.settings_manager import settings_manager
from app.config import UPLOADS_DIR

app = FastAPI(title="Telegram Mass Sender")

# Создаем папки для статики и шаблонов
os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    """Главная страница dashboard"""
    accounts = db.query(Account).all()
    campaigns = db.query(Campaign).order_by(Campaign.created_at.desc()).limit(10).all()

    # Статистика
    total_accounts = len(accounts)
    active_accounts = len([a for a in accounts if a.is_active and a.status == "online"])
    total_campaigns = db.query(Campaign).count()
    messages_sent_today = db.query(SendLog).filter(
        SendLog.sent_at >= datetime.utcnow().date()
    ).count()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "accounts": accounts,
        "campaigns": campaigns,
        "stats": {
            "total_accounts": total_accounts,
            "active_accounts": active_accounts,
            "total_campaigns": total_campaigns,
            "messages_sent_today": messages_sent_today
        }
    })

@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request, db: Session = Depends(get_db)):
    """Страница управления аккаунтами"""
    accounts = db.query(Account).all()
    return templates.TemplateResponse("accounts.html", {
        "request": request,
        "accounts": accounts
    })

@app.post("/accounts/add")
async def add_account(phone: str = Form(...), use_auto_proxy: bool = Form(False)):
    """Добавление нового аккаунта"""
    proxy = None
    if use_auto_proxy:
        proxy = proxy_manager.get_proxy_for_phone(phone)
        if not proxy:
            return JSONResponse({"status": "error", "message": "Нет доступных прокси. Загрузите список прокси."})

    result = await telegram_manager.add_account(phone, proxy)
    return JSONResponse(result)

@app.post("/accounts/verify_code")
async def verify_code(
    phone: str = Form(...),
    code: str = Form(...),
    phone_code_hash: str = Form(...),
    session_name: str = Form(...),
    proxy: Optional[str] = Form(None)
):
    """Подтверждение кода"""
    try:
        # Валидация входных данных
        if not code or len(code.strip()) == 0:
            return JSONResponse({"status": "error", "message": "Код не может быть пустым"})

        if len(code.strip()) != 5:
            return JSONResponse({"status": "error", "message": "Код должен содержать 5 цифр"})

        result = await telegram_manager.verify_code(phone, code.strip(), phone_code_hash, session_name, proxy)

        # Проверяем, что result не None
        if result is None:
            result = {"status": "error", "message": "Внутренняя ошибка сервера"}

        # Если код истек, автоматически отправляем новый
        if result.get("status") == "code_expired":
            new_code_result = await telegram_manager.add_account(phone, proxy)
            if new_code_result and new_code_result.get("status") == "code_required":
                result["new_phone_code_hash"] = new_code_result["phone_code_hash"]
                result["message"] = "Код истек. Новый код отправлен на ваш номер."

        return JSONResponse(result)

    except Exception as e:
        error_msg = str(e)
        print(f"Веб-ошибка при верификации: {error_msg}")

        # Логируем ошибку
        with open("unknown_errors.txt", "a", encoding="utf-8") as f:
            f.write(f"Web verify code error: {error_msg}\n")
            f.write(f"Phone: {phone}\n")
            f.write(f"Exception type: {type(e).__name__}\n")
            f.write("---\n")

        return JSONResponse({"status": "error", "message": f"Ошибка сервера: {error_msg}"})

@app.post("/accounts/verify_password")
async def verify_password(
    phone: str = Form(...),
    password: str = Form(...),
    session_name: str = Form(...),
    proxy: Optional[str] = Form(None)
):
    """Подтверждение пароля 2FA"""
    result = await telegram_manager.verify_password(phone, password, session_name, proxy)
    return JSONResponse(result)

@app.post("/accounts/{account_id}/toggle")
async def toggle_account(account_id: int, db: Session = Depends(get_db)):
    """Включение/отключение аккаунта"""
    account = db.query(Account).filter(Account.id == account_id).first()
    if account:
        account.is_active = not account.is_active
        db.commit()
        return JSONResponse({"status": "success"})
    return JSONResponse({"status": "error", "message": "Аккаунт не найден"})

@app.delete("/accounts/{account_id}")
async def delete_account(account_id: int, db: Session = Depends(get_db)):
    """Удаление аккаунта"""
    account = db.query(Account).filter(Account.id == account_id).first()
    if account:
        db.delete(account)
        db.commit()
        return JSONResponse({"status": "success"})
    return JSONResponse({"status": "error", "message": "Аккаунт не найден"})

@app.get("/campaigns", response_class=HTMLResponse)
async def campaigns_page(request: Request, db: Session = Depends(get_db)):
    """Страница кампаний"""
    campaigns = db.query(Campaign).order_by(Campaign.created_at.desc()).all()
    return templates.TemplateResponse("campaigns.html", {
        "request": request,
        "campaigns": campaigns
    })

@app.get("/campaigns/new", response_class=HTMLResponse)
async def new_campaign_page(request: Request):
    """Страница создания новой кампании"""
    return templates.TemplateResponse("campaign_form.html", {
        "request": request,
        "campaign": None
    })

@app.post("/campaigns")
@app.post("/campaigns/new")
async def create_campaign(
    name: str = Form(...),
    channel_message: str = Form(""),
    group_message: str = Form(""),
    private_message: str = Form(""),
    channels_list: str = Form(""),
    groups_list: str = Form(""),
    private_list: str = Form(""),
    delay_seconds: int = Form(3),
    attachment: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db)
):
    """Создание новой кампании"""

    attachment_path = None
    if attachment and attachment.filename:
        file_path = os.path.join(UPLOADS_DIR, attachment.filename)
        with open(file_path, "wb") as f:
            content = await attachment.read()
            f.write(content)
        attachment_path = file_path

    campaign = Campaign(
        name=name,
        channel_message=channel_message,
        group_message=group_message,
        private_message=private_message,
        channels_list=channels_list,
        groups_list=groups_list,
        private_list=private_list,
        delay_seconds=delay_seconds,
        attachment_path=attachment_path
    )

    db.add(campaign)
    db.commit()

    return RedirectResponse(url="/campaigns", status_code=303)

@app.post("/campaigns/{campaign_id}/start")
async def start_campaign(campaign_id: int):
    """Запуск кампании"""
    result = await message_sender.start_campaign(campaign_id)
    return JSONResponse(result)

@app.post("/campaigns/{campaign_id}/stop")
async def stop_campaign(campaign_id: int):
    """Остановка кампании"""
    result = await message_sender.stop_campaign(campaign_id)
    return JSONResponse(result)

@app.get("/logs")
async def logs_page(request: Request, db: Session = Depends(get_db)):
    """Страница логов"""
    logs = db.query(SendLog).order_by(SendLog.sent_at.desc()).limit(100).all()
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "logs": logs
    })

@app.get("/settings")
async def settings_page(request: Request):
    """Страница настроек антиспам-системы"""
    return templates.TemplateResponse("settings.html", {"request": request})

# API endpoints

@app.get("/proxies", response_class=HTMLResponse)
async def proxies_page(request: Request):
    """Страница управления прокси"""
    return templates.TemplateResponse("proxies.html", {
        "request": request,
        "proxies_count": proxy_manager.get_available_proxies_count(),
        "used_count": proxy_manager.get_used_proxies_count(),
        "proxies": proxy_manager.get_all_proxies()
    })

@app.post("/proxies/upload")
async def upload_proxies(proxies_text: str = Form(...)):
    """Загрузка списка прокси"""
    try:
        proxy_manager.save_proxies(proxies_text)
        return JSONResponse({
            "status": "success",
            "message": f"Загружено {proxy_manager.get_available_proxies_count()} прокси"
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)})

@app.post("/api/proxy/delete/{proxy_id}")
async def delete_proxy(proxy_id: int):
    """Удаление прокси"""
    success = proxy_manager.remove_proxy(proxy_id)
    return {"success": success}

@app.get("/api/settings")
async def get_settings():
    """Получение всех настроек"""
    return {"success": True, "settings": settings_manager.get_settings_dict()}

@app.post("/api/settings")
async def save_all_settings(request: Request):
    """Сохранение всех настроек"""
    try:
        data = await request.json()
        success = settings_manager.update_all_settings(data)
        return {"success": success, "message": "Настройки сохранены" if success else "Ошибка сохранения"}
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.post("/api/settings/{section}")
async def save_settings_section(section: str, request: Request):
    """Сохранение конкретной секции настроек"""
    try:
        data = await request.json()
        success = settings_manager.update_section(section, data)
        return {"success": success, "message": f"Настройки {section} сохранены" if success else "Ошибка сохранения"}
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.post("/api/settings/reset")
async def reset_settings():
    """Сброс настроек к умолчаниям"""
    try:
        success = settings_manager.reset_to_defaults()
        return {"success": success, "message": "Настройки сброшены" if success else "Ошибка сброса"}
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.delete("/accounts/{account_id}")
async def delete_account(account_id: int, db: Session = Depends(get_db)):
    """Удаление аккаунта"""
    account = db.query(Account).filter(Account.id == account_id).first()
    if account:
        # Освобождаем прокси для этого номера
        proxy_manager.clear_proxy_for_phone(account.phone)
        db.delete(account)
        db.commit()
        return JSONResponse({"status": "success"})
    return JSONResponse({"status": "error", "message": "Аккаунт не найден"})

@app.get("/api/accounts")
async def get_accounts_api(db: Session = Depends(get_db)):
    """API для получения списка аккаунтов"""
    accounts = db.query(Account).all()
    accounts_data = []
    for account in accounts:
        accounts_data.append({
            "id": account.id,
            "name": account.name,
            "phone": account.phone,
            "is_active": account.is_active,
            "status": account.status
        })
    return JSONResponse(accounts_data)

@app.get("/api/stats")
async def get_stats(db: Session = Depends(get_db)):
    """API для получения статистики"""
    accounts = db.query(Account).all()
    campaigns = db.query(Campaign).all()

    return JSONResponse({
        "accounts": {
            "total": len(accounts),
            "active": len([a for a in accounts if a.is_active]),
            "online": len([a for a in accounts if a.status == "online"])
        },
        "campaigns": {
            "total": len(campaigns),
            "running": len([c for c in campaigns if c.status == "running"])
        },
        "messages_today": db.query(SendLog).filter(
            SendLog.sent_at >= datetime.utcnow().date()
        ).count(),
        "proxies": {
            "total": proxy_manager.get_available_proxies_count(),
            "used": proxy_manager.get_used_proxies_count()
        }
    })

# Новые маршруты для автоматических кампаний и контактов
@app.get("/api/accounts/{account_id}/contacts")
async def get_contacts(account_id: int):
    """Получение контактов аккаунта"""
    result = await telegram_manager.get_user_contacts(account_id)
    return JSONResponse(result)

@app.get("/api/accounts/{account_id}/chats")
async def get_chats(account_id: int):
    """Получение всех чатов аккаунта"""
    result = await telegram_manager.get_user_chats(account_id)
    return JSONResponse(result)

@app.post("/api/auto-campaign")
async def create_auto_campaign(request: Request, db: Session = Depends(get_db)):
    """Создание автоматической кампании"""
    data = await request.json()

    account_id = data.get('account_id')
    message = data.get('message')
    delay_seconds = data.get('delay_seconds', 5)
    target_types = data.get('target_types', ['private'])

    if not account_id or not message:
        return JSONResponse({"status": "error", "message": "Не указан аккаунт или сообщение"}), 400

    # Получаем активный аккаунт для рассылки
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account or not account.is_active:
        return JSONResponse({"status": "error", "message": "Аккаунт неактивен или не найден"}), 400

    # Получаем контакты и чаты в зависимости от target_types
    target_contacts = []
    if 'private' in target_types:
        contacts_result = await get_contacts(account_id)
        if contacts_result.get("status") == "success":
            target_contacts.extend(contacts_result.get("contacts", []))
    if 'group' in target_types:
        chats_result = await get_chats(account_id)
        if chats_result.get("status") == "success":
            target_contacts.extend(chats_result.get("chats", []))
    if 'channel' in target_types:
        # Предполагаем, что для каналов тоже есть метод get_user_chats с соответствующими типами
        chats_result = await get_chats(account_id)
        if chats_result.get("status") == "success":
            target_contacts.extend(chats_result.get("chats", []))

    # Удаляем дубликаты и форматируем для message_sender
    unique_targets = list({target.get('id') or target.get('username') or target.get('title') for target in target_contacts})
    
    # Создаем кампанию и запускаем рассылку
    result = await message_sender.create_and_start_auto_campaign(account_id, message, delay_seconds, unique_targets)
    return JSONResponse(result)

@app.post("/api/auto-campaign/start")
async def start_auto_campaign(request: Request, db: Session = Depends(get_db)):
    """Создание и запуск автоматической кампании"""
    data = await request.json()

    account_id = data.get('account_id')
    message = data.get('message')
    delay_seconds = data.get('delay_seconds', 5)
    target_types = data.get('target_types', ['private'])

    if not account_id or not message:
        return JSONResponse({"status": "error", "message": "Не указан аккаунт или сообщение"}), 400

    # Получаем активный аккаунт для рассылки
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account or not account.is_active:
        return JSONResponse({"status": "error", "message": "Аккаунт неактивен или не найден"}), 400

    # Получаем контакты и чаты в зависимости от target_types
    target_contacts = []
    if 'private' in target_types:
        contacts_result = await get_contacts(account_id)
        if contacts_result.get("status") == "success":
            target_contacts.extend(contacts_result.get("contacts", []))
    if 'group' in target_types:
        chats_result = await get_chats(account_id)
        if chats_result.get("status") == "success":
            target_contacts.extend(chats_result.get("chats", []))
    if 'channel' in target_types:
        # Предполагаем, что для каналов тоже есть метод get_user_chats с соответствующими типами
        chats_result = await get_chats(account_id)
        if chats_result.get("status") == "success":
            target_contacts.extend(chats_result.get("chats", []))

    # Удаляем дубликаты и форматируем для message_sender
    unique_targets = list({target.get('id') or target.get('username') or target.get('title') for target in target_contacts})

    # Создаем кампанию и запускаем рассылку
    result = await message_sender.create_and_start_auto_campaign(account_id, message, delay_seconds, unique_targets)
    return JSONResponse(result)

@app.get("/auto-campaign")
async def auto_campaign_page(request: Request):
    """Страница создания автоматической кампании"""
    return templates.TemplateResponse("auto_campaign.html", {"request": request})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)