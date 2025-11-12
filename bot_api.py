# app.py
import os
import re
import time
import secrets
import asyncio
import logging
import subprocess
from typing import Optional

from aiohttp import web
import discord
from discord import app_commands
from cryptography.fernet import Fernet, InvalidToken  # if using encryption

# ---------- Configuration ----------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
RENDER_DOMAIN = os.environ.get("RENDER_DOMAIN", "your-render-domain.com")
PORT = int(os.environ.get("PORT", 10000))
TOKEN_TTL = int(os.environ.get("TOKEN_TTL", 600))
BOT_PREFIX = os.environ.get("BOT_PREFIX", "!")
OBFUSCATION_MODE = os.environ.get("OBFUSCATION_MODE", "minify")
EXTERNAL_OBFUSCATOR_CMD = os.environ.get("EXTERNAL_OBFUSCATOR_CMD", "")
ALLOWED_ROLE_IDS = set(x.strip() for x in os.environ.get("ALLOWED_ROLE_IDS", "").split(",") if x.strip())
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
ENABLE_PRIVILEGED = os.environ.get("ENABLE_PRIVILEGED_INTENTS", "false").lower() in ("1", "true", "yes")

# optional UA / rate limiting config (if you added prior patch)
ALLOWED_USER_AGENTS = os.environ.get("ALLOWED_USER_AGENTS", "").strip()
RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX", 10))
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", 60))

# Encryption key (if you used encrypted SCRIPT_*_ENC env vars)
SCRIPT_KEY = os.environ.get("SCRIPT_KEY")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is required")

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger("loader-service")

# ---------- Script store population (plaintext or encrypted) ----------
SCRIPT_STORE = {}
fernet = None
if SCRIPT_KEY:
    try:
        fernet = Fernet(SCRIPT_KEY.encode())
        logger.info("SCRIPT_KEY loaded; encrypted script env vars will be decrypted.")
    except Exception:
        logger.exception("Invalid SCRIPT_KEY format; encrypted script env will be ignored.")

for k, v in os.environ.items():
    if not k.startswith("SCRIPT_"):
        continue
    rest = k[len("SCRIPT_"):]
    if rest.endswith("_ENC"):
        script_id = rest[:-len("_ENC")]
        ciphertext = v.strip()
        if not fernet:
            logger.error("Found encrypted env var %s but SCRIPT_KEY not set/invalid; skipping.", k)
            continue
        try:
            plaintext = fernet.decrypt(ciphertext.encode()).decode("utf-8")
            SCRIPT_STORE[script_id] = plaintext
            logger.info("Loaded encrypted script %s (decrypted into memory).", script_id)
        except InvalidToken:
            logger.error("Failed to decrypt %s: invalid token or wrong SCRIPT_KEY.", script_id)
        except Exception:
            logger.exception("Unexpected error decrypting script %s", script_id)
    else:
        # plaintext variable
        script_id = rest
        SCRIPT_STORE[script_id] = v
        logger.info("Loaded plaintext script %s from environment.", script_id)

DEFAULT_SCRIPT = os.environ.get("SCRIPT_TEXT", 'print("hello from server!")')

# ---------- Simple obfuscation pipeline (minify/mangle) ----------
def run_external_obfuscator_if_needed(original_text: str) -> str:
    cmd = EXTERNAL_OBFUSCATOR_CMD.strip()
    if not cmd:
        return original_text
    try:
        logger.info("Running external obfuscator: %s", cmd)
        proc = subprocess.run(cmd, input=original_text.encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, timeout=30)
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout.decode("utf-8")
        else:
            logger.warning("External obfuscator failed: rc=%s stderr=%s", proc.returncode, proc.stderr.decode() if proc.stderr else "")
            return original_text
    except Exception:
        logger.exception("External obfuscator invocation failed")
        return original_text

def minify_lua(src: str) -> str:
    src = re.sub(r"--\[\[[\s\S]*?\]\]", "", src)
    src = re.sub(r"--[^\n\r]*", "", src)
    src = re.sub(r"[ \t]+", " ", src)
    src = re.sub(r"\r\n?", "\n", src)
    lines = [ln.strip() for ln in src.splitlines() if ln.strip() != ""]
    return " ".join(lines)

def mangle_locals(src: str) -> str:
    local_pattern = re.compile(r"\blocal\s+([A-Za-z_][A-Za-z0-9_]*)\b")
    names = []
    for m in local_pattern.finditer(src):
        name = m.group(1)
        if name not in names:
            names.append(name)
    rename_map = {name: "_v" + secrets.token_hex(2) for name in names}
    if not rename_map:
        return src
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in rename_map.keys()) + r")\b")
    return pattern.sub(lambda m: rename_map[m.group(1)], src)

def obfuscate_lua(src: str, mode: str = "minify") -> str:
    if EXTERNAL_OBFUSCATOR_CMD:
        src = run_external_obfuscator_if_needed(src)
    if mode == "none":
        return src
    if mode == "minify":
        return minify_lua(src)
    if mode == "mangle":
        s = minify_lua(src)
        try:
            return mangle_locals(s)
        except Exception:
            logger.exception("Mangle failed; falling back to minified")
            return s
    return minify_lua(src)

# Precompute obfuscated versions
OBFUSCATED_STORE = {}
for sid, txt in SCRIPT_STORE.items():
    OBFUSCATED_STORE[sid] = obfuscate_lua(txt, mode=OBFUSCATION_MODE)
OBFUSCATED_STORE["_default_"] = obfuscate_lua(DEFAULT_SCRIPT, mode=OBFUSCATION_MODE)

logger.info("Available script IDs on startup: %s", list(OBFUSCATED_STORE.keys()))

# ---------- Token store ----------
token_store = {}
token_lock = asyncio.Lock()

def make_token(user_id: str, script_id: str, ttl: int = TOKEN_TTL) -> str:
    token = secrets.token_hex(24)
    expires_at = time.time() + ttl
    token_store[token] = {"user_id": str(user_id), "script_id": str(script_id), "expires_at": expires_at}
    logger.info("Created token %s for user %s script %s ttl=%s", token[:8], user_id, script_id, ttl)
    return token

# You may keep validate_and_consume_token if used elsewhere
def validate_and_consume_token(token: str, script_id: str, user_id: Optional[str] = None) -> bool:
    row = token_store.get(token)
    if not row:
        return False
    if row["script_id"] != str(script_id):
        return False
    if user_id is not None and row["user_id"] != str(user_id):
        return False
    if time.time() > row["expires_at"]:
        token_store.pop(token, None)
        return False
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

# ---------- Rate limiter helpers ----------
_rate_table = {}
def _record_failed_attempt(ip: str):
    now = time.time()
    arr = _rate_table.get(ip) or []
    arr = [t for t in arr if now - t <= RATE_LIMIT_WINDOW]
    arr.append(now)
    _rate_table[ip] = arr
    return len(arr)

def _is_rate_limited(ip: str) -> bool:
    arr = _rate_table.get(ip) or []
    now = time.time()
    arr = [t for t in arr if now - t <= RATE_LIMIT_WINDOW]
    _rate_table[ip] = arr
    _rate_table[ip] = arr
    return len(arr) > RATE_LIMIT_MAX

# ---------- aiohttp webserver ----------
routes = web.RouteTableDef()

@routes.get("/files/loaders/{script_id}/{token}.lua")
async def serve_loader(request: web.Request):
    script_id = request.match_info.get("script_id")
    token = request.match_info.get("token")
    if not script_id or not token:
        return web.Response(text="bad request", status=400)

    peer = request.remote or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    client_ip = peer or "unknown"
    if _is_rate_limited(client_ip):
        logger.warning("Rate-limited IP %s", client_ip)
        return web.Response(text="rate limited", status=429)

    ua = (request.headers.get("User-Agent") or "").strip()
    if ALLOWED_USER_AGENTS:
        allow = False
        for sub in [s.strip() for s in ALLOWED_USER_AGENTS.split(",") if s.strip()]:
            if sub.lower() in ua.lower():
                allow = True
                break
        if not allow:
            n = _record_failed_attempt(client_ip)
            logger.info("Rejected UA '%s' from %s (not allowed). fail_count=%s", ua, client_ip, n)
            return web.Response(text="forbidden", status=403)

    header_user = request.headers.get("X-User-ID")
    async with token_lock:
        row = token_store.get(token)
        if not row:
            n = _record_failed_attempt(client_ip)
            logger.info("Invalid token attempt for script=%s token=%s from %s. fail_count=%s", script_id, token[:8], client_ip, n)
            return web.Response(text="invalid or expired token", status=403)
        if row["script_id"] != script_id:
            n = _record_failed_attempt(client_ip)
            logger.info("Token/script mismatch token=%s requested=%s actual=%s from %s", token[:8], script_id, row["script_id"], client_ip)
            return web.Response(text="invalid token for script", status=403)
        if header_user and row["user_id"] != str(header_user):
            n = _record_failed_attempt(client_ip)
            logger.info("User binding mismatch token=%s header_user=%s owner=%s from %s", token[:8], header_user, row["user_id"], client_ip)
            return web.Response(text="invalid token for user", status=403)
        token_store.pop(token, None)

    lua_text = OBFUSCATED_STORE.get(script_id) or OBFUSCATED_STORE.get("_default_")
    if lua_text is None:
        return web.Response(text="script not found", status=404)

    logger.info("Served script %s to IP %s UA '%s' (token prefix=%s)", script_id, client_ip, ua[:80], token[:8])
    return web.Response(text=lua_text, content_type="text/plain")

async def make_app():
    app = web.Application()
    app.add_routes(routes)
    return app

# ---------- Discord bot (with slash command) ----------
intents = discord.Intents.default()
if ENABLE_PRIVILEGED:
    intents.message_content = True
    intents.members = True
    logger.info("Running with privileged intents enabled (ensure they are toggled in Dev Portal).")
else:
    intents.message_content = False
    intents.members = False
    logger.info("Running WITHOUT privileged intents.")

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

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
    logger.info("Discord bot ready. Logged in as %s (id: %s)", client.user, client.user.id)
    # Register slash commands globally (can take a few minutes) or to a guild for faster dev-testing:
    try:
        # For fast testing against a single guild, use:
        # await tree.sync(guild=discord.Object(id=<GUILD_ID>))
        await tree.sync()
        logger.info("Slash commands synced.")
    except Exception:
        logger.exception("Failed to sync application commands.")

# Slash command: /script
@tree.command(name="script", description="Get a one-time loader URL for a script ID")
@app_commands.describe(script_id="The script id (the part after SCRIPT_ in env)", duration="Token lifetime in seconds (optional)")
async def slash_script(interaction: discord.Interaction, script_id: str, duration: Optional[int] = None):
    # validate script exists
    if script_id not in OBFUSCATED_STORE:
        await interaction.response.send_message("Unknown script id.", ephemeral=True)
        return

    # permission check: if invoked in a guild, check member roles
    member = None
    if interaction.guild:
        member = interaction.guild.get_member(interaction.user.id)
        # fallback if member is None:
        if not member:
            try:
                member = await interaction.guild.fetch_member(interaction.user.id)
            except Exception:
                member = None

    if member and not user_allowed_to_request(member):
        await interaction.response.send_message("You are not authorized to request scripts.", ephemeral=True)
        return

    # determine TTL
    ttl = TOKEN_TTL if duration is None else max(5, min(3600, int(duration)))  # clamp 5..3600 seconds
    # create token
    async with token_lock:
        token = make_token(interaction.user.id, script_id, ttl=ttl)

    url = f"https://{RENDER_DOMAIN}/files/loaders/{script_id}/{token}.lua"
    dm_text = (
        f"Here is your one-time loader URL (valid {ttl} seconds / single-use):\n\n"
        f"`loadstring(game:HttpGet(\"{url}\"))()`\n\n"
        f"If your client allows custom headers, include header: X-User-ID: {interaction.user.id}\n"
        f"Use it immediately — the token is consumed on first successful fetch or after expiry."
    )
    # Try to DM the user
    try:
        await interaction.user.send(dm_text)
        await interaction.response.send_message(f"I've sent you the loader link in DMs — it lasts {ttl} seconds.", ephemeral=True)
    except discord.Forbidden:
        # couldn't DM: provide the URL in ephemeral reply (warning user)
        await interaction.response.send_message(
            f"I couldn't DM you. Here is the one-time URL (valid {ttl} seconds):\n{url}",
            ephemeral=True
        )
    except Exception as e:
        logger.exception("Failed to DM user: %s", e)
        await interaction.response.send_message("Failed to send DM with the loader URL.", ephemeral=True)

# Keep your old prefix-based on_message optionally (or remove it)
@client.event
async def on_message(message: discord.Message):
    # optional compatibility with legacy !getscript commands
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
        if len(args) < 2:
            await message.reply("Usage: `!getscript <script_id>`")
            return
        script_id = args[1]
        # (reuse logic similar to slash command)
        if script_id not in OBFUSCATED_STORE:
            await message.reply("Unknown script id.")
            return
        allowed = True
        if isinstance(message.author, discord.Member):
            allowed = user_allowed_to_request(message.author)
        if not allowed:
            await message.reply("You are not authorized to request scripts.")
            return
        async with token_lock:
            token = make_token(message.author.id, script_id, ttl=TOKEN_TTL)
        url = f"https://{RENDER_DOMAIN}/files/loaders/{script_id}/{token}.lua"
        dm_text = (
            f"Here is your one-time loader URL (valid {TOKEN_TTL} seconds / single-use):\n\n"
            f"`loadstring(game:HttpGet(\"{url}\"))()`"
        )
        try:
            await message.author.send(dm_text)
            if message.guild:
                await message.reply("I sent you the loader URL in DMs.")
        except discord.Forbidden:
            await message.reply(f"I couldn't DM you. Here is the URL:\n{url}")

# ---------- Entrypoint ----------
async def main():
    asyncio.create_task(cleanup_expired_tokens())
    app = await make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Web server started on port %s", PORT)

    try:
        await client.start(DISCORD_TOKEN)
    except discord.errors.PrivilegedIntentsRequired as e:
        logger.error("PrivilegedIntentsRequired: %s", e)
        logger.error("Enable the privileged intents in the Developer Portal or set ENABLE_PRIVILEGED_INTENTS=false")
        await runner.cleanup()
    except Exception:
        logger.exception("Unexpected exception while starting Discord client")
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
