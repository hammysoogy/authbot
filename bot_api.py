"""
bot_api.py
Safe Discord bot + Flask API template.
Use for legitimate admin/auth tasks. Do NOT use to gate cheating/exploit tools.
"""

import os
import time
import secrets
import asyncio
import threading
import json
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify
from discord.ext import commands
import discord

# Optional MongoDB
try:
    from pymongo import MongoClient
    MONGO_AVAILABLE = True
except Exception:
    MONGO_AVAILABLE = False

# ----------------- Configuration (via env vars) -----------------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")  # required
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", "0"))  # required
API_KEY = os.environ.get("API_KEY", "")  # required: shared secret used by your trusted server
MONGO_URI = os.environ.get("MONGO_URI", None)  # optional (recommended)
FLASK_PORT = int(os.environ.get("PORT", "10000"))  # Render uses PORT env by default
# ----------------------------------------------------------------

if not DISCORD_TOKEN:
    raise SystemExit("DISCORD_TOKEN env var required")
if not LOG_CHANNEL_ID:
    raise SystemExit("LOG_CHANNEL_ID env var required")
if not API_KEY:
    raise SystemExit("API_KEY env var required")

# ----------------- Storage abstraction -----------------
class Storage:
    """Simple abstraction: use MongoDB if available, otherwise in-memory dicts."""
    def __init__(self, mongo_uri: Optional[str] = None):
        self.mongo = None
        self.db = None
        self.keys_coll = None
        self.blacklist_coll = None
        if mongo_uri and MONGO_AVAILABLE:
            try:
                self.mongo = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
                # try a quick ping
                self.mongo.admin.command('ping')
                self.db = self.mongo.get_database("bot_api_db")
                self.keys_coll = self.db.get_collection("keys")
                self.blacklist_coll = self.db.get_collection("blacklist")
                # create simple indexes
                self.keys_coll.create_index("key", unique=True)
                self.blacklist_coll.create_index("hwid", unique=True)
                print("Using MongoDB persistence.")
            except Exception as e:
                print("Failed to connect to MongoDB, falling back to in-memory. Error:", e)
                self.mongo = None

        if not self.mongo:
            self._keys: Dict[str, Dict[str, Any]] = {}
            self._blacklist = set()
            print("Using in-memory persistence (volatile).")

    # keys API
    def create_key(self, key: str, expires_at: float):
        if self.mongo:
            doc = {"key": key, "expires_at": expires_at, "active": True, "created_at": time.time()}
            self.keys_coll.insert_one(doc)
        else:
            self._keys[key] = {"expires_at": expires_at, "active": True, "created_at": time.time()}

    def get_key(self, key: str) -> Optional[Dict[str, Any]]:
        if self.mongo:
            doc = self.keys_coll.find_one({"key": key})
            if doc:
                return {"key": doc["key"], "expires_at": doc["expires_at"], "active": doc.get("active", True)}
            return None
        else:
            return self._keys.get(key)

    def set_key_inactive(self, key: str):
        if self.mongo:
            self.keys_coll.update_one({"key": key}, {"$set": {"active": False}})
        else:
            if key in self._keys:
                self._keys[key]["active"] = False

    # blacklist API
    def add_blacklist(self, hwid: str):
        if self.mongo:
            self.blacklist_coll.update_one({"hwid": hwid}, {"$set": {"hwid": hwid, "created_at": time.time()}}, upsert=True)
        else:
            self._blacklist.add(hwid)

    def is_blacklisted(self, hwid: str) -> bool:
        if self.mongo:
            return self.blacklist_coll.count_documents({"hwid": hwid}, limit=1) > 0
        else:
            return hwid in self._blacklist

    def expire_key(self, key: str) -> bool:
        if self.mongo:
            res = self.keys_coll.update_one({"key": key}, {"$set": {"active": False}})
            return res.modified_count > 0
        else:
            if key in self._keys:
                self._keys[key]["active"] = False
                return True
            return False

# Instantiate storage
storage = Storage(mongo_uri=MONGO_URI)

# ----------------- Flask app -----------------
app = Flask(__name__)

def require_api_key(data: dict) -> (bool, tuple):
    """Validate presence and correctness of API key in JSON body"""
    if not data:
        return False, (jsonify({"ok": False, "error": "missing_json"}), 400)
    if data.get("api_key") != API_KEY:
        return False, (jsonify({"ok": False, "error": "unauthorized"}), 403)
    return True, None

@app.route("/validate", methods=["POST"])
def validate():
    """
    POST JSON:
    {
      "api_key": "...",
      "key": "KEYSTRING",
      "username": "Name",    # optional but useful for logs
      "hwid": "SOMEID"      # optional identifier to blacklist
    }
    Response:
    { ok: true, valid: true/false, reason: "..." }
    """
    data = request.get_json(silent=True)
    ok, err = require_api_key(data)
    if not ok:
        return err

    key = data.get("key", "")
    username = data.get("username", "unknown")
    hwid = data.get("hwid", None)

    # Check blacklist first
    if hwid and storage.is_blacklisted(hwid):
        return jsonify({"ok": True, "valid": False, "reason": "blacklisted"})

    # Look up key
    rec = storage.get_key(key)
    if not rec:
        return jsonify({"ok": True, "valid": False, "reason": "invalid"})

    if not rec.get("active", True):
        return jsonify({"ok": True, "valid": False, "reason": "inactive"})

    if rec.get("expires_at", 0) < time.time():
        # expire it and respond
        storage.set_key_inactive(key)
        return jsonify({"ok": True, "valid": False, "reason": "expired"})

    # mark used (if you want keys one-time)
    storage.set_key_inactive(key)

    # Log to Discord channel (async task)
    async def send_log():
        try:
            embed = discord.Embed(title="Key Redeemed", color=0x00FF00)
            embed.add_field(name="User", value=username, inline=True)
            embed.add_field(name="Key", value=key, inline=True)
            embed.add_field(name="HWID", value=hwid or "—", inline=False)
            embed.timestamp = discord.utils.utcnow()
            ch = bot.get_channel(LOG_CHANNEL_ID)
            if ch:
                await ch.send(embed=embed)
        except Exception as e:
            print("Discord log failed:", e)

    # schedule coroutine
    try:
        asyncio.get_event_loop().create_task(send_log())
    except RuntimeError:
        # if no running loop, schedule onto bot loop
        bot.loop.create_task(send_log())

    return jsonify({"ok": True, "valid": True, "reason": "accepted"})

# health check
@app.route("/")
def index():
    return "<html><body><h2>Bot API running</h2></body></html>"

# ----------------- Discord Bot -----------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Discord bot ready as {bot.user} (id {bot.user.id})")
    # Sync application commands
    try:
        await bot.tree.sync()
        print("Slash commands synced")
    except Exception as e:
        print("Failed to sync commands:", e)

# Slash commands
@bot.tree.command(name="generate", description="Generate a one-time key for X minutes")
async def slash_generate(interaction: discord.Interaction, minutes: int = 10):
    # create a key valid for `minutes`
    key = secrets.token_hex(8).upper()
    expires_at = time.time() + max(1, int(minutes)) * 60
    storage.create_key(key, expires_at)
    await interaction.response.send_message(f"🔑 Key: `{key}` valid for {minutes} minutes", ephemeral=True)

@bot.tree.command(name="expire", description="Expire a key immediately")
async def slash_expire(interaction: discord.Interaction, key: str):
    ok = storage.expire_key(key)
    if ok:
        await interaction.response.send_message(f"⛔ Key `{key}` expired.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ Key not found.", ephemeral=True)

@bot.tree.command(name="blacklist", description="Blacklist an HWID (blocks validate)")
async def slash_blacklist(interaction: discord.Interaction, hwid: str):
    storage.add_blacklist(hwid)
    await interaction.response.send_message(f"🚫 HWID `{hwid}` blacklisted.", ephemeral=True)

# ----------------- Runner -----------------
def run_flask():
    # Render will supply PORT via env var
    port = int(os.environ.get("PORT", FLASK_PORT))
    # Use 0.0.0.0 to be reachable
    app.run(host="0.0.0.0", port=port)

async def run_bot():
    await bot.start(DISCORD_TOKEN)

def main():
    # start Flask in a background thread
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    # run bot in main thread loop
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()
