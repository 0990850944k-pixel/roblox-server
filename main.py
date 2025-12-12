from fastapi import FastAPI, HTTPException, Header, Depends, Request
from pydantic import BaseModel
from pymongo import MongoClient
import os
from dotenv import load_dotenv
import uuid
import certifi
import datetime
import time 

# 👇 БИБЛИОТЕКА ЗАЩИТЫ ОТ СПАМА (Rate Limiting)
# Не забудь добавить 'slowapi' в requirements.txt!
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

load_dotenv()
MONGO_URL = os.getenv("MONGO_URL")

# 🔐 КЛЮЧИ БЕЗОПАСНОСТИ
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME_IN_ENV") 
GAME_SERVER_SECRET = os.getenv("GAME_SERVER_SECRET", "MY_SUPER_SECRET_GAME_KEY_123") 

# Настройка лимитера (ограничитель запросов по IP)
limiter = Limiter(key_func=get_remote_address)

try:
    if not MONGO_URL: raise ValueError("Нет MONGO_URL")
    client = MongoClient(MONGO_URL, tlsCAFile=certifi.where()) 
    db = client["QuestNetworkDB"] 
    client.admin.command('ping')
    print("✅ MONGODB ПОДКЛЮЧЕНА!")
except Exception as e:
    print(f"❌ ОШИБКА БД: {e}")

app = FastAPI()

# Подключаем лимитер к приложению
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- 🛡 ЗАЩИТА 1: ПРОВЕРКА USER-AGENT (ТОЛЬКО ROBLOX) ---
async def verify_roblox_request(request: Request):
    user_agent = request.headers.get("user-agent", "")
    
    # 1. Если запрос от Роблокса (User-Agent содержит "Roblox/") -> Пропускаем
    # 2. Если запрос от ТЕБЯ (есть x-admin-secret) -> Пропускаем (для тестов через Postman)
    
    is_roblox = "Roblox/" in user_agent
    has_admin_secret = request.headers.get("x-admin-secret") == ADMIN_SECRET
    
    if not is_roblox and not has_admin_secret:
        print(f"⛔ Блокировка подозрительного запроса: {user_agent}")
        raise HTTPException(status_code=403, detail="Access Denied: Roblox Servers Only")

# --- 🛡 ЗАЩИТА 2: ПРОВЕРКА ИГРОВОГО КЛЮЧА ---
async def verify_game_secret(x_game_secret: str = Header(None)):
    if x_game_secret != GAME_SERVER_SECRET:
        raise HTTPException(status_code=403, detail="Invalid Game Secret Key")

# --- КОНФИГУРАЦИЯ ТИРОВ ---
# Я поправил выплаты, чтобы они росли вместе с ценой (экономика должна быть честной)
TIER_CONFIG = {
    1: {"cost": 10, "time": 60,  "payout": 7},
    2: {"cost": 15, "time": 180, "payout": 11}, # Было 7, стало 11
    3: {"cost": 25, "time": 300, "payout": 18}  # Было 7, стало 18
}

DAILY_LIMIT = 20

# --- МОДЕЛИ ---
class GameRegistration(BaseModel):
    ownerId: int; placeId: int; name: str; description: str; tier: int = 1; quest_type: str = "time"
class BuyVisits(BaseModel):
    ownerId: int; placeId: int; amount: int
class QuestStart(BaseModel):
    player_id: int; destination_place_id: int; source_place_id: int
class TokenVerification(BaseModel):
    token: str
class RewardClaim(BaseModel):
    player_id: int; current_place_id: int
class AddBalance(BaseModel):
    owner_id: int; amount: int

# --- ЭНДПОИНТЫ ---

# 1. ДАШБОРД
@app.get("/get-dashboard")
@limiter.limit("60/minute") # Макс 60 раз в минуту с одного IP
def get_dashboard(request: Request, ownerId: int, placeId: int):
    users = db["users"]
    games = db["games"]
    user = users.find_one({"_id": int(ownerId)})
    game = games.find_one({"placeId": int(placeId)})
    
    return {
        "success": True, 
        "balance": user.get("balance", 0) if user else 0, 
        "remaining_visits": game.get("remaining_visits", 0) if game else 0,
        "status": game.get("status", "inactive") if game else "not_registered",
        "tier": game.get("tier", 1) if game else 1
    }

# 2. ПОЛУЧЕНИЕ КВЕСТОВ
@app.get("/get-quests")
@limiter.limit("120/minute") # Лимит повыше, так как игроки часто обновляют список
def get_quests(request: Request):
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
            available_quests.append(game)
            
    return {"success": True, "quests": available_quests}

# 3. РЕГИСТРАЦИЯ ИГРЫ (Макс защита)
@app.post("/register-game", dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
@limiter.limit("10/minute")
def register_game(request: Request, data: GameRegistration):
    users_collection = db["users"]
    games_collection = db["games"]
    
    tier_data = TIER_CONFIG.get(data.tier, TIER_CONFIG[1])
    
    if not users_collection.find_one({"_id": data.ownerId}):
        users_collection.insert_one({"_id": data.ownerId, "balance": 0})

    games_collection.update_one(
        {"placeId": data.placeId},
        {"$set": {
            "ownerId": data.ownerId,
            "name": data.name,
            "description": data.description,
            "tier": data.tier,
            "visit_cost": tier_data["cost"],
            "time_required": tier_data["time"],
            "payout_amount": tier_data["payout"],
            "quest_type": data.quest_type,
            "status": "active",
            "last_updated": datetime.datetime.utcnow()
        },
        "$setOnInsert": {"remaining_visits": 0}}, 
        upsert=True
    )
    return {"success": True, "message": f"Registered Tier {data.tier}"}

# 4. ПОКУПКА ВИЗИТОВ
@app.post("/buy-visits", dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
@limiter.limit("20/minute")
def buy_visits(request: Request, data: BuyVisits):
    users = db["users"]
    games = db["games"]

    game = games.find_one({"placeId": data.placeId})
    if not game: return {"success": False, "message": "Game not registered"}
    
    price_per_visit = game.get("visit_cost", 10)
    total_cost = data.amount * price_per_visit
    
    user = users.find_one({"_id": data.ownerId})
    if not user: return {"success": False, "message": "User not found"}
    
    if user.get("balance", 0) < total_cost:
        return {"success": False, "message": f"Need {total_cost} credits"}
    
    users.update_one({"_id": data.ownerId}, {"$inc": {"balance": -total_cost}})
    games.update_one({"placeId": data.placeId}, {"$inc": {"remaining_visits": data.amount}})
    
    return {"success": True, "message": f"Bought {data.amount} visits"}

# 5. СТАРТ КВЕСТА
@app.post("/start-quest", dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
@limiter.limit("60/minute")
def start_quest(request: Request, data: QuestStart):
    quests = db["quests"]
    games = db["games"]
    today_start = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    
    game = games.find_one({"placeId": data.destination_place_id})
    if not game or game.get("remaining_visits", 0) <= 0:
        return {"success": False, "message": "Quests Out of Stock"}

    completed_today = quests.count_documents({
        "target_game": data.destination_place_id,
        "traffic_valid": True, 
        "timestamp": {"$gte": today_start}
    })
    
    if completed_today >= DAILY_LIMIT:
        return {"success": False, "message": "Daily Limit Reached"}

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

# 6. ПРОВЕРКА ТОКЕНА
@app.post("/verify-token", dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
@limiter.limit("60/minute")
def verify_token(request: Request, data: TokenVerification):
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
    
    return {
        "success": True, 
        "quest_type": game_info.get("quest_type", "time"),
        "time_required": game_info.get("time_required", 60)
    }

# 7. ПРОВЕРКА ТРАФИКА И ОПЛАТА
@app.post("/check-traffic", dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
@limiter.limit("60/minute")
def check_traffic(request: Request, data: TokenVerification):
    quests = db["quests"]
    games = db["games"]
    users = db["users"]
    
    quest = quests.find_one({"token": data.token})
    if not quest or quest.get("status") == "started":
        return {"success": False, "message": "Not arrived yet"}
    
    if quest.get("traffic_valid"):
         return {"success": True, "status": quest["status"], "quest_completed": True}

    target_game = games.find_one({"placeId": quest["target_game"]})
    required_time = target_game.get("time_required", 60)

    arrived_at = quest.get("arrived_at")
    if isinstance(arrived_at, str): arrived_at = datetime.datetime.fromisoformat(arrived_at)
    seconds_passed = (datetime.datetime.utcnow() - arrived_at).total_seconds()
    
    if seconds_passed >= required_time:
        # Списываем визит
        res = games.update_one(
            {"_id": target_game["_id"], "remaining_visits": {"$gt": 0}},
            {"$inc": {"remaining_visits": -1}}
        )
        
        # Платим источнику трафика (Source)
        if res.modified_count > 0:
            source_game = games.find_one({"placeId": quest["source_game"]})
            if source_game:
                users.update_one({"_id": source_game["ownerId"]}, {"$inc": {"balance": target_game.get("payout_amount", 7)}})
        
        update_data = {"traffic_valid": True, "completed_tier": target_game.get("tier", 1)}
        quest_type = target_game.get("quest_type", "time")
        
        # Если квест на время -> Сразу завершаем
        if quest_type == "time":
            update_data["status"] = "completed"
        # Если квест Action -> Оставляем статус 'arrived' (или 'action_pending'), ждем /complete-task
        
        quests.update_one({"_id": quest["_id"]}, {"$set": update_data})
        return {"success": True, "quest_completed": (quest_type == "time")}
    else:
        return {"success": False, "message": f"Wait {int(required_time - seconds_passed)}s"}

# 8. ЗАВЕРШЕНИЕ ЭКШЕНА
@app.post("/complete-task", dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
@limiter.limit("30/minute")
def complete_task(request: Request, data: TokenVerification):
    quests = db["quests"]
    games = db["games"]
    quest = quests.find_one({"token": data.token})
    
    if not quest or not quest.get("traffic_valid"):
         return {"success": False, "message": "Traffic not validated"}
    
    target_game = games.find_one({"placeId": quest["target_game"]})
    
    quests.update_one(
        {"_id": quest["_id"]}, 
        {"$set": {"status": "completed", "completed_tier": target_game.get("tier", 1)}}
    )
    return {"success": True}

# 9. ПОЛУЧЕНИЕ НАГРАД
@app.post("/claim-rewards", dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
@limiter.limit("30/minute")
def claim_rewards(request: Request, data: RewardClaim):
    quests = db["quests"]
    pending_quests = list(quests.find({
        "player_id": data.player_id,
        "status": "completed",
        "source_game": data.current_place_id 
    }))
    
    completed_tiers = []
    ids_to_update = []
    
    for q in pending_quests:
        completed_tiers.append(q.get("completed_tier", 1))
        ids_to_update.append(q["_id"])
        
    if ids_to_update:
        quests.update_many(
            {"_id": {"$in": ids_to_update}},
            {"$set": {"status": "claimed"}}
        )
    return {"success": True, "tiers": completed_tiers}

# 10. АДМИН: НАЧИСЛЕНИЕ (Без User-Agent, чтобы ты мог тестить через Postman)
@app.post("/admin/add-balance")
def add_balance(data: AddBalance, x_admin_secret: str = Header(None)):
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid Admin Secret")
    db["users"].update_one(
        {"_id": data.owner_id},
        {"$inc": {"balance": data.amount}},
        upsert=True
    )
    return {"success": True}
