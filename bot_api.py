# app.py
import os
import re
import time
import secrets
import asyncio
from aiohttp import web
import discord
import logging
import subprocess

# ---------- Configuration (use Render environment variables) ----------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")  # required
RENDER_DOMAIN = os.environ.get("RENDER_DOMAIN", "your-render-domain.com")
PORT = int(os.environ.get("PORT", 10000))
TOKEN_TTL = int(os.environ.get("TOKEN_TTL", 60))  # seconds
BOT_PREFIX = os.environ.get("BOT_PREFIX", "!")
OBFUSCATION_MODE = os.environ.get("OBFUSCATION_MODE", "minify")  # none|minify|mangle
EXTERNAL_OBFUSCATOR_CMD = os.environ.get("EXTERNAL_OBFUSCATOR_CMD", "")  # optional CLI to run on source
ALLOWED_ROLE_IDS = set(x.strip() for x in os.environ.get("ALLOWED_ROLE_IDS", "").split(",") if x.strip())
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is required")

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger("loader-service")

# ---------- Script store population ----------
# You can set env vars like SCRIPT_<ID>="<lua source here>" or keep files
SCRIPT_STORE = {}

for k, v in os.environ.items():
    if k.startswith("SCRIPT_"):
        script_id = k[len("SCRIPT_"):]
        SCRIPT_STORE[script_id] = v

# A fallback default script (if no specific script is found for an id)
DEFAULT_SCRIPT = os.environ.get("SCRIPT_TEXT", 'print("hello from server!")')

# ---------- Simple safe obfuscation pipeline ----------
def run_external_obfuscator_if_needed(original_text: str) -> str:
    """If EXTERNAL_OBFUSCATOR_CMD is set, run it with the original script on stdin
       and capture stdout as the obfuscated script. Returns original_text if any failure occurs.
       This allows you to plug in a commercial obfuscator CLI at deploy time."""
    cmd = EXTERNAL_OBFUSCATOR_CMD.strip()
    if not cmd:
        return original_text
    try:
        logger.info("Running external obfuscator: %s", cmd)
        proc = subprocess.run(cmd, input=original_text.encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, timeout=30)
        if proc.returncode == 0 and proc.stdout:
            logger.info("External obfuscator succeeded")
            return proc.stdout.decode("utf-8")
        else:
            logger.warning("External obfuscator failed or returned empty output: rc=%s stderr=%s",
                           proc.returncode, proc.stderr.decode("utf-8") if proc.stderr else "")
            return original_text
    except Exception as e:
        logger.exception("External obfuscator invocation failed: %s", e)
        return original_text

def minify_lua(src: str) -> str:
    # Remove block comments --[[ ... ]]
    src = re.sub(r"--\[\[[\s\S]*?\]\]", "", src)
    # Remove single-line comments -- ...
    src = re.sub(r"--[^\n\r]*", "", src)
    # Collapse runs of whitespace into single space, but keep newlines for clarity (we'll join)
    src = re.sub(r"[ \t]+", " ", src)
    # Remove unnecessary newlines (compress)
    src = re.sub(r"\r\n?", "\n", src)
    lines = [ln.strip() for ln in src.splitlines() if ln.strip() != ""]
    return " ".join(lines)

def mangle_locals(src: str) -> str:
    # Very conservative: find simple local declarations and rename those locals only.
    # This is NOT a full parser and can break edge cases; use cautiously.
    # Step 1: collect local variable names declared as "local <name>" or "local <name> ="
    local_pattern = re.compile(r"\blocal\s+([A-Za-z_][A-Za-z0-9_]*)\b")
    names = []
    for m in local_pattern.finditer(src):
        name = m.group(1)
        if name not in names:
            names.append(name)
    # Build a renaming map for these names
    rename_map = {}
    for i, name in enumerate(names):
        # short hex-like short names
        rename_map[name] = "_v" + secrets.token_hex(2)
    # Replace names with word boundaries; avoid touching globals by only replacing the exact identifiers
    def repl_ident(m):
        ident = m.group(0)
        return rename_map.get(ident, ident)
    if not rename_map:
        return src
    # Create a regex that matches any of the collected names as whole words
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in rename_map.keys()) + r")\b")
    return pattern.sub(lambda m: rename_map[m.group(1)], src)

def obfuscate_lua(src: str, mode: str = "minify") -> str:
    # Apply external obfuscator first if configured (recommended for heavy obfuscation)
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
            logger.exception("Mangle failed; falling back to minified text")
            return s
    # default fallback
    return minify_lua(src)

# Precompute obfuscated versions into a separate store on startup
OBFUSCATED_STORE = {}
for sid, txt in SCRIPT_STORE.items():
    OBFUSCATED_STORE[sid] = obfuscate_lua(txt, mode=OBFUSCATION_MODE)

# Ensure DEFAULT_SCRIPT obfuscated too
OBFUSCATED_STORE["_default_"] = obfuscate_lua(DEFAULT_SCRIPT, mode=OBFUSCATION_MODE)

# ---------- Token store (in-memory) ----------
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
    if row["script_id"] != str(script_id):
        return False
    # Note: in some flows (Roblox HttpGet) custom headers are not available; we tie token -> script and single-use token.
    if row["user_id"] != str(user_id):
        # still allow consumption if you want token not tied to user; comment this out if Roblox can't send header
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

# ---------- aiohttp webserver ----------
routes = web.RouteTableDef()

@routes.get("/files/loaders/{script_id}/{token}.lua")
async def serve_loader(request: web.Request):
    script_id = request.match_info.get("script_id")
    token = request.match_info.get("token")
    if not script_id or not token:
        return web.Response(text="bad request", status=400)

    # For platforms that can attach headers, you can require X-User-ID; otherwise we validate token->script only.
    # We'll attempt to read X-User-ID if present to enforce user-binding, otherwise allow consumption by token alone.
    header_user = request.headers.get("X-User-ID")
    async with token_lock:
        row = token_store.get(token)
        if not row:
            return web.Response(text="invalid or expired token", status=403)
        if row["script_id"] != script_id:
            return web.Response(text="invalid token for script", status=403)
        # If header present, require it to match; if not present, allow token-only validation
        if header_user:
            if row["user_id"] != str(header_user):
                return web.Response(text="invalid token for user", status=403)
        # Consume token
        token_store.pop(token, None)

    # Serve the obfuscated or original script (prefer obfuscated store)
    lua_text = OBFUSCATED_STORE.get(script_id) or OBFUSCATED_STORE.get("_default_")
    if lua_text is None:
        return web.Response(text="script not found", status=404)

    return web.Response(text=lua_text, content_type="text/plain")

async def make_app():
    app = web.Application()
    app.add_routes(routes)
    return app

# ---------- Discord bot with proper intents ----------
intents = discord.Intents.default()
# Enable message content if you want to parse message text commands:
intents.message_content = True  # must be enabled in Developer Portal
# Enable members if you want to check roles/detect members:
intents.members = True  # must be enabled in Developer Portal

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
    logger.info("Discord bot ready. Logged in as %s (id: %s)", client.user, client.user.id)

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
        # permission check
        allowed = True
        if isinstance(message.author, discord.Member):
            allowed = user_allowed_to_request(message.author)
        if not allowed:
            await message.reply("You are not authorized to request scripts.")
            return

        # verify script exists
        if script_id not in OBFUSCATED_STORE:
            await message.reply("Unknown script id.")
            return

        # create token and DM user the loader URL
        async with token_lock:
            token = make_token(message.author.id, script_id, ttl=TOKEN_TTL)

        url = f"https://{RENDER_DOMAIN}/files/loaders/{script_id}/{token}.lua"
        dm_text = (
            f"Here is your one-time loader URL (valid {TOKEN_TTL} seconds / single-use):\n\n"
            f"`loadstring(game:HttpGet(\"{url}\"))()`\n\n"
            f"If your client allows custom headers, include header: X-User-ID: {message.author.id}\n"
            f"Use it immediately — the token is consumed on first successful fetch or after expiry."
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
    # Start cleanup background task
    asyncio.create_task(cleanup_expired_tokens())

    # start aiohttp app
    app = await make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Web server started on port %s", PORT)

    # start discord bot (blocks)
    try:
        await client.start(DISCORD_TOKEN)
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
