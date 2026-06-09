# Path: src/config.py
import os
import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

STOCK_PRESETS = {
    "top5": ["2330", "2317", "2454", "2308", "2881"],  # 市值前五大 (台積電、鴻海、聯發科、台達電、富邦金)
    "semiconductor": ["2330", "2454", "2303", "3711", "2379"],  # 半導體概念 (台積電、聯發科、聯電、日月光、瑞昱)
    "finance": ["2881", "2882", "2891", "2886", "2884"],  # 穩健金融股 (富邦金、國泰金、中信金、兆豐金、玉山金)
    "apple": ["2317", "2330", "2382", "3231", "2357"],  # 蘋果/AI代工供應鏈 (鴻海、台積電、廣達、緯創、華碩)
    "highdividend": ["3034", "2301", "2356", "2449", "3045"],  # 常見高股息 (聯詠、光寶科、英業達、京元電、台灣大)
    "penny": ["2303", "2618", "2324", "2883", "3481", "2353"],  # 熱門銅板股/低價股 (聯電、長榮航、仁寶、開發金、群創、宏碁)
    "small": ["2303", "2618", "2324", "2883", "3481", "2353"]   # 中低價位/小資首選股 (與 penny 相同)
}

STOCK_PRESETS_INFO = {
    "top5": {"name": "市值前五大", "desc": "台積電、鴻海、聯發科、台達電、富邦金"},
    "semiconductor": {"name": "半導體概念", "desc": "台積電、聯發科、聯電、日月光、瑞昱"},
    "finance": {"name": "金融特選股", "desc": "富邦金、國泰金、中信金、兆豐金、玉山金"},
    "apple": {"name": "蘋果/AI鏈", "desc": "鴻海、台積電、廣達、緯創、華碩"},
    "highdividend": {"name": "高股息概念", "desc": "聯詠、光寶科、英業達、京元電、台灣大"},
    "penny": {"name": "銅板題材股", "desc": "聯電、長榮航、仁寶、開發金、群創、宏碁"},
    "small": {"name": "小資首選股", "desc": "中低價位/小資首選股"}
}


def resolve_stock_codes(stocks_arg: str) -> List[str]:
    """
    將使用者傳入的股票代號字串解析為獨立的 4 碼股票代號列表，支援 preset 套餐名稱。
    """
    preset_key = stocks_arg.strip().lower()
    if preset_key in STOCK_PRESETS:
        return STOCK_PRESETS[preset_key]
        
    codes = []
    for s in stocks_arg.split(","):
        s = s.strip()
        if not s:
            continue
        if s.lower() in STOCK_PRESETS:
            codes.extend(STOCK_PRESETS[s.lower()])
        else:
            codes.append(s)
            
    seen = set()
    return [c for c in codes if not (c in seen or seen.add(c))]


# 載入專案根目錄的 .env 檔案
ENV_PATH = Path(__file__).resolve().parent.parent / '.env'
load_dotenv(dotenv_path=ENV_PATH)

# 載入外部配置參數。優先從 Fly.io 的環境變數祕密 (CONFIG_JSON) 載入，次之從本機 config.json 檔案載入
json_config = {}
env_config_json = os.getenv("CONFIG_JSON")
_last_config_load_time = 0.0

def _merge_decrypted_credentials(cfg: dict):
    try:
        from src.services.credential_manager import load_credentials
        decrypted_creds = load_credentials()
        
        # 1. 整合 Supabase URL & Key
        if "supabase" in decrypted_creds:
            s_creds = decrypted_creds["supabase"]
            if s_creds.get("url"):
                cfg["SUPABASE_URL"] = s_creds["url"]
            if s_creds.get("key"):
                cfg["SUPABASE_KEY"] = s_creds["key"]
                
        # 2. 整合 Gmail 設定
        if "gmail" in decrypted_creds:
            g_creds = decrypted_creds["gmail"]
            if g_creds.get("user"):
                cfg["GMAIL_USER"] = g_creds["user"]
            if g_creds.get("appPassword"):
                cfg["GMAIL_APP_PASSWORD"] = g_creds["appPassword"]
            if g_creds.get("to"):
                cfg["EMAIL_TO"] = g_creds["to"]
                
        # 3. 整合 Gemini 金鑰列表
        if "geminiApiKeys" in decrypted_creds and decrypted_creds["geminiApiKeys"]:
            cfg["GEMINI_API_KEYS"] = ",".join(decrypted_creds["geminiApiKeys"])
    except Exception:
        pass

def _reload_config_json_if_needed():
    global json_config, _last_config_load_time
    if os.getenv("CONFIG_JSON"):
        return
    CONFIG_JSON_PATH = Path(os.getcwd()) / 'config.json'
    if CONFIG_JSON_PATH.exists():
        try:
            mtime = CONFIG_JSON_PATH.stat().st_mtime
            if mtime > _last_config_load_time:
                with open(CONFIG_JSON_PATH, 'r', encoding='utf-8') as f:
                    json_config = json.load(f)
                    _last_config_load_time = mtime
                    print(" [配置管理器] 已成功自 config.json 動態重新載入外部配置參數。")
                    try:
                        from src.services.credential_manager import clear_cache
                        clear_cache()
                    except Exception:
                        pass
                    _merge_decrypted_credentials(json_config)
        except Exception as e:
            print(f" [配置管理器] 警告: 動態載入 config.json 失敗: {e}")

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
                _last_config_load_time = CONFIG_JSON_PATH.stat().st_mtime
                print(" [配置管理器] 已成功自 config.json 本機檔案載入外部配置參數。")
        except Exception as e:
            print(f" [配置管理器] 警告: 讀取 config.json 失敗: {e}")

_merge_decrypted_credentials(json_config)


def get_config_val(key: str, default: str = None) -> Optional[str]:
    """
    獲取配置值。優先自載入的 json_config 讀取，其次從環境變數讀取。
    """
    _reload_config_json_if_needed()
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

import time

_db_config_cache = None
_db_config_cache_time = 0.0
CACHE_TTL = 5.0  # cache for 5 seconds

def _get_db_config_cached() -> dict:
    global _db_config_cache, _db_config_cache_time
    now = time.time()
    if _db_config_cache is not None and (now - _db_config_cache_time) < CACHE_TTL:
        return _db_config_cache
    try:
        from src.services.supabase_client import get_db_config
        _db_config_cache = get_db_config()
        _db_config_cache_time = now
        return _db_config_cache
    except Exception:
        # cache empty dict on failure to avoid spamming database
        _db_config_cache = {}
        _db_config_cache_time = now
        return _db_config_cache

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

class LimitsConfig:
    def __init__(self, single_stock: float, daily_total: float, is_paper_trading: bool,
                 single_stock_pct: Optional[float], daily_total_pct: Optional[float], initial_cash: float):
        self._single_stock = single_stock
        self._daily_total = daily_total
        self._is_paper_trading = is_paper_trading
        self._single_stock_pct = single_stock_pct
        self._daily_total_pct = daily_total_pct
        self._initial_cash = initial_cash

    @property
    def single_stock(self) -> float:
        db_cfg = _get_db_config_cached()
        if "TRADING_LIMIT_SINGLE_STOCK" in db_cfg:
            try:
                return float(db_cfg["TRADING_LIMIT_SINGLE_STOCK"])
            except ValueError:
                pass
        val = get_config_val("TRADING_LIMIT_SINGLE_STOCK")
        if val is not None:
            try:
                return float(val)
            except ValueError:
                pass
        return self._single_stock

    @property
    def daily_total(self) -> float:
        db_cfg = _get_db_config_cached()
        if "TRADING_LIMIT_DAILY_TOTAL" in db_cfg:
            try:
                return float(db_cfg["TRADING_LIMIT_DAILY_TOTAL"])
            except ValueError:
                pass
        val = get_config_val("TRADING_LIMIT_DAILY_TOTAL")
        if val is not None:
            try:
                return float(val)
            except ValueError:
                pass
        return self._daily_total

    @property
    def is_paper_trading(self) -> bool:
        db_cfg = _get_db_config_cached()
        if "PAPER_TRADING_MODE" in db_cfg:
            return db_cfg["PAPER_TRADING_MODE"].lower() != "false"
        val = get_config_val("PAPER_TRADING_MODE")
        if val is not None:
            return val.lower() != "false"
        return self._is_paper_trading

    @property
    def single_stock_pct(self) -> Optional[float]:
        db_cfg = _get_db_config_cached()
        if "TRADING_LIMIT_SINGLE_STOCK_PCT" in db_cfg:
            try:
                val = db_cfg["TRADING_LIMIT_SINGLE_STOCK_PCT"]
                return float(val) if val is not None and str(val).strip() != "" else None
            except ValueError:
                pass
        val = get_config_val("TRADING_LIMIT_SINGLE_STOCK_PCT")
        return _parse_float_opt(val) if val is not None else self._single_stock_pct

    @property
    def daily_total_pct(self) -> Optional[float]:
        db_cfg = _get_db_config_cached()
        if "TRADING_LIMIT_DAILY_TOTAL_PCT" in db_cfg:
            try:
                val = db_cfg["TRADING_LIMIT_DAILY_TOTAL_PCT"]
                return float(val) if val is not None and str(val).strip() != "" else None
            except ValueError:
                pass
        val = get_config_val("TRADING_LIMIT_DAILY_TOTAL_PCT")
        return _parse_float_opt(val) if val is not None else self._daily_total_pct

    @property
    def initial_cash(self) -> float:
        db_cfg = _get_db_config_cached()
        if "INITIAL_CASH" in db_cfg:
            try:
                return float(db_cfg["INITIAL_CASH"])
            except ValueError:
                pass
        val = get_config_val("INITIAL_CASH")
        if val is not None:
            try:
                return float(val)
            except ValueError:
                pass
        return self._initial_cash

class AppConfig:
    def __init__(self, env: str, port: int, timezone: str, supabase: SupabaseConfig,
                 credentials: CredentialsConfig, gmail: GmailConfig, limits: LimitsConfig,
                 gemini_api_keys: List[str], gemini_model: str):
        self._env = env
        self._port = port
        self._timezone = timezone
        self.supabase = supabase
        self.credentials = credentials
        self.gmail = gmail
        self.limits = limits
        self.gemini_api_keys = gemini_api_keys
        self._gemini_model = gemini_model

    @property
    def env(self) -> str:
        return self._env

    @property
    def port(self) -> int:
        return self._port

    @property
    def timezone(self) -> str:
        db_cfg = _get_db_config_cached()
        if "TAIWAN_STOCK_TIMEZONE" in db_cfg:
            return db_cfg["TAIWAN_STOCK_TIMEZONE"]
        val = get_config_val("TAIWAN_STOCK_TIMEZONE")
        return val if val is not None else self._timezone

    @property
    def gemini_model(self) -> str:
        db_cfg = _get_db_config_cached()
        if "GEMINI_MODEL" in db_cfg:
            return db_cfg["GEMINI_MODEL"]
        val = get_config_val("GEMINI_MODEL")
        return val if val is not None else self._gemini_model

    @property
    def sandbox_start_date(self) -> str:
        db_cfg = _get_db_config_cached()
        if "SANDBOX_START_DATE" in db_cfg:
            return db_cfg["SANDBOX_START_DATE"]
        val = get_config_val("SANDBOX_START_DATE")
        return val if val is not None else "2026-05-01"

    @property
    def sandbox_end_date(self) -> str:
        db_cfg = _get_db_config_cached()
        if "SANDBOX_END_DATE" in db_cfg:
            return db_cfg["SANDBOX_END_DATE"]
        val = get_config_val("SANDBOX_END_DATE")
        return val if val is not None else "2026-06-08"

    @property
    def is_auto_trading_active(self) -> bool:
        db_cfg = _get_db_config_cached()
        if "AUTO_TRADING_ACTIVE" in db_cfg:
            return db_cfg["AUTO_TRADING_ACTIVE"].lower() != "false"
        val = get_config_val("AUTO_TRADING_ACTIVE")
        if val is not None:
            return val.lower() != "false"
        return True

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

def _parse_float_opt(val: Optional[str]) -> Optional[float]:
    if val is None or val.strip() == "":
        return None
    try:
        return float(val)
    except ValueError:
        return None

_limit_single_pct = _parse_float_opt(get_config_val("TRADING_LIMIT_SINGLE_STOCK_PCT"))
_limit_daily_pct = _parse_float_opt(get_config_val("TRADING_LIMIT_DAILY_TOTAL_PCT"))

try:
    _initial_cash = float(get_config_val("INITIAL_CASH") or "1000000.0")
except ValueError:
    _initial_cash = 1000000.0

# 建立密鑰解密路徑
_credentials_file = get_config_val("CREDENTIALS_FILE_PATH") or str(Path(os.getcwd()) / "credentials.enc")

# 解析 Gmail 配置
_gmail_user = get_config_val("GMAIL_USER") or ""
_gmail_app_pass = get_config_val("GMAIL_APP_PASSWORD") or ""
_gmail_to = get_config_val("EMAIL_TO") or _gmail_user  # 若未特別設定接收信箱，預設寄給自己

_gemini_model = get_config_val("GEMINI_MODEL") or "gemini-1.5-flash"

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
        is_paper_trading=_is_paper,
        single_stock_pct=_limit_single_pct,
        daily_total_pct=_limit_daily_pct,
        initial_cash=_initial_cash
    ),
    gemini_api_keys=gemini_api_keys,
    gemini_model=_gemini_model
)
