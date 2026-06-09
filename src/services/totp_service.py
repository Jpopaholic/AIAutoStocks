# Path: src/services/totp_service.py
import base64
import hashlib
import hmac
import os
import struct
import time
from typing import Optional

def get_totp_secret() -> str:
    """
    獲取 TOTP 金鑰。優先使用環境變數或 config 中的 TOTP_SECRET，
    若未設定則自動根據 MASTER_KEY 生成穩定的 Base32 金鑰。
    """
    from src.config import get_config_val, config
    secret = os.getenv("TOTP_SECRET") or get_config_val("TOTP_SECRET")
    if secret:
        return secret.strip().upper()
    
    # 根據 MASTER_KEY 產生唯一的 Base32 金鑰
    master_key = config.credentials.master_key or "default_master_key"
    hash_bytes = hashlib.sha256(master_key.encode('utf-8')).digest()
    # Base32 編碼前 10 個位元組以產生 16 字元的 Base32 字串
    return base64.b32encode(hash_bytes[:10]).decode('utf-8').upper()

def verify_totp(code: str, window: int = 1) -> bool:
    """
    驗證輸入的 6 位數 TOTP 驗證碼是否正確。
    """
    if not code or not code.isdigit() or len(code) != 6:
        return False
    try:
        secret = get_totp_secret()
        # 補足 Base32 等號填充
        missing_padding = len(secret) % 8
        if missing_padding:
            secret += '=' * (8 - missing_padding)
        key = base64.b32decode(secret)
        
        now = int(time.time())
        time_step = 30
        
        # 允許前後時間漂移（window 個間隔）
        for i in range(-window, window + 1):
            t = (now // time_step) + i
            msg = struct.pack(">Q", t)
            hmac_hash = hmac.new(key, msg, hashlib.sha1).digest()
            offset = hmac_hash[-1] & 0x0f
            truncated_hash = struct.unpack(">I", hmac_hash[offset:offset+4])[0] & 0x7fffffff
            calculated_code = f"{truncated_hash % 1000000:06d}"
            if calculated_code == code:
                return True
    except Exception as e:
        print(f" [TOTP] 驗證錯誤: {e}")
    return False

def get_totp_url() -> str:
    """
    獲取 Google Authenticator 掃碼所需的 URL。
    """
    secret = get_totp_secret()
    label = "AIAutoStocks"
    return f"otpauth://totp/{label}?secret={secret}&issuer=AIAutoStocks"

def create_session_token() -> str:
    """
    產生一個簽章的 Session Token（有效期限 24 小時）。
    """
    from src.config import config
    master_key = config.credentials.master_key or "default_master_key"
    # 24 小時後過期
    expire_time = int(time.time()) + 86400
    expire_str = str(expire_time)
    
    # 計算簽章
    signature = hmac.new(
        master_key.encode('utf-8'),
        expire_str.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    return f"{expire_str}.{signature}"

def verify_session_token(token: str) -> bool:
    """
    驗證 Session Token 是否有效且未過期。
    """
    if not token or "." not in token:
        return False
    try:
        expire_str, signature = token.split(".", 1)
        expire_time = int(expire_str)
        
        # 檢查是否過期
        if expire_time < time.time():
            return False
            
        # 重新計算簽章並比對
        from src.config import config
        master_key = config.credentials.master_key or "default_master_key"
        expected_sig = hmac.new(
            master_key.encode('utf-8'),
            expire_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        # 使用 hmac.compare_digest 防止計時攻擊
        return hmac.compare_digest(expected_sig, signature)
    except Exception:
        return False
