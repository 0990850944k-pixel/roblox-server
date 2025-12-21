import os
import time
import uuid
import datetime
import logging

# Сторонние библиотеки
import certifi
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from pydantic import BaseModel
from pymongo import MongoClient

# Защита от спама
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# --- 1. НАСТРОЙКИ И КОНФИГУРАЦИЯ ---
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("QuestNetwork")

# Переменные окружения
MONGO_URL = os.getenv("MONGO_URL")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME_IN_ENV")
GAME_SERVER_SECRET = os.getenv("GAME_SERVER_SECRET", "MY_SUPER_SECRET_GAME_KEY_123")

# Экономика и Лимиты
DAILY_LIMIT = 20
STARTING_TEST_BALANCE = 500
AUTO_APPROVE_VISITS = 500

TIER_CONFIG = {
    1: {"cost": 8, "time": 60,  "payout": 6},
    2: {"cost": 15, "time": 180, "payout": 11},
    3: {"cost": 30, "time": 300, "payout": 22}
}

# --- 2. ПОДКЛЮЧЕНИЕ БАЗЫ ДАННЫХ ---
limiter = Limiter(key_func=get_remote_address)

try:
    if not MONGO_URL:
        raise ValueError("Переменная MONGO_URL не найдена!")
    
    # tlsCAFile нужен для безопасного подключения к Atlas
    client = MongoClient(MONGO_URL, tlsCAFile=certifi.where())
    db = client["QuestNetworkDB"]
    client.admin.command('ping')
    logger.info("✅ MONGODB УСПЕШНО ПОДКЛЮЧЕНА!")

    users_col = db["users"]
    games_col = db["games"]
    quests_col = db["quests"]

except Exception as e:
    logger.error(f"❌ КРИТИЧЕСКАЯ ОШИБКА БД: {e}")

app = FastAPI(title="Quest Network API", version="2.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# --- 3. ФУНКЦИИ ЗАЩИТЫ ---

async def verify_roblox_request(request: Request):
    """Пропускает только запросы от Roblox серверов или Админа."""
    user_agent = request.headers.get("user-agent", "")
    is_roblox = "Roblox/" in user_agent
    
    has_admin_secret = request.headers.get("x-admin-secret") == ADMIN_SECRET
    has_game_secret = request.headers.get("x-game-secret") == GAME_SERVER_SECRET
    
    if not is_roblox and not has_admin_secret and not has_game_secret:
        raise HTTPException(status_code=403, detail="Roblox Only")

async def verify_game_secret(x_game_secret: str = Header(None)):
    if x_game_secret != GAME_SERVER_SECRET:
        raise HTTPException(status_code=403, detail="Invalid Secret")

async def get_roblox_visits(place_id: int) -> int:
    """Получает реальные визиты игры через API Roblox."""
    try:
        async with httpx.AsyncClient() as client:
            url_univ = f"https://apis.roblox.com/universes/v1/places/{place_id}/universe"
            resp_univ = await client.get(url_univ)
            if resp_univ.status_code != 200: return 0
            universe_id = resp_univ.json().get("universeId")
            
            url_info = f"https://games.roblox.com/v1/games?universeIds={universe_id}"
            resp_info = await client.get(url_info)
            if resp_info.status_code != 200: return 0
            
            data = resp_info.json()
            if data.get("data"):
                return data["data"][0].get("visits", 0)
    except Exception as e:
        logger.warning(f"Ошибка API Roblox: {e}")
        return 0
    return 0


# --- 4. МОДЕЛИ DTO ---

class GameRegistration(BaseModel):
    ownerId: int
    placeId: int
    name: str
    description: str
    tier: int = 1
    quest_type: str = "time"
    # visits поле передавать не обязательно, мы его сами проверим, но если клиент шлет - ок

class BuyVisits(BaseModel):
    ownerId: int
    placeId: int
    amount: int

class QuestStart(BaseModel):
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

class AdminDecision(BaseModel):
    placeId: int
    action: str 


# --- 5. API ЭНДПОИНТЫ ---

# === DASHBOARD ===

@app.get("/get-dashboard", tags=["Dashboard"])
@limiter.limit("60/minute") 
def get_dashboard(request: Request, ownerId: int, placeId: int):
    # Работа с пользователем
    user = users_col.find_one({"_id": int(ownerId)})
    
    if not user:
        users_col.insert_one({"_id": int(ownerId), "balance": 0, "test_balance": STARTING_TEST_BALANCE})
        user = {"balance": 0, "test_balance": STARTING_TEST_BALANCE}
    
    # Миграция для старых юзеров
    if "test_balance" not in user:
        users_col.update_one({"_id": int(ownerId)}, {"$set": {"test_balance": STARTING_TEST_BALANCE}})
        user["test_balance"] = STARTING_TEST_BALANCE

    game = games_col.find_one({"placeId": int(placeId)})
    
    return {
        "success": True, 
        "balance": user.get("balance", 0), 
        "test_balance": user.get("test_balance", 0), 
        "remaining_visits": game.get("remaining_visits", 0) if game else 0,
        "status": game.get("status", "not_registered") if game else "not_registered",
        "tier": game.get("tier", 1) if game else 1
    }


# === GAME MANAGEMENT ===

@app.post("/register-game", tags=["Game Management"], dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
@limiter.limit("10/minute")
async def register_game(request: Request, data: GameRegistration):
    # Создаем юзера, если нет
    users_col.update_one(
        {"_id": data.ownerId}, 
        {"$setOnInsert": {"balance": 0, "test_balance": STARTING_TEST_BALANCE}}, 
        upsert=True
    )

    existing_game = games_col.find_one({"placeId": data.placeId})
    current_status = existing_game.get("status", "inactive") if existing_game else "inactive"
    
    if current_status == "active":
        new_status = "active"
        msg = "(Updated)"
    else:
        real_visits = await get_roblox_visits(data.placeId)
        if real_visits >= AUTO_APPROVE_VISITS:
            new_status = "active"
            msg = "(Auto-Approved)"
        else:
            new_status = "pending"
            msg = "(Sent Review)"
            
    tier_info = TIER_CONFIG.get(data.tier, TIER_CONFIG[1])

    games_col.update_one(
        {"placeId": data.placeId},
        {"$set": {
            "ownerId": data.ownerId, 
            "name": data.name, 
            "description": data.description,
            "tier": data.tier, 
            "visit_cost": tier_info["cost"], 
            "time_required": tier_info["time"],
            "payout_amount": tier_info["payout"], 
            "quest_type": data.quest_type,
            "status": new_status, 
            "last_updated": datetime.datetime.utcnow()
        },
        "$setOnInsert": {"remaining_visits": 0}}, 
        upsert=True
    )
    return {"success": True, "message": f"Registered {msg}", "status": new_status}


@app.post("/buy-visits", tags=["Game Management"], dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
@limiter.limit("30/minute")
def buy_visits(request: Request, data: BuyVisits):
    game = games_col.find_one({"placeId": data.placeId})
    if not game: return {"success": False, "message": "Game not registered"}
    
    if game.get("status") != "active":
        return {"success": False, "message": "⛔ Game is under Review."}
    
    # Расчет стоимости
    cost_per_visit = game.get("visit_cost", 8)
    total_cost = data.amount * cost_per_visit
    
    user = users_col.find_one({"_id": data.ownerId})
    if not user: return {"success": False, "message": "User not found"}
    
    real_bal = user.get("balance", 0)
    test_bal = user.get("test_balance", 0)
    
    # Сначала тратим тестовые, потом реальные
    to_pay_test = min(test_bal, total_cost)
    to_pay_real = total_cost - to_pay_test
    
    if real_bal < to_pay_real:
        return {"success": False, "message": f"Need {total_cost}. Have {test_bal} Test + {real_bal} Real."}
    
    # Транзакция (атомарно было бы лучше, но для MVP пойдет)
    if to_pay_test > 0: 
        users_col.update_one({"_id": data.ownerId}, {"$inc": {"test_balance": -to_pay_test}})
    if to_pay_real > 0: 
        users_col.update_one({"_id": data.ownerId}, {"$inc": {"balance": -to_pay_real}})
        
    games_col.update_one({"placeId": data.placeId}, {"$inc": {"remaining_visits": data.amount}})
    
    return {"success": True, "message": f"Paid (Test:{to_pay_test}, Real:{to_pay_real})"}


# === QUESTS ===

@app.get("/get-quests", tags=["Quests"])
@limiter.limit("120/minute")
def get_quests(request: Request):
    # Берем активные игры, у которых есть визиты
    # Важно: {"_id": 0} чтобы не крашился JSON
    all_active_games = list(games_col.find(
        {"status": "active", "remaining_visits": {"$gt": 0}}, 
        {"_id": 0}
    ))
    available_quests = []
    
    today_start = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Фильтрация по лимитам (можно оптимизировать агрегацией в будущем)
    for game in all_active_games:
        completed_count = quests_col.count_documents({
            "target_game": game.get("placeId"), 
            "traffic_valid": True, 
            "timestamp": {"$gte": today_start}
        })
        if completed_count < DAILY_LIMIT: 
            available_quests.append(game)
            
    return {"success": True, "quests": available_quests}


@app.post("/start-quest", tags=["Quests"], dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
@limiter.limit("60/minute")
def start_quest(request: Request, data: QuestStart):
    game = games_col.find_one({"placeId": data.destination_place_id})
    
    if not game or game.get("remaining_visits", 0) <= 0 or game.get("status") != "active":
        return {"success": False, "message": "Unavailable"}
    
    token = str(uuid.uuid4())
    quests_col.insert_one({
        "token": token, 
        "player_id": data.player_id, 
        "source_game": data.source_place_id, 
        "target_game": data.destination_place_id, 
        "status": "started", 
        "traffic_valid": False, 
        "timestamp": datetime.datetime.utcnow() # Это naive время (без часового пояса)
    })
    return {"success": True, "token": token}


@app.post("/verify-token", tags=["Quests"], dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
def verify_token(request: Request, data: TokenVerification):
    quest = quests_col.find_one({"token": data.token})
    
    if not quest or quest["status"] != "started": 
        return {"success": False, "message": "Invalid token state"}
    
    # Фиксируем время прибытия
    quests_col.update_one(
        {"_id": quest["_id"]}, 
        {"$set": {"status": "arrived", "arrived_at": datetime.datetime.utcnow()}}
    )
    
    game = games_col.find_one({"placeId": quest["target_game"]})
    
    return {
        "success": True, 
        "quest_type": game.get("quest_type", "time"), 
        "time_required": game.get("time_required", 60)
    }


@app.post("/check-traffic", tags=["Quests"], dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
def check_traffic(request: Request, data: TokenVerification):
    quest = quests_col.find_one({"token": data.token})
    
    if not quest: 
        return {"success": False}
    
    # Если уже выполнено - сразу отдаем успех
    if quest.get("traffic_valid"): 
        return {"success": True, "quest_completed": True}
    
    game = games_col.find_one({"placeId": quest["target_game"]})
    
    # === 🛡️ ИСПРАВЛЕНИЕ РАБОТЫ СО ВРЕМЕНЕМ ===
    arrived = quest.get("arrived_at")
    if not arrived:
        return {"success": False, "message": "Not arrived yet"}

    # 1. Если MongoDB вернула строку (иногда бывает), парсим её
    if isinstance(arrived, str):
        # Заменяем Z на +00:00 для совместимости со старыми версиями Python
        arrived = datetime.datetime.fromisoformat(arrived.replace('Z', '+00:00'))
    
    # 2. Приводим к naive (убираем часовой пояс), чтобы вычитать из utcnow()
    if arrived.tzinfo is not None:
        arrived = arrived.replace(tzinfo=None)
        
    now = datetime.datetime.utcnow()
    delta = (now - arrived).total_seconds()
    required_time = game.get("time_required", 60)
    
    logger.info(f"Token {data.token[:5]}... Spent: {delta:.1f}s / Required: {required_time}s")
    
    if delta >= required_time:
        # Сначала проверяем, есть ли еще визиты у игры
        if game.get("remaining_visits", 0) > 0:
            # Атомарное обновление: списываем визит, только если он есть
            res = games_col.update_one(
                {"_id": game["_id"], "remaining_visits": {"$gt": 0}}, 
                {"$inc": {"remaining_visits": -1}}
            )
            
            if res.modified_count > 0:
                # Платим источнику
                src = games_col.find_one({"placeId": quest["source_game"]})
                if src: 
                    users_col.update_one(
                        {"_id": src["ownerId"]}, 
                        {"$inc": {"balance": game.get("payout_amount", 6)}}
                    )
        
        # Обновляем статус квеста
        status = "completed" if game.get("quest_type") == "time" else "arrived"
        quests_col.update_one(
            {"_id": quest["_id"]}, 
            {"$set": {"traffic_valid": True, "completed_tier": game.get("tier", 1), "status": status}}
        )
        return {"success": True, "quest_completed": (game.get("quest_type") == "time")}
    
    return {"success": False, "message": f"Wait {required_time - delta:.1f}s more"}


@app.post("/complete-task", tags=["Quests"], dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
def complete_task(request: Request, data: TokenVerification):
    """Для Action-квестов: завершение после выполнения действия."""
    quest = quests_col.find_one({"token": data.token})
    if quest and quest.get("traffic_valid"):
        tier = games_col.find_one({"placeId": quest["target_game"]}).get("tier", 1)
        quests_col.update_one({"_id": quest["_id"]}, {"$set": {"status": "completed", "completed_tier": tier}})
        return {"success": True}
    return {"success": False}


@app.post("/claim-rewards", tags=["Quests"], dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
def claim_rewards(request: Request, data: RewardClaim):
    pending = list(quests_col.find({
        "player_id": data.player_id, 
        "status": "completed", 
        "source_game": data.current_place_id
    }))
    
    if pending: 
        # Помечаем как claimed, чтобы не выдать дважды
        quests_col.update_many({"_id": {"$in": [q["_id"] for q in pending]}}, {"$set": {"status": "claimed"}})
    
    return {"success": True, "tiers": [q.get("completed_tier", 1) for q in pending]}


# === ADMIN ===

@app.get("/admin/pending-games", tags=["Admin"])
def get_pending_games(x_admin_secret: str = Header(None)):
    if x_admin_secret != ADMIN_SECRET: raise HTTPException(status_code=403)
    # Обязательно убираем _id
    return {"games": list(games_col.find({"status": "pending"}, {"_id": 0}))}


@app.post("/admin/decide-game", tags=["Admin"])
def admin_decide_game(data: AdminDecision, x_admin_secret: str = Header(None)):
    if x_admin_secret != ADMIN_SECRET: raise HTTPException(status_code=403)
    
    new_status = "active" if data.action == "approve" else "rejected"
    res = games_col.update_one({"placeId": data.placeId}, {"$set": {"status": new_status}})
    
    return {"success": res.modified_count > 0, "status": new_status}


@app.post("/admin/add-balance", tags=["Admin"])
def add_balance(data: AddBalance, x_admin_secret: str = Header(None)):
    if x_admin_secret == ADMIN_SECRET:
        users_col.update_one({"_id": data.owner_id}, {"$inc": {"balance": data.amount}}, upsert=True)
        return {"success": True}
    raise HTTPException(status_code=403)
