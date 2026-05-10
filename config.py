import os
from dotenv import load_dotenv

load_dotenv()

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
MAX_HISTORY_ROUNDS = int(os.getenv("MAX_HISTORY_ROUNDS", "20"))
