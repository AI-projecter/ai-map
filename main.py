import os
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jose import jwt, JWTError
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field

import logging

logger = logging.getLogger("uvicorn.error")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="World AI Map")
app.mount("/static", StaticFiles(directory=APP_DIR), name="static")

JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_HOURS = 72
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")

mongo_client = None
db = None
users_collection = None
points_collection = None

@app.on_event("startup")
async def startup():
    global mongo_client, db, users_collection, points_collection
    uri = os.environ.get("MONGODB_URI")
    if uri:
        mongo_client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000)
        db = mongo_client["world_ai_map"]
        users_collection = db["users"]
        points_collection = db["points"]
        await mongo_client.admin.command("ping")
        await users_collection.create_index("email", unique=True)
        await points_collection.create_index([("user_id", 1), ("lat", 1), ("lon", 1)])

@app.on_event("shutdown")
async def shutdown():
    if mongo_client:
        mongo_client.close()

def require_config():
    if not JWT_SECRET:
        raise HTTPException(503, "JWT_SECRET is not configured in Render.")
    if users_collection is None:
        raise HTTPException(503, "MONGODB_URI is not configured or MongoDB is unavailable.")

def hash_password(password: str, salt: Optional[bytes] = None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + ":" + digest.hex()

def verify_password(password: str, stored: str):
    try:
        salt_hex, digest_hex = stored.split(":")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False

def create_token(user_id: str):
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": user_id, "iat": now, "exp": now + timedelta(hours=JWT_HOURS)},
        JWT_SECRET, algorithm=JWT_ALGORITHM
    )

async def current_user(authorization: Optional[str] = Header(default=None)):
    require_config()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Please sign in.")
    token = authorization[7:]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("sub")
    except JWTError:
        raise HTTPException(401, "Session expired. Please sign in again.")
    user = await users_collection.find_one({"_id": user_id})
    if not user:
        raise HTTPException(401, "Account not found.")
    return user

class AuthInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

class PointInput(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)

async def get_json(client, url, params=None, headers=None):
    response = await client.get(url, params=params, headers=headers, timeout=25)
    response.raise_for_status()
    return response.json()

async def fetch_weather(client, lat, lon):
    if not OPENWEATHER_API_KEY:
        return {"unavailable": "OpenWeather API key is not configured"}
    current = await get_json(client, "https://api.openweathermap.org/data/2.5/weather",
        {"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric"})
    forecast = await get_json(client, "https://api.openweathermap.org/data/2.5/forecast",
        {"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric"})
    return {
        "current": {
            "temp": current.get("main", {}).get("temp"),
            "feels_like": current.get("main", {}).get("feels_like"),
            "humidity": current.get("main", {}).get("humidity"),
            "wind": current.get("wind", {}).get("speed"),
            "description": (current.get("weather") or [{}])[0].get("description"),
            "icon": (current.get("weather") or [{}])[0].get("icon"),
        },
        "forecast": [{
            "time": item.get("dt_txt"),
            "temp": item.get("main", {}).get("temp"),
            "description": (item.get("weather") or [{}])[0].get("description"),
            "icon": (item.get("weather") or [{}])[0].get("icon"),
        } for item in forecast.get("list", [])[:8]]
    }

async def reverse_geocode(client, lat, lon):
    try:
        data = await get_json(client, "https://nominatim.openstreetmap.org/reverse",
            {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10},
            {"User-Agent": "WorldAIMap/1.0 (location exploration app)"})
        return {"name": data.get("name") or data.get("display_name", "Selected location"),
                "display_name": data.get("display_name", "")}
    except Exception:
        return {"name": "Selected location", "display_name": ""}

async def groq_analysis(client, lat, lon, place, weather):
    if not GROQ_API_KEY:
        raise HTTPException(503, "GROQ_API_KEY is not configured.")
    prompt = (
        "You are a careful geography guide. Based on the coordinates and supplied reverse-geocoding "
        "and weather data, describe the likely local environment. Do not claim certainty about "
        "unobserved details; clearly distinguish known facts from plausible inference. Respond in "
        "Russian. Return only valid JSON with keys title, description, image_prompt, narration. "
        "description should be 2-4 informative sentences. image_prompt should request a realistic "
        "documentary travel photograph of the likely landscape/settlement, avoid inventing famous "
        "landmarks. narration should be a natural spoken Russian version, about 50-90 words.\n\n"
        f"Coordinates: {lat}, {lon}\nPlace lookup: {place}\nWeather: {weather}"
    )
    response = await client.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={"model": "openai/gpt-oss-120b",
              "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.4, "response_format": {"type": "json_object"}},
        timeout=60
    )
    response.raise_for_status()
    return __import__("json").loads(response.json()["choices"][0]["message"]["content"])

async def generate_image(client, prompt):
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        return None
    url = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/@cf/black-forest-labs/flux-1-schnell"
    response = await client.post(url,
        headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"},
        json={"prompt": prompt, "width": 768, "height": 512, "num_steps": 4},
        timeout=120)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        result = response.json()
        encoded = result.get("result", {}).get("image")
        if encoded:
            return "data:image/png;base64," + encoded
        return None
    return "data:image/png;base64," + base64.b64encode(response.content).decode("ascii")

@app.get("/", response_class=HTMLResponse)
async def home():
    with open(os.path.join(APP_DIR, "index.html"), encoding="utf-8") as file:
        return file.read()

@app.post("/api/register")
async def register(data: AuthInput):
    require_config()
    email = str(data.email).lower().strip()
    if len(data.password) < 8:
        raise HTTPException(400, "Password must contain at least 8 characters.")
    user_id = secrets.token_hex(16)
    try:
        await users_collection.insert_one({
            "_id": user_id, "email": email, "password_hash": hash_password(data.password),
            "created_at": datetime.now(timezone.utc)
        })
    except Exception as exc:
        if exc.__class__.__name__ == "DuplicateKeyError":
            raise HTTPException(409, "An account with this email already exists.")
        raise
    return {"token": create_token(user_id), "email": email}

@app.post("/api/login")
async def login(data: AuthInput):
    require_config()
    email = str(data.email).lower().strip()
    user = await users_collection.find_one({"email": email})
    if not user or not verify_password(data.password, user.get("password_hash", "")):
        raise HTTPException(401, "Incorrect email or password.")
    return {"token": create_token(user["_id"]), "email": email}

@app.get("/api/me")
async def me(user=Depends(current_user)):
    return {"email": user["email"]}

@app.post("/api/points/analyze")
async def analyze_point(data: PointInput, user=Depends(current_user)):
    lat, lon = round(data.lat, 5), round(data.lon, 5)
    query = {"user_id": user["_id"], "lat": lat, "lon": lon}

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            logger.info("Analyzing point: lat=%s, lon=%s", lat, lon)

            try:
                logger.info("Step 1: Reverse geocoding")
                place = await reverse_geocode(client, lat, lon)

                logger.info("Step 2: Fetching weather")
                weather = await fetch_weather(client, lat, lon)

                logger.info("Step 3: Checking cache")
                cached = await points_collection.find_one(query, {"_id": 0})

                # Reuse existing AI and image results
                if cached and cached.get("analysis") and cached.get("image"):
                    cached["weather"] = weather
                    cached["place"] = place
                    cached["updated_at"] = datetime.now(
                        timezone.utc
                    ).isoformat()

                    await points_collection.update_one(
                        query,
                        {"$set": {
                            "weather": weather,
                            "place": place,
                            "updated_at": cached["updated_at"]
                        }}
                    )

                    logger.info("Cached analysis returned successfully")
                    return cached

                logger.info("Step 4: Calling Groq")
                analysis = await groq_analysis(
                    client, lat, lon, place, weather
                )

                logger.info("Step 5: Generating image")
                image_data = await generate_image(
                    client,
                    analysis.get(
                        "image_prompt",
                        "Realistic landscape photograph"
                    )
                )

            except HTTPException:
                logger.exception("HTTPException during point analysis")
                raise

            except httpx.HTTPStatusError as exc:
                logger.exception(
                    "External API HTTP error: status=%s, response=%s",
                    exc.response.status_code,
                    exc.response.text[:1000]
                )
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"External API error: "
                        f"HTTP {exc.response.status_code}"
                    )
                )

            except httpx.RequestError:
                logger.exception("External API connection or timeout error")
                raise HTTPException(
                    status_code=502,
                    detail="Could not connect to an external API."
                )

            except Exception:
                logger.exception("Unexpected error during point analysis")
                raise HTTPException(
                    status_code=500,
                    detail="Unexpected error. Check Render logs."
                )

        now = datetime.now(timezone.utc).isoformat()

        record = {
            "user_id": user["_id"],
            "lat": lat,
            "lon": lon,
            "place": place,
            "weather": weather,
            "analysis": analysis,
            "image": image_data,
            "created_at": now,
            "updated_at": now
        }

        logger.info("Step 6: Saving result to MongoDB")
        await points_collection.update_one(
            query,
            {"$set": record},
            upsert=True
        )

        record.pop("user_id", None)
        logger.info("Point analysis completed successfully")

        return record

    except HTTPException:
        raise

    except Exception:
        logger.exception("Failed to analyze or save point")
        raise HTTPException(
            status_code=500,
            detail="Internal server error. Check Render logs."
        )

@app.get("/api/history")
async def history(user=Depends(current_user)):
    cursor = points_collection.find({"user_id": user["_id"]}, {"_id": 0}).sort("updated_at", -1).limit(100)
    return await cursor.to_list(length=100)


@app.delete("/api/history")
async def clear_history(user=Depends(current_user)):
    result = await points_collection.delete_many({"user_id": user["_id"]})
    return {"deleted": result.deleted_count}
