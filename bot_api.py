import os
import random
import string
import threading
from datetime import datetime
from flask import Flask, request, jsonify, Response
import discord
from discord.ext import commands
from discord import app_commands, ui, ButtonStyle

# ============================================================
# Flask API
# ============================================================
app = Flask(__name__)

# In-memory key storage (can switch to Redis or DB later)
valid_keys = {}

# ======================
# DEFAULT SCRIPT CONTENT
# ======================
DEFAULT_SCRIPT = r'''
-- Teleport GUI LocalScript
local Players = game:GetService("Players")
local TeleportService = game:GetService("TeleportService")
local player = Players.LocalPlayer
local playerGui = player:WaitForChild("PlayerGui")

local screenGui = Instance.new("ScreenGui")
screenGui.Name = "EmptyGameTP_GUI"
screenGui.ResetOnSpawn = false
screenGui.Parent = playerGui

local button = Instance.new("TextButton")
button.Name = "EmptyGameTP_Button"
button.Size = UDim2.new(0, 180, 0, 40)
button.Position = UDim2.new(0.5, -90, 0.5, -20)
button.Text = "EmptyGameTP"
button.Font = Enum.Font.GothamBold
button.TextSize = 16
button.BackgroundColor3 = Color3.fromRGB(45,45,45)
button.TextColor3 = Color3.fromRGB(255,255,255)
button.BorderSizePixel = 0
button.Parent = screenGui

local corner = Instance.new("UICorner")
corner.CornerRadius = UDim.new(0,8)
corner.Parent = button

button.MouseButton1Click:Connect(function()
    -- If a TPExploit with ToggleButton exists, try to toggle it
    if TPExploit and type(TPExploit.ToggleButton) == "function" then
        pcall(function() TPExploit.ToggleButton() end)
    end

    -- Try to get teleport data (if function exists)
    local extraData = nil
    if type(TeleportService.GetLocalPlayerTeleportData) == "function" then
        local ok, res = pcall(function()
            return TeleportService:GetLocalPlayerTeleportData()
        end)
        if ok then extraData = res end
    end

    -- Teleport the local player (safe pcall)
    pcall(function()
        TeleportService:Teleport(game.PlaceId, player, extraData)
    end)
end)
'''


# ============================================================
# Script endpoint
# ============================================================
@app.route("/files/loaders/<script_id>/<file_id>.lua")
def serve_script(script_id, file_id):
    """Serve a Lua script only if a valid key is provided."""
    key = request.args.get("key")

    # Require valid key
    if not key or key not in valid_keys:
        return Response("-- Invalid or missing key. Access denied.", mimetype="text/plain"), 403

    # Fetch script code from environment or fallback
    env_var_name = script_id.upper()
    script_code = os.getenv(env_var_name, DEFAULT_SCRIPT)

    # Log script execution
    user_info = valid_keys[key]
    if "log_channel" in globals() and log_channel:
        embed = discord.Embed(
            title="🧩 Script Executed",
            color=discord.Color.green(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="Discord User", value=f"{user_info['discord_name']} ({user_info['discord_id']})", inline=False)
        embed.add_field(name="Script ID", value=script_id, inline=True)
        embed.add_field(name="Key", value=f"`{key}`", inline=False)
        embed.set_footer(text="Auth System Log")

        view = RevokeButton(key)
        bot.loop.create_task(log_channel.send(embed=embed, view=view))

    return Response(script_code, mimetype="text/plain")


# ============================================================
# Discord Bot Setup
# ============================================================
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree


# ============================================================
# Revoke Button Class
# ============================================================
class RevokeButton(ui.View):
    def __init__(self, key):
        super().__init__(timeout=None)
        self.key = key

    @ui.button(label="Revoke Key", style=ButtonStyle.danger)
    async def revoke(self, interaction: discord.Interaction, button: ui.Button):
        if self.key in valid_keys:
            del valid_keys[self.key]
            await interaction.response.send_message(
                f"🔒 Key `{self.key}` has been revoked and is now invalid!",
                ephemeral=True
            )
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
            await interaction.response.send_message("❌ Key already invalid or revoked.", ephemeral=True)


# ============================================================
# Slash Commands
# ============================================================
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


@tree.command(name="listkeys", description="List all currently valid keys")
async def listkeys(interaction: discord.Interaction):
    if not valid_keys:
        await interaction.response.send_message("No active keys currently.", ephemeral=True)
        return

    embed = discord.Embed(title="🔑 Active Keys", color=discord.Color.blue())
    for key, info in valid_keys.items():
        created = info.get('created_at', 'unknown')
        embed.add_field(
            name=f"`{key}`",
            value=f"👤 {info['discord_name']} (`{info['discord_id']}`)\n🕒 Created: {created}",
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


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


# ============================================================
# Bot Events
# ============================================================
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
        else:
            print("⚠️ Could not find log channel, check LOG_CHANNEL_ID.")
    else:
        print("⚠️ No LOG_CHANNEL_ID set in environment.")


# ============================================================
# Run Flask + Discord Together
# ============================================================
def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    bot.run(os.getenv("DISCORD_TOKEN"))
