import os
import json
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
# =====================================================================
# 2. THE SYSTEM PROMPTS
# =====================================================================
ANALYZER_PROMPT = """
You are a highly intelligent, slightly sassy accountability tutor and expert educator across all academic disciplines. 
Read the provided chat history from a group of students. Analyze it for two things:
1. Academic Misconceptions: Identify specific misunderstandings or gaps in knowledge regarding whatever subject they are studying (e.g., History, Math, Literature, Science). Automatically gauge their academic level based on their vocabulary and topics.
2. Procrastination Trends: Determine if the group is actually studying or getting distracted.

Output strictly in JSON format matching the schema. 
- If they are severely off-task, set 'action_required' to "lock_in" and write a strict, motivational 'summary_message' telling them to focus.
- If they have academic misconceptions, set 'action_required' to "explain". Write a supportive 'summary_message' highlighting their gaps and ask: "I’ve noticed some confusion regarding [Topic]. Would you like a deep-dive explanation?"
"""

HTML_GENERATOR_PROMPT = """
You are an expert educator and technical writer capable of teaching any subject at any level. The users have requested a deep-dive study guide to correct specific misconceptions identified in their recent chat.

Your task is to generate a comprehensive, visually stunning, standalone HTML study guide addressing these topics.

Strict Requirements:
1. Output ONLY valid, raw HTML. Do not wrap the output in markdown code blocks (NO ```html).
2. Adapt your tone, vocabulary, and depth of explanation to the provided 'Student Level' and 'Subject Area'. Use highly relatable analogies tailored to that age group.
3. Use a modern "Dark Mode" aesthetic. Embed Tailwind CSS via CDN (<script src="[https://cdn.tailwindcss.com](https://cdn.tailwindcss.com)"></script>).
4. Include a <style> block with a `@media print` query ensuring the document prints cleanly on physical paper (black text on white background).
5. Structure the content beautifully based on the subject (e.g., use blockquotes for Literature/History, well-styled formula boxes for Math/Physics, code blocks for CS).
6. Directly and gently correct the specific misconceptions they had. Include a "TL;DR Printable Notes" summary section at the bottom.
"""

# =====================================================================
# 3. CORE FUNCTIONS
# =====================================================================
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


def generate_html_resource(chat_history: str, topics: list[str], student_level: str, subject_area: str) -> str:
    """Generates the HTML study notes based on the chat history and metadata, saving it to responses/"""
    print(f"Generating HTML resource for {student_level} {subject_area} on topics: {topics}...")

    # Combine the context for the generator so it knows exactly how to pitch the lesson
    prompt_context = f"""
    Subject Area: {subject_area}
    Student Level: {student_level}
    Topics to Explain: {', '.join(topics)}
    
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

    # Save to file
    os.makedirs("responses", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"responses/study_guide_{timestamp}.html"
    
    with open(file_name, "w", encoding='utf-8') as f:
        f.write(html_content)
        
    print(f"✅ HTML File saved to {file_name}")
    return file_name

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