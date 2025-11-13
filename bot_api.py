# app.py
import os
import time
import json
import secrets
import asyncio
import logging
import base64
import random
from typing import Optional

import discord
from discord import app_commands
from aiohttp import web

# ---------- Configuration ----------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
RENDER_DOMAIN = os.environ.get("RENDER_DOMAIN", "authbot-hn9s.onrender.com")
PORT = int(os.environ.get("PORT", 10000))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Channel IDs (store keys/loaders and logs in Discord channels)
KEY_STORE_CHANNEL_ID = int(os.environ.get("KEY_STORE_CHANNEL_ID", "0"))       # where keys are posted
LOADER_STORE_CHANNEL_ID = int(os.environ.get("LOADER_STORE_CHANNEL_ID", "0")) # where loader tokens are posted
REDEMPTION_LOG_CHANNEL_ID = int(os.environ.get("REDEMPTION_LOG_CHANNEL_ID", "0"))  # where redemptions are logged

ALLOWED_ROLE_IDS = set(x.strip() for x in os.environ.get("ALLOWED_ROLE_IDS", "").split(",") if x.strip())

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is required")

# ---------- logging ----------
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger("discord-key-service")

# ---------- load scripts from environment ----------
# env vars: SCRIPT_<id> = <lua source>
OBFUSCATED_STORE = {}
for k, v in os.environ.items():
    if k.startswith("SCRIPT_") and not k.endswith("_ENC"):
        script_id = k[len("SCRIPT_"):]
        OBFUSCATED_STORE[script_id] = v
DEFAULT_SCRIPT = os.environ.get("SCRIPT_TEXT", 'print("hello from server!")')
OBFUSCATED_STORE.setdefault("_default_", DEFAULT_SCRIPT)

logger.info("Available script IDs: %s", list(OBFUSCATED_STORE.keys()))

# ---------- Discord client & command tree ----------
intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

def user_allowed_to_manage(member: discord.Member) -> bool:
    if not ALLOWED_ROLE_IDS:
        return True
    if str(member.id) in ALLOWED_ROLE_IDS:
        return True
    for r in getattr(member, "roles", []):
        if str(r.id) in ALLOWED_ROLE_IDS:
            return True
    return False

@client.event
async def on_ready():
    logger.info("Logged in as %s (id=%s)", client.user, client.user.id)
    try:
        await tree.sync()
        logger.info("Slash commands synced.")
    except Exception:
        logger.exception("Failed to sync slash commands")

# ---------- utilities for storing/finding messages in channel ----------
# We store small JSON blobs inside fenced codeblocks so they're easy to parse.
def _make_codeblock_json(d: dict) -> str:
    return "```json\n" + json.dumps(d, separators=(",", ":"), ensure_ascii=False) + "\n```"

async def _find_message_with_key(channel: discord.TextChannel, key: str, kind: str) -> Optional[discord.Message]:
    # kind is "key" or "loader" to match JSON "type" field
    async for msg in channel.history(limit=2000):
        content = msg.content
        if "```json" not in content:
            continue
        try:
            start = content.index("```json") + len("```json")
            end = content.index("```", start)
            raw = content[start:end].strip()
            payload = json.loads(raw)
        except Exception:
            continue
        if payload.get("type") == kind and payload.get("key") == key:
            return msg
    return None

async def _post_record(channel: discord.TextChannel, payload: dict) -> discord.Message:
    txt = _make_codeblock_json(payload)
    return await channel.send(txt)

async def _edit_record(msg: discord.Message, new_payload: dict):
    try:
        await msg.edit(content=_make_codeblock_json(new_payload))
    except Exception:
        logger.exception("Failed to edit message %s", msg.id)

# ---------- key management via channel ----------
# key payload example:
# { "type":"key", "key":"abcd1234", "script_id":"myscript", "creator":"123", "created_at":123, "status":"active" }

async def store_key_channel(key: str, script_id: str, creator_id: str, ttl: int):
    ch = client.get_channel(KEY_STORE_CHANNEL_ID)
    if not ch:
        raise RuntimeError("KEY_STORE_CHANNEL_ID invalid or bot missing perms")
    payload = {"type":"key", "key": key, "script_id": script_id, "creator": str(creator_id),
               "created_at": int(time.time()), "ttl": int(ttl), "status":"active"}
    msg = await _post_record(ch, payload)
    logger.info("Created key %s (msg %s) for script %s", key[:8], msg.id, script_id)
    # Optionally, we can schedule expiry by background task (not necessary; TTL enforced by checking created_at)
    return msg

async def consume_key_channel(key: str, redeemer_id: str, redeemer_name: str) -> Optional[dict]:
    ch = client.get_channel(KEY_STORE_CHANNEL_ID)
    if not ch:
        return None
    msg = await _find_message_with_key(ch, key, kind="key")
    if not msg:
        return None
    # parse payload
    raw = msg.content
    start = raw.index("```json") + len("```json")
    end = raw.index("```", start)
    payload = json.loads(raw[start:end].strip())
    # check TTL and status
    if payload.get("status") != "active":
        return None
    created = payload.get("created_at", 0)
    ttl = payload.get("ttl", 300)
    if time.time() > created + ttl:
        # expired: mark as expired
        payload["status"] = "expired"
        await _edit_record(msg, payload)
        return None
    # consume: mark redeemed
    payload["status"] = "redeemed"
    payload["redeemed_by_id"] = str(redeemer_id)
    payload["redeemed_by_name"] = str(redeemer_name)
    payload["redeemed_at"] = int(time.time())
    await _edit_record(msg, payload)
    return payload

async def delete_key_channel(key: str) -> bool:
    ch = client.get_channel(KEY_STORE_CHANNEL_ID)
    if not ch:
        return False
    msg = await _find_message_with_key(ch, key, kind="key")
    if not msg:
        return False
    try:
        await msg.delete()
        return True
    except Exception:
        return False

# ---------- loader token management via channel ----------
# loader payload example:
# { "type":"loader", "key":"<token>", "script_id":"myscript", "creator":"123", "created_at":..., "ttl":..., "status":"active" }

async def store_loader_channel(token: str, script_id: str, creator_id: str, ttl: int):
    ch = client.get_channel(LOADER_STORE_CHANNEL_ID)
    if not ch:
        raise RuntimeError("LOADER_STORE_CHANNEL_ID invalid or bot missing perms")
    payload = {"type":"loader", "key": token, "script_id": script_id, "creator": str(creator_id),
               "created_at": int(time.time()), "ttl": int(ttl), "status":"active"}
    msg = await _post_record(ch, payload)
    logger.info("Created loader token %s (msg %s) for script %s", token[:8], msg.id, script_id)
    return msg

async def consume_loader_channel(token: str) -> Optional[dict]:
    ch = client.get_channel(LOADER_STORE_CHANNEL_ID)
    if not ch:
        return None
    msg = await _find_message_with_key(ch, token, kind="loader")
    if not msg:
        return None
    raw = msg.content
    start = raw.index("```json") + len("```json")
    end = raw.index("```", start)
    payload = json.loads(raw[start:end].strip())
    if payload.get("status") != "active":
        return None
    created = payload.get("created_at", 0)
    ttl = payload.get("ttl", 120)
    if time.time() > created + ttl:
        payload["status"] = "expired"
        await _edit_record(msg, payload)
        return None
    # consume
    payload["status"] = "consumed"
    payload["consumed_at"] = int(time.time())
    await _edit_record(msg, payload)
    return payload

async def delete_loader_channel(token: str) -> bool:
    ch = client.get_channel(LOADER_STORE_CHANNEL_ID)
    if not ch:
        return False
    msg = await _find_message_with_key(ch, token, kind="loader")
    if not msg:
        return False
    try:
        await msg.delete()
        return True
    except Exception:
        return False

# ---------- redemption log helper ----------
async def log_redemption(script_id: str, key: str, roblox_id: str, roblox_name: str):
    ch = client.get_channel(REDEMPTION_LOG_CHANNEL_ID)
    if not ch:
        logger.info("Redemption: script=%s key=%s by %s (%s)", script_id, key[:8], roblox_name, roblox_id)
        return
    embed = discord.Embed(title="🔑 Key Redeemed", color=discord.Color.green(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Script ID", value=script_id, inline=False)
    embed.add_field(name="Key (prefix)", value=f"{key[:8]}...", inline=True)
    embed.add_field(name="Redeemed by", value=f"{roblox_name} ({roblox_id})", inline=False)
    await ch.send(embed=embed)

# ---------- slash commands ----------
@tree.command(name="genkey", description="Generate a one-time redeem key for a script")
@app_commands.describe(script_id="Script id (after SCRIPT_)", ttl="Key TTL seconds (optional)")
async def genkey(interaction: discord.Interaction, script_id: str, ttl: Optional[int] = None):
    member = None
    if interaction.guild:
        try:
            member = interaction.guild.get_member(interaction.user.id) or await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            member = None
    if member and not user_allowed_to_manage(member):
        await interaction.response.send_message("You are not authorized to generate keys.", ephemeral=True)
        return
    if script_id not in OBFUSCATED_STORE:
        await interaction.response.send_message("Unknown script id.", ephemeral=True)
        return
    ttl_final = 300 if ttl is None else max(5, min(86400, int(ttl)))
    key = secrets.token_hex(12)
    await store_key_channel(key, script_id, interaction.user.id, ttl_final)
    dm = f"Generated key for `{script_id}` (ttl {ttl_final}s):\n```\n{key}\n```\nUse it in the loader GUI to redeem."
    try:
        await interaction.user.send(dm)
        await interaction.response.send_message("I DM'd you the key.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"Cannot DM you. Here is the key:\n`{key}`", ephemeral=True)

@tree.command(name="deletekey", description="Delete a redeem key")
@app_commands.describe(key="The key to delete")
async def deletekey(interaction: discord.Interaction, key: str):
    member = None
    if interaction.guild:
        try:
            member = interaction.guild.get_member(interaction.user.id) or await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            member = None
    if member and not user_allowed_to_manage(member):
        await interaction.response.send_message("You are not authorized to delete keys.", ephemeral=True)
        return
    ok = await delete_key_channel(key)
    if ok:
        await interaction.response.send_message("Deleted key (if it existed).", ephemeral=True)
    else:
        await interaction.response.send_message("Key not found or already consumed.", ephemeral=True)

@tree.command(name="script", description="DM a one-time loader loadstring for a random script")
async def script_cmd(interaction: discord.Interaction):
    member = None
    if interaction.guild:
        try:
            member = interaction.guild.get_member(interaction.user.id) or await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            member = None
    if member and not user_allowed_to_manage(member):
        await interaction.response.send_message("You are not authorized to request scripts.", ephemeral=True)
        return
    available = [s for s in OBFUSCATED_STORE.keys() if not s.startswith("_")]
    if not available:
        await interaction.response.send_message("No scripts available.", ephemeral=True)
        return
    script_id = random.choice(available)
    ttl = 120
    token = secrets.token_hex(18)
    await store_loader_channel(token, script_id, interaction.user.id, ttl)
    loadstring_line = f'loadstring(game:HttpGet("https://{RENDER_DOMAIN}/files/loaders/{script_id}/{token}.lua"))()'
    dm_msg = (
        f"One-time loader for `{script_id}` (valid {ttl}s):\n\n"
        f"```lua\n{loadstring_line}\n```\nRun that in your executor to open the key GUI (you must paste a key from `/genkey`)."
    )
    try:
        await interaction.user.send(dm_msg)
        await interaction.response.send_message("I DM'd you a loader (check DMs).", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"I couldn't DM you. Here is the loader (valid {ttl}s):\n{loadstring_line}", ephemeral=True)

# ---------- aiohttp webserver routes ----------
routes = web.RouteTableDef()

LOADER_LUA_TEMPLATE = r'''-- Loader GUI (served by auth server)
local API_REDEEM = "{redeem_url}"
local TIMEOUT_SECONDS = 10
local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")
local LocalPlayer = Players.LocalPlayer

local screen = Instance.new("ScreenGui")
screen.Name = "Loader_Gui"
screen.ResetOnSpawn = false
screen.Parent = game.CoreGui

local frame = Instance.new("Frame", screen)
frame.Size = UDim2.new(0, 420, 0, 160)
frame.Position = UDim2.new(0.5, -210, 0.5, -80)
frame.AnchorPoint = Vector2.new(0.5, 0.5)
frame.BackgroundColor3 = Color3.fromRGB(30,30,30)

local title = Instance.new("TextLabel", frame)
title.Size = UDim2.new(1, -20, 0, 30)
title.Position = UDim2.new(0, 10, 0, 10)
title.BackgroundTransparency = 1
title.Text = "Redeem Key"
title.TextColor3 = Color3.new(1,1,1)
title.Font = Enum.Font.GothamBold
title.TextSize = 20

local input = Instance.new("TextBox", frame)
input.Size = UDim2.new(1, -20, 0, 36)
input.Position = UDim2.new(0, 10, 0, 48)
input.ClearTextOnFocus = false
input.PlaceholderText = "Paste key from bot /genkey here..."
input.TextColor3 = Color3.new(1,1,1)
input.BackgroundColor3 = Color3.fromRGB(45,45,45)
input.Font = Enum.Font.Gotham
input.TextSize = 16

local status = Instance.new("TextLabel", frame)
status.Size = UDim2.new(1, -20, 0, 28)
status.Position = UDim2.new(0, 10, 0, 92)
status.BackgroundTransparency = 1
status.Text = "Status: idle"
status.TextColor3 = Color3.fromRGB(200,200,200)
status.Font = Enum.Font.Gotham
status.TextSize = 14

local btn = Instance.new("TextButton", frame)
btn.Size = UDim2.new(0, 140, 0, 36)
btn.Position = UDim2.new(1, -150, 1, -46)
btn.AnchorPoint = Vector2.new(1,1)
btn.Text = "Redeem"
btn.Font = Enum.Font.GothamBold
btn.TextSize = 16
btn.TextColor3 = Color3.new(1,1,1)
btn.BackgroundColor3 = Color3.fromRGB(70,130,180)

local function setStatus(t)
    status.Text = "Status: " .. t
end

local function http_get(url)
    if typeof(syn) == "table" and syn.request then
        local ok, res = pcall(syn.request, {Url = url, Method = "GET", Timeout = TIMEOUT_SECONDS})
        if ok and res and (res.StatusCode == 200 or tonumber(res.StatusCode) == 200) then return true, res.Body end
        return false, res and (res.Body or ("Status: "..tostring(res.StatusCode))) or "request failed"
    end
    if typeof(http_request) == "function" then
        local ok, res = pcall(http_request, {Url = url, Method = "GET", Timeout = TIMEOUT_SECONDS})
        if ok and res and (res.Success == true or res.Success == nil) then return true, res.Body end
        return false, res and (res.Body or "request failed") or "request failed"
    end
    local ok, body = pcall(function() return game:HttpGet(url) end)
    if ok and body then return true, body end
    return false, body or "HttpGet failed"
end

local function redeemKey(key)
    if not key or key:match("^%s*$") then setStatus("enter a key"); return end
    setStatus("redeeming...")
    local rid = tostring(LocalPlayer.UserId or 0)
    local rname = LocalPlayer.Name or ""
    local url = API_REDEEM .. key .. "?roblox_id=" .. rid .. "&roblox_name=" .. HttpService:UrlEncode(rname)
    local ok, body = http_get(url)
    if not ok then setStatus("http error"); return end
    local okjson, parsed = pcall(function() return HttpService:JSONDecode(body) end)
    if not okjson or not parsed or not parsed.script_b64 then setStatus("invalid/expired key"); return end
    setStatus("valid! running script...")
    local decoded = HttpService:Base64Decode(parsed.script_b64)
    pcall(function() loadstring(decoded)() end)
    setStatus("done")
end

btn.MouseButton1Click:Connect(function()
    redeemKey(input.Text)
end)

input.FocusLost:Connect(function(enter)
    if enter then redeemKey(input.Text) end
end)
'''

@routes.get("/files/loaders/{script_id}/{token}.lua")
async def serve_loader(request: web.Request):
    script_id = request.match_info.get("script_id")
    token = request.match_info.get("token")
    if not script_id or not token:
        return web.Response(text="bad request", status=400)

    # consume loader token message from LOADER_STORE channel
    data = await consume_loader_channel(token)
    if not data:
        logger.info("Invalid/expired loader token request script=%s token=%s", script_id, token[:8])
        return web.Response(text="invalid or expired loader token", status=403)

    if data.get("script_id") != script_id:
        logger.info("Loader token/script mismatch token=%s requested=%s actual=%s", token[:8], script_id, data.get("script_id"))
        return web.Response(text="invalid token for script", status=403)

    loader_lua = LOADER_LUA_TEMPLATE.format(redeem_url=f"https://{RENDER_DOMAIN}/redeem/")
    logger.info("Served loader for script %s (token prefix=%s)", script_id, token[:8])
    return web.Response(text=loader_lua, content_type="text/plain")

@routes.get("/redeem/{key}")
async def redeem_route(request: web.Request):
    key = request.match_info.get("key")
    if not key:
        return web.Response(text="bad request", status=400)

    roblox_id = request.query.get("roblox_id") or request.headers.get("X-Roblox-UserId") or ""
    roblox_name = request.query.get("roblox_name") or request.headers.get("X-Roblox-Name") or ""

    row = await consume_key_channel(key, roblox_id or "unknown", roblox_name or "unknown")
    if not row:
        return web.Response(text="invalid or expired key", status=403)

    script_id = row.get("script_id")
    if script_id not in OBFUSCATED_STORE:
        return web.Response(text="script not found", status=404)
    lua_text = OBFUSCATED_STORE.get(script_id)
    # log redemption to discord channel
    await log_redemption(script_id, key, roblox_id or "unknown", roblox_name or "unknown")

    encoded = base64.b64encode(lua_text.encode("utf-8")).decode("ascii")
    resp = {"script_b64": encoded, "key": key, "redeemed_by": {"roblox_id": roblox_id or "", "roblox_name": roblox_name or ""}, "script_id": script_id}
    return web.json_response(resp)

async def make_app():
    app = web.Application()
    app.add_routes(routes)
    return app

# ---------- Entrypoint ----------
async def main():
    app = await make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Web server started on port %s", PORT)

    try:
        await client.start(DISCORD_TOKEN)
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
