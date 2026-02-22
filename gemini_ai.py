import os
import base64
import json
import requests
from datetime import datetime
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# Initialize the SDK client (Make sure GEMINI_API_KEY is in your environment variables)
load_dotenv()
client = genai.Client()
MODEL_ID = 'gemini-2.5-flash'

# =====================================================================
# 1. PYDANTIC SCHEMA FOR STRUCTURED OUTPUT
# =====================================================================
class ChatAnalysis(BaseModel):
    action_required: str = Field(
        description="Must be either 'explain' if there are academic misconceptions, or 'lock_in' if they are procrastinating."
    )
    summary_message: str = Field(
        description="A short, punchy message to send to Discord. Either roasting them to lock in, or offering an explanation for their gaps."
    )
    topics_to_explain: list[str] = Field(
        description="A list of specific concepts they are getting wrong. Leave empty if action_required is 'lock_in'."
    )
    student_level: str = Field(
        description="Your assessment of their academic level based on the chat (e.g., 'Middle School', 'High School', 'Undergrad', 'Postgrad')."
    )
    subject_area: str = Field(
        description="The general subject they are discussing (e.g., 'Biology', 'Literature', 'Calculus', 'History'). Leave empty if off-task."
    )


class QuizQuestion(BaseModel):
    question: str = Field(description="The quiz question text.")
    options: list[str] = Field(description="Exactly 4 answer options labelled A-D. Each item is the full option text like 'A) Answer text'.")
    correct_answer: str = Field(description="The letter of the correct answer: A, B, C, or D.")
    explanation: str = Field(description="A short explanation of why the correct answer is right and common misconceptions.")


class QuizOutput(BaseModel):
    quiz_title: str = Field(description="A short, catchy title for the quiz.")
    questions: list[QuizQuestion] = Field(description="A list of 5 multiple-choice quiz questions.")


class BattleQuestion(BaseModel):
    question: str = Field(description="A short, direct question about the topic.")
    answer: str = Field(description="The correct answer in a few words (short enough for students to type quickly).")
    accept_also: list[str] = Field(description="Alternative acceptable phrasings/spellings of the correct answer.")
    explanation: str = Field(description="A one-sentence explanation of the answer.")


class BattleOutput(BaseModel):
    questions: list[BattleQuestion] = Field(description="Exactly 5 rapid-fire questions about the topic.")


BATTLE_GENERATOR_PROMPT = """
You are a quiz master creating rapid-fire trivia questions for a student battle.
Given a topic, generate exactly 5 short-answer questions that:

1. Are clear, concise, and have ONE unambiguous correct answer.
2. The answer should be SHORT (1-5 words max) so students can type it quickly.
3. Cover different aspects of the topic, escalating slightly in difficulty.
4. Provide 2-4 alternative acceptable answers in 'accept_also' (different spellings, abbreviations, common phrasings).
5. Include a brief explanation for each answer.
6. Output strictly in JSON format matching the schema.

Example for topic "Biology Cells":
  Q: "What organelle is known as the powerhouse of the cell?"
  Answer: "mitochondria"
  Accept also: ["mitochondrion", "the mitochondria"]
"""

# =====================================================================
# 2. THE SYSTEM PROMPTS
# =====================================================================
ANALYZER_PROMPT = """
You are a highly intelligent, slightly sassy accountability tutor and expert educator across all academic disciplines. 
Read the provided chat history from a group of students. Analyze it for two things:
1. Academic Misconceptions: Identify specific misunderstandings or gaps in knowledge regarding whatever subject they are studying (e.g., History, Math, Literature, Science). Automatically gauge their academic level based on their vocabulary and topics.
2. Procrastination Trends: Determine if the group is actually studying or getting distracted.
3. Ignore all bot commands and any off-topic banter that doesn't relate to their studies.

CRITICAL — Context boundaries:
The chat history is structured into sections separated by "=== Channel: #name ===" or "=== Thread: #channel > ThreadName ===" headers.
- Messages within the SAME section are part of the same conversation.
- Messages in DIFFERENT sections are COMPLETELY SEPARATE conversations and MUST NOT be cross-referenced or conflated.
- A message in "#general > Biology Cells" is ONLY about Biology Cells — not about anything discussed in "#general" or "#general > Chemistry".
- Do NOT attribute statements or topics from one section to another.
- Only report misconceptions that ACTUALLY appear within a section's own messages. Never hallucinate or invent student statements.
- If a word appears in a channel name or thread title, that does NOT mean students discussed that topic in a different thread.

Output strictly in JSON format matching the schema. 
- If they are severely off-task, set 'action_required' to "lock_in" and write a strict, motivational 'summary_message' telling them to focus.
- If they have academic misconceptions, set 'action_required' to "explain". Write a supportive 'summary_message' highlighting their gaps and ask: "I’ve noticed some confusion regarding [Topic]. Would you like a deep-dive explanation?"
"""

HTML_GENERATOR_PROMPT = """
You are an expert educator and technical writer capable of teaching any subject at any level. The users have requested a deep-dive study guide to correct specific misconceptions identified in their recent chat.

CRITICAL — Context boundaries:
The chat history may be structured into sections separated by "=== Channel: #name ===" or "=== Thread: #channel > ThreadName ===" headers.
- Only address misconceptions that ACTUALLY appear in the provided material. Do NOT invent or hallucinate topics.
- If the analysis references a specific thread (e.g. "Biology Cells"), only cover content from that thread — not from unrelated threads.
- Never cross-reference or conflate topics from different sections.

Your task is to generate a comprehensive, visually STUNNING, standalone HTML study guide addressing these topics. This should look like a premium, polished web app — not a boring document.

Strict Requirements:

1. Output ONLY valid, raw HTML. Do not wrap the output in markdown code blocks (NO ```html).

2. Adapt your tone, vocabulary, and depth of explanation to the provided 'Student Level' and 'Subject Area'. Use highly relatable analogies tailored to that age group.

3. TECH STACK — Embed these via CDN in the <head>:
   - Tailwind CSS: <script src="https://cdn.tailwindcss.com"></script>
   - AOS (Animate On Scroll): <link href="https://unpkg.com/aos@2.3.1/dist/aos.css" rel="stylesheet"> and <script src="https://unpkg.com/aos@2.3.1/dist/aos.js"></script>
   - Initialize AOS in a <script> at the bottom of body: AOS.init({ duration: 800, easing: 'ease-out-cubic', once: true });

4. LOGO HEADER — At the very top of the page, create a hero/header section with:
   - The PanikBot logo displayed using the base64 data URI provided in the prompt context (use the exact data URI given). Display it as an <img> with class "h-16 w-auto".
   - CRITICAL: The logo has dark/transparent parts that blend into dark backgrounds. You MUST wrap the logo <img> in a container with a white/light background pill so it's always visible. Use a wrapper like: <div style="background: rgba(255,255,255,0.95); border-radius: 12px; padding: 8px 16px; display: inline-block;"><img ...></div>. This ensures the logo pops against the dark header.
   - The title of the study guide next to or below the logo.
   - A subtle animated gradient background on the header using CSS keyframes (animate the gradient position shifting between #000000, #1a1a2e, #c89116, #000000).
   - Add a glowing pulse effect behind the logo pill using a CSS animation (a soft gold glow that pulses).

5. DESIGN SYSTEM — Use this exact design language throughout:
   - Background: #0a0a0f (very dark, almost black)
   - Card backgrounds: rgba(200, 145, 22, 0.05) with border: 1px solid rgba(200, 145, 22, 0.15) and backdrop-filter: blur(10px)
   - Primary accent: #c89116 (gold)
   - Secondary accent: #e8a830 (lighter gold)
   - Text: #e5e7eb (light gray) for body, #ffffff for headings
   - Gradient accents: linear-gradient(135deg, #c89116, #e8a830) for highlights, badges, and decorative elements
   - Border radius: rounded-2xl for cards, rounded-xl for inner elements
   - ALL content sections should be inside glassmorphism-style cards with the above styling
   - Add a subtle dot-grid or noise texture pattern as an SVG background on the body using CSS (very subtle, opacity ~0.03)

6. ANIMATIONS & INTERACTIONS — Make it feel alive:
   - Every major section/card should use AOS animations: data-aos="fade-up", data-aos="fade-right", etc. Stagger them with data-aos-delay="100", "200", etc.
   - Cards should have a CSS hover effect: transform: translateY(-4px) and a brighter border glow on hover (transition: all 0.3s ease)
   - Add a smooth scroll-to-top button (fixed bottom-right, gold gradient, appears after scrolling 300px down) with smooth scroll behavior
   - Section headings should have a decorative left border (4px solid with gold gradient) and a subtle fade-in
   - Add a floating particle/sparkle effect in the header using a small CSS-only animation (a few small gold dots floating upward with different animation-delay values — keep it subtle, max 5-8 particles)
   - Progress indicator: a thin gold line at the very top of the viewport that fills as the user scrolls down the page (use JS scroll event listener, position: fixed, top: 0, height: 3px, gold gradient background, z-index: 9999)

7. RESPONSIVE LAYOUT:
   - Mobile first. Use Tailwind responsive prefixes (sm:, md:, lg:) throughout.
   - Max content width: max-w-4xl mx-auto with px-4 sm:px-6 lg:px-8 padding
   - Cards should stack on mobile, can be side-by-side grid on larger screens where appropriate (grid grid-cols-1 md:grid-cols-2 gap-6)
   - Font sizes should scale: text-base on mobile, md:text-lg on desktop
   - Tables should be horizontally scrollable on mobile (overflow-x-auto wrapper)
   - Images should be max-w-full with responsive sizing

8. CONTENT STRUCTURE — Use these styled components:
   - "🧠 Key Concept" boxes: gold-bordered cards with a lightbulb emoji header
   - "⚠️ Common Misconception" boxes: red-tinted glassmorphism cards (rgba(239,68,68,0.08) bg, border-red-500/20 border) that show what students got wrong and the correction
   - "💡 Quick Tip" inline callouts with a gold left-border
   - "📝 Example" sections with a slightly different card style (indented, with a code or quote style depending on subject)
   - For Math/Physics: use styled formula boxes with a monospace font, centered, with a subtle gold underline
   - For CS: syntax-highlighted code blocks with a dark card, rounded corners, and a copy button (JS clipboard API)
   - For Literature/History: styled blockquotes with decorative quotation marks (large, gold, absolute positioned)
   - Directly and gently correct the specific misconceptions they had.

9. TL;DR SECTION — At the bottom, add a "⚡ TL;DR — Quick Reference" section:
   - Styled as a special card with a gold gradient top border (4px)
   - Bullet-point summary of ALL key takeaways
   - Designed to be print-friendly (see print styles below)

10. PRINT STYLES — Include a comprehensive @media print block:
    - White background, black text
    - Remove all animations, shadows, blur effects, background images
    - Remove the scroll-to-top button, progress bar, print button, particles
    - Cards should have simple 1px solid #ccc borders
    - Ensure page-break-inside: avoid on cards
    - The logo should still print (but without glow effects)
    - Add "Page X" footer in print

11. PRINT BUTTON — Fixed position, bottom-right corner (bottom-6 right-6), styled as a gold gradient pill button with a printer icon (🖨️ emoji or SVG). On hover, scale up slightly. Triggers window.print(). Hide in @media print.

12. COLOR GRADIENT — Use gradients of #000000 and #c89116 throughout as the primary design motif. Section dividers, decorative lines, badge backgrounds, heading underlines — all should use this gradient.

13. FOOTER — At the very bottom, a centered footer with:
    - The PanikBot logo again (smaller, h-8), wrapped in the same white pill background as the header so it's visible against the dark footer
    - Text: "generated by panikbot — saving grades one ping at a time"
    - Styled with opacity-60, small text, with a gold gradient line above it
    - Current year in the footer

14. IMAGES: You will be provided with a list of image URLs sourced from Wikipedia. ONLY embed an image if it is genuinely useful for understanding the concept (e.g. diagrams, graphs, charts, scientific illustrations, formula visualisations). Do NOT embed images that are photos of people, buildings, places, logos, icons, or decorative/irrelevant images.
    If an image URL looks irrelevant based on its filename/context, skip it entirely. It is BETTER to have no images than to have irrelevant ones.
    When you do embed an image: wrap in a glassmorphism card, use rounded-2xl, shadow-2xl, mx-auto, max-w-md. Add descriptive alt text. Add a caption below. Place near the relevant explanation. Add data-aos="zoom-in" animation.

15. COMMUNITY KNOWLEDGE BASE: You may be provided with a "Community Knowledge" section containing explanations and tips written by real students.
    - If community knowledge is provided, integrate the best insights naturally into your explanations.
    - Add a "✨ Community Spotlight" callout box (styled with a gold/amber glassmorphism card with a ✨ sparkle decorative element) for particularly helpful community contributions.
    - Credit the original author using their @username in the callout, e.g. "💡 Tip from @Username:".
    - Only feature community tips that are accurate and genuinely helpful.
    - If no community knowledge is provided, simply skip this — do not mention it.

16. EXTRA POLISH:
    - Add smooth scroll behavior to the html element: scroll-behavior: smooth
    - Use the CSS ::selection pseudo-element to make text selection gold (#c89116) with dark text
    - Add a subtle text-shadow to the main title for depth
    - Use CSS counter() for auto-numbering sections if appropriate
    - Wrap the entire page in a fade-in animation on load (opacity 0 to 1 over 0.5s)
    - Add aria-labels and semantic HTML (header, main, section, article, footer) for accessibility
"""

QUIZ_GENERATOR_PROMPT = """
You are an expert educator creating a quiz to test students on material from their recent study session.
You will receive chat history and/or study guide content. Generate exactly 5 multiple-choice questions that:

1. Target the core concepts and common misconceptions from the provided material.
2. Have exactly 4 options each (A, B, C, D). Make distractors plausible but clearly wrong.
3. Adapt difficulty to the student level provided.
4. Include a short explanation for each correct answer.
5. Output strictly in JSON format matching the schema.

CRITICAL — Context boundaries:
The chat history may be structured into sections with "=== Channel: #name ===" or "=== Thread: #channel > ThreadName ===" headers.
- Only quiz on content that ACTUALLY appears in the provided material. Do NOT invent topics from thread/channel names.
- Keep questions relevant to the specific thread or channel the students were discussing in.
"""

# =====================================================================
# 3. IMAGE SEARCH (Wikimedia Commons — free, no API key)
# =====================================================================

# Words to strip from search queries for better image results
_FILLER_WORDS = {"and", "or", "the", "of", "in", "a", "an", "its", "their", "with", "for", "to", "on", "by", "is", "are", "was", "were", "that", "this", "from", "as", "at", "between", "relationships", "application", "applications", "understanding", "concepts", "concept", "basics"}


def _simplify_query(query: str) -> str:
    """Strip filler words and shorten query for better image search results."""
    words = [w for w in query.split() if w.lower().strip(",()") not in _FILLER_WORDS]
    # Keep max 3 meaningful keywords
    return " ".join(words[:3])


def search_images(query: str, count: int = 2) -> list[str]:
    """Search Wikipedia for images related to query. Returns list of image URLs.
    Uses Wikipedia's API to find article images (diagrams, charts, illustrations)
    which are far more relevant than random Commons search results."""
    simplified = _simplify_query(query)
    headers = {
        "User-Agent": "PanikBot/1.0 (https://github.com/panikbot; panikbot@example.com)",
    }

    # Step 1: Search Wikipedia for a relevant article
    search_url = "https://en.wikipedia.org/w/api.php"
    search_params = {
        "action": "query",
        "list": "search",
        "srsearch": simplified,
        "srlimit": "3",
        "format": "json",
    }
    try:
        resp = requests.get(search_url, params=search_params, headers=headers, timeout=10)
        resp.raise_for_status()
        search_results = resp.json().get("query", {}).get("search", [])
        if not search_results:
            print(f"  No Wikipedia articles found for '{simplified}'")
            return []
    except Exception as e:
        print(f"  Wikipedia search failed for '{simplified}': {e}")
        return []

    # Step 2: Get images from the top article(s)
    image_urls = []
    for article in search_results[:2]:
        title = article["title"]
        img_params = {
            "action": "query",
            "titles": title,
            "prop": "images",
            "imlimit": "20",
            "format": "json",
        }
        try:
            resp = requests.get(search_url, params=img_params, headers=headers, timeout=10)
            resp.raise_for_status()
            pages = resp.json().get("query", {}).get("pages", {})
            for page in pages.values():
                for img in page.get("images", []):
                    img_title = img.get("title", "")
                    # Filter: only keep SVGs/PNGs that look like diagrams, not icons or logos
                    lower = img_title.lower()
                    # Skip irrelevant images
                    if any(skip in lower for skip in ["icon", "logo", "flag", "commons-logo", "wiki", "edit", "button", "symbol", "medal", "award", "portrait", "photo", "folder", "ambox", "padlock", "question_book", "text-x"]):
                        continue
                    if not any(ext in lower for ext in [".svg", ".png", ".jpg", ".jpeg"]):
                        continue

                    # Step 3: Get the actual image URL
                    url_params = {
                        "action": "query",
                        "titles": img_title,
                        "prop": "imageinfo",
                        "iiprop": "url|mime",
                        "iiurlwidth": "800",
                        "format": "json",
                    }
                    try:
                        img_resp = requests.get(search_url, params=url_params, headers=headers, timeout=10)
                        img_resp.raise_for_status()
                        img_pages = img_resp.json().get("query", {}).get("pages", {})
                        for img_page in img_pages.values():
                            info = img_page.get("imageinfo", [{}])[0]
                            mime = info.get("mime", "")
                            if not mime.startswith("image/"):
                                continue
                            thumb = info.get("thumburl") or info.get("url")
                            if thumb:
                                image_urls.append(thumb)
                                if len(image_urls) >= count:
                                    return image_urls
                    except Exception:
                        continue
        except Exception:
            continue

    return image_urls


def search_images_for_topics(topics: list[str], per_topic: int = 2) -> dict[str, list[str]]:
    """Search for educational images for each topic. Returns {topic: [url, ...]}"""
    result = {}
    for topic in topics:
        urls = search_images(topic, count=per_topic)
        if urls:
            result[topic] = urls
            print(f"  🖼️ Found {len(urls)} images for '{topic}'")
        else:
            print(f"  ⚠️ No images found for '{topic}'")
    return result


# =====================================================================
# 4. CORE FUNCTIONS
# =====================================================================

# Path to the PanikBot logo (relative to project root)
_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "logo.png")


def _get_logo_data_uri() -> str:
    """Read the PanikBot logo and return a base64 data URI string for embedding in HTML."""
    try:
        with open(_LOGO_PATH, "rb") as f:
            logo_bytes = f.read()
        b64 = base64.b64encode(logo_bytes).decode("utf-8")
        return f"data:image/png;base64,{b64}"
    except FileNotFoundError:
        print(f"⚠️ Logo not found at {_LOGO_PATH}, skipping logo embedding.")
        return ""
    except Exception as e:
        print(f"⚠️ Failed to read logo: {e}")
        return ""


def analyze_chat(chat_history: str) -> dict:
    """Takes the raw Discord chat string and returns a structured JSON analysis."""
    print("Analyzing chat history...")
    
    response = client.models.generate_content(
        model=MODEL_ID,
        contents=chat_history,
        config=types.GenerateContentConfig(
            system_instruction=ANALYZER_PROMPT,
            response_mime_type="application/json",
            response_schema=ChatAnalysis,
            temperature=0.2, 
        ),
    )
    
    return response.parsed.model_dump()


def generate_html_resource(chat_history: str, topics: list[str], student_level: str, subject_area: str, rag_context: str = "") -> str:
    """Generates the HTML study notes based on the chat history and metadata, saving it to responses/"""
    print(f"Generating HTML resource for {student_level} {subject_area} on topics: {topics}...")

    # Search for relevant images
    print("Searching for relevant images...")
    topic_images = search_images_for_topics(topics, per_topic=2)

    # Build image list for the prompt
    image_section = ""
    if topic_images:
        image_lines = []
        for topic, urls in topic_images.items():
            for url in urls:
                image_lines.append(f"  - Topic: {topic} | URL: {url}")
        image_section = "\n".join(image_lines)
    else:
        image_section = "No images available."

    # Build community knowledge section
    community_section = ""
    if rag_context.strip():
        community_section = f"""
    Community Knowledge (upvoted explanations from fellow students — integrate the best ones):
{rag_context}
"""
    else:
        community_section = "    Community Knowledge: None available yet."

    # Get the PanikBot logo as a base64 data URI for embedding
    logo_data_uri = _get_logo_data_uri()
    # We use a placeholder in the prompt and replace it post-generation to avoid
    # wasting Gemini tokens on a massive base64 string (~130KB).
    logo_placeholder = "%%PANIKBOT_LOGO_DATA_URI%%"
    if logo_data_uri:
        logo_section = f"""
    PANIKBOT LOGO: Use this exact string as the src attribute for all <img> tags displaying the PanikBot logo in the header and footer:
    {logo_placeholder}
    (This is a placeholder — the actual image data will be injected automatically.)
"""
    else:
        logo_section = "    PANIKBOT LOGO: Not available — use a text-only header with 'PanikBot' styled in the gold gradient instead."

    # Combine the context for the generator so it knows exactly how to pitch the lesson
    prompt_context = f"""
    Subject Area: {subject_area}
    Student Level: {student_level}
    Topics to Explain: {', '.join(topics)}

{logo_section}

    Available Images (embed these throughout the guide near relevant sections):
{image_section}
    
{community_section}

    Original Chat History for Context (address their exact misunderstandings):
    {chat_history}
    """

    response = client.models.generate_content(
        model=MODEL_ID,
        contents=prompt_context,
        config=types.GenerateContentConfig(
            system_instruction=HTML_GENERATOR_PROMPT,
            temperature=0.7, 
        ),
    )

    # Clean up the response in case Gemini includes markdown backticks
    html_content = response.text.strip()
    if html_content.startswith("```html"):
        html_content = html_content[7:]
    if html_content.endswith("```"):
        html_content = html_content[:-3]

    # Inject the actual logo base64 data URI, replacing the placeholder
    if logo_data_uri:
        html_content = html_content.replace(logo_placeholder, logo_data_uri)

    # Save to file
    os.makedirs("responses", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"study_guide_{timestamp}.html"
    file_path = os.path.join("responses", base_name)
    
    with open(file_path, "w", encoding='utf-8') as f:
        f.write(html_content)
        
    print(f"✅ HTML File saved to {file_path}")
    return base_name


def generate_quiz(chat_history: str, study_guide_content: str = "", student_level: str = "Unknown", subject_area: str = "Unknown") -> dict:
    """Generate a 5-question multiple-choice quiz from chat history and/or study guide content."""
    print(f"Generating quiz for {student_level} {subject_area}...")

    prompt_context = f"""
    Subject Area: {subject_area}
    Student Level: {student_level}

    Chat History:
    {chat_history}

    Study Guide Content (if available):
    {study_guide_content if study_guide_content else "No study guide available."}
    """

    response = client.models.generate_content(
        model=MODEL_ID,
        contents=prompt_context,
        config=types.GenerateContentConfig(
            system_instruction=QUIZ_GENERATOR_PROMPT,
            response_mime_type="application/json",
            response_schema=QuizOutput,
            temperature=0.4,
        ),
    )

    return response.parsed.model_dump()


def generate_battle_questions(topic: str) -> dict:
    """Generate 5 rapid-fire short-answer questions for a battle on the given topic."""
    print(f"Generating battle questions for topic: {topic}...")

    response = client.models.generate_content(
        model=MODEL_ID,
        contents=f"Topic: {topic}",
        config=types.GenerateContentConfig(
            system_instruction=BATTLE_GENERATOR_PROMPT,
            response_mime_type="application/json",
            response_schema=BattleOutput,
            temperature=0.5,
        ),
    )

    return response.parsed.model_dump()


# =====================================================================
# Example Usage (You can delete this block when importing to your bot)
# =====================================================================
if __name__ == "__main__":
    # Define a dummy chat string to test (no file needed now!)
    dummy_chat = (
        "User1: Bro what even is a linked list, is it just an array with pointers?\n"
        "User2: I think so, but searching takes O(1) time right?\n"
        "User1: Yeah probably."
    )
    
    # Step 1: Analyze the raw string directly
    print("Testing Analysis...")
    result = analyze_chat(dummy_chat)
    
    print("\n--- API RESULT ---")
    print(json.dumps(result, indent=2))
    
    # Step 2: If the bot gets confirmation from Discord, generate HTML
    if result.get("action_required") == "explain":
        saved_file = generate_html_resource(
            chat_history=dummy_chat,  # Pass the string here, not a file path!
            topics=result["topics_to_explain"],
            student_level=result["student_level"],
            subject_area=result["subject_area"] 
        )
        print(f"\n🚀 SUCCESS! Your AWS sync script can now pick up: {saved_file}")
    else:
        print("\n🛑 Action required was 'lock_in'. No HTML generated. Telling them to focus!")