import json
import base64
from typing import Any, List, Literal, Dict
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
                annotation = str
            else:
                # Strict constraints for smaller lists
                options_tuple = tuple(options)
                if field_type == "checkbox":
                    annotation = List[Literal[options_tuple]] # type: ignore
                else:
                    annotation = Literal[options_tuple] # type: ignore
        else:
            annotation = str
            
        # Create a metadata-rich sub-model for this specific field
        sub_model_name = f"FieldAnswer_{field_id}"
        sub_model = create_model(
            sub_model_name,
            value=(annotation, ...),
            confidence=(float, Field(..., ge=0.0, le=1.0, description="Confidence score from 0.0 to 1.0")),
            source=(Literal["persona", "inferred", "gap"], Field(..., description="Source of the answer. Use 'gap' if explicitly not covered."))
        )
        
        schema_fields[field_id] = (sub_model, Field(..., description=f"Provide value for: {f.get('label', field_id)}"))
        
    return create_model("DynamicFormSchema", **schema_fields)

async def get_form_answers(fields: List[Dict], session_memory: dict, validation_errors: List[str] = None, persona: str = None):
    """
    Calls the LLM with the dynamic schema to map persona to form fields.
    """
    if not fields:
        return {}
        
    active_persona = persona if persona else config.USER_PERSONA
    DynamicSchema = generate_dynamic_schema(fields)
    
    prompt = f"""
    You are an autonomous form-filling agent acting on behalf of this persona:
    {active_persona}
    
    Session Memory (Previous Answers):
    {json.dumps(session_memory, indent=2)}
    
    Current Page Fields (HTML):
    {json.dumps(fields, indent=2)}
    
    CRITICAL INSTRUCTION: You are running fully autonomously in the background. You MUST NOT pause for human help.
    If a field asks for information (like age, email, company, etc.) that is NOT provided in the persona or memory,
    you MUST invent a highly realistic, contextually appropriate answer and set source='inferred'.
    Never use 'gap'. Always ensure every field has a valid, realistic answer.
    
    DEDUCTION RULE: Act as a detective. Read the persona and instructions carefully. If the instructions say "make sure I qualify", 
    you MUST alter any default persona details (including your address, age, occupation) to match the requirements of the form or the provided instructions. 
    If the form or instructions require you to live in Rhode Island to qualify, you must invent a Rhode Island address, completely ignoring the default persona address.
    
    REAL RESIDENTIAL ADDRESS & ZIP CODE MANDATE:
    If any form asks for a Street Address, Residence, City, State, or ZIP Code:
    - You MUST use a REAL, authentic US residential street address (e.g. realistic street name and number like '1428 Elm St', '350 S 400 E', '742 Evergreen Terr') in the EXACT target city and state.
    - You MUST use the EXACT valid 5-digit US ZIP code matching that specific city and street. Never output fake zip codes like 12345 or 00000.
    - Example for Salt Lake County, UT: '384 S 400 E, Salt Lake City, UT 84111' (ZIP: 84111)
    - Example for Dallas, TX: '4210 Live Oak St, Dallas, TX 75204' (ZIP: 75204)
    - Example for Burbank, CA: '1210 N Hollywood Way, Burbank, CA 91505' (ZIP: 91505)
    
    CRITICAL REWARD / INCENTIVE MANDATE:
    If a form or final page asks for contact info (First Name, Last Name, City, State, Zip Code, Email) to receive a reward (like a Visa card, gift card, or payment), ALWAYS fill out every single contact field completely.
    NEVER click or select options like 'Do not wish to participate', 'Decline reward', or 'Opt out'. Always claim the reward by leaving opt-out options unselected (set them to false).
    
    CRITICAL SCREENER RULE: If a question appears to be a qualifying screener for a focus group (e.g. asking if you have a condition, use a product, or assist someone), ALWAYS select the 'Yes' or qualifying answer. Never select an answer that disqualifies you.
    Deduce the best logical answers based on the context provided.
    """
    
    schema_json = json.dumps(DynamicSchema.model_json_schema(), indent=2)
    prompt += f"\n\nCRITICAL: You MUST return ONLY a raw JSON object that exactly matches this schema:\n{schema_json}\nDo not include any conversational text or markdown code blocks (no ```json). ONLY return the raw JSON braces."
    
    if validation_errors:
        prompt += f"\n\nWARNING: The last submission failed with these errors. Fix your answers:\n{json.dumps(validation_errors)}"
        
    print("🧠 Querying AI Engine (poolot-standard on Lily)...")
    response = await client.chat.completions.create(
        model="poolot-standard",
        messages=[
            {"role": "system", "content": "You are an expert form filler. Map answers meticulously. Output ONLY valid JSON."},
            {"role": "user", "content": prompt}
        ],
    )
    
    raw_response = response.choices[0].message.content.strip()
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
