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
    name: str
    description: str
    reward: int
    time_required: int

class QuestStart(BaseModel):
    api_key: str            
    player_id: int          
    destination_place_id: int
    source_place_id: int    # 👈 НОВОЕ: Откуда пришел игрок

class TokenVerification(BaseModel):
    token: str

class RewardClaim(BaseModel):
    player_id: int
    current_place_id: int   # 👈 НОВОЕ: Где сейчас игрок (чтобы выдать награду в правильной игре)

# --- 3. ЭНДПОИНТЫ ---

@app.get("/")
def home():
    return {"status": "Offer Wall Online"}

@app.get("/get-quests")
def get_quests():
    games_collection = db["games"]
    quests = list(games_collection.find({"status": "active"}, {"_id": 0}))
    return {"success": True, "quests": quests}

@app.post("/register-game")
def register_game(data: GameRegistration):
    users_collection = db["users"]
    games_collection = db["games"]
    
    # Регистрация владельца (если нет)
    existing_user = users_collection.find_one({"_id": data.ownerId})
    api_key = existing_user["api_key"] if existing_user else "SK_" + str(uuid.uuid4()).replace("-", "").upper()
    
    if not existing_user:
        users_collection.insert_one({"_id": data.ownerId, "api_key": api_key, "balance": 0})

    # Сохраняем игру (Рекламное объявление)
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
        upsert=True
    )
    return {"success": True, "api_key": api_key}

# 👇 СТАРТ: ЗАПОМИНАЕМ, ОТКУДА ПРИШЕЛ ИГРОК
@app.post("/start-quest")
def start_quest(data: QuestStart):
    quests = db["quests"]
    
    # Тут можно добавить проверку API Key, если SDK интегрирован везде
    
    token = str(uuid.uuid4())
    
    quests.insert_one({
        "token": token,
        "player_id": data.player_id,
        "source_game": data.source_place_id,      # 👈 Запоминаем "Игру А"
        "target_game": data.destination_place_id, # 👈 Запоминаем "Игру Б"
        "status": "started",
        "timestamp": datetime.datetime.utcnow()
    })
    
    print(f"🚀 Игрок {data.player_id} начал квест из игры {data.source_place_id} в {data.destination_place_id}")
    return {"success": True, "token": token}

@app.post("/verify-token")
def verify_token(data: TokenVerification):
    quests = db["quests"]
    quest = quests.find_one({"token": data.token})
    
    if not quest: return {"success": False, "message": "Token not found"}
    if quest["status"] != "started": return {"success": False, "message": "Used token"}
        
    quests.update_one(
        {"_id": quest["_id"]}, 
        {"$set": {"status": "arrived", "arrived_at": datetime.datetime.utcnow()}}
    )
    # Возвращаем ID исходной игры, чтобы знать, куда возвращать игрока
    return {"success": True, "player_id": quest["player_id"], "return_to": quest["source_game"]}

@app.post("/check-timer")
def check_timer(data: TokenVerification):
    quests = db["quests"]
    games = db["games"]
    
    quest = quests.find_one({"token": data.token})
    if not quest or quest.get("status") != "arrived":
        return {"success": False, "message": "Not arrived"}

    # Проверка времени
    game_info = games.find_one({"placeId": quest["target_game"]})
    REQUIRED_TIME = game_info["time_required"] if game_info else 60
    
    arrived_at = quest.get("arrived_at")
    if isinstance(arrived_at, str): arrived_at = datetime.datetime.fromisoformat(arrived_at)
    
    seconds_passed = (datetime.datetime.utcnow() - arrived_at).total_seconds()
    
    if seconds_passed >= REQUIRED_TIME:
        quests.update_one({"_id": quest["_id"]}, {"$set": {"status": "completed", "completed_at": datetime.datetime.utcnow()}})
        return {"success": True, "message": "Квест выполнен! Возвращайся за наградой.", "return_id": quest["source_game"]}
    else:
        return {"success": False, "message": f"Осталось {int(REQUIRED_TIME - seconds_passed)} сек."}

# 👇 ВЫДАЧА НАГРАДЫ (Только если игрок вернулся в Игру А)
@app.post("/claim-rewards")
def claim_rewards(data: RewardClaim):
    quests = db["quests"]
    games = db["games"]
    
    # Ищем квесты, которые выполнены, но не оплачены, И которые были начаты ИМЕННО В ЭТОЙ ИГРЕ
    pending_quests = list(quests.find({
        "player_id": data.player_id,
        "status": "completed",
        "source_game": data.current_place_id # 👈 Критически важно!
    }))
    
    total_reward = 0
    ids_to_update = []
    
    for q in pending_quests:
        g = games.find_one({"placeId": q["target_game"]})
        reward = g["reward"] if g else 0
        total_reward += reward
        ids_to_update.append(q["_id"])
    
    if ids_to_update:
        quests.update_many(
            {"_id": {"$in": ids_to_update}},
            {"$set": {"status": "claimed", "claimed_at": datetime.datetime.utcnow()}}
        )
        return {"success": True, "reward": total_reward}
    else:
        return {"success": True, "reward": 0}
