from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel
from pymongo import MongoClient
import os
from dotenv import load_dotenv
import uuid
import certifi
import datetime

load_dotenv()
MONGO_URL = os.getenv("MONGO_URL")

# 🔐 СЕКРЕТНЫЕ КЛЮЧИ (Задай их в Render Environment Variables!)
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME_IN_ENV") 
GAME_SERVER_SECRET = os.getenv("GAME_SERVER_SECRET", "MY_SUPER_SECRET_GAME_KEY_123") 

try:
    if not MONGO_URL: raise ValueError("Нет MONGO_URL")
    client = MongoClient(MONGO_URL, tlsCAFile=certifi.where()) 
    db = client["QuestNetworkDB"] 
    client.admin.command('ping')
    print("✅ MONGODB ПОДКЛЮЧЕНА!")
except Exception as e:
    print(f"❌ ОШИБКА БД: {e}")

app = FastAPI()

# --- ЗАЩИТА ---
async def verify_game_secret(x_game_secret: str = Header(None)):
    if x_game_secret != GAME_SERVER_SECRET:
        raise HTTPException(status_code=403, detail="Invalid Game Secret Key")

# --- КОНФИГУРАЦИЯ ТИРОВ ---
TIER_CONFIG = {
    1: {"cost": 10, "time": 60,  "payout": 7},
    2: {"cost": 15, "time": 180, "payout": 11},
    3: {"cost": 25, "time": 300, "payout": 18}
}

DAILY_LIMIT = 20

# --- МОДЕЛИ ---
class GameRegistration(BaseModel):
    ownerId: int        
    placeId: int
    name: str
    description: str
    tier: int = 1
    quest_type: str = "time"

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

# --- ЭНДПОИНТЫ ---

# 1. ДАШБОРД (Открытый, только чтение)
@app.get("/get-dashboard")
def get_dashboard(ownerId: int, placeId: int):
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

# 2. ПОЛУЧЕНИЕ КВЕСТОВ (Открытый)
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
            available_quests.append(game)
            
    return {"success": True, "quests": available_quests}

# 3. РЕГИСТРАЦИЯ ИГРЫ (Защищено)
@app.post("/register-game", dependencies=[Depends(verify_game_secret)])
def register_game(data: GameRegistration):
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

# 4. ПОКУПКА ВИЗИТОВ (Защищено)
@app.post("/buy-visits", dependencies=[Depends(verify_game_secret)])
def buy_visits(data: BuyVisits):
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

# 5. СТАРТ КВЕСТА (Защищено)
@app.post("/start-quest", dependencies=[Depends(verify_game_secret)])
def start_quest(data: QuestStart):
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

# 6. ПРОВЕРКА ТОКЕНА (Защищено)
@app.post("/verify-token", dependencies=[Depends(verify_game_secret)])
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
    time_required = game_info.get("time_required", 60)
    
    return {
        "success": True, 
        "quest_type": game_info.get("quest_type", "time"),
        "time_required": time_required 
    }

# 7. ПРОВЕРКА ТРАФИКА (Защищено)
@app.post("/check-traffic", dependencies=[Depends(verify_game_secret)])
def check_traffic(data: TokenVerification):
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
        res = games.update_one(
            {"_id": target_game["_id"], "remaining_visits": {"$gt": 0}},
            {"$inc": {"remaining_visits": -1}}
        )
        
        if res.modified_count > 0:
            source_game = games.find_one({"placeId": quest["source_game"]})
            if source_game:
                users.update_one({"_id": source_game["ownerId"]}, {"$inc": {"balance": target_game.get("payout_amount", 7)}})
        
        update_data = {"traffic_valid": True, "completed_tier": target_game.get("tier", 1)}
        quest_type = target_game.get("quest_type", "time")
        
        if quest_type == "time":
            update_data["status"] = "completed"
        
        quests.update_one({"_id": quest["_id"]}, {"$set": update_data})
        return {"success": True, "quest_completed": (quest_type == "time")}
    else:
        return {"success": False, "message": f"Wait {int(required_time - seconds_passed)}s"}

# 8. ЗАВЕРШЕНИЕ ЭКШЕНА (Защищено)
@app.post("/complete-task", dependencies=[Depends(verify_game_secret)])
def complete_task(data: TokenVerification):
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

# 9. ПОЛУЧЕНИЕ НАГРАД (Защищено)
@app.post("/claim-rewards", dependencies=[Depends(verify_game_secret)])
def claim_rewards(data: RewardClaim):
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

# 10. АДМИН: НАЧИСЛЕНИЕ (Особый ключ)
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
