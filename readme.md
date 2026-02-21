Here is the updated, hackathon-ready GitHub README with the PanikBot name and branding applied throughout.

<div align="center">

🚑 PanikBot
The ultimate multiplayer study tool for panicked 2 AM group chats.

</div>

💡 The Pitch
When students study together, they are highly susceptible to shared misinformation. Standard AI requires you to know what you're confused about in order to ask a question.

PanikBot is a passive diagnostician. It lives in your university Discord server, reads the chaotic study chat, identifies what the entire group is collectively getting wrong, and instantly generates a permanent Notion study guide to save your grades.

✨ Features
Multiplayer Intelligence: Analyzes up to 100 recent messages to map the aggregate knowledge gaps of an entire Discord channel.

Passive Diagnosis: Catches the "unknown unknowns." It steps in when a group confidently agrees on an incorrect concept.

Instant Artifacts: Doesn't just reply in a messy chat. It uses the Notion API to generate a beautifully formatted, permanent, and shareable webpage.

Zero Friction: Students don't need to download a new app or make an account. It meets them exactly where they already study.

🏗️ How it Works
The Trigger: A student types @PanikBot save us in the chat.

The Fetch: The bot uses discord.js to pull and clean the last 100 messages.

The Brain: The chat log is sent to the Gemini API with a strict "Professor" system prompt to identify core misunderstandings.

The Output: The LLM's Markdown output is pushed to the Notion API to create a new, public page.

The Delivery: The bot drops the Notion link back into the Discord channel.