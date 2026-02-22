<div align="center">

# 🚑 PanikBot

**The ambient AI study companion that lives in your Discord group chat.**

*Saving grades one ping at a time.*

[![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)](https://python.org)
[![Discord.py](https://img.shields.io/badge/discord.py-2.0+-5865F2?logo=discord&logoColor=white)](https://discordpy.readthedocs.io)
[![Gemini](https://img.shields.io/badge/Gemini-2.5_Pro-4285F4?logo=google&logoColor=white)](https://ai.google.dev)
[![ChromaDB](https://img.shields.io/badge/ChromaDB-Vector_DB-FF6F00)](https://www.trychroma.com)
[![AWS S3](https://img.shields.io/badge/AWS-S3-FF9900?logo=amazons3&logoColor=white)](https://aws.amazon.com/s3/)

</div>

---

## 💡 The Pitch

When students study together on Discord, they're highly susceptible to shared misinformation. Standard AI tools like ChatGPT require you to **know what you're confused about** in order to ask a question.

**PanikBot is different.** It's a passive diagnostician that lives in your university Discord server, reads the chaotic 2 AM study chat, identifies what the group is collectively getting wrong, and generates premium study guides — all without anyone having to prompt it with the right question.

> *"ChatGPT is a tool you have to remember to use. PanikBot is an active study companion that lives in your group chat, learns from your friends, catches your mistakes before the exam, and literally tells you to lock in when you're distracted."*

---

## ✨ Features

| Feature | Description |
|---|---|
| **🧠 AI Chat Analysis** | Gemini 2.5 Pro reads your study chat, detects academic misconceptions vs. procrastination, and responds accordingly |
| **📄 Study Guide Generation** | Generates stunning, standalone HTML study guides with dark mode, animations, diagrams, and print-friendly layouts |
| **📊 Quiz System** | 5-question MCQ quizzes via Discord polls with per-user scoreboard, weak topic detection, and follow-up study guides |
| **⚔️ Battle Mode** | Real-time competitive short-answer quiz — race to type the answer first, with leaderboards |
| **🧠 Community RAG** | 👍 reactions on thread answers auto-save them to a vector DB. Future study guides reference peer explanations with ✨ Community Spotlight shoutouts |
| **📎 Note Uploads** | Upload PDFs/TXT files directly to the knowledge base with `!learnthis` |
| **🔒 PII Redaction** | Automatic scrubbing of emails, phone numbers, credit cards, passport numbers |
| **🖼️ Smart Images** | Auto-fetches relevant educational diagrams from Wikipedia — filters out irrelevant photos |
| **⚙️ Per-Server Settings** | Configurable time window, channels, keyword filters, RAG threshold — via interactive dropdowns |

---

## 🤖 Commands

| Command | Description |
|---|---|
| `!helpnow` | Show the full help menu |
| `!saveus` | Analyse recent chat → detect misconceptions → offer to generate a study guide |
| `!helpus <topic>` | Generate a targeted study guide on a specific topic |
| `!analyse` | Pick a custom date range via calendar dropdowns and analyse all messages |
| `!quiz` | Generate a 5-question poll quiz, then `!answers` to reveal scores |
| `!battle <topic>` | Start a rapid-fire speed quiz battle in a thread |
| `!battleexplain <topic>` | Quick AI explanation of a topic |
| `!learnthis` | Upload a PDF/TXT file to the community knowledge base |
| `!rag` | View knowledge base stats |
| `!ragsync` | Backfill scan all threads and uploads into the knowledge base |
| `!showsettings` | Display current server settings |
| `!changesettings time <hours>` | Set how far back to scan messages |
| `!changesettings threshold <n>` | Set minimum 👍 reactions for RAG ingestion |
| `@panikbot` | Open interactive settings menu |

---

## 🏗️ Architecture

```
Discord Chat
    │
    ├── !saveus / !analyse ──► Gemini Analysis (structured JSON)
    │                              │
    │                              ├── "lock_in" → Roast message
    │                              └── "explain" → Study Guide Pipeline
    │                                      │
    │                                      ├── RAG Query (ChromaDB)
    │                                      ├── Wikipedia Image Search
    │                                      ├── Gemini HTML Generation
    │                                      └── S3 Upload → Shareable Link
    │
    ├── !quiz ──► Gemini Quiz Gen → Discord Polls → Scoreboard → Weak Topics
    │
    ├── !battle ──► Gemini Battle Gen → Thread → Speed Quiz → Leaderboard
    │
    └── 👍 Reactions on Thread Messages
            │
            └── Auto-save to ChromaDB (RAG Knowledge Base)
                    │
                    └── Referenced in future study guides
                        with ✨ Community Spotlight credits
```

---

## 🛠️ Tech Stack

| Technology | Purpose |
|---|---|
| **Python 3.12** | Core language |
| **discord.py ≥ 2.0** | Bot framework — commands, events, polls, modals, buttons, dropdowns |
| **Google Gemini 2.5 Pro** | Chat analysis, study guide generation, quiz generation, battle questions |
| **Gemini Embeddings** (`gemini-embedding-001`) | Text embeddings for semantic RAG search |
| **ChromaDB** | Local persistent vector database for community knowledge base |
| **AWS S3** | Hosting generated HTML study guides with shareable URLs |
| **Pydantic** | Structured output schemas for Gemini responses |
| **Wikipedia API** | Educational diagram/illustration search |
| **PyPDF2** | PDF text extraction for note uploads |
| **Tailwind CSS + AOS.js** | Embedded in generated study guides for premium styling |

---

## 📁 Project Structure

```
├── panikbot.py          # Main bot — commands, events, UI, message collection, PII cleaning
├── gemini_ai.py         # Gemini AI — analysis, HTML generation, quiz gen, image search
├── rag_store.py         # ChromaDB + Gemini embeddings — community knowledge base
├── notioner.py          # S3 upload module — presigned URL generation
├── requirements.txt     # Python dependencies
├── settings.json        # Per-guild settings (gitignored)
├── assets/
│   └── logo.png         # PanikBot logo (base64-embedded in study guides)
├── responses/           # Generated HTML study guides (gitignored)
├── chromadb_data/       # Local vector database (gitignored)
└── tests/
    ├── test_cleaner.py
    ├── test_notioner.py
    └── test_s3_upload.py
```

---

## 🚀 Getting Started

### Prerequisites

- Python 3.12
- A Discord bot token ([Discord Developer Portal](https://discord.com/developers/applications))
- A Google Gemini API key ([Google AI Studio](https://aistudio.google.com))
- An AWS S3 bucket for study guide hosting

### Installation

```bash
# Clone the repo
git clone https://github.com/your-username/panikbot.git
cd panikbot

# Create and activate a virtual environment
python -m venv venv
# Windows
.\venv\Scripts\activate
# macOS/Linux
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Environment Variables

Create a `.env` file in the project root:

```env
DISCORD_TOKEN=your_discord_bot_token
GEMINI_API_KEY=your_gemini_api_key
AWS_ACCESS_KEY_ID=your_aws_access_key
AWS_SECRET_ACCESS_KEY=your_aws_secret_key
AWS_REGION=eu-west-2
S3_BUCKET=your-s3-bucket-name
```

### Run

```bash
python panikbot.py
```

The bot will automatically sync the RAG knowledge base on startup by scanning all threads for upvoted messages and `!learnthis` uploads.

---

## 🧠 How the RAG Knowledge Base Works

1. Someone asks a question in a **Discord thread**
2. Other students answer in that thread
3. React with 👍 to good answers
4. PanikBot auto-saves answers that hit the reaction threshold to ChromaDB
5. Future `!saveus`, `!helpus`, and `!analyse` study guides reference these peer explanations
6. Contributors get **✨ Community Spotlight** callout boxes crediting their `@username`
7. Students can also upload notes directly with `!learnthis` (PDF/TXT)
8. On startup or via `!ragsync`, the bot backfills all historical upvoted messages and uploads

---

## 🏆 What Makes PanikBot Different

| | ChatGPT / Gemini | PanikBot |
|---|---|---|
| **Activation** | You open a tab and type a question | It's already in your group chat, watching |
| **Context** | You have to explain your situation | It already knows what you're studying from the chat |
| **Multiplayer** | Single-player | Learns from the whole group, elevates everyone |
| **Knowledge** | Only the AI's training data | AI + your community's best explanations (RAG) |
| **Accountability** | Politely answers whatever you ask | Tells you to lock in when you're off-task |

---

<div align="center">

*Built at HackLondon 2026*

**PanikBot** — saving grades one ping at a time 🚑

</div>