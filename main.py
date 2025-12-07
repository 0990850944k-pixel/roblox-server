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
# 👇 ОБНОВИЛИ МОДЕЛЬ: ТЕПЕРЬ МЫ СОХРАНЯЕМ ДЕТАЛИ ИГРЫ
class GameRegistration(BaseModel):
    ownerId: int        
    placeId: int
    name: str           # Название квеста
    description: str    # Описание
    reward: int         # Сколько золота платит владелец
    time_required: int  # Сколько секунд надо сидеть

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

# 👇 ОБНОВЛЕНО: ТЕПЕРЬ БЕРЕМ ИЗ БАЗЫ, А НЕ ИЗ СПИСКА
@app.get("/get-quests")
def get_quests():
    games_collection = db["games"]
    # Берем все игры, которые "active" (можно добавить фильтрацию)
    # _id: 0 означает "не показывать технический ID базы", чтобы не мусорить
    quests = list(games_collection.find({}, {"_id": 0}))
    return {"success": True, "quests": quests}

# 👇 ОБНОВЛЕНО: РЕГИСТРАЦИЯ ПОЛНОЦЕННОЙ ИГРЫ
@app.post("/register-game")
def register_game(data: GameRegistration):
    users_collection = db["users"]
    games_collection = db["games"]
    
    # 1. Проверяем/Создаем пользователя (Владельца)
    existing_user = users_collection.find_one({"_id": data.ownerId})
    api_key = ""
    
    if existing_user:
        api_key = existing_user["api_key"]
    else:
        api_key = "SK_" + str(uuid.uuid4()).replace("-", "").upper()
        users_collection.insert_one({
            "_id": data.ownerId, 
            "api_key": api_key, 
            "balance": 0 
        })

    # 2. Сохраняем саму игру в коллекцию "games"
    # Если игра с таким ID уже есть — обновляем её данные
    games_collection.update_one(
        {"placeId": data.placeId},
        {"$set": {
            "ownerId": data.ownerId,
            "name": data.name,
            "description": data.description,
            "reward": data.reward,
            "time_required": data.time_required,
            "status": "active"
        }},
        upsert=True # Если нет - создать, если есть - обновить
    )
    
    print(f"✅ Игра {data.name} (ID: {data.placeId}) зарегистрирована!")
    return {"success": True, "api_key": api_key}

@app.post("/start-quest")
def start_quest(data: QuestStart):
    users = db["users"]
    quests = db["quests"]
    
    # Ищем владельца по API Key
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

@app.post("/verify-token")
def verify_token(data: TokenVerification):
    quests = db["quests"]
    quest = quests.find_one({"token": data.token})
    
    if not quest:
        return {"success": False, "message": "Токен не найден"}
    
    if quest["status"] != "started":
        return {"success": False, "message": "Токен уже использован"}
        
    quests.update_one(
        {"_id": quest["_id"]}, 
        {"$set": {"status": "arrived", "arrived_at": datetime.datetime.utcnow()}}
    )
    return {"success": True, "player_id": quest["player_id"]}

@app.post("/check-timer")
def check_timer(data: TokenVerification):
    quests = db["quests"]
    quest = quests.find_one({"token": data.token})
    
    if not quest: return {"success": False, "message": "Токен не найден"}
    if quest.get("status") != "arrived": return {"success": False, "message": "Не подтверждено"}

    arrived_at = quest.get("arrived_at")
    if isinstance(arrived_at, str):
        arrived_at = datetime.datetime.fromisoformat(arrived_at)
        
    # В РЕАЛЬНОСТИ: Тут мы должны брать `time_required` из базы игры, а не хардкод 60
    # Но для теста пока оставим 60 или вытащим из квеста
    REQUIRED_TIME = 60 
    
    now = datetime.datetime.utcnow()
    seconds_passed = (now - arrived_at).total_seconds()
    
    if seconds_passed >= REQUIRED_TIME:
        quests.update_one({"_id": quest["_id"]}, {"$set": {"status": "completed"}})
        return {"success": True, "message": "Квест выполнен!", "reward": 100}
    else:
        return {"success": False, "message": f"Жди {int(REQUIRED_TIME - seconds_passed)} сек."}
