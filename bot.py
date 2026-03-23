"""
NEXUS License Bot — Admin key distribution + Ticket system + Security alerts

Flow:
  1. Admin: /givekey <user_id> <hwid>  → bot DMs key, posts embed in keys channel
  2. User opens NEXUS, enters key → app validates offline → webhook → embed updates
  3. Failed login attempts → red security alert in keys channel
  4. /ticket-panel → posts support panel with 4 categories

Setup:
  pip install py-cord
  python bot.py
"""

import asyncio
import discord
import json
import os
import re
import secrets
from pathlib import Path
from datetime import datetime

# ══════════════════════════════════════════════════════════════════
#  CONFIG  ← fill in TICKET_CATEGORY_ID
# ══════════════════════════════════════════════════════════════════
BOT_TOKEN        = "MTQ2NzAwMTg1NTQ3MjE3MzI4OQ.GS8fy1.il36dM_ZYjtL90Cv-yUNz8wuSyCbUUr7fEGvss"
ADMIN_ROLE_ID    = 1404995267144585216     # Role that can run admin commands
KEYS_CHANNEL_ID  = 1466614479335723018    # Channel for key table + security alerts

# Ticket system — set TICKET_CATEGORY_ID to the Discord category ID
# where ticket channels should be created (right-click category → Copy ID)
TICKET_CATEGORY_ID = 1405004559897595974   # Category where ticket channels are created
TICKET_PANEL_CHANNEL_ID = 1405004724918550568  # Channel where the ticket panel is posted
STAFF_ROLE_ID      = ADMIN_ROLE_ID             # Role that can see and manage all tickets

# ── Data file ─────────────────────────────────────────────────────
DATA_FILE = Path(__file__).parent / "bot_data.json"

# ── Ticket categories ─────────────────────────────────────────────
TICKET_CATS = [
    {"id": "purchase", "emoji": "🔑", "label": "Purchase Key",      "desc": "Buy a NEXUS license key"},
    {"id": "support",  "emoji": "🛠️", "label": "Technical Support", "desc": "Help with app issues or installation"},
    {"id": "question", "emoji": "❓", "label": "Question",           "desc": "General questions about NEXUS"},
    {"id": "bug",      "emoji": "🐛", "label": "Bug Report",         "desc": "Report a bug or unexpected behavior"},
]


# ══════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════
def _gen_key() -> str:
    """Generate a cryptographically random license key: NEXUS-XXXX-XXXX-XXXX."""
    raw = secrets.token_hex(6).upper()   # 12 random hex chars
    return f"NEXUS-{raw[:4]}-{raw[4:8]}-{raw[8:12]}"

def _mask_key(key: str) -> str:
    # NEXUS-XXXX-XXXX-XXXX → NEXUS-XXXX-????-????
    parts = key.split("-")
    if len(parts) == 4 and parts[0] == "NEXUS":
        return f"NEXUS-{parts[1]}-????-????"
    return key

def _now() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M")

def _load_data() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"issued": {}, "blacklist": [], "tickets": {}}

def _save_data(data: dict):
    DATA_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════
#  BOT + ADMIN CHECK
# ══════════════════════════════════════════════════════════════════
GUILD_ID = 1404995208130727946   # Your server ID — forces instant command sync

intents = discord.Intents.default()
bot = discord.Bot(intents=intents, debug_guilds=[GUILD_ID])

def is_admin(member) -> bool:
    if not hasattr(member, "guild") or not member.guild:
        return False
    if member.id == member.guild.owner_id:
        return True
    if ADMIN_ROLE_ID:
        return any(r.id == ADMIN_ROLE_ID for r in getattr(member, "roles", []))
    return getattr(member, "guild_permissions", None) and member.guild_permissions.administrator


# ══════════════════════════════════════════════════════════════════
#  EMBED BUILDERS
# ══════════════════════════════════════════════════════════════════
def _issued_embed(user_tag, user_id, key, issued_at):
    e = discord.Embed(title="🔑  Key Issued", color=0xFFD700, timestamp=datetime.now())
    e.add_field(name="👤 Discord User", value=f"<@{user_id}>", inline=False)
    e.add_field(name="🔐 Key",          value=f"```{_mask_key(key)}```",     inline=True)
    e.add_field(name="📅 Issued At",    value=issued_at,                     inline=True)
    e.add_field(name="🖥️ HWID",         value="⏳ Will be recorded on first app login", inline=False)
    e.add_field(name="✅ Status",       value="⏳ Not yet activated",         inline=False)
    e.set_footer(text="NEXUS Gaming Suite • HWID auto-updates when user logs in")
    return e

def _used_embed(user_tag, user_id, hwid, key, issued_at, used_at, ip, os_info):
    e = discord.Embed(title="🔑  Key Activated ✅", color=0x00FF88, timestamp=datetime.now())
    e.add_field(name="👤 Discord User", value=f"<@{user_id}>", inline=False)
    e.add_field(name="🔐 Key",          value=f"```{_mask_key(key)}```",     inline=True)
    e.add_field(name="📅 Issued At",    value=issued_at,                     inline=True)
    e.add_field(name="🖥️ HWID",         value=f"```{hwid}```",              inline=False)
    e.add_field(name="🌐 IP Address",   value=f"`{ip}`",                     inline=True)
    e.add_field(name="🕐 Activated At", value=used_at,                       inline=True)
    if os_info:
        e.add_field(name="💻 OS",       value=f"`{os_info[:60]}`",           inline=False)
    e.add_field(name="✅ Status",       value="✅ Active — HWID Locked",      inline=False)
    e.set_footer(text="NEXUS Gaming Suite")
    return e


# ══════════════════════════════════════════════════════════════════
#  TICKET SYSTEM — VIEWS
# ══════════════════════════════════════════════════════════════════
class CreateTicketView(discord.ui.View):
    """Persistent — survives bot restarts (registered in on_ready)."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="  Create Ticket",
        style=discord.ButtonStyle.success,
        custom_id="nexus:create_ticket",
        emoji="🎫",
    )
    async def btn_create(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_message(
            "**Please select a category for your ticket:**",
            view=SelectCategoryView(),
            ephemeral=True,
        )


class SelectCategoryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(
        placeholder="Choose a category…",
        options=[
            discord.SelectOption(emoji="🔑", label="Purchase Key",      value="purchase", description="Buy a NEXUS license key"),
            discord.SelectOption(emoji="🛠️", label="Technical Support", value="support",  description="Help with app issues or installation"),
            discord.SelectOption(emoji="❓", label="Question",           value="question", description="General questions about NEXUS"),
            discord.SelectOption(emoji="🐛", label="Bug Report",         value="bug",      description="Report a bug or unexpected behavior"),
        ],
    )
    async def select_cat(self, select: discord.ui.Select, interaction: discord.Interaction):
        await _open_ticket(interaction, select.values[0])


class CloseTicketView(discord.ui.View):
    """Persistent close button inside each ticket channel."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="  Close Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="nexus:close_ticket",
        emoji="🔒",
    )
    async def btn_close(self, button: discord.ui.Button, interaction: discord.Interaction):
        # Owner ID is stored in the channel topic: "owner:12345 | ..."
        topic    = getattr(interaction.channel, "topic", "") or ""
        owner_id = None
        for part in topic.split("|"):
            part = part.strip()
            if part.startswith("owner:"):
                try:
                    owner_id = int(part[6:])
                except ValueError:
                    pass

        if not is_admin(interaction.user) and interaction.user.id != owner_id:
            await interaction.response.send_message(
                "❌ Only staff or the ticket owner can close this.", ephemeral=True
            )
            return

        await interaction.response.send_message("🔒 Ticket closing in **5 seconds**…")

        # Update data
        data   = _load_data()
        ch_key = str(interaction.channel.id)
        if ch_key in data.get("tickets", {}):
            data["tickets"][ch_key].update({
                "closed":    True,
                "closed_by": str(interaction.user),
                "closed_at": _now(),
            })
            _save_data(data)

        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except Exception:
            pass


async def _open_ticket(interaction: discord.Interaction, cat_id: str):
    """Create a private ticket channel for the user."""
    guild = interaction.guild
    user  = interaction.user

    # Prevent duplicate tickets
    safe_name = re.sub(r"[^a-z0-9]", "", user.name.lower())[:16] or str(user.id)[:8]
    chan_name  = f"ticket-{safe_name}"
    existing   = discord.utils.get(guild.text_channels, name=chan_name)
    if existing:
        await interaction.response.edit_message(
            content=f"⚠️ You already have an open ticket: {existing.mention}", view=None
        )
        return

    # Permissions
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user:               discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me:           discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }
    if STAFF_ROLE_ID:
        staff_role = guild.get_role(STAFF_ROLE_ID)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

    cat_channel = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
    cat_info    = next((c for c in TICKET_CATS if c["id"] == cat_id), TICKET_CATS[0])

    ticket_channel = await guild.create_text_channel(
        name=chan_name,
        category=cat_channel,
        overwrites=overwrites,
        topic=f"owner:{user.id} | category:{cat_id} | opened:{_now()}",
    )

    # Welcome embed
    embed = discord.Embed(
        title=f"{cat_info['emoji']}  {cat_info['label']}",
        description=(
            f"Welcome {user.mention}! 👋\n\n"
            f"A staff member will be with you shortly.\n"
            f"Please **describe your request in detail** so we can help faster."
        ),
        color=0xFFD700,
        timestamp=datetime.now(),
    )
    embed.add_field(name="Category",  value=f"{cat_info['emoji']} {cat_info['label']}", inline=True)
    embed.add_field(name="User",      value=user.mention,                               inline=True)
    embed.add_field(name="Opened At", value=_now(),                                     inline=True)
    embed.set_footer(text="Click 🔒 Close Ticket when your issue is resolved")

    ping = user.mention
    if STAFF_ROLE_ID:
        r = guild.get_role(STAFF_ROLE_ID)
        if r:
            ping += f" {r.mention}"

    await ticket_channel.send(content=ping, embed=embed, view=CloseTicketView())

    # Save ticket data
    data = _load_data()
    data.setdefault("tickets", {})[str(ticket_channel.id)] = {
        "user":       str(user),
        "user_id":    user.id,
        "category":   cat_id,
        "opened_at":  _now(),
        "closed":     False,
        "closed_by":  None,
        "closed_at":  None,
        "channel_id": ticket_channel.id,
    }
    _save_data(data)

    await interaction.response.edit_message(
        content=f"✅ Ticket created: {ticket_channel.mention}", view=None
    )


# ══════════════════════════════════════════════════════════════════
#  EVENTS
# ══════════════════════════════════════════════════════════════════
@bot.event
async def on_ready():
    print(f"✅  NEXUS Bot online — {bot.user}")
    # Register persistent views so buttons keep working after restarts
    bot.add_view(CreateTicketView())
    bot.add_view(CloseTicketView())
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="NEXUS Suite 🔑")
    )
    # Auto-post ticket panel if the channel is empty or panel doesn't exist yet
    await _ensure_ticket_panel()


async def _ensure_ticket_panel():
    """Post the ticket panel in TICKET_PANEL_CHANNEL_ID if not already there."""
    if not TICKET_PANEL_CHANNEL_ID:
        return
    try:
        ch = bot.get_channel(TICKET_PANEL_CHANNEL_ID)
        if not ch:
            return
        # Check if bot already posted the panel (look for our embed title)
        async for msg in ch.history(limit=20):
            if msg.author == bot.user and msg.embeds:
                if any("NEXUS Support" in (e.title or "") for e in msg.embeds):
                    print("📌 Ticket panel already exists — skipping auto-post")
                    return
        # Not found — post it
        embed = discord.Embed(
            title="🎫  NEXUS Support",
            description=(
                "Need help? Click **Create Ticket** below and choose a category.\n"
                "Our team will assist you as soon as possible."
            ),
            color=0xFFD700,
        )
        cats_text = "\n".join(f"{c['emoji']} **{c['label']}** — {c['desc']}" for c in TICKET_CATS)
        embed.add_field(name="📌 Categories", value=cats_text, inline=False)
        embed.set_footer(text="NEXUS Gaming Suite • Support System")
        await ch.send(embed=embed, view=CreateTicketView())
        print(f"📌 Ticket panel posted in #{ch.name}")
    except Exception as e:
        print(f"⚠️  Could not post ticket panel: {e}")


# ══════════════════════════════════════════════════════════════════
#  REVOKE BUTTON HELPER
# ══════════════════════════════════════════════════════════════════
def _key_action_view(key: str) -> discord.ui.View:
    """Returns a View with a single 🗑️ Revoke button for the given key."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="🗑️ Revoke Key",
        style=discord.ButtonStyle.danger,
        custom_id=f"nexus:revoke:{key}",
    ))
    return view


@bot.event
async def on_interaction(interaction: discord.Interaction):
    """Handle revoke button clicks from key embeds."""
    cid = (interaction.data or {}).get("custom_id", "")
    if not cid.startswith("nexus:revoke:"):
        return  # let the view system handle ticket buttons etc.

    if not is_admin(interaction.user):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return

    key  = cid[len("nexus:revoke:"):]
    data = _load_data()

    if key not in data.get("issued", {}):
        await interaction.response.send_message("❌ Key not found or already revoked.", ephemeral=True)
        return

    revoked_user = data["issued"][key].get("user", "Unknown")
    del data["issued"][key]
    data.setdefault("blacklist", [])
    if key not in data["blacklist"]:
        data["blacklist"].append(key)
    _save_data(data)

    revoked_embed = discord.Embed(
        title="🔑  Key REVOKED 🚫",
        color=0xFF3C3C,
        description=(
            f"Key: `{_mask_key(key)}`\n"
            f"User: **{revoked_user}**\n"
            f"Revoked by: <@{interaction.user.id}>"
        ),
        timestamp=datetime.now(),
    )
    await interaction.response.edit_message(embed=revoked_embed, view=None)


@bot.event
async def on_message(message: discord.Message):
    """Listen for webhook messages from NEXUS app posted in the keys channel."""
    if not message.author.bot or message.channel.id != KEYS_CHANNEL_ID:
        return

    for embed in message.embeds:
        title  = embed.title or ""
        fields = {f.name: f.value for f in embed.fields}

        # ── Key activated ──────────────────────────────────────────
        if "NEXUS_KEY_USED" in title:
            key     = fields.get("KEY",  "").strip("`").strip()
            hwid    = fields.get("HWID", "").strip("`").strip()
            ip      = fields.get("IP",   "Unknown").strip("`").strip()
            os_info = fields.get("OS",   "").strip("`").strip()
            used_at = _now()

            data = _load_data()
            # Look up record by KEY (primary) — key is unique per user
            entry = data["issued"].get(key)
            if entry:
                entry.update(used=True, used_at=used_at, ip=ip, os=os_info, hwid=hwid)
                _save_data(data)

                msg_id = entry.get("msg_id")
                if msg_id:
                    try:
                        ch = bot.get_channel(KEYS_CHANNEL_ID)
                        if ch:
                            orig = await ch.fetch_message(msg_id)
                            await orig.edit(embed=_used_embed(
                                entry["user"], entry["user_id"],
                                hwid, key,
                                entry.get("issued_at", "?"),
                                used_at, ip, os_info,
                            ), view=_key_action_view(key))
                    except Exception:
                        pass

            # Delete the raw webhook message — keep channel clean
            try:
                await message.delete()
            except Exception:
                pass

        # ── Failed login attempt (someone tried a wrong key) ───────
        elif "NEXUS_LOGIN_FAILED" in title:
            hwid    = fields.get("HWID", "?").strip("`").strip()
            ip      = fields.get("IP",   "?").strip("`").strip()
            os_info = fields.get("OS",   "").strip("`").strip()

            alert = discord.Embed(
                title="🚨  Failed Login Attempt",
                description="Someone tried to use NEXUS with an **invalid key**.",
                color=0xFF3C3C,
                timestamp=datetime.now(),
            )
            alert.add_field(name="🖥️ HWID",  value=f"`{hwid}`",    inline=True)
            alert.add_field(name="🌐 IP",     value=f"`{ip}`",      inline=True)
            if os_info:
                alert.add_field(name="💻 OS", value=f"`{os_info}`", inline=False)
            alert.set_footer(text="NEXUS Gaming Suite — Security Alert")

            ch = bot.get_channel(KEYS_CHANNEL_ID)
            if ch:
                try:
                    await ch.send(embed=alert)
                    await message.delete()
                except Exception:
                    pass


# ══════════════════════════════════════════════════════════════════
#  /givekey  —  Admin creates key by Discord User ID
# ══════════════════════════════════════════════════════════════════
@bot.slash_command(name="givekey", description="[Admin] Create a key and send it to a user by Discord ID")
async def givekey(
    ctx:     discord.ApplicationContext,
    user_id: discord.Option(str, "Discord User ID (right-click user → Copy ID)"),  # type: ignore
):
    if not is_admin(ctx.author):
        await ctx.respond("❌ Admin only.", ephemeral=True)
        return

    # Defer immediately — gives us 15 min instead of 3 sec
    await ctx.defer(ephemeral=True)

    # Fetch user by ID
    try:
        user = await bot.fetch_user(int(user_id.strip()))
    except Exception:
        await ctx.followup.send(
            "❌ User not found. Make sure you copied the **User ID** (not username).\n"
            "Enable Developer Mode → right-click user → **Copy ID**.",
            ephemeral=True,
        )
        return

    key       = _gen_key()
    issued_at = _now()
    data      = _load_data()

    # ── 1. DM the key ──────────────────────────────────────────────
    dm_ok = False
    try:
        await user.send(
            f"## 🔐 NEXUS License Key\n\n"
            f"You have been granted a license.\n\n"
            f"**Key:**\n```{key}```\n\n"
            f"Paste the key in the NEXUS login screen.\n"
            f"⚠️ The key will lock to your machine on first use — do not share it."
        )
        dm_ok = True
    except discord.Forbidden:
        pass

    # ── 2. Post in keys channel ────────────────────────────────────
    msg_id = None
    ch = bot.get_channel(KEYS_CHANNEL_ID)
    if ch:
        msg    = await ch.send(embed=_issued_embed(str(user), user.id, key, issued_at), view=_key_action_view(key))
        msg_id = msg.id

    # ── 3. Save (keyed by KEY, not HWID) ──────────────────────────
    data["issued"][key] = {
        "user":      str(user),
        "user_id":   user.id,
        "key":       key,
        "hwid":      None,          # bound on first app login
        "issued_at": issued_at,
        "msg_id":    msg_id,
        "used":      False,
        "used_at":   None,
        "ip":        None,
        "os":        None,
        "given_by":  str(ctx.author),
    }
    _save_data(data)

    dm_status = "✅ DM sent" if dm_ok else "⚠️ DM failed (user has DMs disabled)"
    await ctx.followup.send(
        f"**Key created!**\n"
        f"👤 User: `{user}` (`{user.id}`)\n"
        f"🔐 Key: `{key}`\n"
        f"🖥️ HWID: will be recorded on first login\n"
        f"📨 {dm_status}",
        ephemeral=True,
    )


# ══════════════════════════════════════════════════════════════════
#  /keys  —  Admin table
# ══════════════════════════════════════════════════════════════════
@bot.slash_command(name="keys", description="[Admin] View all issued keys")
async def keys_cmd(ctx: discord.ApplicationContext):
    if not is_admin(ctx.author):
        await ctx.respond("❌ Admin only.", ephemeral=True)
        return

    await ctx.defer(ephemeral=True)

    data   = _load_data()
    issued = data.get("issued", {})
    if not issued:
        await ctx.followup.send("📭 No keys issued yet.", ephemeral=True)
        return

    rows  = list(issued.items())
    pages = [rows[i:i+10] for i in range(0, len(rows), 10)]
    embeds = []
    for pi, page in enumerate(pages):
        e = discord.Embed(
            title=f"📋  NEXUS Keys — {len(issued)} total",
            color=0xFFD700,
            description=f"Page {pi+1}/{len(pages)}",
        )
        for key, v in page:
            status   = "✅ Active" if v.get("used") else "⏳ Pending"
            hwid_str = f"\nHWID: `{v['hwid']}`" if v.get("hwid") else "\nHWID: not bound yet"
            ip_str   = f"\nIP: `{v['ip']}`"      if v.get("ip")   else ""
            e.add_field(
                name=f"{status}  {v.get('user', '?')}",
                value=(
                    f"Key: `{_mask_key(key)}`\n"
                    f"Issued: {v.get('issued_at', '?')}"
                    + hwid_str
                    + (f"\nUsed: {v.get('used_at', '?')}{ip_str}" if v.get("used") else "")
                ),
                inline=False,
            )
        embeds.append(e)

    # Build a revoke button for each key (max 5 per row, max 25 total)
    revoke_view = discord.ui.View(timeout=120)
    for i, (key, v) in enumerate(rows[:25]):
        label = f"🗑️ {v.get('user', 'user')[:18]}"
        revoke_view.add_item(discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.danger,
            custom_id=f"nexus:revoke:{key}",
            row=min(i // 5, 4),
        ))

    await ctx.followup.send(embeds=embeds[:5], view=revoke_view, ephemeral=True)


# ══════════════════════════════════════════════════════════════════
#  /revokekey
# ══════════════════════════════════════════════════════════════════
@bot.slash_command(name="revokekey", description="[Admin] Revoke a key by the key string")
async def revokekey(
    ctx: discord.ApplicationContext,
    key: discord.Option(str, "The license key to revoke (XXXX-XXXX-XXXX-XXXX)"),  # type: ignore
):
    if not is_admin(ctx.author):
        await ctx.respond("❌ Admin only.", ephemeral=True)
        return

    key  = key.strip().upper()
    data = _load_data()

    revoked_user = "?"
    entry = data["issued"].get(key)
    if entry:
        revoked_user = entry.get("user", "?")
        msg_id = entry.get("msg_id")
        if msg_id and KEYS_CHANNEL_ID:
            ch = bot.get_channel(KEYS_CHANNEL_ID)
            if ch:
                try:
                    msg = await ch.fetch_message(msg_id)
                    e   = discord.Embed(
                        title="🔑  Key REVOKED 🚫",
                        color=0xFF3C3C,
                        description=(
                            f"Key: `{_mask_key(key)}`\n"
                            f"User: {revoked_user}\n"
                            f"Revoked by: {ctx.author}"
                        ),
                    )
                    await msg.edit(embed=e)
                except Exception:
                    pass
        del data["issued"][key]

    if key not in data["blacklist"]:
        data["blacklist"].append(key)
    _save_data(data)

    await ctx.respond(
        f"🚫 **Key revoked + blacklisted!**\nKey: `{_mask_key(key)}`\nUser: {revoked_user}",
        ephemeral=True,
    )


# ══════════════════════════════════════════════════════════════════
#  /genkey  —  Quick calc, no save
# ══════════════════════════════════════════════════════════════════
@bot.slash_command(name="genkey", description="[Admin] Generate a random key (preview only, not saved)")
async def genkey(ctx: discord.ApplicationContext):
    if not is_admin(ctx.author):
        await ctx.respond("❌ Admin only.", ephemeral=True)
        return

    key = _gen_key()
    await ctx.respond(
        f"🔑 **Preview Key** *(not saved — use `/givekey` to actually issue)*\n`{key}`",
        ephemeral=True,
    )


# ══════════════════════════════════════════════════════════════════
#  /ticket-panel  —  Post the support panel
# ══════════════════════════════════════════════════════════════════
@bot.slash_command(name="ticket-panel", description="[Admin] Post the NEXUS support ticket panel in this channel")
async def ticket_panel(ctx: discord.ApplicationContext):
    if not is_admin(ctx.author):
        await ctx.respond("❌ Admin only.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🎫  NEXUS Support",
        description=(
            "Need help? Click **Create Ticket** below and choose a category.\n"
            "Our team will assist you as soon as possible."
        ),
        color=0xFFD700,
    )
    cats_text = "\n".join(f"{c['emoji']} **{c['label']}** — {c['desc']}" for c in TICKET_CATS)
    embed.add_field(name="📌 Categories", value=cats_text, inline=False)
    embed.set_footer(text="NEXUS Gaming Suite • Support System")

    await ctx.channel.send(embed=embed, view=CreateTicketView())
    await ctx.respond("✅ Ticket panel posted!", ephemeral=True)


# ══════════════════════════════════════════════════════════════════
#  /nexushelp
# ══════════════════════════════════════════════════════════════════
@bot.slash_command(name="nexushelp", description="NEXUS Bot commands")
async def nexus_help(ctx: discord.ApplicationContext):
    e = discord.Embed(title="🤖 NEXUS License Bot", color=0xFFD700)
    e.add_field(
        name="🛡️ Admin Commands",
        value=(
            "`/givekey <user_id>` — Create & DM key (no HWID needed)\n"
            "`/keys` — View all issued keys\n"
            "`/revokekey <key>` — Revoke a key + blacklist it\n"
            "`/genkey` — Preview a random key (not saved)\n"
            "`/ticket-panel` — Post support panel in this channel\n"
        ),
        inline=False,
    )
    e.add_field(
        name="📌 How to find HWID?",
        value="Open NEXUS → enter any wrong key → HWID appears with a Copy button",
        inline=False,
    )
    e.add_field(
        name="🌐 Auto key-activation tracking",
        value="Set Webhook URL in NEXUS Settings → app reports IP + HWID → embed auto-updates",
        inline=False,
    )
    e.add_field(
        name="🚨 Security alerts",
        value="Every failed login attempt (wrong key) appears as a red alert in this channel",
        inline=False,
    )
    await ctx.respond(embed=e, ephemeral=True)


# ══════════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("🚀 Starting NEXUS License Bot...")
    bot.run(BOT_TOKEN)
