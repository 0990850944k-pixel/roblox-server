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

# --- ЭКОНОМИКА ---
DAILY_LIMIT = 20        # Лимит выполнений одним игроком в день
PRICE_PER_VISIT = 10    # Цена покупки 1 визита (Платит Рекламодатель)
PAYOUT_TO_HOST = 7      # Награда Хосту за 1 визит

# --- МОДЕЛИ ДАННЫХ ---

class GameRegistration(BaseModel):
    ownerId: int        
    placeId: int
    name: str
    description: str
    reward: int
    time_required: int
    quest_type: str = "time"

class BuyVisits(BaseModel):
    ownerId: int
    placeId: int
    amount: int

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

# Модель для начисления баланса (чтобы принимать JSON)
class AddBalance(BaseModel):
    owner_id: int
    amount: int

# --- ЭНДПОИНТЫ ---

# 1. ДАШБОРД (Новое: нужно для админки в Роблоксе)
@app.get("/get-dashboard")
def get_dashboard(ownerId: int, placeId: int):
    users = db["users"]
    games = db["games"]
    
    user = users.find_one({"_id": int(ownerId)})
    game = games.find_one({"placeId": int(placeId)})
    
    balance = user.get("balance", 0) if user else 0
    visits = game.get("remaining_visits", 0) if game else 0
    status = game.get("status", "inactive") if game else "not_registered"
    
    return {
        "success": True, 
        "balance": balance, 
        "remaining_visits": visits,
        "status": status
    }

# 2. ПОЛУЧЕНИЕ КВЕСТОВ
@app.get("/get-quests")
def get_quests():
    games_collection = db["games"]
    quests_collection = db["quests"]
    
    # Показываем только активные игры с положительным балансом визитов
    all_active_games = list(games_collection.find({
        "status": "active",
        "remaining_visits": {"$gt": 0} 
    }, {"_id": 0}))
    
    available_quests = []
    today_start = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    
    for game in all_active_games:
        place_id = game.get("placeId")
        
        completed_count = quests_collection.count_documents({
            "target_game": place_id,
            "traffic_valid": True,
            "timestamp": {"$gte": today_start}
        })
        
        if completed_count < DAILY_LIMIT:
            available_quests.append(game)
            
    return {"success": True, "quests": available_quests}

# 3. РЕГИСТРАЦИЯ ИГРЫ
@app.post("/register-game")
def register_game(data: GameRegistration):
    users_collection = db["users"]
    games_collection = db["games"]
    
    if not users_collection.find_one({"_id": data.ownerId}):
        users_collection.insert_one({
            "_id": data.ownerId, 
            "api_key": "SK_" + str(uuid.uuid4()).hex.upper(), 
            "balance": 0
        })

    games_collection.update_one(
        {"placeId": data.placeId},
        {"$set": {
            "ownerId": data.ownerId,
            "name": data.name,
            "description": data.description,
            "reward": data.reward,
            "time_required": data.time_required,
            "quest_type": data.quest_type,
            "status": "active",
            "last_updated": datetime.datetime.utcnow()
        },
        "$setOnInsert": {"remaining_visits": 0}}, 
        upsert=True
    )
    return {"success": True, "message": "Game registered"}

# 4. ПОКУПКА ВИЗИТОВ (Списание с баланса -> Начисление визитов)
@app.post("/buy-visits")
def buy_visits(data: BuyVisits):
    users = db["users"]
    games = db["games"]

    cost = data.amount * PRICE_PER_VISIT
    
    user = users.find_one({"_id": data.ownerId})
    if not user: return {"success": False, "message": "User not found"}
    
    if user.get("balance", 0) < cost:
        return {"success": False, "message": f"Недостаточно средств! Нужно {cost} кр."}
    
    users.update_one({"_id": data.ownerId}, {"$inc": {"balance": -cost}})
    games.update_one({"placeId": data.placeId}, {"$inc": {"remaining_visits": data.amount}})
    
    return {"success": True, "message": f"Куплено {data.amount} визитов!"}

# 5. СТАРТ КВЕСТА
@app.post("/start-quest")
def start_quest(data: QuestStart):
    quests = db["quests"]
    games = db["games"]
    today_start = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    
    game = games.find_one({"placeId": data.destination_place_id})
    if not game or game.get("remaining_visits", 0) <= 0:
        return {"success": False, "message": "Квесты в эту игру закончились!"}

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
        "traffic_valid": False,
        "timestamp": datetime.datetime.utcnow()
    })
    return {"success": True, "token": token}

# 6. ПРОВЕРКА ТОКЕНА (Прибытие)
@app.post("/verify-token")
def verify_token(data: TokenVerification):
    quests = db["quests"]
    quest = quests.find_one({"token": data.token})
    
    if not quest: return {"success": False, "message": "Token Invalid"}
    if quest["status"] != "started": return {"success": False, "message": "Token Used"}
        
    quests.update_one(
        {"_id": quest["_id"]}, 
        {"$set": {"status": "arrived", "arrived_at": datetime.datetime.utcnow()}}
    )
    
    games = db["games"]
    game_info = games.find_one({"placeId": quest["target_game"]})
    quest_type = game_info["quest_type"] if game_info else "time"
    quest_desc = game_info.get("description", "Quest") if game_info else ""
    
    return {"success": True, "quest_type": quest_type, "description": quest_desc}

# 7. ПРОВЕРКА ТРАФИКА (Списание визита)
@app.post("/check-traffic")
def check_traffic(data: TokenVerification):
    quests = db["quests"]
    games = db["games"]
    users = db["users"]
    
    quest = quests.find_one({"token": data.token})
    if not quest or quest.get("status") == "started":
        return {"success": False, "message": "Not arrived yet"}
    
    if quest.get("traffic_valid"):
         return {"success": True, "status": quest["status"]}

    arrived_at = quest.get("arrived_at")
    if isinstance(arrived_at, str): arrived_at = datetime.datetime.fromisoformat(arrived_at)
    seconds_passed = (datetime.datetime.utcnow() - arrived_at).total_seconds()
    
    if seconds_passed >= 60:
        # Пытаемся списать визит (если они есть)
        target_game = games.find_one_and_update(
            {"placeId": quest["target_game"], "remaining_visits": {"$gt": 0}},
            {"$inc": {"remaining_visits": -1}},
            return_document=True
        )
        
        # Если target_game вернулся None, значит визиты кончились прямо сейчас.
        # Мы все равно засчитаем квест игроку, чтобы не было негатива, но не заплатим хосту.
        if not target_game:
            # Получаем инфо об игре без списания, просто чтобы узнать тип квеста
            target_game = games.find_one({"placeId": quest["target_game"]})
        else:
            # Если списание прошло успешно, платим Хосту
            source_game = games.find_one({"placeId": quest["source_game"]})
            if source_game:
                host_owner_id = source_game["ownerId"]
                users.update_one({"_id": host_owner_id}, {"$inc": {"balance": PAYOUT_TO_HOST}})
        
        update_data = {"traffic_valid": True}
        quest_type = target_game.get("quest_type", "time") if target_game else "time"
        
        if quest_type == "time":
            update_data["status"] = "completed"
            message = "Квест выполнен!"
        else:
            message = "Время вышло. Выполните действие!"

        quests.update_one({"_id": quest["_id"]}, {"$set": update_data})
        
        return {"success": True, "message": message, "quest_completed": (quest_type == "time")}
    else:
        return {"success": False, "message": f"Осталось {int(60 - seconds_passed)} сек."}

# 8. ЗАВЕРШЕНИЕ ЭКШЕНА
@app.post("/complete-task")
def complete_task(data: TokenVerification):
    quests = db["quests"]
    games = db["games"]
    
    quest = quests.find_one({"token": data.token})
    if not quest: return {"success": False}
    
    if not quest.get("traffic_valid"):
         return {"success": False, "message": "Сначала 60 сек!"}
    
    game_info = games.find_one({"placeId": quest["target_game"]})
    quest_type = game_info.get("quest_type", "time")
    
    if quest_type == "time":
         return {"success": True, "message": "Already done"}

    quests.update_one(
        {"_id": quest["_id"]}, 
        {"$set": {"status": "completed", "completed_at": datetime.datetime.utcnow()}}
    )
    return {"success": True, "message": "Задание выполнено!"}

# 9. ПОЛУЧЕНИЕ НАГРАД
@app.post("/claim-rewards")
def claim_rewards(data: RewardClaim):
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

# 10. АДМИН: НАЧИСЛЕНИЕ БАЛАНСА (Для покупок за Робуксы)
# Теперь использует модель AddBalance для приема JSON Body
@app.post("/admin/add-balance")
def add_balance(data: AddBalance):
    db["users"].update_one(
        {"_id": data.owner_id},
        {"$inc": {"balance": data.amount}},
        upsert=True
    )
    return {"success": True, "message": f"Начислено {data.amount} кр."}
