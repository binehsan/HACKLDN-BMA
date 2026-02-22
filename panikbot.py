import os
import json
import re
import io
import asyncio
from datetime import datetime, timedelta, timezone
from collections import Counter
from typing import Dict, Any, List
from gemini_ai import analyze_chat, generate_html_resource, generate_quiz, generate_battle_questions
from notioner import upload_html_and_get_object_url
from rag_store import add_message as rag_add, query_knowledge as rag_query, get_stats as rag_stats, add_document_chunks as rag_add_doc

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

SETTINGS_FILE = "settings.json"

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

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
            "keyword_filters": [],
            "rag_reaction_threshold": 1,
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
# Message collection helpers
# ─────────────────────────────────────────

async def collect_messages_grouped(
    channels: list,
    cutoff_after=None,
    cutoff_before=None,
    keyword_filters: list = None,
    limit_per_source: int = 2000,
    include_threads: bool = True,
) -> dict:
    """Collect messages from channels AND their threads, grouped by context.

    Returns a dict like:
    {
        "#general": [(author, text), ...],
        "#general > Biology Cells": [(author, text), ...],
        "#general > Chemistry": [(author, text), ...],
        "#demo": [(author, text), ...],
    }
    This prevents Gemini from confusing messages across different threads/channels.
    """
    grouped: dict[str, list[tuple[str, str]]] = {}

    history_kwargs = {"limit": limit_per_source}
    if cutoff_after:
        history_kwargs["after"] = cutoff_after
    if cutoff_before:
        history_kwargs["before"] = cutoff_before

    for ch in channels:
        context_key = f"#{ch.name}"

        # --- Collect from the channel itself ---
        try:
            async for msg in ch.history(**history_kwargs):
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
                if keyword_filters:
                    if not any(kw.lower() in cleaned.lower() for kw in keyword_filters):
                        continue
                grouped.setdefault(context_key, []).append((msg.author.display_name, cleaned))
        except discord.Forbidden:
            continue

        # --- Collect from threads inside this channel ---
        if not include_threads:
            continue

        all_threads = []
        try:
            all_threads.extend(ch.threads)  # active threads
        except Exception:
            pass
        try:
            async for thread in ch.archived_threads(limit=50):
                all_threads.append(thread)
        except (discord.Forbidden, discord.HTTPException):
            pass

        for thread in all_threads:
            thread_key = f"#{ch.name} > {thread.name}"
            try:
                async for msg in thread.history(**history_kwargs):
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
                    if keyword_filters:
                        if not any(kw.lower() in cleaned.lower() for kw in keyword_filters):
                            continue
                    grouped.setdefault(thread_key, []).append((msg.author.display_name, cleaned))
            except (discord.Forbidden, discord.HTTPException):
                continue

    return grouped


def format_grouped_messages(grouped: dict) -> str:
    """Format grouped messages into a structured string that clearly separates contexts.

    Output looks like:
    === Channel: #general ===
    Alice: hi everyone
    Bob: hey

    === Thread: #general > Biology Cells ===
    Alice: what is the powerhouse of the cell?
    Bob: mitochondria
    ...

    This prevents the AI from cross-contaminating conversations.
    """
    sections = []
    for context_key, messages in grouped.items():
        if not messages:
            continue
        label = "Thread" if " > " in context_key else "Channel"
        header = f"=== {label}: {context_key} ==="
        lines = [f"{author}: {text}" for author, text in messages]
        sections.append(header + "\n" + "\n".join(lines))
    return "\n\n".join(sections)


def count_grouped_messages(grouped: dict) -> int:
    """Total message count across all groups."""
    return sum(len(msgs) for msgs in grouped.values())


def flatten_grouped_messages(grouped: dict) -> List[tuple]:
    """Flatten back to (context_key, author, text) tuples for legacy code paths."""
    flat = []
    for context_key, messages in grouped.items():
        for author, text in messages:
            flat.append((context_key, author, text))
    return flat

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
    rag_thresh = gs.get("rag_reaction_threshold", 1)
    rag_info = rag_stats(guild_id)
    embed.add_field(name="🧠 RAG Knowledge Base", value=f"{rag_info['total_entries']} entries | Threshold: {rag_thresh} 👍", inline=False)
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

    search_terms = [topic] if topic else (keyword_filters if keyword_filters else None)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    if allowed_channels:
        channels_to_scan = [guild.get_channel(int(cid)) for cid in allowed_channels if guild.get_channel(int(cid))]
    else:
        channels_to_scan = [ch for ch in guild.text_channels if ch.permissions_for(guild.me).read_message_history]

    grouped = await collect_messages_grouped(
        channels_to_scan,
        cutoff_after=cutoff,
        keyword_filters=search_terms,
        limit_per_source=2000,
        include_threads=True,
    )

    total_count = count_grouped_messages(grouped)
    messages_flat = flatten_grouped_messages(grouped)

    title = f"📊 Topic Search: \"{topic}\"" if topic else f"📊 Analysis — Last {hours} Hour(s)"

    if total_count == 0:
        await channel.send(f"No messages found{f' about **{topic}**' if topic else ''} in the last **{hours} hours**.")
        return

    embed = discord.Embed(title=title, color=0x57F287, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Requested by {requester.display_name} · PII redacted")
    embed.add_field(name="Messages found", value=str(total_count), inline=True)
    embed.add_field(name="Contexts", value=str(len(grouped)), inline=True)
    embed.add_field(name="Format",         value=output_format.capitalize(), inline=True)
    if search_terms:
        embed.add_field(name="🔍 Searching for", value=", ".join(search_terms), inline=False)

    if output_format == "summary" and not topic:
        authors = [author for _, author, _ in messages_flat]
        top     = Counter(authors).most_common(5)
        top_str = "\n".join([f"**{a}**: {c} msg(s)" for a, c in top])
        embed.add_field(name="🗣️ Most Active Users", value=top_str or "N/A", inline=False)

    sample_lines = [f"[{ctx_key}] {author}: {text}" for ctx_key, author, text in messages_flat[:5]]
    sample = "\n".join(sample_lines)
    if len(sample) > 1000:
        sample = sample[:1000] + "\n...[truncated]"
    embed.add_field(name="📝 Sample Messages", value=f"```{sample}```", inline=False)

    if output_format == "raw" or topic:
        raw_text   = format_grouped_messages(grouped)
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
        name="🧠 `!rag`",
        value="View the community knowledge base stats. Answers in threads that get 👍 reactions are automatically saved and used in future study guides.",
        inline=False
    )
    embed.add_field(
        name="� `!learnthis`",
        value="Upload a **PDF** or **TXT** file of your notes directly to the knowledge base. Attach the file to the same message.\nExample: Type `!learnthis` and drag your notes PDF into the message.",
        inline=False
    )
    embed.add_field(
        name="�🔄 `!ragsync`",
        value="Scan all threads for 👍-upvoted messages and backfill them into the knowledge base. Run this when starting on a new device or to catch up on historical reactions.",
        inline=False
    )
    embed.add_field(
        name="⚙️ `!changesettings threshold <n>`",
        value="Set the minimum 👍 reactions needed to add answers to the RAG knowledge base.\nExample: `!changesettings threshold 2`",
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

    # Collect recent chat for context (including threads), don't filter by topic
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    if allowed_channels:
        channels_to_scan = [ctx.guild.get_channel(int(cid)) for cid in allowed_channels if ctx.guild.get_channel(int(cid))]
    else:
        channels_to_scan = [ch for ch in ctx.guild.text_channels if ch.permissions_for(ctx.guild.me).read_message_history]

    grouped = await collect_messages_grouped(
        channels_to_scan,
        cutoff_after=cutoff,
        limit_per_source=500,
        include_threads=True,
    )

    # Build chat history with clear context separation
    chat_history = format_grouped_messages(grouped)

    # Query community RAG knowledge base
    community_context = ""
    if ctx.guild:
        try:
            community_context = await asyncio.to_thread(rag_query, ctx.guild.id, [topic])
        except Exception as e:
            print(f"  ⚠️ RAG query failed: {e}")

    try:
        file_path, status_msg = await generate_with_progress(
            ctx.channel,
            asyncio.to_thread(
                generate_html_resource,
                chat_history=chat_history,
                topics=[topic],
                student_level="Unknown",
                subject_area=topic,
                rag_context=community_context,
            ),
        )
    except Exception as e:
        await ctx.send(f"❌ Study guide generation failed: {e}")
        return

    await status_msg.edit(content="📊 **Generating Study Guide**\n`[██████████]`\n_☁️ Uploading to S3..._")

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
    elif setting.lower() == "threshold":
        try:
            threshold = int(value)
            if threshold < 1:
                raise ValueError()
            set_guild_setting(ctx.guild.id, "rag_reaction_threshold", threshold)
            await ctx.send(f"✅ RAG reaction threshold updated to **{threshold}** 👍.")
        except ValueError:
            await ctx.send("❌ Please provide a positive number.\nExample: `!changesettings threshold 2`")
    else:
        await ctx.send(f"❌ Unknown setting `{setting}`.\nAvailable: `time`, `threshold` — or use `@panikbot` for the full settings menu.")


@bot.command(name="rag")
async def rag_cmd(ctx):
    if ctx.guild is None:
        await ctx.send("Run this in a server channel.")
        return

    stats = rag_stats(ctx.guild.id)
    guild_settings = get_guild_settings(ctx.guild.id)
    threshold = guild_settings.get("rag_reaction_threshold", 1)

    embed = discord.Embed(
        title="🧠 Community Knowledge Base (RAG)",
        description=(
            "PanikBot tracks answers in **threads** that receive 👍 reactions.\n"
            "These community-sourced explanations are stored and used to enhance "
            "future study guides with peer-generated analogies and insights."
        ),
        color=0xC89116,
    )
    embed.add_field(name="📚 Total Entries", value=str(stats["total_entries"]), inline=True)
    embed.add_field(name="👍 Reaction Threshold", value=str(threshold), inline=True)
    embed.add_field(
        name="How it works",
        value=(
            "1. Someone asks a question in a **thread**\n"
            "2. Others answer in that thread\n"
            "3. React with 👍 to good answers\n"
            "4. Bot auto-saves answers that hit the threshold\n"
            "5. **Or** upload notes directly with `!learnthis` (PDF/TXT)\n"
            "6. Future `!saveus`/`!helpus`/`!quiz` guides reference these explanations\n"
            "7. Contributors get **✨ Community Spotlight** shoutouts!"
        ),
        inline=False,
    )
    embed.set_footer(text="Change threshold: !changesettings threshold <n>")
    await ctx.send(embed=embed)


@bot.command(name="learnthis")
async def learnthis_cmd(ctx):
    """Upload a PDF (or TXT) to the RAG knowledge base so future study guides can reference it."""
    if ctx.guild is None:
        await ctx.send("Run this in a server channel.")
        return

    # Check for PDF / TXT attachments
    pdf_attachments = [a for a in ctx.message.attachments if a.filename.lower().endswith(".pdf")]
    txt_attachments = [a for a in ctx.message.attachments if a.filename.lower().endswith(".txt")]

    if not pdf_attachments and not txt_attachments:
        await ctx.send(
            "📎 **How to use `!learnthis`:**\n"
            "Attach a **PDF** or **TXT** file to your message along with the command.\n"
            "Example: Type `!learnthis` and drag-and-drop your notes PDF into the same message.\n\n"
            "The content will be chunked, embedded, and added to the 🧠 **Community Knowledge Base** "
            "so future study guides and quizzes can reference your notes!"
        )
        return

    total_chunks = 0

    # Process PDF attachments
    for attachment in pdf_attachments:
        await ctx.send(f"📄 Processing **{attachment.filename}**...")

        try:
            file_bytes = await attachment.read()
        except Exception as e:
            await ctx.send(f"❌ Failed to download `{attachment.filename}`: {e}")
            continue

        # Extract text from PDF
        try:
            import PyPDF2

            reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
            pages_text = []
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    pages_text.append(page_text.strip())
            full_text = "\n\n".join(pages_text)
        except Exception as e:
            await ctx.send(f"❌ Failed to parse `{attachment.filename}` — is it a valid PDF? Error: {e}")
            continue

        if not full_text.strip():
            await ctx.send(f"⚠️ `{attachment.filename}` appears to be empty or image-only (no extractable text). Skipping.")
            continue

        # Clean PII from the extracted text
        full_text = clean_text(full_text)

        # Add to RAG
        try:
            chunks_added = await asyncio.to_thread(
                rag_add_doc,
                guild_id=ctx.guild.id,
                author_name=ctx.author.display_name,
                content=full_text,
                source_name=attachment.filename,
            )
            total_chunks += chunks_added
            await ctx.send(f"✅ `{attachment.filename}` — added **{chunks_added}** knowledge chunks to the RAG!")
        except Exception as e:
            await ctx.send(f"❌ Failed to add `{attachment.filename}` to RAG: {e}")
            continue

    # Process TXT attachments
    for attachment in txt_attachments:
        await ctx.send(f"📝 Processing **{attachment.filename}**...")

        try:
            file_bytes = await attachment.read()
            full_text = file_bytes.decode("utf-8", errors="replace")
        except Exception as e:
            await ctx.send(f"❌ Failed to read `{attachment.filename}`: {e}")
            continue

        if not full_text.strip():
            await ctx.send(f"⚠️ `{attachment.filename}` is empty. Skipping.")
            continue

        full_text = clean_text(full_text)

        try:
            chunks_added = await asyncio.to_thread(
                rag_add_doc,
                guild_id=ctx.guild.id,
                author_name=ctx.author.display_name,
                content=full_text,
                source_name=attachment.filename,
            )
            total_chunks += chunks_added
            await ctx.send(f"✅ `{attachment.filename}` — added **{chunks_added}** knowledge chunks to the RAG!")
        except Exception as e:
            await ctx.send(f"❌ Failed to add `{attachment.filename}` to RAG: {e}")
            continue

    if total_chunks > 0:
        final_stats = rag_stats(ctx.guild.id)
        embed = discord.Embed(
            title="🧠 Knowledge Base Updated!",
            description=(
                f"Successfully processed your upload and added **{total_chunks}** chunks.\n"
                f"The knowledge base now has **{final_stats['total_entries']}** total entries.\n\n"
                f"Your notes will be referenced in future `!saveus`, `!helpus`, and `!quiz` results as "
                f"**✨ Community Spotlight** contributions credited to **@{ctx.author.display_name}**."
            ),
            color=0xC89116,
        )
        embed.set_footer(text="PanikBot RAG · Upload more notes anytime with !learnthis")
        await ctx.send(embed=embed)


@bot.command(name="ragsync")
async def ragsync_cmd(ctx):
    """Scan all threads in the server and backfill messages with enough 👍 reactions into the RAG knowledge base."""
    if ctx.guild is None:
        await ctx.send("Run this in a server channel.")
        return

    guild_settings = get_guild_settings(ctx.guild.id)
    threshold = guild_settings.get("rag_reaction_threshold", 1)

    await ctx.send(
        f"🔄 **Syncing RAG knowledge base...**\n"
        f"Scanning all threads for messages with ≥ {threshold} 👍 reactions. This may take a moment..."
    )

    added = 0
    updated = 0
    scanned_threads = 0
    scanned_messages = 0

    # Gather all text channels the bot can read
    channels = [
        ch for ch in ctx.guild.text_channels
        if ch.permissions_for(ctx.guild.me).read_message_history
    ]

    for channel in channels:
        # Get active threads
        try:
            active_threads = channel.threads
        except Exception:
            active_threads = []

        # Get archived threads
        archived_threads = []
        try:
            async for thread in channel.archived_threads(limit=100):
                archived_threads.append(thread)
        except (discord.Forbidden, discord.HTTPException):
            pass

        all_threads = list(active_threads) + archived_threads

        for thread in all_threads:
            scanned_threads += 1
            try:
                async for message in thread.history(limit=1000):
                    if message.author.bot:
                        continue
                    if not message.content.strip():
                        continue

                    scanned_messages += 1

                    # Check for 👍 reactions
                    thumbs_count = 0
                    for reaction in message.reactions:
                        if str(reaction.emoji) == "👍":
                            thumbs_count = reaction.count
                            break

                    if thumbs_count >= threshold:
                        # Check if it's a new or existing entry
                        stats_before = rag_stats(ctx.guild.id)
                        try:
                            await asyncio.to_thread(
                                rag_add,
                                guild_id=ctx.guild.id,
                                message_id=message.id,
                                author_name=message.author.display_name,
                                content=message.content,
                                channel_name=channel.name,
                                thread_name=thread.name,
                                reaction_count=thumbs_count,
                            )
                            stats_after = rag_stats(ctx.guild.id)
                            if stats_after["total_entries"] > stats_before["total_entries"]:
                                added += 1
                            else:
                                updated += 1
                        except Exception as e:
                            print(f"  ❌ RAG sync failed for message {message.id}: {e}")
            except (discord.Forbidden, discord.HTTPException):
                continue

    final_stats = rag_stats(ctx.guild.id)
    embed = discord.Embed(
        title="✅ RAG Sync Complete",
        color=0xC89116,
    )
    embed.add_field(name="🔍 Threads Scanned", value=str(scanned_threads), inline=True)
    embed.add_field(name="💬 Messages Checked", value=str(scanned_messages), inline=True)
    embed.add_field(name="➕ New Entries Added", value=str(added), inline=True)
    embed.add_field(name="🔄 Entries Updated", value=str(updated), inline=True)
    embed.add_field(name="📚 Total KB Entries", value=str(final_stats["total_entries"]), inline=True)
    embed.set_footer(text=f"Threshold: {threshold} 👍 | Change with !changesettings threshold <n>")
    await ctx.send(embed=embed)


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

    grouped = await collect_messages_grouped(
        channels_to_scan,
        cutoff_after=cutoff,
        keyword_filters=keyword_filters if keyword_filters else None,
        limit_per_source=2000,
        include_threads=True,
    )

    total = count_grouped_messages(grouped)
    if total == 0:
        await ctx.send(f"No messages found in the last **{hours} hours**.")
        return

    chat_history = format_grouped_messages(grouped)

    await ctx.send(f"📨 Collected **{total}** messages across **{len(grouped)}** contexts. Analysing with Gemini...")

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

    # Query community RAG knowledge base
    community_context = ""
    if ctx.guild:
        try:
            community_context = await asyncio.to_thread(rag_query, ctx.guild.id, topics)
        except Exception as e:
            print(f"  ⚠️ RAG query failed: {e}")

    try:
        file_path, status_msg = await generate_with_progress(
            ctx.channel,
            asyncio.to_thread(
                generate_html_resource,
                chat_history=chat_history,
                topics=topics,
                student_level=level,
                subject_area=subject,
                rag_context=community_context,
            ),
        )
    except Exception as e:
        await ctx.send(f"❌ Study guide generation failed: {e}")
        return

    # Upload to S3 and get a URL
    await status_msg.edit(content="📊 **Generating Study Guide**\n`[██████████]`\n_☁️ Uploading to S3..._")

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

        grouped = await collect_messages_grouped(
            channels_to_scan,
            cutoff_after=start_dt,
            cutoff_before=end_dt,
            keyword_filters=keyword_filters if keyword_filters else None,
            limit_per_source=5000,
            include_threads=True,
        )

        total = count_grouped_messages(grouped)
        if total == 0:
            await ctx.send("No messages found in that date range.")
            return

        chat_history = format_grouped_messages(grouped)

        await ctx.send(f"📨 Collected **{total}** messages across **{len(grouped)}** contexts. Analysing with Gemini...")

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

        # Query community RAG knowledge base
        community_context = ""
        if ctx.guild:
            try:
                community_context = await asyncio.to_thread(rag_query, ctx.guild.id, topics)
            except Exception as e:
                print(f"  ⚠️ RAG query failed: {e}")

        try:
            file_path, status_msg = await generate_with_progress(
                ctx.channel,
                asyncio.to_thread(
                    generate_html_resource,
                    chat_history=chat_history,
                    topics=topics,
                    student_level=level,
                    subject_area=subject,
                    rag_context=community_context,
                ),
            )
        except Exception as e:
            await ctx.send(f"❌ Study guide generation failed: {e}")
            return

        await status_msg.edit(content="📊 **Generating Study Guide**\n`[██████████]`\n_☁️ Uploading to S3..._")

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


class DateSelectView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.add_item(DateSelectDropdown(ctx, 'start'))
        self.add_item(DateSelectDropdown(ctx, 'end'))


class DateSelectDropdown(discord.ui.Select):
    def __init__(self, ctx, which):
        self.ctx = ctx
        self.which = which
        now = datetime.now()
        years = [str(now.year - 1), str(now.year), str(now.year + 1)]
        months = [str(m).zfill(2) for m in range(1, 13)]
        days = [str(d).zfill(2) for d in range(1, 32)]
        hours = [str(h).zfill(2) for h in range(0, 24)]
        minutes = [str(m).zfill(2) for m in range(0, 60, 5)]
        options = []
        for y in years:
            for m in months:
                for d in days:
                    for h in hours:
                        for min in minutes:
                            label = f"{y}-{m}-{d} {h}:{min}"
                            options.append(discord.SelectOption(label=label, value=label))
        super().__init__(
            placeholder=f"Select {which} date/time...",
            min_values=1, max_values=1,
            options=options[:100],  # Discord limit
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"Selected {self.which} date/time: {self.values[0]}", ephemeral=True)


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
        # Fallback: scrape recent messages (with thread awareness)
        guild_settings   = get_guild_settings(ctx.guild.id)
        hours            = guild_settings.get("hours", 24)
        allowed_channels = guild_settings.get("channels", [])
        keyword_filters  = guild_settings.get("keyword_filters", [])

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        if allowed_channels:
            channels_to_scan = [ctx.guild.get_channel(int(cid)) for cid in allowed_channels if ctx.guild.get_channel(int(cid))]
        else:
            channels_to_scan = [ch for ch in ctx.guild.text_channels if ch.permissions_for(ctx.guild.me).read_message_history]

        grouped = await collect_messages_grouped(
            channels_to_scan,
            cutoff_after=cutoff,
            keyword_filters=keyword_filters if keyword_filters else None,
            limit_per_source=2000,
            include_threads=True,
        )

        total = count_grouped_messages(grouped)
        if total == 0:
            await ctx.send(f"No messages found in the last **{hours} hours**.")
            return

        chat_history = format_grouped_messages(grouped)

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
                # Query community RAG knowledge base
                community_context = ""
                if ctx.guild:
                    try:
                        community_context = await asyncio.to_thread(rag_query, ctx.guild.id, weak_topics)
                    except Exception as e:
                        print(f"  ⚠️ RAG query failed: {e}")

                try:
                    file_name, status_msg = await generate_with_progress(
                        ctx.channel,
                        asyncio.to_thread(
                            generate_html_resource,
                            chat_history,
                            weak_topics,
                            student_level,
                            subject_area,
                            rag_context=community_context,
                        ),
                    )
                except Exception as e:
                    await ctx.send(f"❌ Study guide generation failed: {e}")
                    bot._quiz_pending = None
                    return

                await status_msg.edit(content="📊 **Generating Study Guide**\n`[██████████]`\n_☁️ Uploading to S3..._")

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
# !battle — rapid-fire quiz battle in a thread
# ─────────────────────────────────────────

@bot.command(name="battle")
async def battle_cmd(ctx, *, topic: str = None):
    if ctx.guild is None:
        await ctx.send("Run this in a server channel.")
        return
    if not topic:
        await ctx.send("Please provide a topic.\nExample: `!battle Biology Cells`")
        return

    await ctx.send(f"⚔️ **Battle Mode!** Generating 5 questions on **{topic}**...")

    try:
        battle_data = await asyncio.to_thread(generate_battle_questions, topic)
    except Exception as e:
        await ctx.send(f"❌ Failed to generate battle questions: {e}")
        return

    questions = battle_data.get("questions", [])
    if not questions:
        await ctx.send("❌ Couldn't generate questions. Try again.")
        return

    # Create a thread for the battle
    thread = await ctx.channel.create_thread(
        name=f"⚔️ Battle: {topic[:80]}",
        type=discord.ChannelType.public_thread,
        auto_archive_duration=60,
    )

    # Track players and scores
    players: Dict[int, Dict[str, Any]] = {}  # user_id -> {display_name, score}

    await thread.send(
        f"⚔️ **BATTLE: {topic}** ⚔️\n\n"
        f"**Rules:**\n"
        f"• 5 rapid-fire questions — type your answer in chat!\n"
        f"• First correct answer wins the point\n"
        f"• Next question drops 5 seconds after a correct answer\n"
        f"• If nobody gets it in 30 seconds, I'll explain and move on\n\n"
        f"Get ready... first question in **3 seconds!**"
    )
    await asyncio.sleep(3)

    for i, q in enumerate(questions, 1):
        # Build set of acceptable answers (case-insensitive)
        correct_answers = {q["answer"].strip().lower()}
        for alt in q.get("accept_also", []):
            correct_answers.add(alt.strip().lower())

        await thread.send(f"**Question {i}/5:**\n>>> {q['question']}")

        # Wait for the first correct answer (30s timeout)
        winner = None

        def check_battle(m):
            return m.channel == thread and not m.author.bot

        deadline = asyncio.get_event_loop().time() + 30.0

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                msg = await bot.wait_for("message", check=check_battle, timeout=remaining)
            except asyncio.TimeoutError:
                break

            # Register player if new
            if msg.author.id not in players:
                players[msg.author.id] = {"display_name": msg.author.display_name, "score": 0}

            # Check if answer is correct
            user_answer = msg.content.strip().lower()
            if user_answer in correct_answers:
                winner = msg.author
                players[msg.author.id]["score"] += 1
                break

        if winner:
            await thread.send(f"✅ **Correct!** {winner.mention} gets the point! 🎯\n💡 _{q['explanation']}_")
            if i < len(questions):
                await thread.send("⏳ Next question in **5 seconds**...")
                await asyncio.sleep(5)
        else:
            # Nobody got it — explain the answer
            await thread.send(
                f"⏰ **Time's up!** Nobody got it.\n"
                f"The answer was: **{q['answer']}**\n"
                f"💡 _{q['explanation']}_"
            )
            if i < len(questions):
                await thread.send("⏳ Next question in **5 seconds**...")
                await asyncio.sleep(5)

    # ── Final leaderboard ──
    if not players:
        await thread.send("😅 No one participated! Battle over.")
        await ctx.send(f"⚔️ Battle on **{topic}** finished in {thread.mention} — but nobody played!")
        return

    sorted_players = sorted(players.values(), key=lambda p: p["score"], reverse=True)
    winner_name = sorted_players[0]["display_name"]
    winner_score = sorted_players[0]["score"]

    leaderboard_lines = []
    medals = ["🥇", "🥈", "🥉"]
    for idx, p in enumerate(sorted_players):
        medal = medals[idx] if idx < len(medals) else "▫️"
        leaderboard_lines.append(f"{medal} **{p['display_name']}** — {p['score']}/5")

    embed = discord.Embed(
        title=f"⚔️ Battle Results: {topic}",
        description="\n".join(leaderboard_lines),
        color=0xFFD700,
    )
    embed.set_footer(text="PanikBot Battle Mode")

    if winner_score > 0:
        await thread.send(f"🏆 **{winner_name} wins the battle with {winner_score}/5!** 🏆")
    else:
        await thread.send("😬 Nobody scored any points this round!")

    await thread.send(embed=embed)
    await ctx.send(f"⚔️ Battle on **{topic}** is over! Check results in {thread.mention}")


@bot.command(name="battleexplain")
async def battleexplain_cmd(ctx, *, topic: str = None):
    if not topic:
        await ctx.send("Please provide a topic/question to explain.")
        return
    try:
        result = await asyncio.to_thread(analyze_chat, topic)
        summary = result.get("summary_message", "No explanation returned.")
        await ctx.send(f"💡 Explanation: {summary}")
    except Exception as e:
        await ctx.send(f"❌ Failed to generate explanation: {e}")


# ─────────────────────────────────────────
# Progress bar helper for study guide generation
# ─────────────────────────────────────────

async def progress_bar_task(channel, status_msg, stop_event: asyncio.Event):
    """Animate a progress bar in-chat while study guide generates."""
    stages = [
        ("🔍 Analysing chat history...",       "░░░░░░░░░░", 0),
        ("🧠 Thinking about topics...",        "██░░░░░░░░", 1),
        ("🖼️ Searching for images...",         "████░░░░░░", 2),
        ("📝 Writing study guide...",          "██████░░░░", 3),
        ("📝 Writing study guide...",          "███████░░░", 4),
        ("📝 Still writing...",               "████████░░", 5),
        ("✨ Polishing...",                    "█████████░", 6),
        ("✨ Almost done...",                  "██████████", 7),
    ]
    idx = 0
    try:
        while not stop_event.is_set():
            stage_label, bar, _ = stages[min(idx, len(stages) - 1)]
            text = f"📊 **Generating Study Guide**\n`[{bar}]`\n_{stage_label}_"
            try:
                await status_msg.edit(content=text)
            except discord.NotFound:
                return
            idx += 1
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


async def generate_with_progress(channel, generate_coro):
    """Run a generation coroutine while showing a progress bar. Returns the result."""
    status_msg = await channel.send("📊 **Generating Study Guide**\n`[░░░░░░░░░░]`\n_Starting..._")
    stop_event = asyncio.Event()
    progress = asyncio.create_task(progress_bar_task(channel, status_msg, stop_event))

    try:
        result = await generate_coro
        return result, status_msg
    finally:
        stop_event.set()
        progress.cancel()
        try:
            await progress
        except asyncio.CancelledError:
            pass

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
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Track 👍 reactions on thread messages for the community RAG knowledge base."""
    if str(payload.emoji) != "👍":
        return
    if payload.guild_id is None:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    try:
        channel = guild.get_channel(payload.channel_id) or await guild.fetch_channel(payload.channel_id)
    except Exception:
        return

    # Only track messages inside threads
    if not isinstance(channel, discord.Thread):
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception:
        return

    # Ignore bot messages
    if message.author.bot:
        return

    # Count 👍 reactions on this message
    thumbs_count = 0
    for reaction in message.reactions:
        if str(reaction.emoji) == "👍":
            thumbs_count = reaction.count
            break

    # Get guild threshold setting
    guild_settings = get_guild_settings(payload.guild_id)
    threshold = guild_settings.get("rag_reaction_threshold", 1)

    if thumbs_count >= threshold:
        # Add/update in RAG knowledge base
        parent_name = channel.parent.name if channel.parent else "unknown"
        try:
            await asyncio.to_thread(
                rag_add,
                guild_id=payload.guild_id,
                message_id=message.id,
                author_name=message.author.display_name,
                content=message.content,
                channel_name=parent_name,
                thread_name=channel.name,
                reaction_count=thumbs_count,
            )
        except Exception as e:
            print(f"  ❌ RAG add failed: {e}")


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    """Remove entries from RAG if reactions drop below threshold."""
    if str(payload.emoji) != "👍":
        return
    if payload.guild_id is None:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    try:
        channel = guild.get_channel(payload.channel_id) or await guild.fetch_channel(payload.channel_id)
    except Exception:
        return

    if not isinstance(channel, discord.Thread):
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception:
        return

    thumbs_count = 0
    for reaction in message.reactions:
        if str(reaction.emoji) == "👍":
            thumbs_count = reaction.count
            break

    guild_settings = get_guild_settings(payload.guild_id)
    threshold = guild_settings.get("rag_reaction_threshold", 1)

    if thumbs_count < threshold:
        try:
            from rag_store import remove_message as rag_remove
            await asyncio.to_thread(rag_remove, payload.guild_id, payload.message_id)
            print(f"  🗑️ Removed from RAG (reactions dropped below threshold): {payload.message_id}")
        except Exception as e:
            print(f"  ❌ RAG remove failed: {e}")
    else:
        # Update the count
        parent_name = channel.parent.name if channel.parent else "unknown"
        try:
            await asyncio.to_thread(
                rag_add,
                guild_id=payload.guild_id,
                message_id=message.id,
                author_name=message.author.display_name,
                content=message.content,
                channel_name=parent_name,
                thread_name=channel.name,
                reaction_count=thumbs_count,
            )
        except Exception as e:
            print(f"  ❌ RAG update failed: {e}")


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