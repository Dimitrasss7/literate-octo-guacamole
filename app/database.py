from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Text, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
from app.config import DATABASE_URL

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String, unique=True, index=True)
    name = Column(String)
    status = Column(String, default="offline")  # online, offline, blocked, error
    session_data = Column(Text)  # зашифрованная сессия
    proxy = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_activity = Column(DateTime, default=datetime.utcnow)
    messages_sent_today = Column(Integer, default=0)
    messages_sent_hour = Column(Integer, default=0)
    last_message_time = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)

class Campaign(Base):
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    description = Column(Text, nullable=True)
    channel_message = Column(Text, nullable=True)
    group_message = Column(Text, nullable=True)
    private_message = Column(Text, nullable=True)
    channels_list = Column(Text, nullable=True)
    groups_list = Column(Text, nullable=True)
    private_list = Column(Text, nullable=True)
    delay_seconds = Column(Integer, default=3)
    attachment_path = Column(String, nullable=True)
    status = Column(String, default="created")  # created, running, paused, completed, scheduled, cancelled
    created_at = Column(DateTime, default=datetime.utcnow)
    scheduled_start = Column(DateTime, nullable=True)  # время запланированного старта
    account_id = Column(Integer, nullable=True)  # для привязки к конкретному аккаунту

class SendLog(Base):
    __tablename__ = "send_logs"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer)
    account_id = Column(Integer)
    recipient = Column(String)
    recipient_type = Column(String)  # channel, group, private
    status = Column(String)  # sent, failed, blocked
    message = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    sent_at = Column(DateTime, default=datetime.utcnow)

# Создаем таблицы
Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()