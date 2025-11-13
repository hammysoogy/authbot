import os
import random
import string
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime
from flask import Flask, jsonify

# --- Discord setup ---
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
TOKEN = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))

# --- Flask setup (Render will use this) ---
app = Flask(__name__)

# --- In-memory key storage (replace with Redis or database later) ---
keys = {}  # {key: {"used": False, "user": None, "script_id": None}}
active_scripts = {}  # {user_id: script_id}


# --- Helper function to generate random keys ---
def generate_key():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))


# --- Discord Views ---
class RevokeKeyView(discord.ui.View):
    def __init__(self, key):
        super().__init__(timeout=None)
        self.key = key

    @discord.ui.button(label="Revoke Key", style=discord.ButtonStyle.danger)
    async def revoke(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.key in keys:
            del keys[self.key]
            await interaction.response.send_message(f"✅ Key `{self.key}` revoked successfully!", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ This key has already been revoked or doesn’t exist.", ephemeral=True)


# --- Commands ---
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user}")


@bot.tree.command(name="genkey", description="Generate a new one-time key")
async def genkey(interaction: discord.Interaction):
    key = generate_key()
    keys[key] = {"used": False, "user": None, "script_id": None}
    await interaction.response.send_message(f"✅ Your key: `{key}` (one-time use)", ephemeral=True)


@bot.tree.command(name="delete", description="Delete/revoke a key manually")
@app_commands.describe(key="The key to revoke")
async def delete(interaction: discord.Interaction, key: str):
    if key in keys:
        del keys[key]
        await interaction.response.send_message(f"🗑️ Key `{key}` deleted.", ephemeral=True)
    else:
        await interaction.response.send_message("⚠️ Key not found.", ephemeral=True)


@bot.tree.command(name="script", description="Get your unique script")
async def script(interaction: discord.Interaction):
    script_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
    active_scripts[interaction.user.id] = script_id

    loader = f'loadstring(game:HttpGet("https://authbot-hn9s.onrender.com/files/loaders/{script_id}.lua"))()'
    await interaction.user.send(f"🧠 **Your script ID:** `{script_id}`\n\nPaste this in your executor:\n```lua\n{loader}\n```")
    await interaction.response.send_message("📩 Sent your loader to DMs!", ephemeral=True)


# --- Web endpoint: Key redemption ---
@app.route("/redeem/<key>/<script_id>/<user>", methods=["GET"])
def redeem(key, script_id, user):
    if key not in keys:
        return jsonify({"success": False, "message": "Invalid key"}), 403

    if keys[key]["used"]:
        return jsonify({"success": False, "message": "Key already used"}), 403

    keys[key]["used"] = True
    keys[key]["user"] = user
    keys[key]["script_id"] = script_id

    # Log to Discord
    embed = discord.Embed(title="🔑 Key Redeemed", color=discord.Color.green())
    embed.add_field(name="Script ID", value=script_id, inline=False)
    embed.add_field(name="Key", value=f"`{key}`", inline=False)
    embed.add_field(name="Redeemed by", value=user, inline=False)
    embed.set_footer(text=f"Redeemed at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")

    view = RevokeKeyView(key)

    async def send_embed():
        channel = bot.get_channel(LOG_CHANNEL_ID)
        if channel:
            await channel.send(embed=embed, view=view)

    bot.loop.create_task(send_embed())

    return jsonify({"success": True, "message": "yay u done it auth system up and working!"})


# --- Web endpoint: Key validation ---
@app.route("/api/validate/<key>", methods=["GET"])
def validate_key(key):
    if key in keys and not keys[key]["used"]:
        return jsonify({"valid": True})
    return jsonify({"valid": False})


# --- Run bot + web ---
import threading

def run_flask():
    app.run(host="0.0.0.0", port=10000)

threading.Thread(target=run_flask).start()
bot.run(TOKEN)
