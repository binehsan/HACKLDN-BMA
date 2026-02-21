import os
import json
import re
import io
import asyncio
from datetime import datetime, timedelta, timezone
from collections import Counter
from typing import Dict, Any, List
from gemini_ai import analyze_chat, generate_html_resource, generate_quiz
from notioner import upload_html_and_get_object_url

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

SETTINGS_FILE = "settings.json"

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Per-guild latest study session context (populated by !saveus and !analyse)
# {guild_id: {"chat_history": str, "student_level": str, "subject_area": str, "topics": list}}
bot._last_session = {}

# ─────────────────────────────────────────
# Settings helpers
# ─────────────────────────────────────────

def load_settings() -> Dict[str, Any]:
    if not os.path.exists(SETTINGS_FILE):
        return {}
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_settings(settings: Dict[str, Any]):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def get_guild_settings(guild_id: int) -> Dict[str, Any]:
    settings = load_settings()
    guild_key = str(guild_id)
    if guild_key not in settings:
        settings[guild_key] = {
            "hours": 24,
            "channels": [],
            "output_format": "summary",
            "keyword_filters": []
        }
        save_settings(settings)
    return settings[guild_key]


def set_guild_setting(guild_id: int, key: str, value: Any):
    settings = load_settings()
    settings.setdefault(str(guild_id), {})[key] = value
    save_settings(settings)


# ─────────────────────────────────────────
# PII cleaning
# ─────────────────────────────────────────

PII_PATTERNS = [
    ("email",          re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("credit_card",    re.compile(r"\b(?:\d[ -]*?){13,16}\b")),
    ("phone",          re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{2,4}\)?[-.\s]?){1,3}\d{2,4}\b")),
    ("passport_label", re.compile(r"(?i)\bpassport\s*(?:no\.?|number|#)?[:\s]*([A-Z0-9-]{5,12})\b")),
]


def clean_text(text: str) -> str:
    if not text:
        return text
    cleaned = text
    for name, pattern in PII_PATTERNS:
        if name == "passport_label":
            cleaned = pattern.sub("[REDACTED_PASSPORT]", cleaned)
        else:
            cleaned = pattern.sub(lambda m, n=name: f"[REDACTED_{n.upper()}]", cleaned)
    return cleaned


# ─────────────────────────────────────────
# UI Components
# ─────────────────────────────────────────

class HoursSelect(discord.ui.Select):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        options = [
            discord.SelectOption(label="1 hour",   value="1"),
            discord.SelectOption(label="6 hours",  value="6"),
            discord.SelectOption(label="12 hours", value="12"),
            discord.SelectOption(label="24 hours", value="24", default=True),
            discord.SelectOption(label="48 hours", value="48"),
        ]
        super().__init__(
            placeholder="⏱️ Select time window...",
            min_values=1, max_values=1,
            options=options, row=0
        )

    async def callback(self, interaction: discord.Interaction):
        hours = int(self.values[0])
        set_guild_setting(self.guild_id, "hours", hours)
        await interaction.response.send_message(
            f"✅ Time window set to **{hours} hour(s)**.", ephemeral=True
        )


class OutputFormatSelect(discord.ui.Select):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        options = [
            discord.SelectOption(label="Summary", value="summary", description="Condensed overview"),
            discord.SelectOption(label="Raw",     value="raw",     description="Full cleaned messages"),
        ]
        super().__init__(
            placeholder="📄 Output format...",
            min_values=1, max_values=1,
            options=options, row=1
        )

    async def callback(self, interaction: discord.Interaction):
        fmt = self.values[0]
        set_guild_setting(self.guild_id, "output_format", fmt)
        await interaction.response.send_message(
            f"✅ Output format set to **{fmt}**.", ephemeral=True
        )


class KeywordModal(discord.ui.Modal, title="Set Keyword Filters"):
    keywords = discord.ui.TextInput(
        label="Keywords (comma-separated)",
        placeholder="e.g. budget, launch, deadline",
        required=False,
        max_length=300
    )

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.keywords.value
        kw_list = [k.strip() for k in raw.split(",") if k.strip()]
        set_guild_setting(self.guild_id, "keyword_filters", kw_list)
        if kw_list:
            await interaction.response.send_message(
                f"✅ Keyword filters set: **{', '.join(kw_list)}**", ephemeral=True
            )
        else:
            await interaction.response.send_message("✅ Keyword filters cleared.", ephemeral=True)


class ChannelSelectView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.add_item(ChannelMultiSelect(guild_id))


class ChannelMultiSelect(discord.ui.ChannelSelect):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(
            placeholder="📡 Pick channels to analyze (leave empty = all)",
            min_values=0,
            max_values=10,
            channel_types=[discord.ChannelType.text],
            row=0
        )

    async def callback(self, interaction: discord.Interaction):
        channel_ids = [str(c.id) for c in self.values]
        set_guild_setting(self.guild_id, "channels", channel_ids)
        if channel_ids:
            names = ", ".join([f"<#{cid}>" for cid in channel_ids])
            await interaction.response.send_message(
                f"✅ Analyzing channels: {names}", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "✅ Will analyze **all channels**.", ephemeral=True
            )


class SettingsView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.add_item(HoursSelect(guild_id))
        self.add_item(OutputFormatSelect(guild_id))

    @discord.ui.button(label="🔑 Set Keywords", style=discord.ButtonStyle.secondary, row=2)
    async def set_keywords(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(KeywordModal(self.guild_id))

    @discord.ui.button(label="📡 Pick Channels", style=discord.ButtonStyle.secondary, row=2)
    async def pick_channels(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ChannelSelectView(self.guild_id)
        await interaction.response.send_message(
            "Select the channels you want panikbot to analyze:", view=view, ephemeral=True
        )

    @discord.ui.button(label="🔍 Run Analysis Now", style=discord.ButtonStyle.primary, row=3)
    async def run_analysis(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await do_analysis(interaction.channel, interaction.guild, interaction.user)

    @discord.ui.button(label="📋 Show Current Settings", style=discord.ButtonStyle.secondary, row=3)
    async def show_settings_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_settings_embed(interaction.guild.id, interaction.response.send_message, ephemeral=True)


# ─────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────

async def send_settings_embed(guild_id: int, send_fn, ephemeral=False):
    gs      = get_guild_settings(guild_id)
    hours   = gs.get("hours", 24)
    fmt     = gs.get("output_format", "summary")
    kws     = gs.get("keyword_filters", [])
    chans   = gs.get("channels", [])
    chan_str = ", ".join([f"<#{c}>" for c in chans]) if chans else "All channels"
    kw_str  = ", ".join(kws) if kws else "None"

    embed = discord.Embed(title="⚙️ PanikBot — Current Settings", color=0x5865F2)
    embed.add_field(name="⏱️ Time Window",     value=f"{hours} hour(s)", inline=True)
    embed.add_field(name="📄 Output Format",   value=fmt.capitalize(),   inline=True)
    embed.add_field(name="📡 Channels",        value=chan_str,            inline=False)
    embed.add_field(name="🔑 Keyword Filters", value=kw_str,             inline=False)
    embed.set_footer(text="Use !changesettings time <n> to update, or @panikbot for the full menu.")

    if ephemeral:
        await send_fn(embed=embed, ephemeral=True)
    else:
        await send_fn(embed=embed)


async def do_analysis(channel, guild, requester, topic: str = None):
    guild_settings   = get_guild_settings(guild.id)
    hours            = guild_settings.get("hours", 24)
    output_format    = guild_settings.get("output_format", "summary")
    keyword_filters  = guild_settings.get("keyword_filters", [])
    allowed_channels = guild_settings.get("channels", [])

    search_terms = [topic] if topic else keyword_filters
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    if allowed_channels:
        channels_to_scan = [guild.get_channel(int(cid)) for cid in allowed_channels if guild.get_channel(int(cid))]
    else:
        channels_to_scan = [ch for ch in guild.text_channels if ch.permissions_for(guild.me).read_message_history]

    messages_cleaned: List[tuple] = []  # (channel_name, author_name, cleaned_text)
    total_count = 0

    for ch in channels_to_scan:
        try:
            async for msg in ch.history(limit=2000, after=cutoff):
                if msg.author.bot:
                    continue
                try:
                    cleaned = clean_text(msg.content)
                except Exception:
                    continue
                if not cleaned.strip():
                    continue
                if search_terms:
                    if not any(term.lower() in cleaned.lower() for term in search_terms):
                        continue
                messages_cleaned.append((ch.name, msg.author.display_name, cleaned))
                total_count += 1
        except discord.Forbidden:
            continue

    title = f"📊 Topic Search: \"{topic}\"" if topic else f"📊 Analysis — Last {hours} Hour(s)"

    if total_count == 0:
        await channel.send(f"No messages found{f' about **{topic}**' if topic else ''} in the last **{hours} hours**.")
        return

    embed = discord.Embed(title=title, color=0x57F287, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Requested by {requester.display_name} · PII redacted")
    embed.add_field(name="Messages found", value=str(total_count), inline=True)
    embed.add_field(name="Format",         value=output_format.capitalize(), inline=True)
    if search_terms:
        embed.add_field(name="🔍 Searching for", value=", ".join(search_terms), inline=False)

    if output_format == "summary" and not topic:
        authors = [author for _, author, _ in messages_cleaned]
        top     = Counter(authors).most_common(5)
        top_str = "\n".join([f"**{a}**: {c} msg(s)" for a, c in top])
        embed.add_field(name="🗣️ Most Active Users", value=top_str or "N/A", inline=False)

    sample_lines = [f"[#{ch}] {author}: {text}" for ch, author, text in messages_cleaned[:5]]
    sample = "\n".join(sample_lines)
    if len(sample) > 1000:
        sample = sample[:1000] + "\n...[truncated]"
    embed.add_field(name="📝 Sample Messages", value=f"```{sample}```", inline=False)

    if output_format == "raw" or topic:
        raw_text   = "\n".join([f"[#{ch}] {author}: {text}" for ch, author, text in messages_cleaned])
        file_bytes = raw_text.encode("utf-8")
        file = discord.File(fp=io.BytesIO(file_bytes), filename="results.txt")
        await channel.send(embed=embed, file=file)
    else:
        await channel.send(embed=embed)


# ─────────────────────────────────────────
# Commands
# ─────────────────────────────────────────

@bot.command(name="helpnow")
async def helpnow_cmd(ctx):
    embed = discord.Embed(
        title="🤖 PanikBot — Commands",
        description="Here's everything I can do:",
        color=0x5865F2
    )
    embed.add_field(
        name="📋 `!showsettings`",
        value="Show the current bot settings for this server.",
        inline=False
    )
    embed.add_field(
        name="⏱️ `!changesettings time <hours>`",
        value="Change how far back to scan messages.\nExample: `!changesettings time 12`",
        inline=False
    )
    embed.add_field(
        name="🔍 `!saveus`",
        value="Run a full analysis of messages based on your current settings.",
        inline=False
    )
    embed.add_field(
        name="💬 `!helpus <topic>`",
        value="Search all messages for a specific topic and return matching messages + a full results file.\nExample: `!helpus project deadline`",
        inline=False
    )
    embed.add_field(
        name="🧠 `!quiz`",
        value="Generate a 5-question quiz from recent chat + study guides. Answer via polls, then type `!answers` to reveal results.",
        inline=False
    )
    embed.add_field(
        name="📅 `!analyse`",
        value="Pick two dates/times and analyse all messages between them. Same flow as `!saveus` but for a custom date range.",
        inline=False
    )
    embed.add_field(
        name="⚙️ `@panikbot`",
        value="Mention me to open the full interactive settings menu with dropdowns and buttons.",
        inline=False
    )
    embed.set_footer(text="PanikBot · PII is always redacted from results")
    await ctx.send(embed=embed)


@bot.command(name="helpus")
async def helpus_cmd(ctx, *, topic: str = None):
    if ctx.guild is None:
        await ctx.send("Run this in a server channel.")
        return
    if not topic:
        await ctx.send("Please provide a topic.\nExample: `!helpus project deadline`")
        return

    await ctx.send(f"📚 Topic: **{topic}**...")

    guild_settings = get_guild_settings(ctx.guild.id)
    hours          = guild_settings.get("hours", 24)
    allowed_channels = guild_settings.get("channels", [])

    # Still collect recent chat for context, but don't filter by topic
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    if allowed_channels:
        channels_to_scan = [ctx.guild.get_channel(int(cid)) for cid in allowed_channels if ctx.guild.get_channel(int(cid))]
    else:
        channels_to_scan = [ch for ch in ctx.guild.text_channels if ch.permissions_for(ctx.guild.me).read_message_history]

    messages_collected: List[tuple] = []

    for ch in channels_to_scan:
        try:
            async for msg in ch.history(limit=500, after=cutoff):
                if msg.author.bot:
                    continue
                if msg.content.strip().startswith("!"):
                    continue
                try:
                    cleaned = clean_text(msg.content)
                except Exception:
                    continue
                if not cleaned.strip():
                    continue
                messages_collected.append((ch.name, msg.author.display_name, cleaned))
        except discord.Forbidden:
            continue

    # Build chat history for context (or use empty string if none)
    chat_history = "\n".join([f"[#{ch}] {author}: {text}" for ch, author, text in messages_collected])

    await ctx.send(f"📝 Generating your study guide for **{topic}**, hang tight...")

    try:
        file_path = await asyncio.to_thread(
            generate_html_resource,
            chat_history=chat_history,
            topics=[topic],
            student_level="Unknown",
            subject_area=topic
        )
    except Exception as e:
        await ctx.send(f"❌ Study guide generation failed: {e}")
        return

    await ctx.send("☁️ Uploading to S3...")

    try:
        upload_result = await asyncio.to_thread(upload_html_and_get_object_url, file_path)
    except Exception as e:
        await ctx.send(f"❌ Upload failed: {e}")
        return

    if not upload_result.get("success"):
        await ctx.send(f"❌ Upload failed: {upload_result.get('error', 'Unknown error')}")
        return

    detail = upload_result.get("detail", "Study guide uploaded successfully.")
    url = upload_result.get("url", "")
    await ctx.send(f"✅ {detail}")
    if url:
        await ctx.send(f"📎 **Link to study guide:** {url}")
    else:
        await ctx.send("⚠️ Upload succeeded but no URL was returned.")


@bot.command(name="showsettings")
async def showsettings_cmd(ctx):
    if ctx.guild is None:
        await ctx.send("Settings are per-server; run this in a server channel.")
        return
    await send_settings_embed(ctx.guild.id, ctx.send)


@bot.command(name="changesettings")
async def changesettings_cmd(ctx, setting: str = None, *, value: str = None):
    if ctx.guild is None:
        await ctx.send("Run this in a server channel.")
        return
    if setting is None or value is None:
        await ctx.send("Usage: `!changesettings time <hours>`\nExample: `!changesettings time 12`")
        return
    if setting.lower() == "time":
        try:
            hours = int(value)
            if hours <= 0:
                raise ValueError()
            set_guild_setting(ctx.guild.id, "hours", hours)
            await ctx.send(f"✅ Time window updated to **{hours} hour(s)**.")
        except ValueError:
            await ctx.send("❌ Please provide a positive number.\nExample: `!changesettings time 12`")
    else:
        await ctx.send(f"❌ Unknown setting `{setting}`.\nAvailable: `time` — or use `@panikbot` for the full settings menu.")


@bot.command(name="saveus")
async def saveus_cmd(ctx):
    if ctx.guild is None:
        await ctx.send("Run this in a server channel.")
        return

    guild_settings   = get_guild_settings(ctx.guild.id)
    hours            = guild_settings.get("hours", 24)
    allowed_channels = guild_settings.get("channels", [])
    keyword_filters  = guild_settings.get("keyword_filters", [])

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    if allowed_channels:
        channels_to_scan = [ctx.guild.get_channel(int(cid)) for cid in allowed_channels if ctx.guild.get_channel(int(cid))]
    else:
        channels_to_scan = [ch for ch in ctx.guild.text_channels if ch.permissions_for(ctx.guild.me).read_message_history]

    messages_collected: List[tuple] = []

    for ch in channels_to_scan:
        try:
            async for msg in ch.history(limit=2000, after=cutoff):
                if msg.author.bot:
                    continue
                try:
                    cleaned = clean_text(msg.content)
                except Exception:
                    continue
                if not cleaned.strip():
                    continue
                if keyword_filters:
                    if not any(kw.lower() in cleaned.lower() for kw in keyword_filters):
                        continue
                messages_collected.append((ch.name, msg.author.display_name, cleaned))
        except discord.Forbidden:
            continue

    if not messages_collected:
        await ctx.send(f"No messages found in the last **{hours} hours**.")
        return

    chat_history = "\n".join([f"[#{ch}] {author}: {text}" for ch, author, text in messages_collected])
    chat_history = strip_bot_commands(chat_history)

    await ctx.send(f"📨 Collected **{len(messages_collected)}** messages. Analysing with Gemini...")

    try:
        result = await asyncio.to_thread(analyze_chat, chat_history)
    except Exception as e:
        await ctx.send(f"❌ Gemini analysis failed: {e}")
        return

    action  = result.get("action_required", "")
    summary = result.get("summary_message", "No summary returned.")
    topics  = result.get("topics_to_explain", [])
    level   = result.get("student_level", "Unknown")
    subject = result.get("subject_area", "Unknown")

    # Store latest session so !quiz can use it
    bot._last_session[ctx.guild.id] = {
        "chat_history": chat_history,
        "student_level": level,
        "subject_area": subject,
        "topics": topics,
    }

    prompt_msg = (
        f"**📊 Analysis Complete**\n\n"
        f"{summary}\n\n"
        f"{'👉 Would you like me to generate a study guide for these topics? **(Yes/No)**' if action == 'explain' else '🔒 Time to lock in!'}"
    )
    await ctx.send(prompt_msg)

    if action != "explain" or not topics:
        return

    def check_next(m):
        return m.channel == ctx.channel and not m.author.bot

    try:
        reply = await bot.wait_for("message", check=check_next, timeout=60.0)
    except asyncio.TimeoutError:
        await ctx.send("⏰ No response received. Skipping study guide generation.")
        return

    if reply.author != ctx.author or reply.content.strip().lower() not in ("yes", "no"):
        await ctx.send(f"⚠️ I needed a **Yes/No** from {ctx.author.mention} as the very next message. Please run `!saveus` again.")
        return

    if reply.content.strip().lower() == "no":
        await ctx.send("👍 No problem! Let me know if you need anything else.")
        return

    await ctx.send("📝 Generating your study guide, hang tight...")

    try:
        file_path = await asyncio.to_thread(
            generate_html_resource,
            chat_history=chat_history,
            topics=topics,
            student_level=level,
            subject_area=subject
        )
    except Exception as e:
        await ctx.send(f"❌ Study guide generation failed: {e}")
        return

    # Upload to S3 and get a URL
    await ctx.send("☁️ Uploading to S3...")

    try:
        upload_result = await asyncio.to_thread(upload_html_and_get_object_url, file_path)
    except Exception as e:
        await ctx.send(f"❌ Upload failed: {e}")
        return

    if not upload_result.get("success"):
        await ctx.send(f"❌ Upload failed: {upload_result.get('error', 'Unknown error')}")
        return

    detail = upload_result.get("detail", "Study guide uploaded successfully.")
    url = upload_result.get("url", "")
    await ctx.send(f"✅ {detail}")
    if url:
        await ctx.send(f"📎 **Link to study guide:** {url}")
    else:
        await ctx.send("⚠️ Upload succeeded but no URL was returned.")


# ─────────────────────────────────────────
# !analyse — custom date-range analysis
# ─────────────────────────────────────────

class DateTimeModal(discord.ui.Modal, title="Enter Date & Time Range"):
    start_date = discord.ui.TextInput(
        label="Start  (YYYY-MM-DD HH:MM)",
        placeholder="e.g. 2026-02-20 09:00",
        required=True,
        max_length=16,
    )
    end_date = discord.ui.TextInput(
        label="End  (YYYY-MM-DD HH:MM)",
        placeholder="e.g. 2026-02-21 18:00",
        required=True,
        max_length=16,
    )

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        # Parse dates
        fmt = "%Y-%m-%d %H:%M"
        try:
            start_dt = datetime.strptime(self.start_date.value.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            await interaction.response.send_message(
                f"❌ Invalid start date `{self.start_date.value}`. Use format `YYYY-MM-DD HH:MM`.",
                ephemeral=True,
            )
            return
        try:
            end_dt = datetime.strptime(self.end_date.value.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            await interaction.response.send_message(
                f"❌ Invalid end date `{self.end_date.value}`. Use format `YYYY-MM-DD HH:MM`.",
                ephemeral=True,
            )
            return

        if end_dt <= start_dt:
            await interaction.response.send_message("❌ End date must be after start date.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"📅 Scanning messages from **{start_dt.strftime(fmt)}** to **{end_dt.strftime(fmt)}** UTC..."
        )

        ctx = self.ctx
        guild_settings   = get_guild_settings(ctx.guild.id)
        allowed_channels = guild_settings.get("channels", [])
        keyword_filters  = guild_settings.get("keyword_filters", [])

        if allowed_channels:
            channels_to_scan = [ctx.guild.get_channel(int(cid)) for cid in allowed_channels if ctx.guild.get_channel(int(cid))]
        else:
            channels_to_scan = [ch for ch in ctx.guild.text_channels if ch.permissions_for(ctx.guild.me).read_message_history]

        messages_collected: List[tuple] = []
        for ch in channels_to_scan:
            try:
                async for msg in ch.history(limit=5000, after=start_dt, before=end_dt):
                    if msg.author.bot:
                        continue
                    try:
                        cleaned = clean_text(msg.content)
                    except Exception:
                        continue
                    if not cleaned.strip():
                        continue
                    if keyword_filters:
                        if not any(kw.lower() in cleaned.lower() for kw in keyword_filters):
                            continue
                    messages_collected.append((ch.name, msg.author.display_name, cleaned))
            except discord.Forbidden:
                continue

        if not messages_collected:
            await ctx.send("No messages found in that date range.")
            return

        chat_history = "\n".join([f"[#{ch}] {author}: {text}" for ch, author, text in messages_collected])
        chat_history = strip_bot_commands(chat_history)

        await ctx.send(f"📨 Collected **{len(messages_collected)}** messages. Analysing with Gemini...")

        try:
            result = await asyncio.to_thread(analyze_chat, chat_history)
        except Exception as e:
            await ctx.send(f"❌ Gemini analysis failed: {e}")
            return

        action  = result.get("action_required", "")
        summary = result.get("summary_message", "No summary returned.")
        topics  = result.get("topics_to_explain", [])
        level   = result.get("student_level", "Unknown")
        subject = result.get("subject_area", "Unknown")

        # Store latest session so !quiz can use it
        bot._last_session[ctx.guild.id] = {
            "chat_history": chat_history,
            "student_level": level,
            "subject_area": subject,
            "topics": topics,
        }

        prompt_msg = (
            f"**📊 Analysis Complete**\n\n"
            f"{summary}\n\n"
            f"{'👉 Would you like me to generate a study guide for these topics? **(Yes/No)**' if action == 'explain' else '🔒 Time to lock in!'}"
        )
        await ctx.send(prompt_msg)

        if action != "explain" or not topics:
            return

        def check_next(m):
            return m.channel == ctx.channel and m.author.id == ctx.author.id

        try:
            reply = await bot.wait_for("message", check=check_next, timeout=60.0)
        except asyncio.TimeoutError:
            await ctx.send("⏰ No response received. Skipping study guide generation.")
            return

        if reply.content.strip().lower() not in ("yes", "y"):
            await ctx.send("👍 No problem! Let me know if you need anything else.")
            return

        await ctx.send("📝 Generating your study guide, hang tight...")

        try:
            file_path = await asyncio.to_thread(
                generate_html_resource,
                chat_history=chat_history,
                topics=topics,
                student_level=level,
                subject_area=subject,
            )
        except Exception as e:
            await ctx.send(f"❌ Study guide generation failed: {e}")
            return

        await ctx.send("☁️ Uploading to S3...")

        try:
            upload_result = await asyncio.to_thread(upload_html_and_get_object_url, file_path)
        except Exception as e:
            await ctx.send(f"❌ Upload failed: {e}")
            return

        if not upload_result.get("success"):
            await ctx.send(f"❌ Upload failed: {upload_result.get('error', 'Unknown error')}")
            return

        detail = upload_result.get("detail", "Study guide uploaded successfully.")
        url = upload_result.get("url", "")
        await ctx.send(f"✅ {detail}")
        if url:
            await ctx.send(f"📎 **Link to study guide:** {url}")
        else:
            await ctx.send("⚠️ Upload succeeded but no URL was returned.")


class AnalyseDateButton(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=120)
        self.ctx = ctx

    @discord.ui.button(label="📅 Pick Date Range", style=discord.ButtonStyle.primary)
    async def pick_dates(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DateTimeModal(self.ctx))


@bot.command(name="analyse")
async def analyse_cmd(ctx):
    if ctx.guild is None:
        await ctx.send("Run this in a server channel.")
        return

    embed = discord.Embed(
        title="📅 Analyse — Custom Date Range",
        description="Click the button below to enter a start and end date/time.\nI'll scrape all messages between those two timestamps and analyse them.",
        color=0x5865F2,
    )
    embed.add_field(name="Format", value="`YYYY-MM-DD HH:MM` (UTC)", inline=False)
    embed.set_footer(text="PanikBot · PII is always redacted")
    view = AnalyseDateButton(ctx)
    await ctx.send(embed=embed, view=view)


@bot.command(name="quiz")
async def quiz_cmd(ctx):
    if ctx.guild is None:
        await ctx.send("Run this in a server channel.")
        return

    # Use the latest session if available (from !saveus or !analyse)
    session = bot._last_session.get(ctx.guild.id)

    if session:
        chat_history  = session["chat_history"]
        student_level = session["student_level"]
        subject_area  = session["subject_area"]
        await ctx.send(f"🧠 Generating a quiz from your latest study session ({subject_area})...")
    else:
        # Fallback: scrape recent messages like before
        guild_settings   = get_guild_settings(ctx.guild.id)
        hours            = guild_settings.get("hours", 24)
        allowed_channels = guild_settings.get("channels", [])
        keyword_filters  = guild_settings.get("keyword_filters", [])

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        if allowed_channels:
            channels_to_scan = [ctx.guild.get_channel(int(cid)) for cid in allowed_channels if ctx.guild.get_channel(int(cid))]
        else:
            channels_to_scan = [ch for ch in ctx.guild.text_channels if ch.permissions_for(ctx.guild.me).read_message_history]

        messages_collected: List[tuple] = []
        for ch in channels_to_scan:
            try:
                async for msg in ch.history(limit=2000, after=cutoff):
                    if msg.author.bot:
                        continue
                    try:
                        cleaned = clean_text(msg.content)
                    except Exception:
                        continue
                    if not cleaned.strip():
                        continue
                    if keyword_filters:
                        if not any(kw.lower() in cleaned.lower() for kw in keyword_filters):
                            continue
                    messages_collected.append((ch.name, msg.author.display_name, cleaned))
            except discord.Forbidden:
                continue

        if not messages_collected:
            await ctx.send(f"No messages found in the last **{hours} hours**.")
            return

        chat_history = "\n".join([f"[#{ch}] {author}: {text}" for ch, author, text in messages_collected])
        chat_history = strip_bot_commands(chat_history)

        await ctx.send("🧠 Generating a quiz from recent messages...")

        # Analyse to get level/subject
        try:
            analysis = await asyncio.to_thread(analyze_chat, chat_history)
        except Exception as e:
            await ctx.send(f"❌ Analysis failed: {e}")
            return

        student_level = analysis.get("student_level", "Unknown")
        subject_area  = analysis.get("subject_area", "Unknown")

    # Read the latest study guide HTML files from responses/ for extra context
    study_guide_content = ""
    responses_dir = "responses"
    if os.path.isdir(responses_dir):
        html_files = sorted(
            [f for f in os.listdir(responses_dir) if f.endswith(".html")],
            key=lambda f: os.path.getmtime(os.path.join(responses_dir, f)),
            reverse=True,
        )
        for html_file in html_files[:3]:  # last 3 guides
            try:
                with open(os.path.join(responses_dir, html_file), "r", encoding="utf-8") as fh:
                    study_guide_content += fh.read() + "\n\n"
            except Exception:
                continue

    try:
        quiz_data = await asyncio.to_thread(
            generate_quiz, chat_history, study_guide_content, student_level, subject_area
        )
    except Exception as e:
        await ctx.send(f"❌ Quiz generation failed: {e}")
        return

    questions = quiz_data.get("questions", [])
    if not questions:
        await ctx.send("❌ Couldn't generate any questions. Try again later.")
        return

    quiz_title = quiz_data.get("quiz_title", "PanikBot Quiz")
    await ctx.send(f"📝 **{quiz_title}** — {len(questions)} questions incoming!\n*Answer the polls, then the person who started the quiz must type `!answers` as the **very next message** to reveal answers.*")

    # Send each question as a Discord poll
    poll_messages = []
    for i, q in enumerate(questions, 1):
        # Build option lines (strip leading letter prefix for clean display)
        option_lines = []
        for opt in q["options"]:
            cleaned_opt = re.sub(r"^[A-Da-d]\)\s*", "", opt).strip()
            option_lines.append(cleaned_opt)

        # Format question + answers into the poll question text
        # Discord poll question max 300 chars
        q_text = f"Q{i}: {q['question']}"
        full_text = (
            f"{q_text}\n"
            f"A) {option_lines[0]}\n"
            f"B) {option_lines[1]}\n"
            f"C) {option_lines[2]}\n"
            f"D) {option_lines[3]}"
        )

        # If it exceeds 300 chars, send the full Q&A as an embed then poll with just the Q number
        if len(full_text) > 300:
            qa_embed = discord.Embed(
                title=f"Q{i}: {q['question']}",
                description=(
                    f"**A)** {option_lines[0]}\n"
                    f"**B)** {option_lines[1]}\n"
                    f"**C)** {option_lines[2]}\n"
                    f"**D)** {option_lines[3]}"
                ),
                color=0xC89116,
            )
            await ctx.send(embed=qa_embed)
            poll_question = f"Q{i}: Your answer?"
        else:
            poll_question = full_text

        poll = discord.Poll(
            question=poll_question,
            duration=timedelta(hours=1),
            multiple=False,
        )
        for letter in ["A", "B", "C", "D"]:
            poll.add_answer(text=letter)

        poll_msg = await ctx.send(poll=poll)
        poll_messages.append(poll_msg)

    # Store answers + poll messages for later reveal
    bot._quiz_pending = {
        "channel_id": ctx.channel.id,
        "requester_id": ctx.author.id,
        "questions": questions,
        "poll_messages": poll_messages,
        "quiz_title": quiz_title,
        "chat_history": chat_history,
        "student_level": student_level,
        "subject_area": subject_area,
    }

    # Wait for the very next message — must be !answers from the requester
    def check_answers(m):
        return m.channel == ctx.channel and not m.author.bot

    try:
        reply = await bot.wait_for("message", check=check_answers, timeout=300.0)
    except asyncio.TimeoutError:
        await ctx.send("⏰ No `!answers` received within 5 minutes. Quiz answers discarded.")
        bot._quiz_pending = None
        return

    if reply.author.id != ctx.author.id or reply.content.strip().lower() != "!answers":
        await ctx.send(
            f"⚠️ The very next message must be `!answers` from {ctx.author.mention}. "
            f"Quiz cancelled — run `!quiz` again to retry."
        )
        bot._quiz_pending = None
        return

    # ── End polls and collect per-user results ──
    await ctx.send("📊 Ending polls and analysing results...")

    # Map: user_id -> {display_name, correct, wrong, wrong_topics}
    user_results: Dict[int, Dict[str, Any]] = {}
    answer_lines = []

    for i, (q, poll_msg) in enumerate(zip(questions, poll_messages), 1):
        correct_letter = q["correct_answer"].strip().upper()

        # End the poll so we can read final results
        try:
            poll_msg = await poll_msg.end_poll()
        except Exception:
            # Poll may have already ended or we lack perms — re-fetch
            try:
                poll_msg = await ctx.channel.fetch_message(poll_msg.id)
            except Exception:
                pass

        poll_obj = poll_msg.poll
        if poll_obj is None:
            answer_lines.append(f"**Q{i}:** _(could not read poll results)_")
            continue

        # Build letter -> answer mapping  (A, B, C, D)
        letter_map = {}
        for idx, ans in enumerate(poll_obj.answers):
            letter = chr(65 + idx)  # A, B, C, D
            letter_map[letter] = ans

        # Correct answer full text
        correct_text = ""
        for opt in q["options"]:
            if opt.strip().upper().startswith(correct_letter):
                correct_text = opt
                break

        # Collect voters for each answer
        for letter, ans in letter_map.items():
            is_correct = letter == correct_letter
            try:
                voters = [u async for u in ans.voters()]
            except Exception:
                voters = []
            for user in voters:
                if user.bot:
                    continue
                if user.id not in user_results:
                    user_results[user.id] = {
                        "display_name": user.display_name,
                        "correct": 0,
                        "wrong": 0,
                        "wrong_topics": [],
                    }
                if is_correct:
                    user_results[user.id]["correct"] += 1
                else:
                    user_results[user.id]["wrong"] += 1
                    user_results[user.id]["wrong_topics"].append(q["question"])

        answer_lines.append(
            f"**Q{i}:** {correct_text}\n"
            f"💡 _{q['explanation']}_"
        )

    # ── Send answer key ──
    answer_embed = discord.Embed(
        title=f"✅ Answers — {quiz_title}",
        description="\n\n".join(answer_lines),
        color=0xC89116,
    )
    answer_embed.set_footer(text="Generated by PanikBot · saving grades one ping at a time")
    await ctx.send(embed=answer_embed)

    # ── Per-user scoreboard ──
    if user_results:
        scoreboard_lines = []
        all_wrong_topics: list[str] = []
        for uid, data in sorted(user_results.items(), key=lambda x: x[1]["correct"], reverse=True):
            total = data["correct"] + data["wrong"]
            pct = int(data["correct"] / total * 100) if total else 0
            emoji = "🟢" if pct >= 80 else ("🟡" if pct >= 50 else "🔴")
            scoreboard_lines.append(
                f"{emoji} **{data['display_name']}** — {data['correct']}/{total} ({pct}%)"
            )
            all_wrong_topics.extend(data["wrong_topics"])

        score_embed = discord.Embed(
            title="📋 Quiz Scoreboard",
            description="\n".join(scoreboard_lines),
            color=0x5865F2,
        )
        score_embed.set_footer(text="Based on poll votes")
        await ctx.send(embed=score_embed)

        # ── Identify topics that need more study ──
        if all_wrong_topics:
            topic_counts = Counter(all_wrong_topics)
            weak_topics = [t for t, _ in topic_counts.most_common(5)]

            weak_list = "\n".join([f"• {t}" for t in weak_topics])
            await ctx.send(
                f"📉 **Topics that need more work:**\n{weak_list}\n\n"
                f"👉 Would you like me to explain these topics in depth? **(Yes/No)**"
            )

            # Wait for yes/no from the quiz requester
            def check_yes_no(m):
                return (
                    m.channel == ctx.channel
                    and m.author.id == ctx.author.id
                    and m.content.strip().lower() in ("yes", "no", "y", "n")
                )

            try:
                yn_reply = await bot.wait_for("message", check=check_yes_no, timeout=120.0)
            except asyncio.TimeoutError:
                await ctx.send("⏰ No response received. Skipping explanation generation.")
                bot._quiz_pending = None
                return

            if yn_reply.content.strip().lower() in ("yes", "y"):
                await ctx.send("📝 Generating explanation study guide, hang tight...")

                try:
                    file_name = await asyncio.to_thread(
                        generate_html_resource,
                        chat_history,
                        weak_topics,
                        student_level,
                        subject_area,
                    )
                except Exception as e:
                    await ctx.send(f"❌ Study guide generation failed: {e}")
                    bot._quiz_pending = None
                    return

                await ctx.send("☁️ Uploading to S3...")

                try:
                    upload_result = await asyncio.to_thread(upload_html_and_get_object_url, file_name)
                except Exception as e:
                    await ctx.send(f"❌ Upload failed: {e}")
                    bot._quiz_pending = None
                    return

                if not upload_result.get("success"):
                    await ctx.send(f"❌ Upload failed: {upload_result.get('error', 'Unknown error')}")
                    bot._quiz_pending = None
                    return

                detail = upload_result.get("detail", "Study guide uploaded successfully.")
                url = upload_result.get("url", "")
                await ctx.send(f"✅ {detail}")
                if url:
                    await ctx.send(f"📎 **Link to study guide:** {url}")
                else:
                    await ctx.send("⚠️ Upload succeeded but no URL was returned.")
            else:
                await ctx.send("👍 No worries — keep studying and run `!quiz` again when you're ready!")
        else:
            await ctx.send("🎉 Everyone got everything right! No weak topics detected.")
    else:
        await ctx.send("ℹ️ No poll votes were detected — couldn't analyse results.")

    bot._quiz_pending = None


def strip_bot_commands(chat_history: str) -> str:
    """Remove any lines that contain bot commands (! appearing anywhere in the message)."""
    lines = chat_history.split("\n")
    filtered = [line for line in lines if "!" not in line]
    return "\n".join(filtered)

# ─────────────────────────────────────────
# Events
# ─────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Ready! Type !help in Discord to see all commands.")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        # Silently ignore unknown commands (e.g. !answers is handled by wait_for)
        return
    raise error


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if bot.user in message.mentions and not message.content.strip().startswith("!"):
        if message.guild is None:
            await message.channel.send("Mention me in a server channel to open settings.")
            return
        embed = discord.Embed(
            title="⚙️ PanikBot Settings",
            description="Use the dropdowns and buttons below to configure me.\nAll changes are saved instantly.",
            color=0x5865F2
        )
        view = SettingsView(message.guild.id)
        await message.channel.send(embed=embed, view=view)
        return
    await bot.process_commands(message)


# ─────────────────────────────────────────
# Run
# ─────────────────────────────────────────

if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("Set the DISCORD_TOKEN environment variable to run the bot.")
    else:
        bot.run(token)