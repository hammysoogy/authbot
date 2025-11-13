import os
import random
import string
import threading
from datetime import datetime
from flask import Flask, request, jsonify, Response
import discord
from discord.ext import commands
from discord import app_commands, ui, ButtonStyle

# =====================
# Flask API
# =====================
app = Flask(__name__)

valid_keys = {}  # in-memory key store

@app.route("/files/loaders/<script_id>/<file_id>.lua")
def serve_script(script_id, file_id):
    """Serve script if key is valid"""
    key = request.args.get("key")
    if not key or key not in valid_keys:
        return Response("-- Invalid or missing key.", mimetype="text/plain"), 403

    env_var_name = script_id.upper()
    script_code = os.getenv(env_var_name)
    if not script_code:
        return jsonify({"error": "Unknown script ID"}), 404

    user_info = valid_keys[key]
    if "log_channel" in globals() and log_channel:
        embed = discord.Embed(
            title="🧩 Script Executed",
            color=discord.Color.green(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="User", value=f"{user_info.get('discord_name', 'Unknown')} ({user_info.get('discord_id')})", inline=False)
        embed.add_field(name="Script ID", value=script_id, inline=False)
        embed.add_field(name="Key", value=f"`{key}`", inline=False)
        embed.set_footer(text="Auth System Log")
        view = RevokeButton(key)
        bot.loop.create_task(log_channel.send(embed=embed, view=view))

    return Response(script_code, mimetype="text/plain")


# =====================
# Discord Bot Setup
# =====================
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree


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
            await interaction.response.send_message("❌ Already revoked.", ephemeral=True)


# =====================
# Slash Commands
# =====================
@tree.command(name="genkey", description="Generate a one-time script key")
async def genkey(interaction: discord.Interaction):
    key = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    valid_keys[key] = {
        "discord_name": interaction.user.name,
        "discord_id": interaction.user.id,
        "created_at": datetime.utcnow().isoformat()
    }
    await interaction.response.send_message(f"✅ Key generated: `{key}`", ephemeral=True)


@tree.command(name="deletekey", description="Delete a specific key")
async def deletekey(interaction: discord.Interaction, key: str):
    if key in valid_keys:
        del valid_keys[key]
        await interaction.response.send_message(f"🗑️ Key `{key}` deleted.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Key not found.", ephemeral=True)


@tree.command(name="listkeys", description="List all active keys")
async def listkeys(interaction: discord.Interaction):
    if not valid_keys:
        await interaction.response.send_message("No active keys currently.", ephemeral=True)
        return

    embed = discord.Embed(title="🔑 Active Keys", color=discord.Color.blue())
    for key, info in valid_keys.items():
        created = info.get("created_at", "unknown")
        embed.add_field(
            name=f"`{key}`",
            value=f"👤 {info['discord_name']} (`{info['discord_id']}`)\n🕒 {created}",
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="script", description="Get your script loader")
async def script(interaction: discord.Interaction, script_name: str):
    script_id = script_name.lower()
    random_file_id = ''.join(random.choices('abcdef0123456789', k=40))
    url = f"https://{os.getenv('RENDER_DOMAIN')}/files/loaders/{script_id}/{random_file_id}.lua?key={{key}}"
    loadstring_code = f'loadstring(game:HttpGet("{url}"))()'

    await interaction.user.send(
        f"✅ Your loader for `{script_name}`:\n```lua\n{loadstring_code}\n```\nReplace `{{key}}` with your key from `/genkey`."
    )
    await interaction.response.send_message("📩 Sent your loader via DM!", ephemeral=True)


# =====================
# /embed Command
# =====================
@tree.command(name="embed", description="Create a rich styled embed message.")
async def embed_command(
    interaction: discord.Interaction,
    title: str,
    description: str,
    script: str = None,
    color: str = "blue",
    footer: str = None
):
    """Send a rich embed with optional Lua script block."""
    color_map = {
        "blue": discord.Color.blue(),
        "green": discord.Color.green(),
        "red": discord.Color.red(),
        "gold": discord.Color.gold(),
        "purple": discord.Color.purple()
    }
    embed_color = color_map.get(color.lower(), discord.Color.blue())

    embed = discord.Embed(title=title, description=description, color=embed_color)

    if script:
        embed.add_field(name="📜 Script:", value=f"```lua\n{script}\n```", inline=False)
    if footer:
        embed.set_footer(text=footer)

    await interaction.response.send_message(embed=embed)
    print(f"✅ Embed posted by {interaction.user}")


# =====================
# Bot Events
# =====================
@bot.event
async def on_ready():
    global log_channel
    print(f"✅ Logged in as {bot.user}")
    await tree.sync()
    log_channel_id = int(os.getenv("LOG_CHANNEL_ID", "0"))
    if log_channel_id:
        log_channel = bot.get_channel(log_channel_id)
        print(f"📝 Logging to: {log_channel.name if log_channel else 'not found'}")


# =====================
# Run Flask + Bot
# =====================
def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    bot.run(os.getenv("DISCORD_TOKEN"))
