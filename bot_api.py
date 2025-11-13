import os
import random
import string
import threading
import base64
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, Response
import discord
from discord.ext import commands
from discord import app_commands, ui, ButtonStyle

# -----------------------
# Configuration
# -----------------------
KEY_LIFETIME_MINUTES = int(os.getenv("KEY_LIFETIME_MINUTES", "10"))
RENDER_DOMAIN = os.getenv("RENDER_DOMAIN", "authbot-hn9s.onrender.com")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

# -----------------------
# App + in-memory store
# -----------------------
app = Flask(__name__)
valid_keys = {}  # key -> {discord_name, discord_id, created_at, script_id(optional)}

# -----------------------
# Discord bot setup
# -----------------------
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree
log_channel = None


# -----------------------
# Revoke button view
# -----------------------
class RevokeButton(ui.View):
    def __init__(self, key):
        super().__init__(timeout=None)
        self.key = key

    @ui.button(label="Revoke Key", style=ButtonStyle.danger)
    async def revoke(self, interaction: discord.Interaction, button: ui.Button):
        if self.key in valid_keys:
            del valid_keys[self.key]
            await interaction.response.send_message(f"🔒 Key `{self.key}` revoked.", ephemeral=True)
            try:
                embed = interaction.message.embeds[0]
                new_embed = embed.copy()
                new_embed.color = discord.Color.red()
                new_embed.add_field(name="Revoked By", value=f"{interaction.user} ({interaction.user.id})", inline=False)
                new_embed.set_footer(text=f"Revoked at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
                await interaction.message.edit(embed=new_embed, view=None)
            except Exception:
                pass
        else:
            await interaction.response.send_message("❌ Already revoked.", ephemeral=True)


# -----------------------
# Helper: create key
# -----------------------
def make_key(owner_name: str, owner_id: int, script_id: str = None):
    key = ''.join(random.choices(string.ascii_letters + string.digits, k=24))
    valid_keys[key] = {
        "discord_name": owner_name,
        "discord_id": owner_id,
        "created_at": datetime.utcnow().isoformat(),
        "script_id": script_id,
    }
    return key


# -----------------------
# Redeem endpoint (called by loader running inside Roblox client)
# -----------------------
# returns JSON: { "ok": true, "script_b64": "<base64>", "script_id": "...", "key": "..." }
@app.route("/redeem/<key>", methods=["GET"])
def redeem_key(key):
    # optional: script_id param for extra validation
    script_id = request.args.get("script_id", "")
    roblox_name = request.args.get("roblox_name", "")  # from the client loader, optional
    # validate key exists
    info = valid_keys.get(key)
    if not info:
        return jsonify({"ok": False, "error": "invalid_or_expired"}), 403

    # check TTL
    created = datetime.fromisoformat(info["created_at"])
    if datetime.utcnow() - created > timedelta(minutes=KEY_LIFETIME_MINUTES):
        # expire
        del valid_keys[key]
        return jsonify({"ok": False, "error": "expired"}), 403

    # Optional: enforce script_id match if stored (not required)
    if info.get("script_id") and script_id and info["script_id"] != script_id:
        return jsonify({"ok": False, "error": "script_mismatch"}), 403

    # load actual script from env var: script_id uppercased
    if not script_id:
        return jsonify({"ok": False, "error": "missing_script_id"}), 400
    env_name = script_id.upper()
    script_code = os.getenv(env_name)
    if not script_code:
        return jsonify({"ok": False, "error": "script_not_found"}), 404

    # consume the key (one-time)
    try:
        del valid_keys[key]
    except KeyError:
        pass

    # log redemption to discord (async)
    try:
        if log_channel:
            embed = discord.Embed(title="🔑 Key Redeemed", color=discord.Color.green(), timestamp=datetime.utcnow())
            embed.add_field(name="Script ID", value=script_id, inline=False)
            embed.add_field(name="Key", value=f"`{key}`", inline=False)
            embed.add_field(name="Redeemed by (roblox)", value=f"{roblox_name or 'unknown'}", inline=False)
            embed.add_field(name="Key Creator (discord)", value=f"{info.get('discord_name')} ({info.get('discord_id')})", inline=False)
            view = RevokeButton(key)
            bot.loop.create_task(log_channel.send(embed=embed, view=view))
    except Exception:
        pass

    # Return the script as base64 to avoid text/encoding problems
    b64 = base64.b64encode(script_code.encode("utf-8")).decode("ascii")
    return jsonify({"ok": True, "script_b64": b64, "script_id": script_id})


# -----------------------
# Loader file endpoint (NO KEY in URL)
# serves a small loader that expects a global 'script_key' variable
# -----------------------
@app.route("/files/loaders/<script_id>/<file_id>.lua")
def serve_loader(script_id, file_id):
    """
    Returns a small loader Lua that:
      1) reads global `script_key` set by the executor user
      2) calls /redeem/<key>?script_id=<script_id>&roblox_name=<PlayerName>
      3) on success decodes base64 script and runs it via loadstring
    """
    domain = RENDER_DOMAIN
    loader_lua = f'''-- Loader (no key in URL). Set script_key before running the loadstring.
local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")
local player = Players.LocalPlayer

-- script_key must be defined by the user before running this loader:
-- script_key = "YOUR_KEY"
if not script_key or type(script_key) ~= "string" or script_key == "" then
    error("script_key not set; please put your key in script_key before running.")
    return
end

local function safe_get(url)
    local ok, res = pcall(function() return HttpService:GetAsync(url) end)
    if not ok then return nil, res end
    return res, nil
end

local key = script_key
local redeem_url = string.format("https://{domain}/redeem/%s?script_id={script_id}&roblox_name=%s", key, HttpService:UrlEncode(player.Name))
local body, err = safe_get(redeem_url)
if not body then
    warn("Failed to contact auth server:", err)
    return
end

local ok, data = pcall(function() return HttpService:JSONDecode(body) end)
if not ok or not data or not data.ok then
    warn("Key redeem failed:", (data and data.error) or tostring(body))
    return
end

-- decode base64 script and run it
local b64 = data.script_b64
local bytes = HttpService:Base64Decode(b64)
local func, load_err = loadstring(bytes)
if not func then
    warn("Failed to load script:", load_err)
    return
end

-- run the protected script
pcall(func)
'''
    # Return loader as plain text
    return Response(loader_lua, mimetype="text/plain")


# -----------------------
# Bot Slash commands (genkey, deletekey, listkeys, script)
# -----------------------
@tree.command(name="genkey", description="Generate a one-time script key")
async def genkey(interaction: discord.Interaction, script_name: str = None):
    k = make_key(interaction.user.name, interaction.user.id, script_id=script_name.lower() if script_name else None)
    await interaction.response.send_message(f"✅ Key: `{k}` (one-time, expires in {KEY_LIFETIME_MINUTES} minutes)", ephemeral=True)


@tree.command(name="deletekey", description="Delete/revoke a key")
async def deletekey(interaction: discord.Interaction, key: str):
    if key in valid_keys:
        del valid_keys[key]
        await interaction.response.send_message(f"🗑️ Key `{key}` deleted.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Key not found.", ephemeral=True)


@tree.command(name="listkeys", description="Show active keys")
async def listkeys(interaction: discord.Interaction):
    if not valid_keys:
        await interaction.response.send_message("No active keys", ephemeral=True)
        return
    embed = discord.Embed(title="Active Keys", color=discord.Color.blue())
    now = datetime.utcnow()
    for key, info in list(valid_keys.items()):
        created = datetime.fromisoformat(info["created_at"])
        remaining = timedelta(minutes=KEY_LIFETIME_MINUTES) - (now - created)
        if remaining.total_seconds() <= 0:
            del valid_keys[key]
            continue
        embed.add_field(name=f"`{key}`", value=f"{info['discord_name']} ({info['discord_id']}) — expires in {int(remaining.total_seconds()//60)}m", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="script", description="Get loader link for a script id")
async def script(interaction: discord.Interaction, script_name: str):
    script_id = script_name.lower()
    file_id = ''.join(random.choices('abcdef0123456789', k=40))
    url = f"https://{RENDER_DOMAIN}/files/loaders/{script_id}/{file_id}.lua"
    # DM the user the loader; they must set script_key in their executor before running it
    loadstring_code = f'-- put your key in `script_key` then run\\nscript_key = "YOUR_KEY"\\nloadstring(game:HttpGet("{url}"))()'
    try:
        await interaction.user.send(f"Your loader for `{script_id}`:\n```lua\n{loadstring_code}\n```")
        await interaction.response.send_message("✅ Sent loader to DMs. Put your key in `script_key` and run it.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("Unable to DM you. Enable DMs and try again.", ephemeral=True)


# -----------------------
# Bot events
# -----------------------
@bot.event
async def on_ready():
    global log_channel
    print(f"Logged in as {bot.user}")
    await tree.sync()
    if LOG_CHANNEL_ID:
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            print("Logging channel ready:", log_channel.id)


# -----------------------
# Run Flask + Bot
# -----------------------
def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))

if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    bot.run(os.getenv("DISCORD_TOKEN"))
