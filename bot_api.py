# app.py
import os
import asyncio
import time
import secrets
from aiohttp import web
import base64
import discord

# ---------- Configuration (set these in Render environment variables) ----------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")  # required
RENDER_DOMAIN = os.environ.get("RENDER_DOMAIN", "https://authbot-hn9s.onrender.com")  # your-render URL
PORT = int(os.environ.get("PORT", 10000))
TOKEN_TTL = int(os.environ.get("TOKEN_TTL", 60))  # seconds
BOT_PREFIX = os.environ.get("BOT_PREFIX", "!")
SCRIPT_STORE = {}  # script_id -> lua text; (for demo we load from env or you can load from files)

# Example small script map (replace by loading from secure storage or env vars)
# You can set environment variables like SCRIPT_<ID> or load from a secure file.
# For demo: a default demo script if none specified.
DEFAULT_SCRIPT = os.environ.get("SCRIPT_TEXT", 'print("hello from server!")')

# optional: comma-separated role IDs or user IDs allowed to request
ALLOWED_ROLE_IDS = set(x.strip() for x in os.environ.get("ALLOWED_ROLE_IDS", "").split(",") if x.strip())

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is required")

# If you want to populate scripts from env like SCRIPT_d229253... = "<lua code>"
for k, v in os.environ.items():
    if k.startswith("SCRIPT_"):
        script_id = k[len("SCRIPT_"):]
        SCRIPT_STORE[script_id] = v

# ---------- In-memory single-use token store ----------
# token -> { user_id: str, script_id: str, expires_at: float }
token_store = {}
token_lock = asyncio.Lock()

def make_token(user_id: str, script_id: str, ttl: int = TOKEN_TTL) -> str:
    token = secrets.token_hex(24)
    expires_at = time.time() + ttl
    token_store[token] = {"user_id": str(user_id), "script_id": str(script_id), "expires_at": expires_at}
    return token

def validate_and_consume_token(token: str, script_id: str, user_id: str) -> bool:
    row = token_store.get(token)
    if not row:
        return False
    if row["user_id"] != str(user_id):
        return False
    if row["script_id"] != str(script_id):
        return False
    if time.time() > row["expires_at"]:
        token_store.pop(token, None)
        return False
    # consume token
    token_store.pop(token, None)
    return True

async def cleanup_expired_tokens():
    while True:
        now = time.time()
        async with token_lock:
            for k, v in list(token_store.items()):
                if v["expires_at"] <= now:
                    token_store.pop(k, None)
        await asyncio.sleep(30)

# ---------- aiohttp webserver ----------
routes = web.RouteTableDef()

@routes.get("/files/loaders/{script_id}/{token}.lua")
async def serve_loader(request: web.Request):
    script_id = request.match_info.get("script_id")
    token = request.match_info.get("token")
    # The Discord-bot ties token -> user. We need to know who is requesting.
    # The Roblox HttpGet won't include a Discord user header, so we rely on:
    # token is single-use and unguessable. To tighten, you may include a HMAC
    # or require the token include the user id encoded; here we keep token single-use.
    if not script_id or not token:
        return web.Response(text="bad request", status=400)

    # Validate token exists and is for this script; consume it only if valid.
    async with token_lock:
        row = token_store.get(token)
        if not row:
            return web.Response(text="invalid or expired token", status=403)
        if row["script_id"] != script_id:
            return web.Response(text="invalid token for script", status=403)
        # If you want to tie to a particular user ID, validate here by comparing headers.
        # NOTE: Roblox HttpGet normally doesn't let you set custom headers, so we don't require them.
        # consume:
        token_store.pop(token, None)

    # Fetch script content
    lua_text = SCRIPT_STORE.get(script_id)
    if lua_text is None:
        # fallback to default if you wish
        lua_text = DEFAULT_SCRIPT

    # Optionally: serve minified/obfuscated content
    return web.Response(text=lua_text, content_type="text/plain")

async def make_app():
    app = web.Application()
    app.add_routes(routes)
    return app

# ---------- Discord bot ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)

def user_allowed_to_request(member: discord.Member) -> bool:
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
    print(f"Discord bot ready. Logged in as {client.user} (id: {client.user.id})")

@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    content = message.content.strip()
    if not content.startswith(BOT_PREFIX):
        return
    args = content[len(BOT_PREFIX):].strip().split()
    if not args:
        return
    cmd = args[0].lower()

    if cmd == "getscript":
        # usage: !getscript <script_id>
        if len(args) < 2:
            await message.reply("Usage: `!getscript <script_id>`")
            return
        script_id = args[1]
        # check permission if in guild
        allowed = True
        if isinstance(message.author, discord.Member):
            allowed = user_allowed_to_request(message.author)
        if not allowed:
            await message.reply("You are not authorized to request scripts.")
            return

        # verify script exists (optional)
        if script_id not in SCRIPT_STORE:
            await message.reply("Unknown script id.")
            return

        # create token and DM user the loader URL
        async with token_lock:
            token = make_token(message.author.id, script_id, ttl=TOKEN_TTL)

        url = f"https://{RENDER_DOMAIN}/files/loaders/{script_id}/{token}.lua"
        dm_text = (
            f"Here is your one-time loader URL (valid {TOKEN_TTL} seconds / single-use):\n\n"
            f"`loadstring(game:HttpGet(\"{url}\"))()`\n\n"
            f"Use it immediately — the token will be consumed on first fetch or after expiry."
        )
        try:
            await message.author.send(dm_text)
            if message.guild:
                await message.reply("I sent you the loader URL in DMs.")
        except discord.Forbidden:
            await message.reply("I couldn't DM you. Please enable DMs and try again.")
        except Exception as e:
            await message.reply(f"Failed to send DM: {e}")

# ---------- Entrypoint ----------
async def main():
    # token cleanup background task
    asyncio.create_task(cleanup_expired_tokens())

    # start aiohttp web app
    app = await make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Web server started on port {PORT}")

    # start discord bot (blocks)
    try:
        await client.start(DISCORD_TOKEN)
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
