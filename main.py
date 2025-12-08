from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient
import os
from dotenv import load_dotenv
import uuid
import certifi
import datetime

load_dotenv()
MONGO_URL = os.getenv("MONGO_URL")

try:
    if not MONGO_URL: raise ValueError("Нет MONGO_URL")
    client = MongoClient(MONGO_URL, tlsCAFile=certifi.where()) 
    db = client["QuestNetworkDB"] 
    client.admin.command('ping')
    print("✅ MONGODB ПОДКЛЮЧЕНА!")
except Exception as e:
    print(f"❌ ОШИБКА БД: {e}")

app = FastAPI()
DAILY_LIMIT = 20 

# --- МОДЕЛИ ---
class GameRegistration(BaseModel):
    ownerId: int        
    placeId: int
    name: str
    description: str
    reward: int
    time_required: int
    quest_type: str = "time"  # 👈 НОВОЕ: "time" или "action"

class QuestStart(BaseModel):
    api_key: str            
    player_id: int          
    destination_place_id: int
    source_place_id: int

class TokenVerification(BaseModel):
    token: str

class RewardClaim(BaseModel):
    player_id: int
    current_place_id: int

# --- ЭНДПОИНТЫ ---

@app.get("/get-quests")
def get_quests():
    games_collection = db["games"]
    quests_collection = db["quests"]
    all_active_games = list(games_collection.find({"status": "active"}, {"_id": 0}))
    available_quests = []
    today_start = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    
    for game in all_active_games:
        place_id = game.get("placeId")
        completed_count = quests_collection.count_documents({
            "target_game": place_id,
            "traffic_valid": True, # 👈 Считаем только валидный трафик
            "timestamp": {"$gte": today_start}
        })
        if completed_count < DAILY_LIMIT:
            available_quests.append(game)
    return {"success": True, "quests": available_quests}

@app.post("/register-game")
def register_game(data: GameRegistration):
    users_collection = db["users"]
    games_collection = db["games"]
    
    existing_user = users_collection.find_one({"_id": data.ownerId})
    if existing_user:
        api_key = existing_user["api_key"]
    else:
        api_key = "SK_" + str(uuid.uuid4()).replace("-", "").upper()
        users_collection.insert_one({"_id": data.ownerId, "api_key": api_key, "balance": 0})

    games_collection.update_one(
        {"placeId": data.placeId},
        {"$set": {
            "ownerId": data.ownerId,
            "name": data.name,
            "description": data.description,
            "reward": data.reward,
            "time_required": data.time_required,
            "quest_type": data.quest_type, # 👈 Сохраняем тип квеста
            "status": "active",
            "last_updated": datetime.datetime.utcnow()
        }},
        upsert=True
    )
    return {"success": True, "api_key": api_key}

@app.post("/start-quest")
def start_quest(data: QuestStart):
    quests = db["quests"]
    today_start = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Лимит проверяем по валидному трафику
    completed_today = quests.count_documents({
        "target_game": data.destination_place_id,
        "traffic_valid": True, 
        "timestamp": {"$gte": today_start}
    })
    
    if completed_today >= DAILY_LIMIT:
        return {"success": False, "message": "Лимит на сегодня исчерпан"}

    token = str(uuid.uuid4())
    quests.insert_one({
        "token": token,
        "player_id": data.player_id,
        "source_game": data.source_place_id,
        "target_game": data.destination_place_id,
        "status": "started",
        "traffic_valid": False, # 👈 Пока трафик не засчитан
        "timestamp": datetime.datetime.utcnow()
    })
    return {"success": True, "token": token}

@app.post("/verify-token")
def verify_token(data: TokenVerification):
    quests = db["quests"]
    quest = quests.find_one({"token": data.token})
    
    if not quest: return {"success": False, "message": "Токен не найден"}
    if quest["status"] != "started": return {"success": False, "message": "Токен использован"}
        
    quests.update_one(
        {"_id": quest["_id"]}, 
        {"$set": {"status": "arrived", "arrived_at": datetime.datetime.utcnow()}}
    )
    
    # Возвращаем тип квеста, чтобы игра знала, что делать (Таймер или Босс)
    games = db["games"]
    game_info = games.find_one({"placeId": quest["target_game"]})
    quest_type = game_info["quest_type"] if game_info else "time"
    
    return {"success": True, "quest_type": quest_type}

# 👇 ГЛАВНОЕ ИЗМЕНЕНИЕ: ВАЛИДАЦИЯ ТРАФИКА (ДЕНЬГИ)
@app.post("/check-traffic")
def check_traffic(data: TokenVerification):
    quests = db["quests"]
    games = db["games"]
    
    quest = quests.find_one({"token": data.token})
    if not quest or quest.get("status") == "started":
        return {"success": False, "message": "Not arrived yet"}
    
    # Если уже валидировали, просто говорим ОК
    if quest.get("traffic_valid"):
         return {"success": True, "status": quest["status"]}

    # Проверка 60 секунд (ЖЕСТКАЯ)
    arrived_at = quest.get("arrived_at")
    if isinstance(arrived_at, str): arrived_at = datetime.datetime.fromisoformat(arrived_at)
    seconds_passed = (datetime.datetime.utcnow() - arrived_at).total_seconds()
    
    if seconds_passed >= 60: # Всегда 60 секунд для денег
        
        # 1. Фиксируем валидный трафик (Деньги)
        update_data = {"traffic_valid": True}
        
        # 2. Если квест НА ВРЕМЯ -> сразу завершаем его для игрока
        game_info = games.find_one({"placeId": quest["target_game"]})
        quest_type = game_info.get("quest_type", "time")
        
        if quest_type == "time":
            update_data["status"] = "completed"
            message = "Квест выполнен!"
        else:
            # Если ACTION, то статус остается 'arrived', ждем действия
            message = "Трафик засчитан. Ждем выполнения задания..."

        quests.update_one({"_id": quest["_id"]}, {"$set": update_data})
        
        return {"success": True, "message": message, "quest_completed": (quest_type == "time")}
    else:
        return {"success": False, "message": f"Осталось {int(60 - seconds_passed)} сек."}

# 👇 НОВЫЙ ЭНДПОИНТ: ВЫПОЛНЕНИЕ ДЕЙСТВИЯ (Босс, Кнопка и т.д.)
@app.post("/complete-task")
def complete_task(data: TokenVerification):
    quests = db["quests"]
    quest = quests.find_one({"token": data.token})
    
    if not quest: return {"success": False}
    
    # Защита: Нельзя выполнить задание, если не прошло 60 секунд (трафик не засчитан)
    # Или можно убрать это, если разрешаем быстрые спидраны
    if not quest.get("traffic_valid"):
         return {"success": False, "message": "Сначала проведите 60 секунд в игре!"}

    quests.update_one(
        {"_id": quest["_id"]}, 
        {"$set": {"status": "completed", "completed_at": datetime.datetime.utcnow()}}
    )
    return {"success": True, "message": "Задание выполнено!"}

@app.post("/claim-rewards")
def claim_rewards(data: RewardClaim):
    # (Тут код такой же, как был, он работает отлично)
    # ...
    # (Оставь старый код claim_rewards)
    quests = db["quests"]
    games = db["games"]
    pending_quests = list(quests.find({
        "player_id": data.player_id,
        "status": "completed",
        "source_game": data.current_place_id 
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
