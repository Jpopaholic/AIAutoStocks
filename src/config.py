# Path: src/config.py
import os
import json
from dataclasses import dataclass
from pathlib import Path
from typing import List
from dotenv import load_dotenv

# 載入專案根目錄的 .env 檔案
ENV_PATH = Path(__file__).resolve().parent.parent / '.env'
load_dotenv(dotenv_path=ENV_PATH)

# 載入外部配置參數。優先從 Fly.io 的環境變數祕密 (CONFIG_JSON) 載入，次之從本機 config.json 檔案載入
json_config = {}
env_config_json = os.getenv("CONFIG_JSON")

if env_config_json:
    try:
        json_config = json.loads(env_config_json)
        print(" [配置管理器] 已成功自環境變數 CONFIG_JSON (Fly.io Secret) 載入外部配置參數。")
    except Exception as e:
        print(f" [配置管理器] 警告: 解析環境變數 CONFIG_JSON 失敗: {e}")
else:
    CONFIG_JSON_PATH = Path(os.getcwd()) / 'config.json'
    if CONFIG_JSON_PATH.exists():
        try:
            with open(CONFIG_JSON_PATH, 'r', encoding='utf-8') as f:
                json_config = json.load(f)
                print(" [配置管理器] 已成功自 config.json 本機檔案載入外部配置參數。")
        except Exception as e:
            print(f" [配置管理器] 警告: 讀取 config.json 失敗: {e}")

def get_config_val(key: str, default: str = None) -> str | None:
    """
    獲取配置值。優先自載入的 json_config 讀取，其次從環境變數讀取。
    """
    val = json_config.get(key)
    if val is not None:
        return str(val)
    return os.getenv(key, default)

def _validate_config():
    """
    執行嚴格的環境變數與外部檔案配置驗證，若缺少關鍵配置則主動拋出例外中斷執行。
    """
    errors = []

    # 1. 驗證 Supabase 配置
    supabase_url = get_config_val("SUPABASE_URL")
    if not supabase_url:
        errors.append("缺少配置: SUPABASE_URL")
    else:
        if not (supabase_url.startswith("http://") or supabase_url.startswith("https://")):
            errors.append("SUPABASE_URL 格式不正確，必須為有效的 URL")

    if not get_config_val("SUPABASE_KEY"):
        errors.append("缺少配置: SUPABASE_KEY (Supabase API 金鑰/Service Role Key)")

    # 2. 驗證 Gemini API 金鑰配置
    raw_gemini_keys = json_config.get("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEYS")
    if not raw_gemini_keys:
        errors.append("缺少配置: GEMINI_API_KEYS (Gemini API 金鑰，多組金鑰可用逗號分隔)")

    # 3. 驗證 安全憑證與金鑰管理器 相關變數
    if not get_config_val("MASTER_KEY"):
        errors.append("缺少配置: MASTER_KEY (用於解密安全憑證與金鑰管理器的解密主金鑰)")

    # 4. 驗證 Gmail 傳輸配置 (不需架設外部 SMTP 或 Resend，直接利用 Gmail)
    gmail_user = get_config_val("GMAIL_USER")
    gmail_app_pass = get_config_val("GMAIL_APP_PASSWORD")
    if not gmail_user:
        errors.append("缺少配置: GMAIL_USER (您的 Gmail 帳號/寄件人地址)")
    if not gmail_app_pass:
        errors.append("缺少配置: GMAIL_APP_PASSWORD (您的 Gmail 應用程式安全密碼)")

    # 若有錯誤則主動拋錯中斷執行
    if errors:
        import sys
        print(" [配置錯誤] 系統啟動失敗，缺少或配置了無效的參數：", file=sys.stderr, flush=True)
        for err in errors:
            print(f"  - {err}", file=sys.stderr, flush=True)
        raise ValueError("參數驗證失敗:\n" + "\n".join(errors))

# 執行嚴格的啟動驗證
_validate_config()

# 解析 Gemini API 金鑰列表
_raw_gemini_keys = json_config.get("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEYS")
if isinstance(_raw_gemini_keys, list):
    gemini_api_keys = [str(k).strip() for k in _raw_gemini_keys if k]
elif isinstance(_raw_gemini_keys, str):
    gemini_api_keys = [k.strip() for k in _raw_gemini_keys.split(",") if k.strip()]
else:
    gemini_api_keys = []

@dataclass(frozen=True)
class SupabaseConfig:
    url: str
    key: str

@dataclass(frozen=True)
class CredentialsConfig:
    master_key: str
    file_path: str

@dataclass(frozen=True)
class GmailConfig:
    user: str
    app_password: str
    to_addr: str

@dataclass(frozen=True)
class LimitsConfig:
    single_stock: float
    daily_total: float
    is_paper_trading: bool

@dataclass(frozen=True)
class AppConfig:
    env: str
    port: int
    timezone: str
    supabase: SupabaseConfig
    credentials: CredentialsConfig
    gmail: GmailConfig
    limits: LimitsConfig
    gemini_api_keys: List[str]

# 載入數值型參數並設定安全的預設值 (交易限額防呆機制)
_env = get_config_val("NODE_ENV") or get_config_val("PYTHON_ENV") or "development"
try:
    _port = int(get_config_val("PORT") or "3000")
except ValueError:
    _port = 3000

_timezone = get_config_val("TAIWAN_STOCK_TIMEZONE") or "Asia/Taipei"

try:
    _limit_single = float(get_config_val("TRADING_LIMIT_SINGLE_STOCK") or "50000.0")
except ValueError:
    _limit_single = 50000.0

try:
    _limit_daily = float(get_config_val("TRADING_LIMIT_DAILY_TOTAL") or "150000.0")
except ValueError:
    _limit_daily = 150000.0

_is_paper = (get_config_val("PAPER_TRADING_MODE") or "true").lower() != "false"

# 建立密鑰解密路徑
_credentials_file = get_config_val("CREDENTIALS_FILE_PATH") or str(Path(os.getcwd()) / "credentials.enc")

# 解析 Gmail 配置
_gmail_user = get_config_val("GMAIL_USER") or ""
_gmail_app_pass = get_config_val("GMAIL_APP_PASSWORD") or ""
_gmail_to = get_config_val("EMAIL_TO") or _gmail_user  # 若未特別設定接收信箱，預設寄給自己

# 導出防凍結的唯讀配置物件
config = AppConfig(
    env=_env,
    port=_port,
    timezone=_timezone,
    supabase=SupabaseConfig(
        url=get_config_val("SUPABASE_URL") or "",
        key=get_config_val("SUPABASE_KEY") or ""
    ),
    credentials=CredentialsConfig(
        master_key=get_config_val("MASTER_KEY") or "",
        file_path=_credentials_file
    ),
    gmail=GmailConfig(
        user=_gmail_user,
        app_password=_gmail_app_pass,
        to_addr=_gmail_to
    ),
    limits=LimitsConfig(
        single_stock=_limit_single,
        daily_total=_limit_daily,
        is_paper_trading=_is_paper
    ),
    gemini_api_keys=gemini_api_keys
)
