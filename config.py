import os
from dotenv import load_dotenv

load_dotenv()

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
YYF_CODEX_CONFIG = os.getenv("YYF_CODEX_CONFIG", "~/.yyf-codex/config.yaml")
YYF_CODEX_STATE = os.getenv("YYF_CODEX_STATE", "~/.yyf-codex/state.sqlite")
