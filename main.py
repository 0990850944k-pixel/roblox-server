import os
import time
import uuid
import datetime
import logging

import certifi
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from pydantic import BaseModel
from pymongo import MongoClient
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("QuestNetwork")

MONGO_URL = os.getenv("MONGO_URL")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME_IN_ENV")
GAME_SERVER_SECRET = os.getenv("GAME_SERVER_SECRET", "MY_SUPER_SECRET_GAME_KEY_123")

# Лимиты
DAILY_LIMIT = 20
STARTING_TEST_BALANCE = 500
# ⚠️ Оставляем 0 для тестов, чтобы игры появлялись сразу
AUTO_APPROVE_VISITS = 0 

TIER_CONFIG = {
    1: {"cost": 8, "time": 60,  "payout": 6},
    2: {"cost": 15, "time": 180, "payout": 11},
    3: {"cost": 30, "time": 300, "payout": 22}
}

limiter = Limiter(key_func=get_remote_address)

try:
    if not MONGO_URL: raise ValueError("No MONGO_URL")
    client = MongoClient(MONGO_URL, tlsCAFile=certifi.where())
    db = client["QuestNetworkDB"]
    users_col = db["users"]
    games_col = db["games"]
    quests_col = db["quests"]
    logger.info("✅ MONGODB CONNECTED")
except Exception as e:
    logger.error(f"❌ DB ERROR: {e}")

app = FastAPI(title="Quest Network API", version="2.7") # Version 2.7 (Ghost Quest Fix)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- HELPERS ---
async def verify_roblox_request(request: Request):
    user_agent = request.headers.get("user-agent", "")
    is_roblox = "Roblox/" in user_agent
    has_admin = request.headers.get("x-admin-secret") == ADMIN_SECRET
    has_game = request.headers.get("x-game-secret") == GAME_SERVER_SECRET
    if not is_roblox and not has_admin and not has_game:
        raise HTTPException(status_code=403, detail="Roblox Only")

async def verify_game_secret(x_game_secret: str = Header(None)):
    if x_game_secret != GAME_SERVER_SECRET: raise HTTPException(status_code=403)

async def fetch_roblox_game_data(place_id: int):
    try:
        async with httpx.AsyncClient() as client:
            resp_univ = await client.get(f"https://apis.roblox.com/universes/v1/places/{place_id}/universe")
            if resp_univ.status_code != 200: return None
            univ_id = resp_univ.json().get("universeId")
            
            resp_info = await client.get(f"https://games.roblox.com/v1/games?universeIds={univ_id}")
            if resp_info.status_code != 200: return None
            data = resp_info.json().get("data", [])
            if not data: return None
            return {"ownerId": data[0]["creator"]["id"], "visits": data[0]["visits"], "name": data[0]["name"]}
    except: return None

async def get_roblox_visits(place_id: int) -> int:
    d = await fetch_roblox_game_data(place_id)
    return d["visits"] if d else 0

# --- MODELS ---
class GameRegistration(BaseModel):
    ownerId: int
    placeId: int
    name: str
    description: str
    tier: int = 1
    quest_type: str = "time"
    time_required: int = 60
    reward_text: str = "Reward"

class GameConfigSync(BaseModel):
    placeId: int
    currency_name: str
    rewards: dict 

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

# --- ENDPOINTS ---

@app.get("/get-dashboard", tags=["Dashboard"])
@limiter.limit("60/minute") 
def get_dashboard(request: Request, ownerId: int, placeId: int):
    user = users_col.find_one({"_id": int(ownerId)})
    if not user:
        users_col.insert_one({"_id": int(ownerId), "balance": 0, "test_balance": STARTING_TEST_BALANCE})
        user = {"balance": 0, "test_balance": STARTING_TEST_BALANCE}
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

@app.post("/sync-config", tags=["Game Management"], dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
def sync_config(request: Request, data: GameConfigSync):
    res = games_col.update_one(
        {"placeId": data.placeId},
        {"$set": {
            "currency_name": data.currency_name,
            "rewards_config": data.rewards,
            "last_synced": datetime.datetime.utcnow()
        }}
    )
    if res.matched_count == 0: return {"success": False, "message": "Game not registered"}
    return {"success": True}

@app.post("/register-game", tags=["Game Management"], dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
async def register_game(request: Request, data: GameRegistration):
    users_col.update_one({"_id": data.ownerId}, {"$setOnInsert": {"balance": 0, "test_balance": STARTING_TEST_BALANCE}}, upsert=True)
    
    status = "pending"
    real_visits = await get_roblox_visits(data.placeId)
    if real_visits >= AUTO_APPROVE_VISITS: status = "active"
    
    old_game = games_col.find_one({"placeId": data.placeId})
    if old_game and old_game.get("status") == "active": status = "active"

    tier_info = TIER_CONFIG.get(data.tier, TIER_CONFIG[1])
    final_time = max(data.time_required, tier_info["time"])
    final_reward = data.reward_text if data.reward_text else "See Details"

    games_col.update_one(
        {"placeId": data.placeId},
        {"$set": {
            "ownerId": data.ownerId, "name": data.name, "description": data.description,
            "tier": data.tier, "visit_cost": tier_info["cost"], 
            "time_required": final_time, "payout_amount": tier_info["payout"], 
            "quest_type": data.quest_type, "status": status, "reward_text": final_reward,
            "last_updated": datetime.datetime.utcnow(),
        },
        "$setOnInsert": {
            "remaining_visits": 0,
            "last_refill_at": datetime.datetime.utcnow() 
        }}, upsert=True
    )
    return {"success": True, "status": status}

@app.post("/buy-visits", tags=["Game Management"], dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
def buy_visits(request: Request, data: BuyVisits):
    game = games_col.find_one({"placeId": data.placeId})
    if not game or game.get("status") != "active": return {"success": False, "message": "Unavailable"}
    
    total = data.amount * game.get("visit_cost", 8)
    user = users_col.find_one({"_id": data.ownerId})
    if not user: return {"success": False}
    
    t_bal, r_bal = user.get("test_balance", 0), user.get("balance", 0)
    pay_t = min(t_bal, total)
    pay_r = total - pay_t
    
    if r_bal < pay_r: return {"success": False}
    
    if pay_t > 0: users_col.update_one({"_id": data.ownerId}, {"$inc": {"test_balance": -pay_t}})
    if pay_r > 0: users_col.update_one({"_id": data.ownerId}, {"$inc": {"balance": -pay_r}})
    
    # 🔥 ОБНОВЛЯЕМ ПАРТИЮ ПРИ ПОКУПКЕ 🔥
    games_col.update_one(
        {"placeId": data.placeId}, 
        {
            "$inc": {"remaining_visits": data.amount},
            "$set": {"last_refill_at": datetime.datetime.utcnow()} 
        }
    )
    
    return {"success": True}

@app.get("/get-quests", tags=["Quests"])
def get_quests(request: Request, playerId: int):
    # Логика фильтрации по партиям (Batch Logic)
    completed_quests = list(quests_col.find(
        {"player_id": int(playerId), "status": {"$in": ["completed", "claimed"]}},
        {"target_game": 1, "timestamp": 1}
    ))
    
    last_completion_map = {}
    for q in completed_quests:
        pid = q["target_game"]
        ts = q["timestamp"]
        if pid not in last_completion_map or ts > last_completion_map[pid]:
            last_completion_map[pid] = ts

    yesterday = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
    active_user_quests = list(quests_col.find(
        {
            "player_id": int(playerId), 
            "status": {"$in": ["started", "arrived"]}, 
            "timestamp": {"$gte": yesterday}
        },
        {"target_game": 1}
    ))
    current_active_ids = [q["target_game"] for q in active_user_quests]

    all_games = list(games_col.find({"status": "active"}, {"_id": 0}))
    final_quests = []
    
    for game in all_games:
        pid = game["placeId"]
        
        if pid in current_active_ids:
            final_quests.append(game)
            continue
            
        if game.get("remaining_visits", 0) <= 0:
            continue
            
        last_refill_at = game.get("last_refill_at")
        if pid in last_completion_map:
            last_completed_at = last_completion_map[pid]
            if not last_refill_at: continue
            if last_completed_at >= last_refill_at: continue
        
        final_quests.append(game)

    return {"success": True, "quests": final_quests}

# === 🔥 ФИКС: ЗАЩИТА ОТ "ПРИЗРАЧНЫХ" КВЕСТОВ 🔥 ===
@app.post("/start-quest", tags=["Quests"], dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
def start_quest(request: Request, data: QuestStart):
    game = games_col.find_one({"placeId": data.destination_place_id})
    if not game: return {"success": False}
    if game.get("remaining_visits", 0) <= 0: return {"success": False, "message": "No visits left"}
    
    # 1. ПРОВЕРЯЕМ: А не начал ли он этот квест уже?
    # Если да — не создаем новый, а возвращаем старый токен.
    existing_quest = quests_col.find_one({
        "player_id": data.player_id,
        "target_game": data.destination_place_id,
        "status": "started"
    })
    
    if existing_quest:
        return {"success": True, "token": existing_quest["token"]}
        
    # 2. Если нет — создаем новый
    token = str(uuid.uuid4())
    quests_col.insert_one({
        "token": token, "player_id": data.player_id, 
        "source_game": data.source_place_id, "target_game": data.destination_place_id, 
        "status": "started", "traffic_valid": False, "payout_processed": False,
        "timestamp": datetime.datetime.utcnow()
    })
    return {"success": True, "token": token}
# ===================================================

@app.post("/verify-token", tags=["Quests"], dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
def verify_token(request: Request, data: TokenVerification):
    quest = quests_col.find_one({"token": data.token})
    if quest:
        diff = datetime.datetime.utcnow() - quest["timestamp"]
        if diff.total_seconds() > 86400: return {"success": False, "message": "Expired"}
    if not quest or quest["status"] != "started": return {"success": False}
    quests_col.update_one({"_id": quest["_id"]}, {"$set": {"status": "arrived", "arrived_at": datetime.datetime.utcnow()}})
    game = games_col.find_one({"placeId": quest["target_game"]})
    tier_info = TIER_CONFIG.get(game.get("tier", 1))
    return {"success": True, "quest_type": game.get("quest_type", "time"), "time_required": game.get("time_required", 60), "tier_time": tier_info["time"]}

@app.post("/check-traffic", tags=["Quests"], dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
async def check_traffic(request: Request, data: TokenVerification):
    quest = quests_col.find_one({"token": data.token})
    if not quest: return {"success": False}
    game = games_col.find_one({"placeId": quest["target_game"]})
    tier_info = TIER_CONFIG.get(game.get("tier", 1))
    arrived = quest.get("arrived_at")
    if isinstance(arrived, str): arrived = datetime.datetime.fromisoformat(arrived.replace('Z', '+00:00'))
    if arrived.tzinfo: arrived = arrived.replace(tzinfo=None)
    delta = (datetime.datetime.utcnow() - arrived).total_seconds()
    tier_time, quest_time = tier_info["time"], game["time_required"]
    if delta >= tier_time and not quest.get("payout_processed"):
        games_col.update_one({"_id": game["_id"]}, {"$inc": {"remaining_visits": -1}})
        quests_col.update_one({"_id": quest["_id"]}, {"$set": {"payout_processed": True}})
        src_id = quest["source_game"]
        src = games_col.find_one({"placeId": src_id})
        owner_pay = src["ownerId"] if src else None
        if not owner_pay:
            r_data = await fetch_roblox_game_data(src_id)
            if r_data:
                owner_pay = r_data["ownerId"]
                games_col.insert_one({"placeId": src_id, "ownerId": owner_pay, "name": r_data["name"], "status": "inactive"})
        if owner_pay: users_col.update_one({"_id": owner_pay}, {"$inc": {"balance": tier_info["payout"]}}, upsert=True)
    if delta >= quest_time:
        if not quest.get("traffic_valid"):
            quests_col.update_one({"_id": quest["_id"]}, {"$set": {"traffic_valid": True, "completed_tier": game.get("tier", 1), "status": "completed"}})
            return {"success": True, "quest_completed": True}
        return {"success": True, "quest_completed": True}
    return {"success": False, "message": "Keep playing"}

@app.post("/complete-task", tags=["Quests"], dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
def complete_task(request: Request, data: TokenVerification):
    quest = quests_col.find_one({"token": data.token})
    if quest and quest.get("traffic_valid"):
        tier = games_col.find_one({"placeId": quest["target_game"]}).get("tier", 1)
        quests_col.update_one({"_id": quest["_id"]}, {"$set": {"status": "completed", "completed_tier": tier}})
        return {"success": True}
    return {"success": False}

@app.post("/claim-rewards", tags=["Quests"], dependencies=[Depends(verify_game_secret), Depends(verify_roblox_request)])
def claim_rewards(request: Request, data: RewardClaim):
    pending = list(quests_col.find({"player_id": data.player_id, "status": "completed", "source_game": data.current_place_id}))
    if pending: quests_col.update_many({"_id": {"$in": [q["_id"] for q in pending]}}, {"$set": {"status": "claimed"}})
    return {"success": True, "tiers": [q.get("completed_tier", 1) for q in pending]}

@app.get("/admin/pending-games")
def p(x_admin_secret: str = Header(None)): return list(games_col.find({"status": "pending"}, {"_id": 0}))
@app.post("/admin/decide-game")
def d(d: AdminDecision, x_admin_secret: str = Header(None)): games_col.update_one({"placeId": d.placeId}, {"$set": {"status": "active" if d.action=="approve" else "rejected"}}); return {"ok": True}
@app.post("/admin/add-balance")
def a(d: AddBalance, x_admin_secret: str = Header(None)): users_col.update_one({"_id": d.owner_id}, {"$inc": {"balance": d.amount}}, upsert=True); return {"ok": True}
