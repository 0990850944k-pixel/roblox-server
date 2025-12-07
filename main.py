from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from pymongo import MongoClient
import os
from dotenv import load_dotenv
import uuid
import certifi
import datetime

# --- 1. НАСТРОЙКА ---
load_dotenv()
MONGO_URL = os.getenv("MONGO_URL")

try:
    if not MONGO_URL:
        raise ValueError("MONGO_URL не найден в .env файле!")
    
    client = MongoClient(MONGO_URL, tlsCAFile=certifi.where()) 
    db = client["QuestNetworkDB"] 
    client.admin.command('ping')
    print("✅ MONGODB ПОДКЛЮЧЕНА!")
except Exception as e:
    print(f"❌ ОШИБКА ПОДКЛЮЧЕНИЯ: {e}")

app = FastAPI()

# --- 2. МОДЕЛИ ДАННЫХ ---
class GameRegistration(BaseModel):
    ownerId: int        
    placeId: int        

class QuestStart(BaseModel):
    api_key: str            
    player_id: int          
    destination_place_id: int 

class TokenVerification(BaseModel):
    token: str

# --- 3. ЭНДПОИНТЫ ---

@app.get("/")
def home():
    return {"status": "Online"}

@app.post("/register-game")
def register_game(data: GameRegistration):
    users_collection = db["users"]
    existing_user = users_collection.find_one({"_id": data.ownerId})
    
    if existing_user:
        return {"success": True, "api_key": existing_user["api_key"]}
    else:
        new_api_key = "SK_" + str(uuid.uuid4()).replace("-", "").upper()
        users_collection.insert_one({
            "_id": data.ownerId, "api_key": new_api_key, "balance": 0, "games": [data.placeId]
        })
        return {"success": True, "api_key": new_api_key}

@app.post("/start-quest")
def start_quest(data: QuestStart):
    users = db["users"]
    quests = db["quests"]
    
    user = users.find_one({"api_key": data.api_key})
    if not user:
        raise HTTPException(status_code=401, detail="Неверный API Key")
    
    token = str(uuid.uuid4())
    
    quests.insert_one({
        "token": token,
        "player_id": data.player_id,
        "from_owner": user["_id"],
        "target_game": data.destination_place_id,
        "status": "started",
        "timestamp": datetime.datetime.utcnow()
    })
    
    print(f"🚀 Выдан токен: {token} для игрока {data.player_id}")
    return {"success": True, "token": token}

# 👇 ЭТАП 1: ИГРОК ПРИБЫЛ 👇
@app.post("/verify-token")
def verify_token(data: TokenVerification):
    quests = db["quests"]
    
    quest = quests.find_one({"token": data.token})
    
    if not quest:
        return {"success": False, "message": "Токен не найден"}
    
    if quest["status"] != "started":
        # Если игрок перезашел, но уже был отмечен как arrived, можно вернуть success,
        # чтобы таймер продолжился, но для простоты пока оставим ошибку.
        return {"success": False, "message": "Токен уже использован или истек"}
        
    quests.update_one(
        {"_id": quest["_id"]}, 
        {"$set": {
            "status": "arrived", 
            "arrived_at": datetime.datetime.utcnow()
        }}
    )
    
    print(f"✅ Игрок {quest['player_id']} прибыл! Таймер запущен.")
    return {"success": True, "player_id": quest["player_id"]}

# 👇 ЭТАП 2: ПРОВЕРКА ТАЙМЕРА (НОВОЕ) 👇
@app.post("/check-timer")
def check_timer(data: TokenVerification):
    quests = db["quests"]
    
    quest = quests.find_one({"token": data.token})
    
    if not quest:
        return {"success": False, "message": "Токен не найден"}
    
    # Игрок должен был сначала дернуть verify-token
    if quest.get("status") != "arrived":
        return {"success": False, "message": "Сначала подтвердите прибытие (verify-token)"}

    # Математика времени
    arrived_at = quest.get("arrived_at")
    
    # Защита от глюков формата даты
    if isinstance(arrived_at, str):
        arrived_at = datetime.datetime.fromisoformat(arrived_at)
        
    now = datetime.datetime.utcnow()
    seconds_passed = (now - arrived_at).total_seconds()
    
    REQUIRED_TIME = 60 # Время в секундах
    
    if seconds_passed >= REQUIRED_TIME:
        # ✅ КВЕСТ ВЫПОЛНЕН
        quests.update_one(
            {"_id": quest["_id"]}, 
            {"$set": {"status": "completed", "completed_at": now}}
        )
        return {"success": True, "message": "Квест выполнен!", "reward": 100}
    else:
        # ⏳ ЕЩЕ РАНО
        remaining = int(REQUIRED_TIME - seconds_passed)
        return {"success": False, "message": f"Жди еще {remaining} сек."}
