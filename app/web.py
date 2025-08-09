
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
async def add_account(phone: str = Form(...), proxy: Optional[str] = Form(None)):
    """Добавление нового аккаунта"""
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
    result = await telegram_manager.verify_code(phone, code, phone_code_hash, session_name, proxy)
    
    # Если код истек, автоматически отправляем новый
    if result.get("status") == "code_expired":
        new_code_result = await telegram_manager.add_account(phone, proxy)
        if new_code_result.get("status") == "code_required":
            result["new_phone_code_hash"] = new_code_result["phone_code_hash"]
            result["message"] = "Код истек. Новый код отправлен на ваш номер."
    
    return JSONResponse(result)

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

@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, db: Session = Depends(get_db)):
    """Страница логов"""
    logs = db.query(SendLog).order_by(SendLog.sent_at.desc()).limit(100).all()
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "logs": logs
    })

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
        ).count()
    })
