import os
import random
import string
import threading
from datetime import datetime, timedelta
from flask import Flask, request, Response
import discord
from discord.ext import commands
from discord import ui, ButtonStyle

# ----------------------
# Configuration
# ----------------------
KEY_LIFETIME_MINUTES = int(os.getenv("KEY_LIFETIME_MINUTES", "10"))
RENDER_DOMAIN = os.getenv("RENDER_DOMAIN", "authbot-hn9s.onrender.com")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# The external script URL you want the loader to execute
EXTERNAL_SCRIPT_URL = "https://pastefy.app/1YBZrX1C/raw"

# ----------------------
# App + in-memory store
# ----------------------
app = Flask(__name__)
valid_keys = {}  # key -> {discord_name, discord_id, created_at, script_id(optional)}

# ----------------------
# Helper functions
# ----------------------
def make_key(owner_name: str, owner_id: int, script_id: str = None):
    key = ''.join(random.choices(string.ascii_letters + string.digits, k=24))
    valid_keys[key] = {
        "discord_name": owner_name,
        "discord_id": owner_id,
        "created_at": datetime.utcnow().isoformat(),
        "script_id": script_id,
    }
    return key

def key_is_valid_and_consume(key: str, script_id: str = None) -> (bool, str):
    """
    Validate TTL and script id if provided. If valid, consume (delete) the key and return (True, "").
    If invalid, return (False, reason).
    """
    info = valid_keys.get(key)
    if not info:
        return False, "invalid_or_missing"
    created = datetime.fromisoformat(info["created_at"])
    if datetime.utcnow() - created > timedelta(minutes=KEY_LIFETIME_MINUTES):
        # expired
        del valid_keys[key]
        return False, "expired"
    # optional script scope
    if info.get("script_id") and script_id and info["script_id"] != script_id:
        return False, "script_mismatch"
    # consume
    try:
        del valid_keys[key]
    except KeyError:
        pass
    return True, ""

# ----------------------
# Flask route: serve lua when ?key= is provided
# ----------------------
@app.route("/files/loaders/<script_id>/<file_id>.lua")
def serve_loader_with_key(script_id, file_id):
    """
    If ?key= is present and valid, return a small loader Lua which fetches and runs EXTERNAL_SCRIPT_URL.
    Otherwise deny (403) with a short message (no script disclosure).
    """
    key = request.args.get("key")
    if not key:
        return Response("-- Missing key parameter. Access denied.", mimetype="text/plain"), 403

    ok, reason = key_is_valid_and_consume(key, script_id=script_id)
    if not ok:
        # do not reveal details beyond a short reason
        if reason == "expired":
            return Response("-- Key expired. Please generate a new key.", mimetype="text/plain"), 403
        if reason == "script_mismatch":
            return Response("-- Key not valid for this script.", mimetype="text/plain"), 403
        return Response("-- Invalid or missing key. Access denied.", mimetype="text/plain"), 403

    # Build loader Lua that executes the external script URL
    # This loader is minimal and immediately runs the external raw URL via game:HttpGet + loadstring.
    loader_lua = (
        f'-- Loader returned by auth server (executes external script)\n'
        f'local ok, res = pcall(function() return game:HttpGet("{EXTERNAL_SCRIPT_URL}") end)\n'
        f'if not ok or not res then warn("Failed to fetch external script:", res); return end\n'
        f'local fn, err = loadstring(res)\n'
        f'if not fn then warn("Failed to load external script:", err); return end\n'
        f'pcall(fn)\n'
    )

    # Log to Discord (non-blocking)
    try:
        if LOG_CHANNEL_ID and bot and bot.is_ready():
            embed = discord.Embed(
                title="🧩 Script Executed",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Script ID", value=script_id, inline=False)
            embed.add_field(name="Key (consumed)", value=f"`{key}`", inline=False)
            embed.set_footer(text="Auth System Log")
            view = RevokeButton(key)
            bot.loop.create_task(log_channel.send(embed=embed, view=view))
    except Exception:
        pass

    return Response(loader_lua, mimetype="text/plain")

# ----------------------
# Discord Bot Setup
# ----------------------
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree
log_channel = None

# ----------------------
# Revoke button view
# ----------------------
class RevokeButton(ui.View):
    def __init__(self, key):
        super().__init__(timeout=None)
        self.key = key

    @ui.button(label="Revoke Key", style=ButtonStyle.danger)
    async def revoke(self, interaction: discord.Interaction, button: ui.Button):
        # If key exists (unlikely after consumption) remove it; otherwise report already consumed
        if self.key in valid_keys:
            del valid_keys[self.key]
            await interaction.response.send_message(f"🔒 Key `{self.key}` revoked.", ephemeral=True)
            try:
                embed = interaction.message.embeds[0]
                new_embed = embed.copy()
                new_embed.color = discord.Color.red()
                new_embed.add_field(name="Revoked By", value=f"{interaction.user} ({interaction.user.id})", inline=False)
                await interaction.message.edit(embed=new_embed, view=None)
            except Exception:
                pass
        else:
            await interaction.response.send_message("❌ Key already consumed or revoked.", ephemeral=True)

# ----------------------
# Slash commands
# ----------------------
@tree.command(name="genkey", description="Generate a one-time script key (optionally tied to a script id)")
async def genkey(interaction: discord.Interaction, script_name: str = None):
    script_id = script_name.lower() if script_name else None
    k = make_key(interaction.user.name, interaction.user.id, script_id=script_id)
    # DM user the key and instructions
    url_example = f'https://{RENDER_DOMAIN}/files/loaders/{script_id or "script_id"}/<fileid>.lua?key={k}'
    try:
        await interaction.user.send(f"🔑 Your key: `{k}`\nLoader example:\n```lua\nloadstring(game:HttpGet(\"{url_example}\"))()\n```")
        await interaction.response.send_message("✅ Key generated and DM'd to you.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"✅ Key: `{k}` (unable to DM) — use the URL: {url_example}", ephemeral=True)

@tree.command(name="script", description="Get a loader URL for a script id (placeholder key)")
async def script(interaction: discord.Interaction, script_name: str):
    script_id = script_name.lower()
    file_id = ''.join(random.choices('abcdef0123456789', k=40))
    url = f"https://{RENDER_DOMAIN}/files/loaders/{script_id}/{file_id}.lua?key={{key}}"
    loadstring_code = f'loadstring(game:HttpGet("{url}"))()'
    try:
        await interaction.user.send(f"Loader for `{script_id}`:\n```lua\n{loadstring_code}\n```")
        await interaction.response.send_message("📩 Sent loader URL to your DMs (replace {key} with your key)", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"Loader: ```lua\n{loadstring_code}\n```", ephemeral=True)

@tree.command(name="listkeys", description="List active (unexpired) keys")
async def listkeys(interaction: discord.Interaction):
    if not valid_keys:
        await interaction.response.send_message("No active keys.", ephemeral=True)
        return
    embed = discord.Embed(title="🔑 Active Keys", color=discord.Color.blue())
    now = datetime.utcnow()
    for key, info in list(valid_keys.items()):
        created = datetime.fromisoformat(info["created_at"])
        remaining = timedelta(minutes=KEY_LIFETIME_MINUTES) - (now - created)
        if remaining.total_seconds() <= 0:
            del valid_keys[key]
            continue
        embed.add_field(name=f"`{key}`", value=f"{info['discord_name']} ({info['discord_id']}) — expires in {int(remaining.total_seconds()//60)}m", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="deletekey", description="Delete a specific key")
async def deletekey(interaction: discord.Interaction, key: str):
    if key in valid_keys:
        del valid_keys[key]
        await interaction.response.send_message(f"🗑️ Key `{key}` deleted.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Key not found.", ephemeral=True)

# ----------------------
# Bot setup
# ----------------------
@bot.event
async def on_ready():
    global log_channel
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    await tree.sync()
    if LOG_CHANNEL_ID:
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            print(f"Logging to channel: {log_channel.id}")
        else:
            print("Warning: LOG_CHANNEL_ID set but channel not found (bot may lack permissions).")

# ----------------------
# Run both
# ----------------------
def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))

if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    bot.run(DISCORD_TOKEN)
