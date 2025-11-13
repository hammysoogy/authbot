import os
import random
import string
from flask import Flask, request, jsonify, Response
import discord
from discord.ext import commands
from discord import app_commands, ui, ButtonStyle

# =====================
# Flask API
# =====================
app = Flask(__name__)

# In-memory key storage (replace with Redis or database later if needed)
valid_keys = {}

@app.route("/files/loaders/<script_id>/<file_id>.lua")
def serve_script(script_id, file_id):
    """Serve a Lua script only if a valid key is provided."""
    key = request.args.get("key")
    if not key or key not in valid_keys:
        return Response('-- Invalid or missing key. Access denied.', mimetype="text/plain"), 403

    env_var_name = script_id.upper()
    script_code = os.getenv(env_var_name)
    if not script_code:
        return jsonify({"error": "Unknown script ID"}), 404

    # Log to Discord channel when someone runs the script
    user_info = valid_keys[key]
    if "log_channel" in globals():
        embed = discord.Embed(
            title="🧩 Script Executed",
            description=f"**User:** {user_info['discord_name']}\n**Key:** `{key}`\n**Script:** `{script_id}`",
            color=discord.Color.green()
        )
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
            await interaction.response.send_message(
                f"🔒 Key `{self.key}` has been revoked and is now invalid!", ephemeral=True
            )
        else:
            await interaction.response.send_message("❌ Key already invalid or revoked.", ephemeral=True)


@tree.command(name="genkey", description="Generate a one-time script key")
async def genkey(interaction: discord.Interaction):
    key = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    valid_keys[key] = {"discord_name": interaction.user.name}
    await interaction.response.send_message(f"✅ Key generated: `{key}`", ephemeral=True)


@tree.command(name="deletekey", description="Delete a specific key")
async def deletekey(interaction: discord.Interaction, key: str):
    if key in valid_keys:
        del valid_keys[key]
        await interaction.response.send_message(f"🗑️ Key `{key}` deleted.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Key not found.", ephemeral=True)


@tree.command(name="script", description="Get a loader for your script")
async def script(interaction: discord.Interaction, script_name: str):
    script_id = script_name.lower()
    random_file_id = ''.join(random.choices('abcdef0123456789', k=40))
    url = f"https://{os.getenv('RENDER_DOMAIN')}/files/loaders/{script_id}/{random_file_id}.lua?key={{key}}"

    loadstring_code = f'loadstring(game:HttpGet("{url}"))()'
    await interaction.user.send(
        f"✅ Your loader for `{script_name}`:\n```lua\n{loadstring_code}\n```\n"
        "Replace `{key}` with your generated key from `/genkey`."
    )
    await interaction.response.send_message("📩 Check your DMs for the loader!", ephemeral=True)


@bot.event
async def on_ready():
    global log_channel
    print(f"✅ Logged in as {bot.user}")
    await tree.sync()

    log_channel_id = int(os.getenv("LOG_CHANNEL_ID", "0"))
    if log_channel_id:
        log_channel = bot.get_channel(log_channel_id)
        if log_channel:
            print(f"📝 Logging to channel: {log_channel.name}")


# =====================
# Run Flask + Discord Together
# =====================
import threading

def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    bot.run(os.getenv("DISCORD_TOKEN"))
