import asyncio
import json
import base64
from typing import Any, List, Literal, Dict, Union
from pydantic import BaseModel, create_model, Field
from openai import AsyncOpenAI
from rapidfuzz import process, fuzz
import config

# Initialize OpenAI Client to use Lily (Poolot)
client = AsyncOpenAI(api_key=config.LILY_API_KEY, base_url="https://agent.poolot.com/v1")

def generate_dynamic_schema(fields: List[Dict]) -> type[BaseModel]:
    """
    Dynamically generates a Pydantic model enforcing the exact options available on the page.
    For massive dropdowns (>20 options), it requests a generic string to protect the token context, 
    which we will fuzzy-match locally.
    """
    schema_fields = {}
    for f in fields:
        field_id = f.get("id", "unknown_field")
        field_type = f.get("type", "text")
        options = f.get("options", [])
        
        # Determine the annotation constraint based on field type and option count
        if field_type in ["select", "radio", "checkbox"] and len(options) > 0:
            if len(options) > 20:
                # Protect context window: ask for a string, we fuzzy match later
                annotation = Union[str, int, float]
            else:
                # Strict constraints for smaller lists
                options_tuple = tuple(options)
                if field_type == "checkbox":
                    annotation = Union[List[Literal[options_tuple]], Literal[options_tuple], bool, str] # type: ignore
                else:
                    annotation = Union[Literal[options_tuple], str, int, float] # type: ignore
        elif field_type == "checkbox":
            # Individual boolean checkbox (e.g. opt-in, terms, individual race/military item)
            annotation = Union[bool, str, int]
        else:
            annotation = Union[str, bool, int, float]
            
        # Create a metadata-rich sub-model for this specific field
        sub_model_name = f"FieldAnswer_{field_id}"
        sub_model = create_model(
            sub_model_name,
            value=(annotation, ...),
            confidence=(float, Field(0.9, ge=0.0, le=1.0, description="Confidence score from 0.0 to 1.0")),
            source=(Literal["persona", "inferred", "deduced"], Field("deduced", description="Source of the answer.")),
            status=(Literal["answered", "inferred", "unanswered"], Field("answered", description="Status of the decision.")),
            reason=(str, Field("Contextual deduction based on established persona and memory.", description="Brief logical justification / deduction reason based on persona and session memory."))
        )
        
        schema_fields[field_id] = (sub_model, Field(..., description=f"Provide value for: {f.get('label', field_id)}"))
        
    return create_model("DynamicFormSchema", **schema_fields)

async def _get_form_answers_single_chunk(fields: List[Dict], session_memory: dict, validation_errors: List[str] = None, persona: str = None):
    if not fields:
        return {}
        
    from datetime import datetime
    now = datetime.now()
    current_date_str = now.strftime('%B %d, %Y')
    current_year = now.year

    active_persona = persona if persona else config.USER_PERSONA
    DynamicSchema = generate_dynamic_schema(fields)
    
    extra_mandate = ""
    if persona and any(k in persona.lower() for k in ["bowen", "nursing"]):
        extra_mandate = """
    STRICT OVERRIDE MANDATE:
    You MUST prioritize the user's specific instructions over any conflicting general defaults.
    - Ensure Age (25-59), Gender, Ward, Qualifications, Experience, and Rank strictly comply with the user's logic rules.
        """
    
    prompt = f"""
    You are an autonomous form-filling agent acting on behalf of this persona:
    {active_persona}
    
    {extra_mandate}
    
    TODAY'S DATE AND CURRENT YEAR:
    Today is {current_date_str} (Current Year: {current_year}).
    
    Session Memory (Previous Answers):
    {json.dumps(session_memory, indent=2)}
    
    Current Page Fields (HTML):
    {json.dumps(fields, indent=2)}
    
    COHERENT CONTEXTUAL DEDUCTION PRINCIPLE (CRITICAL):
    You are an autonomous form-filling agent running in the background. You must NEVER pause for human help or leave required fields blank.
    However, you MUST NOT use wild or arbitrary invention.
    Instead, you MUST use COHERENT CONTEXTUAL DEDUCTION:
    - When a field asks for information (like consumer habits, product preferences, brand familiarity, hobbies, or ratings) not explicitly written in the persona:
      1. Analyze the persona's established demographic profile (age, profession, income, household size, location, lifestyle) and previous answers in Session Memory.
      2. Deduce an answer that is 100% logically, demographically, and psychologically coherent with that profile.
      3. Maintain absolute consistency with related questions answered on earlier pages (Session Memory).
      4. Set source='deduced' and provide a brief 'reason' explaining how your answer aligns with the persona.
    
    DATE OF BIRTH & AGE STRICT CONSISTENCY MANDATE:
    If any question asks for a Date of Birth (DOB) or Birthdate (for yourself, a child, or family member):
    - Check Session Memory for their previously stated Age (e.g. if a child's age was answered as 7 years old).
    - You MUST calculate the birth year with 100% mathematical precision:
      Birth Year = {current_year} - Age (adjusted for the month/day relative to today's date).
    - For example, if today is in {current_year} and a child is 7 years old, their birthdate MUST be in {current_year - 7} (if their birthday has already passed this year) or {current_year - 8} (if their birthday is later this year).
    - NEVER output an inconsistent birth year (e.g. child age 7 cannot be born in 2018 when current year is {current_year})! Surveys cross-validate DOB against age and will disqualify you for discrepancies.

    DEDUCTION RULE: Act as a detective. Read the persona and instructions carefully. If the instructions say "make sure I qualify", 
    you MUST alter any default persona details (including your address, age, occupation) to match the requirements of the form or the provided instructions. 
    If the form or instructions require you to live in Rhode Island to qualify, you must invent a Rhode Island address, completely ignoring the default persona address.
    
    REAL RESIDENTIAL ADDRESS & ZIP CODE MANDATE:
    If any form asks for a Street Address, Residence, City, State, or ZIP Code:
    - You MUST use a REAL, authentic residential street address in the EXACT target city and state.
    - You MUST use the EXACT valid 5-digit US ZIP code (or target country postal code) matching that specific city and street. Never output fake zip codes like 12345 or 00000.
    
    CRITICAL REWARD / INCENTIVE MANDATE:
    If a form or final page asks for contact info (First Name, Last Name, City, State, Zip Code, Email) to receive a reward (like a Visa card, gift card, or payment), ALWAYS fill out every single contact field completely.
    - If a question asks whether you want to receive the reward/gift card (e.g. "I confirm that I have completed the survey and would like to receive the Amazon gift card"), you MUST ALWAYS select "Yes" or the affirmative option! NEVER select "No".
    - MANDATORY GIFT CARD SELECTION: If any question asks you to select your preferred Gift Card or Reward brand/type (e.g. Amazon vs. Walmart, Target, Visa, etc.): You MUST ALWAYS choose Amazon (or 'Amazon Gift Card'). NEVER choose Walmart, Target, or other retailers.
    - If a question asks whether you want to opt-in or participate in a Zoom meeting, interview, or follow-up study (e.g. "Would you like to opt in to a Zoom meeting to talk more about your experiences?"), you MUST ALWAYS select "Yes" or the affirmative option!
    - NEVER click or select options like 'Do not wish to participate', 'Decline reward', or 'Opt out'. Always claim the reward by leaving opt-out options unselected (set them to false).
    
    CRITICAL SCREENER RULE: If a question appears to be a qualifying screener for a focus group (e.g. asking if you have a condition, use a product, or assist someone), ALWAYS select the 'Yes' or qualifying answer. Never select an answer that disqualifies you.
    Deduce the best logical answers based on the context provided.
    
    CRITICAL RULE — ZERO 'OTHER' TOLERANCE:
    If any question has multiple options and one of the options is 'Other', 'Other:', 'Another', or 'Other (please specify)', you MUST NEVER select or answer 'Other'!
    ALWAYS neglect 'Other' completely!
    Select ONLY from the standard predefined categories (e.g. Administration, Plan design, Leadership, White, Male, etc.).
    Leave all 'Other' checkboxes as False and all 'Other' text fields completely empty ("").

    QUESTIONNAIRE, MATRIX & LIKERT SCALE INTELLIGENCE MANDATE:
    When answering survey evaluation grids, matrix statements, rating scales, or Likert questions (e.g., Strongly Agree -> Strongly Disagree, Very Satisfied -> Very Dissatisfied):
    1. READ AND INTERPRET EVERY SINGLE STATEMENT:
       - You MUST carefully read each statement and evaluate its exact semantic meaning.
       - NEVER blindly pick 'Agree' or 'Strongly Agree' across every row! Straight-lining is an instant red flag that triggers anti-bot traps and disqualifies survey submissions.
    2. REVERSE-CODED & INVERTED POLARITY STATEMENTS (CRITICAL):
       - Survey creators intentionally place reverse-worded, negative, or counter-statements to test respondent attention and catch bots.
       - You MUST detect statement polarity relative to your established stance in Session Memory:
         * Positive statement: If you established a positive attitude toward a topic/service/product and the statement is positive (e.g., "The platform is easy to use", "I enjoy spending time with pets"), select 'Agree' or 'Strongly Agree'.
         * Inverted/Negative statement: If you like or support something, and a statement is framed negatively (e.g., "I do not like pets", "The platform is confusing and frustrating", "I am dissatisfied with the performance"), you MUST select 'Disagree' or 'Strongly Disagree' (or 'No')!
         * Never answer 'Agree' to a statement that contradicts what you just agreed to previously!
    3. CROSS-QUESTION LOGICAL COHERENCE:
       - Constantly check 'Session Memory' to ensure your opinions, ratings, and choices remain 100% logically consistent from page to page.
    4. NATURAL SENTIMENT VARIATION (NO STRAIGHT-LINING):
       - Human respondents naturally have nuanced opinions. Across a multi-statement grid, provide a realistic, authentic distribution (a blend of 'Agree', 'Strongly Agree', 'Somewhat Agree', and occasional 'Neither / Neutral' or 'Disagree' where appropriate to the statement), rather than clicking the exact same column down the entire page.
    5. ATTENTION CHECKS & HONESTY TRAPS:
       - If a statement contains an explicit trap instruction (e.g., "To ensure you are paying attention, please select 'Somewhat Disagree'", "Select option 2 for this item"), you MUST follow the trap instruction exactly.
    """
    
    schema_json = json.dumps(DynamicSchema.model_json_schema(), indent=2)
    prompt += f"\n\nCRITICAL: You MUST return ONLY a raw JSON object that exactly matches this schema:\n{schema_json}\nDo not include any conversational text or markdown code blocks (no ```json). ONLY return the raw JSON braces."
    
    if validation_errors:
        prompt += f"\n\nWARNING: The last submission failed with these errors. Fix your answers:\n{json.dumps(validation_errors)}"
        
    raw_response = ""
    for attempt in range(2):
        print(f"🧠 Querying AI Engine (poolot-standard on Lily, batch of {len(fields)} fields, attempt {attempt + 1})...")
        try:
            response = await client.chat.completions.create(
                model="poolot-standard",
                messages=[
                    {"role": "system", "content": "You are an expert form filler. Map answers meticulously. Output ONLY valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3
            )
            raw_content = response.choices[0].message.content if (response and response.choices and len(response.choices) > 0) else None
            if raw_content and raw_content.strip():
                raw_response = raw_content.strip()
                break
        except Exception as e:
            print(f"⚠️ AI call error on attempt {attempt + 1}: {e}")
            await asyncio.sleep(1)
            
    if not raw_response:
        print("⚠️ AI Engine returned empty response content after retries. Returning fallback empty dictionary.")
        return {}
        
    if raw_response.startswith("```json"):
        raw_response = raw_response[7:-3].strip()
    elif raw_response.startswith("```"):
        raw_response = raw_response[3:-3].strip()
        
    try:
        json_data = json.loads(raw_response)
        # Normalize if the LLM outputted flat key-value pairs instead of { "value": ..., "confidence": ... }
        if isinstance(json_data, dict):
            for f in fields:
                fid = f.get("id")
                if fid in json_data:
                    val = json_data[fid]
                    if not isinstance(val, dict):
                        # Convert flat value to the expected sub-model dict structure
                        json_data[fid] = {
                            "value": val,
                            "confidence": 1.0,
                            "source": "inferred"
                        }
                    elif "value" not in val:
                        json_data[fid] = {
                            "value": str(val),
                            "confidence": 1.0,
                            "source": "inferred"
                        }
        # Validate against the Pydantic schema
        answers = DynamicSchema(**json_data).model_dump()
        for fid, a in answers.items():
            r = a.get("reason")
            val = a.get("value")
            if r and r != "Contextual deduction based on established persona and memory.":
                print(f"💡 Coherent Deduction [{fid}]: '{val}' (Reason: {r})")
    except Exception as e:
        print(f"⚠️ Pydantic schema validation note: {e}\nRaw Response: {raw_response}")
        # Robust fallback: extract field values directly from json_data so we never fail form filling
        answers = {}
        try:
            if isinstance(json_data, dict):
                for f in fields:
                    fid = f.get("id")
                    if fid in json_data:
                        v = json_data[fid]
                        val_actual = v.get("value") if isinstance(v, dict) else v
                        answers[fid] = {
                            "value": val_actual,
                            "confidence": 1.0,
                            "source": "inferred"
                        }
        except Exception:
            pass
    
    # --- Fuzzy Match Post-Processing ---
    # For massive dropdowns where we requested a raw string, find the closest actual DOM option
    for f in fields:
        field_id = f.get("id")
        options = f.get("options", [])
        if len(options) > 20 and field_id in answers:
            raw_val = answers[field_id]["value"]
            if isinstance(raw_val, str):
                best_match = process.extractOne(raw_val, options, scorer=fuzz.WRatio)
                if best_match and best_match[1] > 60:
                    print(f"🔍 Fuzzy matched '{raw_val}' to actual option '{best_match[0]}' (Score: {best_match[1]})")
                    answers[field_id]["value"] = best_match[0]
                
    return answers

async def get_form_answers(fields: List[Dict], session_memory: dict, validation_errors: List[str] = None, persona: str = None):
    """
    Main entry point for AI form filling.
    If fields count is large (> 20), automatically chunks fields into batches of 20
    so LLM context/output token limits are never exceeded and empty responses are avoided.
    """
    if not fields:
        return {}
        
    CHUNK_SIZE = 20
    if len(fields) > CHUNK_SIZE:
        print(f"📦 Large form detected ({len(fields)} fields). Processing in chunks of {CHUNK_SIZE} for 100% reliability...")
        all_answers = {}
        combined_memory = dict(session_memory)
        for i in range(0, len(fields), CHUNK_SIZE):
            if i > 0:
                await asyncio.sleep(1.2)
            chunk = fields[i:i + CHUNK_SIZE]
            chunk_ans = await _get_form_answers_single_chunk(chunk, combined_memory, validation_errors, persona)
            all_answers.update(chunk_ans)
            for cid, cval in chunk_ans.items():
                matching_f = next((f for f in chunk if f.get("id") == cid), None)
                if matching_f and "value" in cval:
                    combined_memory[matching_f.get("label", cid)] = cval["value"]
        return all_answers
    else:
        return await _get_form_answers_single_chunk(fields, session_memory, validation_errors, persona)


def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

async def get_vision_form_answers(image_path: str, num_boxes: int, session_memory: dict):
    """
    Sends the screenshot with numbered bounding boxes to the Vision AI.
    """
    print("👁️ Querying Vision AI Engine (Requires a Vision-capable model)...")
    base64_image = encode_image(image_path)
    
    class BoxAction(BaseModel):
        box_id: int = Field(..., description="The ID number of the red bounding box on the image.")
        value: str = Field(..., description="The value to type or click for this box. For checkboxes/radios, return 'CLICK'.")
        confidence: float
        source: Literal["persona", "inferred", "gap"]
        
    class VisionResponse(BaseModel):
        actions: List[BoxAction]
        
    prompt = f"""
    You are an autonomous form-filling agent acting on behalf of this persona:
    {config.USER_PERSONA}
    
    Session Memory: {json.dumps(session_memory, indent=2)}
    
    Look at the provided screenshot. There are {num_boxes} red bounding boxes with numbers.
    For each numbered box that corresponds to a form input, provide the value that should be filled.
    If the box is a radio button, checkbox, or button to submit, set the value to 'CLICK'.
    Ignore boxes that are just text or labels and do not require interaction.
    
    CRITICAL INSTRUCTION: You are running fully autonomously. If a field requires info not in the persona, 
    you MUST invent a highly realistic answer and set source='inferred'. Never use 'gap'.
    """
    
    try:
        response = await client.beta.chat.completions.parse(
            model="poolot-vision", # Poolot Vision model name
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            response_format=VisionResponse,
        )
        return response.choices[0].message.parsed.model_dump()
    except Exception as e:
        print(f"❌ Vision API failed. Make sure your API key supports a Vision model like gpt-4o: {e}")
        return {"actions": []}

async def extract_instructions_from_image(image_path: str) -> str:
    """Uses Vision LLM to extract instructions, rules, or guides from an uploaded image."""
    print("👁️ Analyzing uploaded image for instructions...")
    
    try:
        base64_image = encode_image(image_path)
        
        system_prompt = (
            "You are an AI assistant. The user has uploaded an image (such as a screenshot of rules, "
            "a guide, a rubric, or instructions) to guide an autonomous form-filling bot. "
            "Your task is to extract all the rules, guidelines, facts, or instructions from this image. "
            "Do NOT treat this as a persona unless it explicitly represents one. "
            "Write out the explicit instructions, rules, or context found in the image clearly so "
            "the bot can use it to intelligently deduce the best answers for a questionnaire."
        )
        
        response = await client.chat.completions.create(
            model="poolot-vision",
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract all instructions, context, or guides from this image."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                    ]
                }
            ],
            temperature=0.2,
            max_tokens=1024,
        )
        
        extracted_text = response.choices[0].message.content.strip()
        print(f"✅ Vision extracted instructions:\n{extracted_text}")
        return extracted_text
        
    except Exception as e:
        print(f"❌ Vision Instruction Extraction Error: {e}")
        return ""
