from fastapi import FastAPI, HTTPException, Header, Depends, Request
from pydantic import BaseModel
from pymongo import MongoClient
import os
from dotenv import load_dotenv
import uuid
import certifi
import datetime
import time 
import httpx 

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

load_dotenv()
MONGO_URL = os.getenv("MONGO_URL")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME_IN_ENV") 
GAME_SERVER_SECRET = os.getenv("GAME_SERVER_SECRET", "MY_SUPER_SECRET_GAME_KEY_123") 

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
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

TIER_CONFIG = {
    1: {"cost": 8, "time": 60,  "payout": 6},
    2: {"cost": 15, "time": 180, "payout": 11},
    3: {"cost": 30, "time": 300, "payout": 22}
}

DAILY_LIMIT = 20
STARTING_TEST_BALANCE = 500
AUTO_APPROVE_VISITS = 500

# --- 🛡 ЗАЩИТА (ИСПРАВЛЕНО) ---
async def verify_roblox_request(request: Request):
    user_agent = request.headers.get("user-agent", "")
    is_roblox = "Roblox/" in user_agent
    
    # Проверяем наличие ключей
    has_admin_secret = request.headers.get("x-admin-secret") == ADMIN_SECRET
    has_game_secret = request.headers.get("x-game-secret") == GAME_SERVER_SECRET # 👈 ДОБАВЛЕНО
    
    # Пропускаем, если это Роблокс ИЛИ есть правильный админ-ключ ИЛИ есть правильный игровой ключ
    if not is_roblox and not has_admin_secret and not has_game_secret:
        print(f"⛔ Blocked Request. UA: {user_agent}")
        raise HTTPException(status_code=403, detail="Roblox Only")

async def verify_game_secret(x_game_secret: str = Header(None)):
    if x_game_secret != GAME_SERVER_SECRET:
        raise HTTPException(status_code=403, detail="Invalid Secret")

async def get_roblox_visits(place_id: int):
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
    except Exception:
        return 0
    return 0

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
class AdminDecision(BaseModel):
    placeId: int; action: str 

# --- ЭНДПОИНТЫ ---

@app.get("/get-dashboard")
@limiter.limit("60/minute") 
def get_dashboard(request: Request, ownerId: int, placeId: int):
    users = db["users"]
    games = db["games"]
    user = users.find_one({"_id": int(ownerId)})
    game = games.find_one({"placeId": int(placeId)})
    
    return {
        "success": True, 
        "balance": user.get("balance", 0) if user else 0, 
        "test_balance": user.get("test_balance", 0) if user else 0, 
        "remaining_visits": game.get("remaining_visits", 0) if game else 0,
        "status": game.get("status", "not_registered") if game else "not_registered",
        "tier": game.get("tier", 1) if game else 1
    }

@app.get("/get-quests")
@limiter.limit("120/minute")
def get_quests(request: Request):
    all_active_games = list(db["games"].find({"status": "active", "remaining_visits": {"$gt": 0}}, {"_id": 0}))
    available_quests = []
    today_start = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    for game in all_active_games:
        completed = db["quests"].count_documents({"target_game": game.get("placeId"), "traffic_valid": True, "timestamp": {"$gte": today_start}})
        if completed < DAILY_LIMIT: available_quests.append(game)
    return {"success": True, "quests": available_quests}

@app.post("/register-game", dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
@limiter.limit("10/minute")
async def register_game(request: Request, data: GameRegistration):
    users = db["users"]
    games = db["games"]
    tier_data = TIER_CONFIG.get(data.tier, TIER_CONFIG[1])
    
    user = users.find_one({"_id": data.ownerId})
    if not user:
        users.insert_one({"_id": data.ownerId, "balance": 0, "test_balance": STARTING_TEST_BALANCE})
    elif "test_balance" not in user:
        users.update_one({"_id": data.ownerId}, {"$set": {"test_balance": STARTING_TEST_BALANCE}})

    existing_game = games.find_one({"placeId": data.placeId})
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

    games.update_one(
        {"placeId": data.placeId},
        {"$set": {
            "ownerId": data.ownerId, "name": data.name, "description": data.description,
            "tier": data.tier, "visit_cost": tier_data["cost"], "time_required": tier_data["time"],
            "payout_amount": tier_data["payout"], "quest_type": data.quest_type,
            "status": new_status, 
            "last_updated": datetime.datetime.utcnow()
        },
        "$setOnInsert": {"remaining_visits": 0}}, 
        upsert=True
    )
    return {"success": True, "message": f"Registered {msg}", "status": new_status}

@app.post("/buy-visits", dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
@limiter.limit("20/minute")
def buy_visits(request: Request, data: BuyVisits):
    users = db["users"]
    games = db["games"]

    game = games.find_one({"placeId": data.placeId})
    if not game: return {"success": False, "message": "Game not registered"}
    
    if game.get("status") != "active":
        return {"success": False, "message": "⛔ Game is under Review."}
    
    total_cost = data.amount * game.get("visit_cost", 10)
    user = users.find_one({"_id": data.ownerId})
    if not user: return {"success": False, "message": "User not found"}
    
    real_bal = user.get("balance", 0)
    test_bal = user.get("test_balance", 0)
    
    to_pay_test = min(test_bal, total_cost)
    to_pay_real = total_cost - to_pay_test
    
    if real_bal < to_pay_real:
        return {"success": False, "message": f"Need {total_cost}. Have {test_bal} Test + {real_bal} Real."}
    
    if to_pay_test > 0: users.update_one({"_id": data.ownerId}, {"$inc": {"test_balance": -to_pay_test}})
    if to_pay_real > 0: users.update_one({"_id": data.ownerId}, {"$inc": {"balance": -to_pay_real}})
        
    games.update_one({"placeId": data.placeId}, {"$inc": {"remaining_visits": data.amount}})
    return {"success": True, "message": f"Paid (Test:{to_pay_test}, Real:{to_pay_real})"}

@app.get("/admin/pending-games")
def get_pending_games(x_admin_secret: str = Header(None)):
    if x_admin_secret != ADMIN_SECRET: raise HTTPException(status_code=403)
    return {"games": list(db["games"].find({"status": "pending"}, {"_id": 0}))}

@app.post("/admin/decide-game")
def admin_decide_game(data: AdminDecision, x_admin_secret: str = Header(None)):
    if x_admin_secret != ADMIN_SECRET: raise HTTPException(status_code=403)
    new_status = "active" if data.action == "approve" else "rejected"
    res = db["games"].update_one({"placeId": data.placeId}, {"$set": {"status": new_status}})
    return {"success": res.modified_count > 0, "status": new_status}

@app.post("/start-quest", dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
@limiter.limit("60/minute")
def start_quest(request: Request, data: QuestStart):
    game = db["games"].find_one({"placeId": data.destination_place_id})
    if not game or game.get("remaining_visits", 0) <= 0 or game.get("status") != "active":
        return {"success": False, "message": "Unavailable"}
    today = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    if db["quests"].count_documents({"target_game": data.destination_place_id, "traffic_valid": True, "timestamp": {"$gte": today}}) >= DAILY_LIMIT:
        return {"success": False, "message": "Limit Reached"}
    token = str(uuid.uuid4())
    db["quests"].insert_one({"token": token, "player_id": data.player_id, "source_game": data.source_place_id, "target_game": data.destination_place_id, "status": "started", "traffic_valid": False, "timestamp": datetime.datetime.utcnow()})
    return {"success": True, "token": token}

@app.post("/verify-token", dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
def verify_token(request: Request, data: TokenVerification):
    quest = db["quests"].find_one({"token": data.token})
    if not quest or quest["status"] != "started": return {"success": False}
    db["quests"].update_one({"_id": quest["_id"]}, {"$set": {"status": "arrived", "arrived_at": datetime.datetime.utcnow()}})
    game = db["games"].find_one({"placeId": quest["target_game"]})
    return {"success": True, "quest_type": game.get("quest_type", "time"), "time_required": game.get("time_required", 60)}

@app.post("/check-traffic", dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
def check_traffic(request: Request, data: TokenVerification):
    quest = db["quests"].find_one({"token": data.token})
    if not quest or quest.get("status") == "started": return {"success": False}
    if quest.get("traffic_valid"): return {"success": True, "quest_completed": True}
    game = db["games"].find_one({"placeId": quest["target_game"]})
    arrived = quest.get("arrived_at")
    if isinstance(arrived, str): arrived = datetime.datetime.fromisoformat(arrived)
    if (datetime.datetime.utcnow() - arrived).total_seconds() >= game.get("time_required", 60):
        if db["games"].update_one({"_id": game["_id"], "remaining_visits": {"$gt": 0}}, {"$inc": {"remaining_visits": -1}}).modified_count > 0:
            src = db["games"].find_one({"placeId": quest["source_game"]})
            if src: db["users"].update_one({"_id": src["ownerId"]}, {"$inc": {"balance": game.get("payout_amount", 7)}})
        status = "completed" if game.get("quest_type") == "time" else "arrived"
        db["quests"].update_one({"_id": quest["_id"]}, {"$set": {"traffic_valid": True, "completed_tier": game.get("tier", 1), "status": status}})
        return {"success": True, "quest_completed": (game.get("quest_type") == "time")}
    return {"success": False}

@app.post("/complete-task", dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
def complete_task(request: Request, data: TokenVerification):
    quest = db["quests"].find_one({"token": data.token})
    if quest and quest.get("traffic_valid"):
        tier = db["games"].find_one({"placeId": quest["target_game"]}).get("tier", 1)
        db["quests"].update_one({"_id": quest["_id"]}, {"$set": {"status": "completed", "completed_tier": tier}})
        return {"success": True}
    return {"success": False}

@app.post("/claim-rewards", dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
def claim_rewards(request: Request, data: RewardClaim):
    pending = list(db["quests"].find({"player_id": data.player_id, "status": "completed", "source_game": data.current_place_id}))
    if pending: db["quests"].update_many({"_id": {"$in": [q["_id"] for q in pending]}}, {"$set": {"status": "claimed"}})
    return {"success": True, "tiers": [q.get("completed_tier", 1) for q in pending]}

@app.post("/admin/add-balance")
def add_balance(data: AddBalance, x_admin_secret: str = Header(None)):
    if x_admin_secret == ADMIN_SECRET:
        db["users"].update_one({"_id": data.owner_id}, {"$inc": {"balance": data.amount}}, upsert=True)
        return {"success": True}
    raise HTTPException(status_code=403)
