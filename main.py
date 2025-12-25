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

# 🔥 ИСПРАВЛЕНИЕ: ЖЕСТКО ЗАДАЕМ КЛЮЧ, ЧТОБЫ ОН СОВПАДАЛ С LUA
ADMIN_SECRET = "MY_SUPER_SECRET_GAME_KEY_123" 
GAME_SERVER_SECRET = "MY_SUPER_SECRET_GAME_KEY_123" # На всякий случай дублируем

# Лимиты
DAILY_LIMIT = 20
STARTING_TEST_BALANCE = 500
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
    keys_col = db["api_keys"] 
    logger.info("✅ MONGODB CONNECTED")
except Exception as e:
    logger.error(f"❌ DB ERROR: {e}")

app = FastAPI(title="Quest Network API", version="4.1 Fixed")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# === 🛡️ ФИНАЛЬНАЯ СИСТЕМА БЕЗОПАСНОСТИ ===
async def verify_request(
    request: Request, 
    x_api_key: str = Header(None), 
    x_admin_secret: str = Header(None)
):
    # 1. Проверяем АДМИНА (Приоритет №1)
    if x_admin_secret == ADMIN_SECRET:
        return {"role": "admin", "owner_id": None} 

    # 2. Проверяем ЮЗЕРА (Приоритет №2)
    if x_api_key:
        key_doc = keys_col.find_one({"key": x_api_key})
        if key_doc:
            return {"role": "user", "owner_id": key_doc["owner_id"]}
    
    # 3. Если ничего не подошло - логируем попытку взлома и блокируем
    user_agent = request.headers.get("user-agent", "")
    
    # Дебаг в консоль сервера (поможет понять, что приходит)
    logger.warning(f"⛔ AUTH FAILED. Secret: {x_admin_secret} | Key: {x_api_key} | UA: {user_agent}")
    
    if "Roblox/" not in user_agent:
         raise HTTPException(status_code=403, detail="Roblox Only")

    raise HTTPException(status_code=403, detail="Invalid Credentials")

# --- HELPERS ---
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
class GenerateKeyRequest(BaseModel):
    user_id: int

class GameRegistration(BaseModel):
    ownerId: int = None 
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
    current_place_id: int = None 

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

@app.post("/admin/generate-key")
def generate_key(data: GenerateKeyRequest, auth: dict = Depends(verify_request)):
    if auth["role"] != "admin": raise HTTPException(status_code=403, detail="Admin only")
    
    existing = keys_col.find_one({"owner_id": data.user_id})
    if existing:
        return {"success": True, "api_key": existing["key"], "is_new": False}
    
    new_key = "sk_" + uuid.uuid4().hex[:24] 
    keys_col.insert_one({
        "key": new_key,
        "owner_id": data.user_id,
        "created_at": datetime.datetime.utcnow()
    })
    return {"success": True, "api_key": new_key, "is_new": True}

@app.get("/get-dashboard", tags=["Dashboard"])
@limiter.limit("60/minute") 
def get_dashboard(request: Request, ownerId: int, placeId: int, auth: dict = Depends(verify_request)):
    if auth["role"] == "user" and auth["owner_id"] != ownerId:
        raise HTTPException(status_code=403, detail="Cannot view other's dashboard")

    user = users_col.find_one({"_id": int(ownerId)})
    if not user:
        users_col.insert_one({"_id": int(ownerId), "balance": 0, "test_balance": STARTING_TEST_BALANCE})
        user = {"balance": 0, "test_balance": STARTING_TEST_BALANCE}
    
    if "test_balance" not in user:
        users_col.update_one({"_id": int(ownerId)}, {"$set": {"test_balance": STARTING_TEST_BALANCE}})
        user["test_balance"] = STARTING_TEST_BALANCE

    user_games_cursor = games_col.find({"ownerId": int(ownerId)})
    
    my_campaigns = []
    for g in user_games_cursor:
        my_campaigns.append({
            "gameId": g.get("placeId"),
            "gameName": g.get("name", "Unknown"),
            "status": g.get("status", "pending"),
            "remaining_visits": g.get("remaining_visits", 0),
            "tier": g.get("tier", 1)
        })

    current_game = games_col.find_one({"placeId": int(placeId)})

    return {
        "success": True, 
        "balance": user.get("balance", 0), 
        "test_balance": user.get("test_balance", 0),
        "my_campaigns": my_campaigns,
        "current_status": current_game.get("status", "not_registered") if current_game else "not_registered"
    }

@app.post("/sync-config", tags=["Game Management"], dependencies=[Depends(verify_request)])
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

@app.post("/register-game", tags=["Game Management"])
async def register_game(data: GameRegistration, auth: dict = Depends(verify_request)):
    real_owner_id = auth["owner_id"] if auth["role"] == "user" else data.ownerId
    if not real_owner_id: raise HTTPException(status_code=400, detail="Owner ID unknown")

    users_col.update_one({"_id": real_owner_id}, {"$setOnInsert": {"balance": 0, "test_balance": STARTING_TEST_BALANCE}}, upsert=True)
    
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
            "ownerId": real_owner_id, 
            "name": data.name, "description": data.description,
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

@app.post("/buy-visits", tags=["Game Management"], dependencies=[Depends(verify_request)])
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
    yesterday = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
    all_user_quests = list(quests_col.find(
        {"player_id": int(playerId), "timestamp": {"$gte": yesterday}},
        {"target_game": 1, "status": 1, "timestamp": 1}
    ))

    game_states = {} 
    for q in all_user_quests:
        pid = q["target_game"]
        ts = q["timestamp"]
        status = q["status"]
        if pid not in game_states or ts > game_states[pid]["timestamp"]:
            game_states[pid] = {"status": status, "timestamp": ts}

    all_games = list(games_col.find({"status": "active"}, {"_id": 0}))
    final_list = []

    for game in all_games:
        pid = game["placeId"]
        user_state = game_states.get(pid)

        if user_state:
            if user_state["status"] in ["started", "arrived"]:
                final_list.append(game)
                continue
            if user_state["status"] in ["completed", "claimed"]:
                last_refill = game.get("last_refill_at")
                if last_refill and user_state["timestamp"] < last_refill:
                    final_list.append(game)
                continue

        if game.get("remaining_visits", 0) > 0:
            final_list.append(game)

    return {"success": True, "quests": final_list}

@app.post("/start-quest", tags=["Quests"], dependencies=[Depends(verify_request)])
def start_quest(request: Request, data: QuestStart):
    game = games_col.find_one({"placeId": data.destination_place_id})
    if not game: return {"success": False}
    if game.get("remaining_visits", 0) <= 0: return {"success": False, "message": "No visits left"}
    
    existing_quest = quests_col.find_one({
        "player_id": data.player_id,
        "target_game": data.destination_place_id,
        "status": "started"
    })
    
    if existing_quest:
        quests_col.update_one(
            {"_id": existing_quest["_id"]},
            {"$set": {
                "timestamp": datetime.datetime.utcnow(),
                "source_game": data.source_place_id 
            }}
        )
        return {"success": True, "token": existing_quest["token"]}
        
    token = str(uuid.uuid4())
    quests_col.insert_one({
        "token": token, "player_id": data.player_id, 
        "source_game": data.source_place_id, "target_game": data.destination_place_id, 
        "status": "started", "traffic_valid": False, "payout_processed": False,
        "timestamp": datetime.datetime.utcnow()
    })
    return {"success": True, "token": token}

@app.post("/verify-token", tags=["Quests"], dependencies=[Depends(verify_request)])
def verify_token(request: Request, data: TokenVerification):
    quest = quests_col.find_one({"token": data.token})
    if not quest: return {"success": False, "message": "Token not found"}

    diff = datetime.datetime.utcnow() - quest["timestamp"]
    if diff.total_seconds() > 86400: return {"success": False, "message": "Expired"}

    game = games_col.find_one({"placeId": quest["target_game"]})
    if not game: return {"success": False, "message": "Game not found"}
        
    tier_info = TIER_CONFIG.get(game.get("tier", 1))

    if quest["status"] == "arrived":
        arrived_at = quest.get("arrived_at")
        if isinstance(arrived_at, str): 
            try: arrived_at = datetime.datetime.fromisoformat(arrived_at.replace('Z', '+00:00'))
            except: pass 
        if isinstance(arrived_at, datetime.datetime) and arrived_at.tzinfo: arrived_at = arrived_at.replace(tzinfo=None)
            
        if arrived_at and (datetime.datetime.utcnow() - arrived_at).total_seconds() < 15:
             return {"success": True, "quest_type": game.get("quest_type", "time"), "time_required": game.get("time_required", 60), "tier_time": tier_info["time"]}

    if quest["status"] != "started": return {"success": False, "message": "Status is not started"}
    
    quests_col.update_one({"_id": quest["_id"]}, {"$set": {"status": "arrived", "arrived_at": datetime.datetime.utcnow()}})
    return {"success": True, "quest_type": game.get("quest_type", "time"), "time_required": game.get("time_required", 60), "tier_time": tier_info["time"]}

@app.post("/check-traffic", tags=["Quests"], dependencies=[Depends(verify_request)])
async def check_traffic(request: Request, data: TokenVerification):
    try:
        quest = quests_col.find_one({"token": data.token})
        if not quest: return {"success": False, "message": "Quest not found"}
        
        game = games_col.find_one({"placeId": quest["target_game"]})
        if not game: return {"success": False, "message": "Target game not found"} 

        tier_val = int(game.get("tier", 1))
        tier_info = TIER_CONFIG.get(tier_val, TIER_CONFIG[1])
        
        arrived = quest.get("arrived_at")
        if isinstance(arrived, str): 
            try: arrived = datetime.datetime.fromisoformat(arrived.replace('Z', '+00:00'))
            except: pass
        if arrived and arrived.tzinfo: arrived = arrived.replace(tzinfo=None)
        
        if not arrived: return {"success": False, "message": "Not arrived yet"}

        delta = (datetime.datetime.utcnow() - arrived).total_seconds()
        tier_time, quest_time = tier_info["time"], game["time_required"]
        
        if delta >= tier_time and not quest.get("payout_processed"):
            try:
                games_col.update_one({"_id": game["_id"]}, {"$inc": {"remaining_visits": -1}})
                quests_col.update_one({"_id": quest["_id"]}, {"$set": {"payout_processed": True}})
                
                src_id = quest.get("source_game")
                if src_id:
                    src = games_col.find_one({"placeId": src_id})
                    owner_pay = None
                    if src: owner_pay = src.get("ownerId")
                    if not owner_pay:
                        try:
                            r_data = await fetch_roblox_game_data(src_id)
                            if r_data:
                                owner_pay = r_data["ownerId"]
                                games_col.update_one({"placeId": src_id}, {"$setOnInsert": {"ownerId": owner_pay, "name": r_data["name"], "status": "inactive"}}, upsert=True)
                        except: pass

                    if owner_pay: 
                        users_col.update_one({"_id": int(owner_pay)}, {"$inc": {"balance": tier_info["payout"]}}, upsert=True)

            except Exception as e:
                logger.error(f"❌ CRITICAL PAYOUT ERROR: {e}")

        if delta >= quest_time:
            if not quest.get("traffic_valid"):
                quests_col.update_one({"_id": quest["_id"]}, {"$set": {"traffic_valid": True, "completed_tier": tier_val, "status": "completed"}})
                return {"success": True, "quest_completed": True}
            return {"success": True, "quest_completed": True}
            
        return {"success": False, "message": "Keep playing"}
    except Exception as e:
        logger.error(f"❌ UNHANDLED ERROR in check-traffic: {e}")
        return {"success": False, "message": "Server Error", "error": str(e)}

@app.post("/complete-task", tags=["Quests"], dependencies=[Depends(verify_request)])
def complete_task(request: Request, data: TokenVerification):
    quest = quests_col.find_one({"token": data.token})
    if quest and quest.get("traffic_valid"):
        tier = games_col.find_one({"placeId": quest["target_game"]}).get("tier", 1)
        quests_col.update_one({"_id": quest["_id"]}, {"$set": {"status": "completed", "completed_tier": tier}})
        return {"success": True}
    return {"success": False}

@app.post("/claim-rewards", tags=["Quests"], dependencies=[Depends(verify_request)])
def claim_rewards(request: Request, data: RewardClaim):
    pending = list(quests_col.find({"player_id": data.player_id, "status": "completed", "source_game": data.current_place_id}))
    if pending: quests_col.update_many({"_id": {"$in": [q["_id"] for q in pending]}}, {"$set": {"status": "claimed"}})
    return {"success": True, "tiers": [q.get("completed_tier", 1) for q in pending]}

# === АДМИНСКИЕ ФУНКЦИИ (ТОЛЬКО ХАБ) ===
@app.get("/admin/pending-games", dependencies=[Depends(verify_request)])
def p(auth: dict = Depends(verify_request)): 
    if auth["role"] != "admin": raise HTTPException(status_code=403)
    return list(games_col.find({"status": "pending"}, {"_id": 0}))

@app.post("/admin/decide-game", dependencies=[Depends(verify_request)])
def d(d: AdminDecision, auth: dict = Depends(verify_request)): 
    if auth["role"] != "admin": raise HTTPException(status_code=403)
    games_col.update_one({"placeId": d.placeId}, {"$set": {"status": "active" if d.action=="approve" else "rejected"}})
    return {"ok": True}

@app.post("/admin/add-balance", dependencies=[Depends(verify_request)])
def a(d: AddBalance, auth: dict = Depends(verify_request)): 
    if auth["role"] != "admin": raise HTTPException(status_code=403)
    users_col.update_one({"_id": d.owner_id}, {"$inc": {"balance": d.amount}}, upsert=True)
    return {"ok": True}
