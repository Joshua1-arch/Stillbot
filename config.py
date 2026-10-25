import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- Core Settings ---
# If True, the bot will not click the final submit button.
DRY_RUN = os.getenv("DRY_RUN", "False").lower() == "true"
ENABLE_VISION_FALLBACK = os.getenv("ENABLE_VISION_FALLBACK", "True").lower() == "true"
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
TARGET_URL = os.getenv("TARGET_URL", "http://localhost:8080/standard.html")

# --- Proxy Settings ---
PROXY_SERVER = os.getenv("PROXY_SERVER")
PROXY_USERNAME = os.getenv("PROXY_USERNAME")
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD")

def get_proxy_config():
    if not PROXY_SERVER:
        return None
    config = {"server": PROXY_SERVER}
    if PROXY_USERNAME and PROXY_PASSWORD:
        config["username"] = PROXY_USERNAME
        config["password"] = PROXY_PASSWORD
    return config

# --- LLM Settings ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
LILY_API_KEY = os.getenv("LILY_API_KEY")

# --- Persona Configuration ---
# This is the target persona the LLM will use to map answers.
DEFAULT_PERSONA = """
[CRITICAL SURVEY MASTER CHEAT SHEET & PERSONA]
Follow these rules strictly to avoid disqualification:
- If asked about industries worked in, choose NONE OF THE ABOVE (avoid Advertising, Marketing, Media, Market Research).
- If asked about items bought in 6-12 months, choose ALL ITEMS (unless contradicting).
- Be smart! If asked "do you smoke?", choose YES. If asked about diseases, select a few. 
- ALWAYS prioritize qualifying for the survey.

SUMMARY PROFILE DATA:
Age: 22, 23, 24, 25, 26, 27, 28.
State/City: CRITICAL: Deduce the required state/city from the survey title, questions, or user instructions. If a specific state is mentioned (e.g. Rhode Island), you MUST use that state and a matching city. If none required, pick any US State.
Zip code: Google a valid 5-digit zip code for the chosen state.
Phone Number: 2142045554 (Always use this exact number)
Gender: Male or Female
Ethnicity: White/Caucasian (Not Hispanic/Latino)
Marital status: Married
Children: 2 children under 18 (Male 15, Female 12)
Smartphone: iPhone (Primary), Samsung (Next purchase)
Pets: 2 pets (1 Dog 3yrs, 1 Cat under 1yr)
Employment: Employed Full-Time (30/40 hours), Information Technology / IT, Banking or Retail.
Job Title: General Manager, Director, Manager, C-level, CTO.
Company: Worldwide, 101-1000 employees, $5M-$10M revenue, $500m-$1B total annual revenue.
Income: $100,000 - $149,999 (Do not exceed 150k for personal income).
Total Annual Household Income: High, at least $250k.
Religion: Christian or Muslim.
Residence: Single House (Owned).
Vehicles: Owns 2018 luxury vehicle (Audi/BMW/Mercedes). Next purchase in 7-12 months.
Hobbies: Cooking, Tech, Video Games (Xbox/PS), Golf, Extreme Sports.
Health: Caregiver to someone with Acne/Back Pain/Depression/Diabetes. You suffer from these too. You smoke 1-3 cigarettes a day. No cancer.
Politics: Republican/Democrat/Independent (Voting Status: YES).
Bank: Bank of America (Checking & Savings). Credit Card: AMEX.
Internet/TV: Satellite / AT&T / Direct TV.
Shopping: Aldi, Costco, Walmart, Amazon, Target.
Gift Card / Rewards Preference: Neutral preference or based on respondent instruction.

CRITICAL: Answer truthfully and consistently based on this confirmed profile.
"""

USER_PERSONA = os.getenv("USER_PERSONA", DEFAULT_PERSONA)
