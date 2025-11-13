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
DEFAULT_KEY_LIFETIME_MINUTES = int(os.getenv("KEY_LIFETIME_MINUTES", "10"))
RENDER_DOMAIN = os.getenv("RENDER_DOMAIN", "authbot-hn9s.onrender.com")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# The external script URL you want to execute
EXTERNAL_SCRIPT_URL = "https://pastefy.app/1YBZrX1C/raw"

# ----------------------
# App + in-memory store
# ----------------------
app = Flask(__name__)
valid_keys = {}  # key -> info dict

# ----------------------
# Helper
# ----------------------
def make_key(owner_name: str, owner_id: int, script_id: str = None, lifetime=None):
    key = ''.join(random.choices(string.ascii_letters + string.digits, k=24))
    expires_at = None
    if lifetime != "infinite":
        minutes = lifetime or DEFAULT_KEY_LIFETIME_MINUTES
        expires_at = (datetime.utcnow() + timedelta(minutes=minutes)).isoformat()

    valid_keys[key] = {
        "discord_name": owner_name,
        "discord_id": owner_id,
        "created_at": datetime.utcnow().isoformat(),
        "expires_at": expires_at,
        "script_id": script_id,
    }
    return key


def key_is_valid(key: str, script_id: str = None):
    info = valid_keys.get(key)
    if not info:
        return False, "invalid_or_missing"
    if info["expires_at"]:
        expires_at = datetime.fromisoformat(info["expires_at"])
        if datetime.utcnow() > expires_at:
            del valid_keys[key]
            return False, "expired"
    if info.get("script_id") and script_id and info["script_id"] != script_id:
        return False, "script_mismatch"
    return True, ""


# ----------------------
# Flask route
# ----------------------
@app.route("/files/loaders/<script_id>/<file_id>.lua")
def serve_loader(script_id, file_id):
    key = request.args.get("key")
    if not key:
        return Response("-- Missing key. Access denied.", mimetype="text/plain"), 403

    ok, reason = key_is_valid(key, script_id)
    if not ok:
        msg = {
            "invalid_or_missing": "-- Invalid key. Access denied.",
            "expired": "-- Key expired. Generate a new one.",
            "script_mismatch": "-- Key not valid for this script."
        }.get(reason, "-- Access denied.")
        return Response(msg, mimetype="text/plain"), 403

    # Return loader that runs your external Pastefy script
    loader_lua = (
        f'-- Authenticated loader\n'
        f'-- Key: {key}\n'
        f'local ok, res = pcall(function() return game:HttpGet("{EXTERNAL_SCRIPT_URL}") end)\n'
        f'if not ok or not res then warn("Failed to fetch external script:", res); return end\n'
        f'local fn, err = loadstring(res)\n'
        f'if not fn then warn("Failed to load external script:", err); return end\n'
        f'pcall(fn)\n'
    )

    # Log use to Discord
    try:
        if LOG_CHANNEL_ID and bot and bot.is_ready():
            embed = discord.Embed(
                title="🧩 Script Executed",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Script ID", value=script_id, inline=False)
            embed.add_field(name="Key Used", value=f"`{key}`", inline=False)
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
# Revoke Button
# ----------------------
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
                new_embed.add_field(name="Revoked By", value=f"{interaction.user}", inline=False)
                await interaction.message.edit(embed=new_embed, view=None)
            except Exception:
                pass
        else:
            await interaction.response.send_message("❌ Key already invalid.", ephemeral=True)


# ----------------------
# Slash Commands
# ----------------------
@tree.command(name="genkey", description="Generate a reusable script key (optionally set minutes or 'infinite')")
async def genkey(interaction: discord.Interaction, script_name: str = None, lifetime: str = None):
    script_id = script_name.lower() if script_name else None
    lifetime_value = None
    if lifetime:
        if lifetime.lower() == "infinite":
            lifetime_value = "infinite"
        else:
            try:
                lifetime_value = int(lifetime)
            except ValueError:
                await interaction.response.send_message("⚠️ Invalid lifetime. Use a number (minutes) or 'infinite'.", ephemeral=True)
                return

    key = make_key(interaction.user.name, interaction.user.id, script_id, lifetime=lifetime_value)
    file_id = ''.join(random.choices('abcdef0123456789', k=40))
    url = f"https://{RENDER_DOMAIN}/files/loaders/{script_id or 'script'}/{file_id}.lua?key={key}"
    code = f'loadstring(game:HttpGet("{url}"))()'

    try:
        await interaction.user.send(f"✅ Key generated: `{key}`\nLifetime: `{lifetime or DEFAULT_KEY_LIFETIME_MINUTES}` minutes\n```lua\n{code}\n```")
        await interaction.response.send_message("📩 Key sent to your DMs.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"✅ Key: `{key}`\n```lua\n{code}\n```", ephemeral=True)


@tree.command(name="listkeys", description="List all currently valid keys")
async def listkeys(interaction: discord.Interaction):
    if not valid_keys:
        await interaction.response.send_message("No active keys.", ephemeral=True)
        return

    embed = discord.Embed(title="🔑 Active Keys", color=discord.Color.blurple())
    now = datetime.utcnow()
    for key, info in list(valid_keys.items()):
        exp = info["expires_at"]
        if exp:
            exp_dt = datetime.fromisoformat(exp)
            if exp_dt < now:
                del valid_keys[key]
                continue
            remaining = int((exp_dt - now).total_seconds() / 60)
            remain_str = f"{remaining}m left"
        else:
            remain_str = "∞ Infinite"

        embed.add_field(
            name=f"`{key}`",
            value=f"👤 {info['discord_name']} ({info['discord_id']})\n⏳ {remain_str}",
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="deletekey", description="Delete a specific key")
async def deletekey(interaction: discord.Interaction, key: str):
    if key in valid_keys:
        del valid_keys[key]
        await interaction.response.send_message(f"🗑️ Key `{key}` deleted.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Key not found.", ephemeral=True)


# ----------------------
# Bot Events
# ----------------------
@bot.event
async def on_ready():
    global log_channel
    print(f"✅ Logged in as {bot.user}")
    await tree.sync()
    if LOG_CHANNEL_ID:
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            print(f"📝 Logging to: {log_channel.name}")
        else:
            print("⚠️ Log channel not found.")


# ----------------------
# Run Flask + Bot
# ----------------------
def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    bot.run(DISCORD_TOKEN)
