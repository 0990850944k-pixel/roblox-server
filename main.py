from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from pymongo import MongoClient
import os
from dotenv import load_dotenv
import uuid
import certifi
import datetime

load_dotenv()
MONGO_URL = os.getenv("MONGO_URL")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME_IN_ENV") # Секретный ключ для админки

try:
    if not MONGO_URL: raise ValueError("Нет MONGO_URL")
    client = MongoClient(MONGO_URL, tlsCAFile=certifi.where()) 
    db = client["QuestNetworkDB"] 
    client.admin.command('ping')
    print("✅ MONGODB ПОДКЛЮЧЕНА!")
except Exception as e:
    print(f"❌ ОШИБКА БД: {e}")

app = FastAPI()

# --- КОНФИГУРАЦИЯ ТИРОВ ---
# cost: Цена для рекламодателя (кредиты)
# time: Время удержания (секунды)
# payout: Выплата хосту, откуда пришел игрок (кредиты)
TIER_CONFIG = {
    1: {"cost": 10, "time": 60,  "payout": 7},
    2: {"cost": 15, "time": 180, "payout": 11},
    3: {"cost": 25, "time": 300, "payout": 18}
}

DAILY_LIMIT = 20        # Лимит выполнений одним игроком в день

# --- МОДЕЛИ ДАННЫХ ---

class GameRegistration(BaseModel):
    ownerId: int        
    placeId: int
    name: str
    description: str
    tier: int = 1           # Теперь мы принимаем Тир (1, 2, 3)
    quest_type: str = "time"
    # reward и time_required больше не нужны во входных данных, сервер их сам определит

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

class AddBalance(BaseModel):
    owner_id: int
    amount: int

# --- ЭНДПОИНТЫ ---

# 1. ДАШБОРД
@app.get("/get-dashboard")
def get_dashboard(ownerId: int, placeId: int):
    users = db["users"]
    games = db["games"]
    
    user = users.find_one({"_id": int(ownerId)})
    game = games.find_one({"placeId": int(placeId)})
    
    balance = user.get("balance", 0) if user else 0
    visits = game.get("remaining_visits", 0) if game else 0
    status = game.get("status", "inactive") if game else "not_registered"
    # Добавляем инфо о тире для отображения
    tier = game.get("tier", 1) if game else 1
    
    return {
        "success": True, 
        "balance": balance, 
        "remaining_visits": visits,
        "status": status,
        "tier": tier
    }

# 2. ПОЛУЧЕНИЕ КВЕСТОВ
@app.get("/get-quests")
def get_quests():
    games_collection = db["games"]
    quests_collection = db["quests"]
    
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
            # Можно добавить форматированное время для отображения в UI
            game['display_time'] = f"{game.get('time_required', 60)}s"
            available_quests.append(game)
            
    return {"success": True, "quests": available_quests}

# 3. РЕГИСТРАЦИЯ ИГРЫ (ОБНОВЛЕНО ПОД ТИРЫ)
@app.post("/register-game")
def register_game(data: GameRegistration):
    users_collection = db["users"]
    games_collection = db["games"]
    
    # 1. Определяем настройки по Тиру (если тир не 1-3, берем 1)
    tier_data = TIER_CONFIG.get(data.tier, TIER_CONFIG[1])
    
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
            
            "tier": data.tier,                  # Сохраняем Тир
            "visit_cost": tier_data["cost"],    # Сохраняем цену покупки
            "time_required": tier_data["time"], # Сохраняем время выполнения
            "payout_amount": tier_data["payout"], # Сохраняем выплату хосту
            
            "quest_type": data.quest_type,
            "status": "active",
            "last_updated": datetime.datetime.utcnow()
        },
        "$setOnInsert": {"remaining_visits": 0}}, 
        upsert=True
    )
    return {
        "success": True, 
        "message": f"Registered Tier {data.tier} (Time: {tier_data['time']}s)"
    }

# 4. ПОКУПКА ВИЗИТОВ (ЦЕНА ТЕПЕРЬ ДИНАМИЧЕСКАЯ)
@app.post("/buy-visits")
def buy_visits(data: BuyVisits):
    users = db["users"]
    games = db["games"]

    # Сначала получаем игру, чтобы узнать цену за визит
    game = games.find_one({"placeId": data.placeId})
    if not game: return {"success": False, "message": "Game not registered"}
    
    price_per_visit = game.get("visit_cost", 10) # Дефолт 10, если старая запись
    total_cost = data.amount * price_per_visit
    
    user = users.find_one({"_id": data.ownerId})
    if not user: return {"success": False, "message": "User not found"}
    
    if user.get("balance", 0) < total_cost:
        return {"success": False, "message": f"Нужно {total_cost} кр. (Тир {game.get('tier', 1)})"}
    
    users.update_one({"_id": data.ownerId}, {"$inc": {"balance": -total_cost}})
    games.update_one({"placeId": data.placeId}, {"$inc": {"remaining_visits": data.amount}})
    
    return {"success": True, "message": f"Куплено {data.amount} визитов за {total_cost} кр."}

# 5. СТАРТ КВЕСТА
@app.post("/start-quest")
def start_quest(data: QuestStart):
    quests = db["quests"]
    games = db["games"]
    today_start = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    
    game = games.find_one({"placeId": data.destination_place_id})
    if not game or game.get("remaining_visits", 0) <= 0:
        return {"success": False, "message": "Квесты закончились!"}

    completed_today = quests.count_documents({
        "target_game": data.destination_place_id,
        "traffic_valid": True, 
        "timestamp": {"$gte": today_start}
    })
    
    if completed_today >= DAILY_LIMIT:
        return {"success": False, "message": "Лимит исчерпан"}

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

# 6. ПРОВЕРКА ТОКЕНА (ВОЗВРАЩАЕМ ВРЕМЯ ВЫПОЛНЕНИЯ)
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
    
    quest_type = game_info.get("quest_type", "time")
    quest_desc = game_info.get("description", "Quest")
    # Важно: отдаем клиенту нужное время для таймера
    time_required = game_info.get("time_required", 60)
    
    return {
        "success": True, 
        "quest_type": quest_type, 
        "description": quest_desc,
        "time_required": time_required 
    }

# 7. ПРОВЕРКА ТРАФИКА (ДИНАМИЧЕСКОЕ ВРЕМЯ + ВЫПЛАТА ХОСТУ)
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

    # Получаем требования целевой игры
    target_game = games.find_one({"placeId": quest["target_game"]})
    if not target_game: return {"success": False, "message": "Game deleted"}

    required_time = target_game.get("time_required", 60)

    arrived_at = quest.get("arrived_at")
    if isinstance(arrived_at, str): arrived_at = datetime.datetime.fromisoformat(arrived_at)
    seconds_passed = (datetime.datetime.utcnow() - arrived_at).total_seconds()
    
    if seconds_passed >= required_time:
        # 1. Списываем визит (если есть)
        res = games.update_one(
            {"_id": target_game["_id"], "remaining_visits": {"$gt": 0}},
            {"$inc": {"remaining_visits": -1}}
        )
        
        # 2. Если списание прошло успешно, платим Хосту
        if res.modified_count > 0:
            source_game = games.find_one({"placeId": quest["source_game"]})
            if source_game:
                host_owner_id = source_game["ownerId"]
                # Платим сумму, указанную в конфиге игры (payout_amount)
                payout = target_game.get("payout_amount", 7)
                users.update_one({"_id": host_owner_id}, {"$inc": {"balance": payout}})
        
        # 3. Обновляем статус квеста
        update_data = {
            "traffic_valid": True,
            "completed_tier": target_game.get("tier", 1) # Запоминаем, какой тир выполнил игрок
        }
        
        quest_type = target_game.get("quest_type", "time")
        if quest_type == "time":
            update_data["status"] = "completed"
            message = "Квест выполнен!"
        else:
            message = "Время вышло. Выполните действие!" # Для Action квестов

        quests.update_one({"_id": quest["_id"]}, {"$set": update_data})
        
        return {"success": True, "message": message, "quest_completed": (quest_type == "time")}
    else:
        return {"success": False, "message": f"Wait {int(required_time - seconds_passed)}s"}

# 8. ЗАВЕРШЕНИЕ ЭКШЕНА (Без изменений, но сохраняет тир)
@app.post("/complete-task")
def complete_task(data: TokenVerification):
    quests = db["quests"]
    games = db["games"]
    
    quest = quests.find_one({"token": data.token})
    if not quest: return {"success": False}
    
    if not quest.get("traffic_valid"):
         return {"success": False, "message": "Сначала таймер!"}
    
    target_game = games.find_one({"placeId": quest["target_game"]})
    quest_type = target_game.get("quest_type", "time")
    
    if quest_type == "time":
         return {"success": True, "message": "Already done"}

    quests.update_one(
        {"_id": quest["_id"]}, 
        {"$set": {
            "status": "completed", 
            "completed_at": datetime.datetime.utcnow(),
            "completed_tier": target_game.get("tier", 1) # На всякий случай обновляем тир
        }}
    )
    return {"success": True, "message": "Задание выполнено!"}

# 9. ПОЛУЧЕНИЕ НАГРАД (ВОЗВРАЩАЕТ СПИСОК ТИРОВ)
@app.post("/claim-rewards")
def claim_rewards(data: RewardClaim):
    quests = db["quests"]
    
    # Ищем выполненные квесты для этого Source Game
    pending_quests = list(quests.find({
        "player_id": data.player_id,
        "status": "completed",
        "source_game": data.current_place_id 
    }))
    
    completed_tiers = []
    ids_to_update = []
    
    for q in pending_quests:
        completed_tiers.append(q.get("completed_tier", 1)) # Собираем тиры
        ids_to_update.append(q["_id"])
        
    if ids_to_update:
        quests.update_many(
            {"_id": {"$in": ids_to_update}},
            {"$set": {"status": "claimed", "claimed_at": datetime.datetime.utcnow()}}
        )
        # Возвращаем массив тиров, чтобы Roblox сам решил, сколько платить золота
        return {"success": True, "tiers": completed_tiers}
    else:
        return {"success": True, "tiers": []}

# 10. АДМИН: НАЧИСЛЕНИЕ БАЛАНСА (С ЗАЩИТОЙ)
@app.post("/admin/add-balance")
def add_balance(data: AddBalance, x_admin_secret: str = Header(None)):
    # Проверка пароля из Headers
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid Secret Key")

    db["users"].update_one(
        {"_id": data.owner_id},
        {"$inc": {"balance": data.amount}},
        upsert=True
    )
    return {"success": True, "message": f"Начислено {data.amount} кр."}
